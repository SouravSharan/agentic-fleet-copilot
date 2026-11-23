"""Integration tests for per-tenant hash-chained audit logs (concurrency, tamper, TZ, isolation)."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import errors

from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import AuditService, RefusalPayload
from scripts.db_setup import run_setup


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=2,
        max_size=12,
    ) as manager:
        yield manager


def _ctx(company_id: str, actor_id: str = "admin-1", trace_id: str = "trace-1") -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role="it_admin",
        trace_id=trace_id,
    )


def test_sequential_verify_and_independent_tenants(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    audit = AuditService(db_manager)
    acme = _ctx("acme-001", trace_id="trace-a")
    globex = _ctx("globex-002", trace_id="trace-g")

    for i in range(5):
        audit.record_standalone(acme, "refusal", RefusalPayload(reason_code=f"acme_{i}"))
    for i in range(3):
        audit.record_standalone(globex, "refusal", RefusalPayload(reason_code=f"globex_{i}"))

    v_acme = audit.verify(acme)
    v_globex = audit.verify(globex)
    assert v_acme == {"ok": True, "rows_checked": 5, "first_bad_seq": None}
    assert v_globex == {"ok": True, "rows_checked": 3, "first_bad_seq": None}

    acme_seqs = [r["seq_no"] for r in audit.list(acme)]
    globex_seqs = [r["seq_no"] for r in audit.list(globex)]
    assert acme_seqs == [1, 2, 3, 4, 5]
    assert globex_seqs == [1, 2, 3]


def test_concurrent_8x25_events_contiguous_and_valid(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    audit = AuditService(db_manager)

    def _worker(worker_idx: int) -> None:
        ctx = _ctx("acme-001", actor_id=f"worker-{worker_idx}", trace_id=f"trace-w-{worker_idx}")
        for j in range(25):
            audit.record_standalone(
                ctx, "refusal", RefusalPayload(reason_code=f"w{worker_idx}_{j}")
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_worker, i) for i in range(8)]
        for f in futs:
            f.result()

    verify = audit.verify(_ctx("acme-001"))
    assert verify == {"ok": True, "rows_checked": 200, "first_bad_seq": None}

    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute("SELECT seq_no FROM copilot.audit_logs ORDER BY seq_no ASC")
        seqs = [r["seq_no"] for r in cur.fetchall()]
    assert seqs == list(range(1, 201))


def test_admin_payload_tamper_returns_first_bad_seq(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    audit = AuditService(db_manager)
    ctx = _ctx("acme-001")
    for i in range(5):
        audit.record_standalone(ctx, "refusal", RefusalPayload(reason_code=f"r_{i}"))

    assert audit.verify(ctx)["ok"] is True

    # Tamper with row seq_no = 3 as DB admin
    with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute(
                """
                UPDATE copilot.audit_logs
                SET payload_json = '{"reason_code": "tampered"}'::jsonb
                WHERE company_id = 'acme-001' AND seq_no = 3
                """
            )

    res = audit.verify(ctx)
    assert res["ok"] is False
    assert res["first_bad_seq"] == 3


def test_timezone_change_does_not_break_verification(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    audit = AuditService(db_manager)
    ctx = _ctx("acme-001")
    audit.record_standalone(ctx, "refusal", RefusalPayload(reason_code="utc_write"))

    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
        audit.record(cur, ctx, "refusal", RefusalPayload(reason_code="tokyo_write"))

    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute("SET LOCAL TIME ZONE 'Asia/Tokyo'")
        cur.execute("SELECT ok, rows_checked, first_bad_seq FROM copilot.verify_audit_chain()")
        row = cur.fetchone()
        assert row is not None
        assert row["ok"] is True
        assert row["rows_checked"] == 2
        assert row["first_bad_seq"] is None


def test_client_supplied_seq_and_hash_are_ignored_and_heads_inaccessible(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    audit = AuditService(db_manager)
    ctx = _ctx("acme-001")

    # Client tries to forge seq_no=999 and prev_hash/row_hash -> trigger overwrites all three
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            """
            INSERT INTO copilot.audit_logs (
                trace_id, company_id, actor_id, actor_role, event_type,
                payload_json, seq_no, prev_hash, row_hash
            )
            VALUES (
                'trace-forge', 'acme-001', 'acme_admin', 'it_admin', 'refusal',
                '{"reason_code": "forged_seq"}'::jsonb,
                999, decode('ff', 'hex'), decode('aa', 'hex')
            )
            RETURNING seq_no, prev_hash, row_hash
            """
        )
        row = cur.fetchone()
        assert row is not None
        assert row["seq_no"] == 1
        assert bytes(row["prev_hash"]) == b"\x00" * 32
        assert len(bytes(row["row_hash"])) == 32

    assert audit.verify(ctx) == {"ok": True, "rows_checked": 1, "first_bad_seq": None}

    # App roles cannot read or touch copilot.audit_chain_heads
    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute("SELECT * FROM copilot.audit_chain_heads")

    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.telemetry_ro_cursor("acme-001") as cur:
            cur.execute("SELECT * FROM copilot.audit_chain_heads")
