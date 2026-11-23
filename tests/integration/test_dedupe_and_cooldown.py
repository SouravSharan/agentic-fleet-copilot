"""Integration tests for active-proposal deduplication, DB partial unique index, and cooldowns."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import errors

from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
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


def test_duplicate_pending_refused_unique_index_enforced_and_cooldown_expires(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, MockAdapter())

    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-dedupe-1",
    )
    thread_id = "acme-001:dedupe-thread"
    turn_id = "turn-dedupe-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "ticket")

    ledger = EvidenceLedger()
    tr = analyze_fleet_trends(ctx, ["compliance_drift"], repo, policy_svc)
    for ev in tr.evidence:
        ledger.add(ev)
    handles = [
        ev.handle for ev in ledger.all() if ev.handle is not None and ev.device_id == "1LYSSFD074BB"
    ]

    draft_in = DraftInput(
        tool_name="open_remediation_ticket",
        target_type="device",
        target_id="1LYSSFD074BB",
        arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix 1"},
        rationale="screen_lock failed",
        evidence_handles=handles,
    )

    # 1. Stage first proposal -> allowed
    r1 = svc.stage(ctx, [draft_in], thread_id, turn_id, ledger, clock)
    assert len(r1.staged) == 1
    prop_id = r1.staged[0]["proposal_id"]
    dk = r1.staged[0]["dedupe_key"]

    # 2. Second identical pending proposal -> refused by precheck
    draft_in_2 = draft_in.model_copy(
        update={
            "arguments": {"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix 2"}
        }
    )
    v2 = svc.precheck(ctx, draft_in_2, ledger, clock).verdict
    assert v2.decision == "refuse"
    assert any("duplicate" in r.lower() or "active" in r.lower() for r in v2.reasons)

    # 3. Direct DB INSERT with same active dedupe_key violates partial unique index
    with pytest.raises(errors.UniqueViolation):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.action_proposals (
                    proposal_id, batch_id, thread_id, turn_id, company_id,
                    proposed_by_actor_id, tool_name, target_type, target_id,
                    arguments_json, rationale, evidence_snapshot_ids,
                    evidence_as_of, policy_check_json, status,
                    dedupe_key, approval_hash, policy_version, idempotency_key
                )
                VALUES (
                    'prop-direct-dup', 'batch-dup', %s, %s, 'acme-001',
                    'acme_admin', 'open_remediation_ticket', 'device', '1LYSSFD074BB',
                    '{}'::jsonb, 'dup', '["snap_1"]'::jsonb,
                    '2026-06-12T10:00:00Z', '{}'::jsonb, 'pending_approval',
                    %s, 'h', 'p', 'prop-direct-dup'
                )
                """,
                (thread_id, turn_id, dk),
            )

    # 4. Approve and execute first proposal -> cooldown blocks re-staging for 7 days
    assert svc.decide(ctx, prop_id, "approve", "queue", clock).applied is True
    assert svc.execute(ctx, prop_id, clock).status == "executed"

    clock.advance(timedelta(days=3))
    v_cool = svc.precheck(ctx, draft_in_2, ledger, clock).verdict
    assert v_cool.decision == "refuse"
    assert any("cooldown" in r.lower() for r in v_cool.reasons)

    # 5. Advance past 7-day ticket cooldown -> allowed again!
    clock.advance(timedelta(days=5))
    v_after = svc.precheck(ctx, draft_in_2, ledger, clock).verdict
    assert v_after.decision == "allow"
