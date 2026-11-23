"""Integration test verifying both ACTIONS_ENABLED=false and global enabled=false kill-switches."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from fleet_copilot.clock import FixedClock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService, DraftInput
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
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


def test_kill_switch_env_and_global_db_row_both_refuse(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-killswitch",
    )

    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["compliance_drift"], repo, policy_svc).evidence:
        ledger.add(ev)
    handles = [
        ev.handle for ev in ledger.all() if ev.handle is not None and ev.device_id == "1LYSSFD074BB"
    ][:2]

    draft_in = DraftInput(
        tool_name="open_remediation_ticket",
        target_type="device",
        target_id="1LYSSFD074BB",
        arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
        rationale="screen_lock failed",
        evidence_handles=handles,
    )

    # 1. ACTIONS_ENABLED=false env kill-switch refuses
    svc_off = ActionService(
        db_manager,
        repo,
        store,
        policy_svc,
        audit_svc,
        MockAdapter(),
        settings=Settings(actions_enabled=False),
    )
    v_env = svc_off.precheck(ctx, draft_in, ledger, clock).verdict
    assert v_env.decision == "refuse"
    assert any("disabled" in r.lower() or "actions_enabled" in r.lower() for r in v_env.reasons)

    # 2. Global '*' enabled=false DB kill-switch refuses EVEN IF tenant sets enabled=true
    svc_on = ActionService(
        db_manager,
        repo,
        store,
        policy_svc,
        audit_svc,
        MockAdapter(),
        settings=Settings(actions_enabled=True),
    )
    try:
        with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE copilot.tenant_tool_configs
                    SET enabled = false
                    WHERE company_id = '*' AND tool_name = 'open_remediation_ticket'
                    """
                )
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.tenant_tool_configs (
                    company_id, tool_name, enabled, requires_approval, min_role, updated_by
                )
                VALUES ('acme-001', 'open_remediation_ticket', true, true, 'it_admin', 'test')
                ON CONFLICT (company_id, tool_name) DO UPDATE SET enabled = true
                """
            )

        v_db = svc_on.precheck(ctx, draft_in, ledger, clock).verdict
        assert v_db.decision == "refuse"
        assert any("disabled" in r.lower() for r in v_db.reasons)
    finally:
        with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE copilot.tenant_tool_configs
                    SET enabled = true
                    WHERE company_id = '*' AND tool_name = 'open_remediation_ticket'
                    """
                )
                conn.execute(
                    """
                    DELETE FROM copilot.tenant_tool_configs
                    WHERE company_id = 'acme-001' AND tool_name = 'open_remediation_ticket'
                    """
                )
