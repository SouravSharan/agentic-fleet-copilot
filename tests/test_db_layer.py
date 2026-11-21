"""End-to-end PostgreSQL verification tests for Stage 1 (telemetry + copilot schemas).

Verifies:
- Role least-privilege flags (NOSUPERUSER, NOBYPASSRLS, read-only default on telemetry_ro)
- Fail-closed RLS on BOTH `telemetry.*` and `copilot.*` (unset AND empty tenant)
- Strict read-only enforcement on `telemetry_ro` and cross-schema denial across all 3 roles
- Composite tenant-scoped FK protection (blocks cross-tenant thread/turn linkage)
- Pooled connection (`max_size=1`) tenant non-leakage across commit AND exception rollback
- Live-feed & malformed JSONB resilience (5-field payloads, JSON nulls, wrong types,
  zero/negative denominators, multi-volume snapshots, retries, out-of-order late arrivals,
  form_factor classification including battery-powered "Studio" laptops and unknown devices)
- DB-enforced append-only `copilot.audit_logs`, no-DELETE operational history, and
  global `'*'` policy/tool-config protection
- Standalone `scripts/telemetry_sim.py` validation, idempotency, and all 4 scenarios
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from typer.testing import CliRunner

from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager, InvalidTenantError, validate_company_id

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.db_setup import run_setup  # noqa: E402
from scripts.telemetry_sim import (  # noqa: E402
    app as sim_app,
)
from scripts.telemetry_sim import (  # noqa: E402
    ingest_snapshot,
    reset_dataset,
    seed_dataset,
    validate_snapshot_record,
)


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    """Ensure schema is bootstrapped (idempotently) and seeded to the 750-row baseline."""
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    # Re-run to verify migration idempotency on an already-initialized DB
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        yield manager

    reset_dataset(writer_url=settings.telemetry_writer_database_url)


@pytest.fixture()
def restore_telemetry_baseline() -> Iterator[None]:
    """Restore the 750-row telemetry baseline after tests that inject live snapshots."""
    yield
    reset_dataset(writer_url=get_settings().telemetry_writer_database_url)


# ---------------------------------------------------------------------------
# 1. Input Validation & Role Security Flags
# ---------------------------------------------------------------------------


def test_validate_company_id_rejects_invalid_values() -> None:
    assert validate_company_id("acme-001") == "acme-001"
    for bad in (
        "",
        "   ",
        " acme-001",
        "acme-001 ",
        "*",
        "-leading-dash",
        "_leading_underscore",
        "a" * 65,
        "acme; DROP TABLE x",
        "../etc",
        "x",
        None,
        123,
    ):
        with pytest.raises(InvalidTenantError):
            validate_company_id(bad)  # type: ignore[arg-type]


def test_db_roles_are_least_privilege(db_manager: DatabaseManager) -> None:
    with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin
                FROM pg_roles
                WHERE rolname IN ('telemetry_writer', 'telemetry_ro', 'copilot_app')
                ORDER BY rolname
                """
            )
            rows = cur.fetchall()
            assert len(rows) == 3
            for rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin in rows:
                assert rolcanlogin is True, rolname
                assert rolsuper is False, rolname
                assert rolcreatedb is False, rolname
                assert rolcreaterole is False, rolname
                assert rolbypassrls is False, rolname


# ---------------------------------------------------------------------------
# 2. Fail-Closed RLS (Telemetry & Copilot, Unset + Empty Tenant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tenant_setting", [None, ""])
def test_telemetry_rls_fail_closed_when_tenant_unset_or_empty(
    db_manager: DatabaseManager,
    tenant_setting: str | None,
) -> None:
    settings = get_settings()
    with psycopg.connect(settings.telemetry_ro_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if tenant_setting is not None:
                    cur.execute(
                        "SELECT set_config('app.current_tenant', %s, true)",
                        (tenant_setting,),
                    )
                for relation in (
                    "telemetry.companies",
                    "telemetry.telemetry_snapshots",
                    "telemetry.v_snapshots_deduped",
                    "telemetry.v_latest_device_state",
                    "telemetry.v_disk_volumes",
                    "telemetry.v_compliance_results",
                    "telemetry.v_installed_software",
                    "telemetry.v_network_interfaces",
                ):
                    cur.execute(f"SELECT COUNT(*) FROM {relation}")
                    assert cur.fetchone()[0] == 0, f"Expected 0 rows on {relation}"


@pytest.mark.parametrize("tenant_setting", [None, ""])
def test_copilot_rls_fail_closed_when_tenant_unset_or_empty(
    db_manager: DatabaseManager,
    tenant_setting: str | None,
) -> None:
    """Even seeded global '*' policies/tool configs must return 0 rows when tenant is unset."""
    settings = get_settings()
    with psycopg.connect(settings.copilot_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if tenant_setting is not None:
                    cur.execute(
                        "SELECT set_config('app.current_tenant', %s, true)",
                        (tenant_setting,),
                    )
                for table in (
                    "copilot.threads",
                    "copilot.turns",
                    "copilot.tenant_policies",
                    "copilot.tenant_tool_configs",
                    "copilot.action_proposals",
                    "copilot.audit_logs",
                ):
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    assert cur.fetchone()[0] == 0, f"Expected 0 rows on {table}"

        with pytest.raises(errors.InsufficientPrivilege):
            with conn.transaction():
                with conn.cursor() as cur:
                    if tenant_setting is not None:
                        cur.execute(
                            "SELECT set_config('app.current_tenant', %s, true)",
                            (tenant_setting,),
                        )
                    cur.execute(
                        """
                        INSERT INTO copilot.threads (thread_id, company_id, actor_id)
                        VALUES ('acme-001:unset-test', 'acme-001', 'admin-1')
                        """
                    )


def test_telemetry_rls_tenant_isolation_and_counts(db_manager: DatabaseManager) -> None:
    expected = {
        "acme-001": (10, 300, "Acme"),
        "globex-002": (8, 240, "Globex"),
        "initech-003": (7, 210, "Initech"),
    }
    for company_id, (dev_count, snap_count, display_name) in expected.items():
        with db_manager.telemetry_ro_cursor(company_id) as cur:
            cur.execute("SELECT company_id, display_name FROM telemetry.companies")
            companies = cur.fetchall()
            assert len(companies) == 1
            assert companies[0]["company_id"] == company_id
            assert companies[0]["display_name"] == display_name

            cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
            assert cur.fetchone()["n"] == dev_count

            cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_snapshots_deduped")
            assert cur.fetchone()["n"] == snap_count

            cur.execute("SELECT DISTINCT company_id FROM telemetry.v_snapshots_deduped")
            assert [r["company_id"] for r in cur.fetchall()] == [company_id]

    # Cross-tenant device lookup must return 0 rows (no existence oracle)
    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute(
            "SELECT * FROM telemetry.v_latest_device_state WHERE device_id = %s",
            ("E4V0SB5I0MYU",),  # belongs to globex-002
        )
        assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# 3. Read-Only, Cross-Schema & Pool Non-Leakage Enforcement
# ---------------------------------------------------------------------------


def test_telemetry_ro_cannot_mutate_telemetry(db_manager: DatabaseManager) -> None:
    for stmt in (
        "DELETE FROM telemetry.telemetry_snapshots",
        "UPDATE telemetry.companies SET display_name = 'Hacked'",
        "TRUNCATE TABLE telemetry.telemetry_snapshots",
    ):
        with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
            with db_manager.telemetry_ro_cursor("acme-001") as cur:
                cur.execute(stmt)

    # Even on a raw connection, role-level default_transaction_read_only & grants block writes
    settings = get_settings()
    with psycopg.connect(settings.telemetry_ro_database_url, prepare_threshold=None) as conn:
        with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO telemetry.companies (company_id, display_name)
                    VALUES ('evil-999', 'Evil')
                    """
                )
        conn.rollback()


def test_cross_schema_access_is_denied(db_manager: DatabaseManager) -> None:
    # telemetry_ro cannot read copilot schema
    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.telemetry_ro_cursor("acme-001") as cur:
            cur.execute("SELECT COUNT(*) FROM copilot.audit_logs")

    # copilot_app cannot read telemetry schema
    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute("SELECT COUNT(*) FROM telemetry.telemetry_snapshots")

    # telemetry_writer cannot read copilot schema
    settings = get_settings()
    with psycopg.connect(settings.telemetry_writer_database_url, prepare_threshold=None) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM copilot.audit_logs")
        conn.rollback()


def test_pooled_connection_does_not_leak_tenant_across_commit_or_rollback(
    db_manager: DatabaseManager,
) -> None:
    """Force single-connection pool reuse (max_size=1) across Tenant A -> Tenant B."""
    settings = get_settings()
    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=1,
    ) as single_pool_db:
        # 1) Clean commit on acme-001 followed by globex-002 on same physical connection
        with single_pool_db.telemetry_ro_cursor("acme-001") as cur:
            cur.execute("SELECT DISTINCT company_id FROM telemetry.v_latest_device_state")
            assert [r["company_id"] for r in cur.fetchall()] == ["acme-001"]

        with single_pool_db.telemetry_ro_cursor("globex-002") as cur:
            cur.execute("SELECT DISTINCT company_id FROM telemetry.v_latest_device_state")
            assert [r["company_id"] for r in cur.fetchall()] == ["globex-002"]

        # 2) Mid-transaction exception rollback on acme-001 followed by initech-003
        with pytest.raises(RuntimeError, match="boom"):
            with single_pool_db.telemetry_ro_cursor("acme-001") as cur:
                cur.execute("SELECT 1")
                raise RuntimeError("boom")

        with single_pool_db.telemetry_ro_cursor("initech-003") as cur:
            cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
            assert cur.fetchone()["n"] == 7

        # 3) Same rollback test on copilot_app pool
        with pytest.raises(RuntimeError, match="copilot-boom"):
            with single_pool_db.copilot_cursor("acme-001") as cur:
                cur.execute("SELECT 1")
                raise RuntimeError("copilot-boom")

        with single_pool_db.copilot_cursor("globex-002") as cur:
            cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
            assert cur.fetchone()["t"] == "globex-002"


# ---------------------------------------------------------------------------
# 4. Live-Feed, Missing-Field & Malformed JSONB Edge Cases
# ---------------------------------------------------------------------------


def test_live_partial_payload_malformed_jsonb_and_form_factor_edge_cases(
    db_manager: DatabaseManager,
    restore_telemetry_baseline: None,
) -> None:
    # 1) Minimal 5-field payload on an existing Acme laptop (all optional blocks omitted)
    minimal_id = ingest_snapshot(
        {
            "company_id": "acme-001",
            "device_id": "1LYSSFD074BB",
            "employee_id": "emp-acme-1001",
            "collected_at": "2026-06-13T10:00:00Z",
            "agent_version": "43.0.0-minimal",
        }
    )

    # 2) Drifted/malformed JSON types + zero/negative denominators + multi-volume disks
    drifted_id = ingest_snapshot(
        {
            "company_id": "acme-001",
            "device_id": "7VP16KHV88LM",
            "employee_id": "emp-acme-1000",
            "collected_at": "2026-06-13T10:05:00Z",
            "agent_version": "43.0.0-drifted",
            "os": "unexpected-string",
            "memory": {"total_memory_bytes": 0, "used_memory_bytes": 100},
            "battery": {
                "battery_present": True,
                "percentage": 85.5,
                "cycle_count": "not-an-int",
                "condition": "Normal",
            },
            "network": {"not": "an-array"},
            "disk_volumes": [
                {"volume_name": "Zero", "size_bytes": 0, "available_bytes": 0, "encrypted": True},
                {
                    "volume_name": "Neg",
                    "size_bytes": -100,
                    "available_bytes": 50,
                    "encrypted": "false",
                },
                {
                    "volume_name": "Good",
                    "size_bytes": 1000,
                    "available_bytes": 250,
                    "encrypted": True,
                },
                "stray-non-object-element",
            ],
            "compliance_results": None,
            "installed_software": None,
        }
    )

    # 3) Brand-new device with ONLY 5 fields -> form_factor must be 'unknown'
    ingest_snapshot(
        {
            "company_id": "acme-001",
            "device_id": "NEWDEV000001",
            "employee_id": "emp-acme-1001",
            "collected_at": "2026-06-13T10:10:00Z",
            "agent_version": "43.0.0-minimal",
        }
    )

    # 4) Battery-powered "Surface Laptop Studio" -> must classify as 'laptop', NOT 'desktop'
    ingest_snapshot(
        {
            "company_id": "acme-001",
            "device_id": "STUDIO000001",
            "employee_id": "emp-acme-1002",
            "collected_at": "2026-06-13T10:15:00Z",
            "agent_version": "43.0.0",
            "device_identity": {"model_name": "Surface Laptop Studio"},
            "battery": {"battery_present": True, "condition": "Normal", "cycle_count": 42},
        }
    )

    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        # All 6 views must remain queryable without error despite malformed payload
        for view in (
            "telemetry.v_snapshots_deduped",
            "telemetry.v_latest_device_state",
            "telemetry.v_disk_volumes",
            "telemetry.v_compliance_results",
            "telemetry.v_installed_software",
            "telemetry.v_network_interfaces",
        ):
            cur.execute(f"SELECT COUNT(*) AS n FROM {view}")
            assert cur.fetchone()["n"] > 0

        # Existing laptop carries forward hardware/OS identity & form_factor across partial payload
        cur.execute(
            """
            SELECT snapshot_id, model_name, os_platform, os_hostname, serial_number,
                   form_factor, has_battery_block, ever_had_battery, ram_used_pct, snapshot_count
            FROM telemetry.v_latest_device_state
            WHERE device_id = '1LYSSFD074BB'
            """
        )
        laptop = cur.fetchone()
        assert laptop["snapshot_id"] == minimal_id
        assert laptop["model_name"] == "MacBook Pro"
        assert laptop["os_platform"] == "darwin"
        assert laptop["os_hostname"] == "acme-macbook-2"
        assert laptop["serial_number"] == "1LYSSFD074BB"
        assert laptop["form_factor"] == "laptop"
        assert laptop["has_battery_block"] is False
        assert laptop["ever_had_battery"] is True
        assert laptop["ram_used_pct"] is None
        assert laptop["snapshot_count"] == 31

        # Drifted snapshot safely coerces float percentage, nulls bad cycle_count & zero RAM %
        cur.execute(
            """
            SELECT snapshot_id, ram_used_pct, battery_percentage, battery_cycle_count,
                   has_network_block, form_factor
            FROM telemetry.v_latest_device_state
            WHERE device_id = '7VP16KHV88LM'
            """
        )
        drifted = cur.fetchone()
        assert drifted["snapshot_id"] == drifted_id
        assert drifted["ram_used_pct"] is None
        assert drifted["battery_percentage"] == 85
        assert drifted["battery_cycle_count"] is None
        assert drifted["has_network_block"] is False
        assert drifted["form_factor"] == "laptop"

        # Multi-volume disks on drifted snapshot: Zero/Neg have NULL free_pct, Good has 25.00
        cur.execute(
            """
            SELECT volume_name, free_pct, encrypted
            FROM telemetry.v_disk_volumes
            WHERE snapshot_id = %s
            ORDER BY volume_name
            """,
            (drifted_id,),
        )
        vols = {r["volume_name"]: (r["free_pct"], r["encrypted"]) for r in cur.fetchall()}
        assert vols["Zero"] == (None, True)
        assert vols["Neg"] == (None, False)
        assert float(vols["Good"][0]) == 25.0
        assert vols["Good"][1] is True

        # Brand-new 5-field device -> unknown; Surface Laptop Studio -> laptop; Mac mini -> desktop
        cur.execute(
            """
            SELECT device_id, form_factor
            FROM telemetry.v_latest_device_state
            WHERE device_id IN ('NEWDEV000001', 'STUDIO000001', 'DLT35MHGTH2D')
            """
        )
        ff = {r["device_id"]: r["form_factor"] for r in cur.fetchall()}
        assert ff == {
            "NEWDEV000001": "unknown",
            "STUDIO000001": "laptop",
            "DLT35MHGTH2D": "desktop",
        }


def test_duplicate_retry_dedupes_and_late_snapshot_does_not_overwrite_latest(
    db_manager: DatabaseManager,
    restore_telemetry_baseline: None,
) -> None:
    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute(
            """
            SELECT collected_at, payload
            FROM telemetry.v_snapshots_deduped
            WHERE device_id = '1LYSSFD074BB'
            ORDER BY collected_at DESC
            LIMIT 1
            """
        )
        latest_row = cur.fetchone()

    latest_payload = json.loads(json.dumps(latest_row["payload"]))
    retry_payload = dict(latest_payload)
    retry_payload["agent_version"] = "42.0.0-retry"
    retry_id = ingest_snapshot(retry_payload, salt="retry")

    late_payload = dict(latest_payload)
    late_payload["collected_at"] = "2026-05-01T00:00:00Z"
    late_payload["agent_version"] = "41.0.0-late"
    ingest_snapshot(late_payload)

    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n
            FROM telemetry.v_snapshots_deduped
            WHERE device_id = '1LYSSFD074BB'
            """
        )
        assert cur.fetchone()["n"] == 31

        cur.execute(
            """
            SELECT snapshot_id, agent_version, collected_at
            FROM telemetry.v_latest_device_state
            WHERE device_id = '1LYSSFD074BB'
            """
        )
        latest = cur.fetchone()
        assert latest["snapshot_id"] == retry_id
        assert latest["agent_version"] == "42.0.0-retry"
        assert latest["collected_at"] == latest_row["collected_at"]


# ---------------------------------------------------------------------------
# 5. Dataset Ground-Truth Sanity Check via Live Views
# ---------------------------------------------------------------------------


def test_dataset_ground_truth_matches_views(db_manager: DatabaseManager) -> None:
    for company_id in ("acme-001", "globex-002", "initech-003"):
        with db_manager.telemetry_ro_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM telemetry.v_compliance_results
                WHERE severity = 'high' AND status <> 'pass'
                """
            )
            assert cur.fetchone()["n"] == 0

    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute(
            """
            SELECT device_id FROM telemetry.v_latest_device_state
            WHERE battery_condition = 'Service Recommended'
            ORDER BY device_id
            """
        )
        assert [r["device_id"] for r in cur.fetchall()] == [
            "7VP16KHV88LM",
            "8NM23J95R5I6",
            "M4XVHUV1MEPZ",
        ]

        cur.execute(
            """
            SELECT ROUND(AVG(ram_used_pct), 1) AS avg_ram
            FROM telemetry.v_snapshots_deduped
            WHERE device_id = 'M4XVHUV1MEPZ'
            """
        )
        assert float(cur.fetchone()["avg_ram"]) > 90.0

        cur.execute(
            """
            SELECT l.device_id, d.free_pct
            FROM telemetry.v_latest_device_state AS l
            JOIN telemetry.v_disk_volumes AS d ON d.snapshot_id = l.snapshot_id
            WHERE d.free_pct < 5.0
            ORDER BY l.device_id
            """
        )
        assert [r["device_id"] for r in cur.fetchall()] == ["8NM23J95R5I6", "MT7PJB7N5LRE"]


# ---------------------------------------------------------------------------
# 6. Copilot Schema Security: Composite FKs, Append-Only Audit, Policies & Tools
# ---------------------------------------------------------------------------


def test_copilot_threads_turns_proposals_composite_fk_and_no_delete(
    db_manager: DatabaseManager,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    acme_thread = f"acme-001:{suffix}"
    acme_turn = f"turn-{suffix}"
    acme_prop = f"prop-{suffix}"

    try:
        # Acme creates thread -> turn -> proposal
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.threads (thread_id, company_id, actor_id, title)
                VALUES (%s, 'acme-001', 'admin-acme-1', 'Disk check')
                """,
                (acme_thread,),
            )
            cur.execute(
                """
                INSERT INTO copilot.turns (
                    turn_id, trace_id, thread_id, company_id, actor_id, user_message, status
                )
                VALUES (%s, %s, %s, 'acme-001', 'admin-acme-1', 'Check disks', 'waiting_approval')
                """,
                (acme_turn, f"trace-{suffix}", acme_thread),
            )
            cur.execute(
                """
                INSERT INTO copilot.action_proposals (
                    proposal_id, batch_id, thread_id, turn_id, company_id,
                    proposed_by_actor_id, tool_name, target_type, target_id,
                    arguments_json, rationale, evidence_snapshot_ids,
                    evidence_as_of, policy_check_json, status
                )
                VALUES (
                    %s, %s, %s, %s, 'acme-001',
                    'admin-acme-1', 'open_remediation_ticket', 'device', '1LYSSFD074BB',
                    '{}'::jsonb, 'screen_lock failing', '["snap_1"]'::jsonb,
                    '2026-06-12T10:00:00Z', '{"passed": true}'::jsonb, 'pending_approval'
                )
                """,
                (acme_prop, f"batch-{suffix}", acme_thread, acme_turn),
            )

        # Globex sees 0 rows across threads, turns, and proposals
        with db_manager.copilot_cursor("globex-002") as cur:
            for table in ("copilot.threads", "copilot.turns", "copilot.action_proposals"):
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE thread_id = %s",
                    (acme_thread,),
                )
                assert cur.fetchone()["n"] == 0

        # Composite FK blocks Globex from linking a turn to Acme's thread_id
        with pytest.raises(errors.ForeignKeyViolation):
            with db_manager.copilot_cursor("globex-002") as cur:
                cur.execute(
                    """
                    INSERT INTO copilot.turns (
                        turn_id, trace_id, thread_id, company_id, actor_id, user_message
                    )
                    VALUES (%s, 'trace-x', %s, 'globex-002', 'admin-globex-1', 'hijack')
                    """,
                    (f"turn-evil-{suffix}", acme_thread),
                )

        # Composite FK blocks Globex from linking an action_proposal to Acme's turn
        with pytest.raises(errors.ForeignKeyViolation):
            with db_manager.copilot_cursor("globex-002") as cur:
                cur.execute(
                    """
                    INSERT INTO copilot.action_proposals (
                        proposal_id, batch_id, thread_id, turn_id, company_id,
                        proposed_by_actor_id, tool_name, target_type, target_id,
                        arguments_json, rationale, evidence_snapshot_ids,
                        evidence_as_of, policy_check_json, status
                    )
                    VALUES (
                        %s, 'batch-x', %s, %s, 'globex-002',
                        'admin-globex-1', 'notify_employee', 'employee', 'emp-acme-1001',
                        '{}'::jsonb, 'hijack', '[]'::jsonb,
                        '2026-06-12T10:00:00Z', '{}'::jsonb, 'pending_approval'
                    )
                    """,
                    (f"prop-evil-{suffix}", acme_thread, acme_turn),
                )

        # Cross-tenant INSERT with wrong company_id blocked by RLS WITH CHECK
        with pytest.raises(errors.InsufficientPrivilege):
            with db_manager.copilot_cursor("globex-002") as cur:
                cur.execute(
                    """
                    INSERT INTO copilot.threads (thread_id, company_id, actor_id)
                    VALUES (%s, 'acme-001', 'admin-globex-1')
                    """,
                    (f"acme-001:forged-{suffix}",),
                )

        # Runtime DELETE is revoked on operational history tables (threads, turns, action_proposals)
        for stmt in (
            f"DELETE FROM copilot.action_proposals WHERE proposal_id = '{acme_prop}'",
            f"DELETE FROM copilot.turns WHERE turn_id = '{acme_turn}'",
            f"DELETE FROM copilot.threads WHERE thread_id = '{acme_thread}'",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                with db_manager.copilot_cursor("acme-001") as cur:
                    cur.execute(stmt)
    finally:
        with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM copilot.threads WHERE thread_id = %s", (acme_thread,))


def test_copilot_audit_logs_are_append_only_and_tenant_isolated(
    db_manager: DatabaseManager,
) -> None:
    trace_id = f"trace-{uuid.uuid4().hex[:8]}"
    try:
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.audit_logs (
                    trace_id, company_id, actor_id, actor_role, event_type, payload_json
                )
                VALUES (%s, 'acme-001', 'admin-acme-1', 'it_admin', 'request_received', %s::jsonb)
                RETURNING audit_id
                """,
                (trace_id, json.dumps({"msg": "test"})),
            )
            audit_id = cur.fetchone()["audit_id"]

        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute("SELECT trace_id FROM copilot.audit_logs WHERE audit_id = %s", (audit_id,))
            assert cur.fetchone()["trace_id"] == trace_id

        with db_manager.copilot_cursor("globex-002") as cur:
            cur.execute("SELECT * FROM copilot.audit_logs WHERE audit_id = %s", (audit_id,))
            assert cur.fetchall() == []

        # Cross-tenant INSERT blocked by RLS WITH CHECK
        with pytest.raises(errors.InsufficientPrivilege):
            with db_manager.copilot_cursor("globex-002") as cur:
                cur.execute(
                    """
                    INSERT INTO copilot.audit_logs (
                        trace_id, company_id, actor_id, actor_role, event_type
                    )
                    VALUES (%s, 'acme-001', 'admin-globex-1', 'it_admin', 'forged')
                    """,
                    (trace_id,),
                )

        # UPDATE, DELETE, and TRUNCATE are all blocked at DB privilege level
        for stmt in (
            f"UPDATE copilot.audit_logs SET event_type = 'tampered' WHERE audit_id = {audit_id}",
            f"DELETE FROM copilot.audit_logs WHERE audit_id = {audit_id}",
            "TRUNCATE TABLE copilot.audit_logs",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                with db_manager.copilot_cursor("acme-001") as cur:
                    cur.execute(stmt)
    finally:
        with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM copilot.audit_logs WHERE trace_id = %s", (trace_id,))


def test_copilot_policies_and_tool_configs_global_protection(
    db_manager: DatabaseManager,
) -> None:
    # 1) Global '*' policies & all 13 default tool configs are readable
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "SELECT metric_key, value_json FROM copilot.tenant_policies WHERE company_id = '*'"
        )
        global_policies = {r["metric_key"]: r["value_json"] for r in cur.fetchall()}
        assert global_policies["low_disk_free_pct"] == 15.0
        assert global_policies["high_ram_used_pct"] == 80.0
        assert "uTorrent" in global_policies["flagged_software_list"]

        cur.execute("SELECT tool_name FROM copilot.tenant_tool_configs WHERE company_id = '*'")
        tools = {r["tool_name"] for r in cur.fetchall()}
        assert len(tools) == 13
        assert {
            "create_upgrade_order",
            "open_remediation_ticket",
            "flag_device_for_replacement",
            "notify_employee",
        }.issubset(tools)

    # 2) Tenant can insert own policy & tool override; Globex cannot see either
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            """
            INSERT INTO copilot.tenant_policies (
                company_id, scope_type, scope_value, metric_key, value_json, updated_by
            )
            VALUES ('acme-001', 'tenant', '', 'low_disk_free_pct', '10.0'::jsonb, 'admin-acme-1')
            ON CONFLICT (company_id, scope_type, scope_value, metric_key)
            DO UPDATE SET value_json = EXCLUDED.value_json
            """
        )
        cur.execute(
            """
            INSERT INTO copilot.tenant_tool_configs (
                company_id, tool_name, enabled, requires_approval, min_role, updated_by
            )
            VALUES ('acme-001', 'create_upgrade_order', false, true, 'it_admin', 'admin-acme-1')
            ON CONFLICT (company_id, tool_name)
            DO UPDATE SET enabled = EXCLUDED.enabled
            """
        )

    with db_manager.copilot_cursor("globex-002") as cur:
        cur.execute("SELECT * FROM copilot.tenant_policies WHERE company_id = 'acme-001'")
        assert cur.fetchall() == []
        cur.execute("SELECT * FROM copilot.tenant_tool_configs WHERE company_id = 'acme-001'")
        assert cur.fetchall() == []

    # 3) Tenants CANNOT insert, update, or delete global '*' policies or tool configs
    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.tenant_policies (
                    company_id, scope_type, scope_value, metric_key, value_json, updated_by
                )
                VALUES ('*', 'global', '', 'evil_key', '1'::jsonb, 'admin-acme-1')
                """
            )

    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.tenant_policies (
                    company_id, scope_type, scope_value, metric_key, value_json, updated_by
                )
                VALUES ('acme-001', 'global', '', 'evil_key', '1'::jsonb, 'admin-acme-1')
                """
            )

    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.tenant_tool_configs (
                    company_id, tool_name, enabled, requires_approval, min_role
                )
                VALUES ('*', 'evil_tool', true, false, 'viewer')
                """
            )

    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            """
            UPDATE copilot.tenant_policies
            SET value_json = '99.0'::jsonb
            WHERE company_id = '*' AND metric_key = 'low_disk_free_pct'
            """
        )
        assert cur.rowcount == 0

        cur.execute("DELETE FROM copilot.tenant_policies WHERE company_id = '*'")
        assert cur.rowcount == 0

        cur.execute(
            """
            UPDATE copilot.tenant_tool_configs
            SET enabled = false
            WHERE company_id = '*' AND tool_name = 'notify_employee'
            """
        )
        assert cur.rowcount == 0

        cur.execute("DELETE FROM copilot.tenant_tool_configs WHERE company_id = '*'")
        assert cur.rowcount == 0

        # Clean up Acme's own overrides
        cur.execute(
            """
            DELETE FROM copilot.tenant_policies
            WHERE company_id = 'acme-001' AND metric_key = 'low_disk_free_pct'
            """
        )
        cur.execute(
            """
            DELETE FROM copilot.tenant_tool_configs
            WHERE company_id = 'acme-001' AND tool_name = 'create_upgrade_order'
            """
        )


# ---------------------------------------------------------------------------
# 7. Simulator Validation, Idempotency & Scenarios
# ---------------------------------------------------------------------------


def test_simulator_validation_idempotency_and_scenarios(
    db_manager: DatabaseManager,
    restore_telemetry_baseline: None,
) -> None:
    # Validation rejects non-dicts, missing/whitespace fields, and wildcard company_id
    for bad_record in (
        None,
        ["not", "a", "dict"],
        {"company_id": "acme-001"},
        {
            "company_id": "   ",
            "device_id": "D1",
            "employee_id": "E1",
            "collected_at": "2026-06-12T00:00:00Z",
            "agent_version": "42.0.0",
        },
        {
            "company_id": "*",
            "device_id": "D1",
            "employee_id": "E1",
            "collected_at": "2026-06-12T00:00:00Z",
            "agent_version": "42.0.0",
        },
    ):
        with pytest.raises((ValueError, InvalidTenantError)):
            validate_snapshot_record(bad_record)

    # Seed idempotency: second seed inserts 0 new rows
    inserted, total = seed_dataset()
    assert inserted == 0
    assert total == 750

    runner = CliRunner()
    # Invalid scenario fails fast with non-zero exit code
    bad_res = runner.invoke(sim_app, ["scenario", "does-not-exist"])
    assert bad_res.exit_code != 0

    # Chain partial-payload -> toctou-resolved -> high-sev-alert -> duplicate-and-late
    for sc in ("partial-payload", "toctou-resolved", "high-sev-alert", "duplicate-and-late"):
        res = runner.invoke(sim_app, ["scenario", sc])
        assert res.exit_code == 0, res.output

    # Confirm high-sev-alert actually produced a high-severity failure even after partial-payload!
    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute(
            """
            SELECT status
            FROM telemetry.v_compliance_results
            WHERE device_id = '1LYSSFD074BB'
              AND check_id = 'disk_encryption'
              AND severity = 'high'
            ORDER BY collected_at DESC
            LIMIT 1
            """
        )
        assert cur.fetchone()["status"] == "fail"
