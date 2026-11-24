"""Integration tests for demo_ops SECURITY DEFINER reset, scenario injection, and nightly job."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.ops.demo_reset import reset_company
from fleet_copilot.ops.nightly import run_nightly
from fleet_copilot.ops.scenarios import inject_scenario_via_demo_ops
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService, DraftInput
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService, DemoResetPayload
from fleet_copilot.services.fleet import get_device_details
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)
    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        yield manager


def test_demo_ops_cannot_select_or_delete_any_table_directly(
    db_manager: DatabaseManager,
) -> None:
    """demo_ops has zero direct table privileges across telemetry, copilot, and ops."""
    settings = get_settings()
    tables = (
        "telemetry.telemetry_snapshots",
        "telemetry.companies",
        "copilot.threads",
        "copilot.turns",
        "copilot.action_proposals",
        "copilot.integration_outbox",
        "copilot.audit_logs",
        "copilot.spend_ledger",
        "ops.runtime_settings",
        "ops.seed_snapshot_ids",
    )
    with psycopg.connect(settings.demo_ops_database_url, prepare_threshold=None) as conn:
        for tbl in tables:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1")  # type: ignore[arg-type]
            conn.rollback()
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"DELETE FROM {tbl} WHERE false")  # type: ignore[arg-type]
            conn.rollback()


def test_demo_ops_functions_refuse_when_demo_mode_is_off(
    db_manager: DatabaseManager,
) -> None:
    """ops.demo_reset_company and ops.inject_scenario raise when DEMO_MODE table flag is false."""
    settings = get_settings()
    try:
        with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE ops.runtime_settings SET value_bool = false WHERE key = 'demo_mode'"
                )

        # Even attempting a session GUC spoof cannot enable demo functions
        with psycopg.connect(settings.demo_ops_database_url, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.demo_mode = 'true'")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT ops.demo_reset_company('acme-001', 'test')")
            conn.rollback()
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT ops.inject_scenario('acme-001', 'toctou-resolved', NULL)")
            conn.rollback()
    finally:
        with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE ops.runtime_settings SET value_bool = true WHERE key = 'demo_mode'"
                )


def test_demo_reset_preserves_baseline_snapshot_ids_other_tenants_and_spend_ledger(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    restore_telemetry_baseline: None,
) -> None:
    settings = get_settings()
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    action_svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, MockAdapter())
    clock = FixedClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))

    # Capture Acme baseline (id, snapshot_id) pairs BEFORE any scenario injection
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        acme_baseline_before = set(
            conn.execute(
                """
                SELECT id, snapshot_id
                FROM telemetry.telemetry_snapshots
                WHERE company_id = 'acme-001'
                """
            ).fetchall()
        )
        globex_baseline_before = set(
            conn.execute(
                """
                SELECT id, snapshot_id
                FROM telemetry.telemetry_snapshots
                WHERE company_id = 'globex-002'
                """
            ).fetchall()
        )
        with conn.transaction():
            conn.execute("TRUNCATE TABLE copilot.spend_ledger RESTART IDENTITY")

    # Populate Acme and Globex operational state + spend_ledger
    acme_ctx = TenantContext(
        company_id="acme-001", actor_id="rev_01:acme_admin", role="it_admin", trace_id="tr-rst-a"
    )
    globex_ctx = TenantContext(
        company_id="globex-002",
        actor_id="rev_01:globex_admin",
        role="it_admin",
        trace_id="tr-rst-g",
    )

    store.ensure_thread("acme-001", "acme-001:t-reset", acme_ctx.actor_id, title="Acme chat")
    store.ensure_turn(
        "acme-001", "acme-001:t-reset", "turn-a1", "tr-rst-a", acme_ctx.actor_id, "hi"
    )
    store.ensure_thread("globex-002", "globex-002:t-keep", globex_ctx.actor_id, title="Globex chat")
    store.ensure_turn(
        "globex-002", "globex-002:t-keep", "turn-g1", "tr-rst-g", globex_ctx.actor_id, "hi"
    )
    audit_svc.record_standalone(globex_ctx, "demo_reset", DemoResetPayload(reason="globex_seed"))

    # Stage + execute an Acme proposal so outbox + proposals + audit exist for Acme
    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(acme_ctx, ["compliance_drift"], repo, policy_svc).evidence:
        ledger.add(ev)
    for ev in get_device_details(acme_ctx, "1LYSSFD074BB", repo, policy_svc).evidence:
        ledger.add(ev)
    handles = [
        ev.handle
        for ev in ledger.all()
        if ev.handle is not None
        and ev.device_id == "1LYSSFD074BB"
        and ev.metric.startswith("compliance:screen_lock")
    ]
    st = action_svc.stage(
        acme_ctx,
        [
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
                rationale="Fix",
                evidence_handles=handles,
            )
        ],
        "acme-001:t-reset",
        "turn-a1",
        ledger,
        clock,
    )
    pid = st.staged[0]["proposal_id"]
    assert action_svc.decide(acme_ctx, pid, "approve", "api", clock).applied is True
    assert action_svc.execute(acme_ctx, pid, clock).status == "executed"
    assert len(store.list_outbox("acme-001")) == 1

    # Record spend in spend_ledger -> MUST survive demo reset!
    store.record_spend(
        "acme-001",
        actor_id=acme_ctx.actor_id,
        rev_id="rev_01",
        turn_id="turn-a1",
        cost_usd=0.04,
        created_at=clock.now(),
    )

    # Inject 2 scenarios into Acme via demo_ops (no admin DSN!)
    snap1 = inject_scenario_via_demo_ops("acme-001", "toctou-resolved", device_id="1LYSSFD074BB")
    snap2 = inject_scenario_via_demo_ops(
        "acme-001", "prompt-injection-software", device_id="1LYSSFD074BB"
    )
    assert snap1 != snap2

    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        acme_after_inject = conn.execute(
            "SELECT COUNT(*) FROM telemetry.telemetry_snapshots WHERE company_id = 'acme-001'"
        ).fetchone()
        assert acme_after_inject is not None
        assert int(acme_after_inject[0]) == len(acme_baseline_before) + 2

    # Reset ONLY acme-001 via demo_ops role (no admin DSN!)
    summary = reset_company("acme-001", reason="test_reset")
    assert summary["company_id"] == "acme-001"
    assert summary["deleted_telemetry_scenarios"] == 2
    assert summary["deleted_outbox"] == 1
    assert summary["deleted_proposals"] == 1
    assert summary["deleted_threads"] == 1

    # Acme baseline rows (including original DB IDs!) are identical — NOT rewritten or reseeded
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        acme_baseline_after = set(
            conn.execute(
                """
                SELECT id, snapshot_id
                FROM telemetry.telemetry_snapshots
                WHERE company_id = 'acme-001'
                """
            ).fetchall()
        )
        globex_baseline_after = set(
            conn.execute(
                """
                SELECT id, snapshot_id
                FROM telemetry.telemetry_snapshots
                WHERE company_id = 'globex-002'
                """
            ).fetchall()
        )
    assert acme_baseline_after == acme_baseline_before
    assert globex_baseline_after == globex_baseline_before

    # Acme operational tables cleared, fresh audit chain verifies with 1 demo_reset row
    assert store.list_threads("acme-001", actor_id=acme_ctx.actor_id) == []
    assert store.list_proposals("acme-001") == []
    assert store.list_outbox("acme-001") == []
    acme_audit = audit_svc.list(acme_ctx)
    assert [e["event_type"] for e in acme_audit] == ["demo_reset"]
    assert audit_svc.verify(acme_ctx) == {"ok": True, "rows_checked": 1, "first_bad_seq": None}

    # Globex is untouched
    assert len(store.list_threads("globex-002", actor_id=globex_ctx.actor_id)) == 1
    assert audit_svc.verify(globex_ctx)["ok"] is True

    # spend_ledger is UNTOUCHED
    assert store.spend_total_usd("acme-001") == pytest.approx(0.04, abs=1e-6)
    assert store.turns_by_rev_since("acme-001", "rev_01", None) == 1


def test_nightly_runs_clean_on_seeded_and_empty_db(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    restore_telemetry_baseline: None,
) -> None:
    # 1. Seeded DB: nightly expires, recovers, resets all 3 companies, and chains verify
    res = run_nightly()
    assert set(res["companies"]) == {"acme-001", "globex-002", "initech-003"}
    audit_svc = AuditService(db_manager)
    for cid in ("acme-001", "globex-002", "initech-003"):
        ctx = TenantContext(company_id=cid, actor_id="ops", role="it_admin", trace_id="tr-n")
        assert audit_svc.verify(ctx)["ok"] is True

    # 2. Empty DB (no companies): nightly still completes cleanly with 0 errors
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute("TRUNCATE TABLE telemetry.telemetry_snapshots, telemetry.companies")
    empty_res = run_nightly()
    assert empty_res["companies"] == []
