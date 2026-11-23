"""Integration test verifying recover_executing() executes stuck proposals idempotently."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

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


def test_stuck_executing_proposal_recovers_with_same_idempotency_key_and_receipt(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    adapter = MockAdapter()
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, adapter)

    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-recover-1",
    )
    thread_id = "acme-001:recover-thread"
    turn_id = "turn-recover-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "upgrade")

    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["ram"], repo, policy_svc).evidence:
        ledger.add(ev)
    handles = [
        ev.handle for ev in ledger.all() if ev.handle is not None and ev.device_id == "M4XVHUV1MEPZ"
    ][:2]

    staged = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="M4XVHUV1MEPZ",
                arguments={
                    "device_id": "M4XVHUV1MEPZ",
                    "component": "ram",
                    "spec": "32 GB DDR5 SODIMM",
                },
                rationale="Sustained RAM pressure",
                evidence_handles=handles,
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    prop_id = staged.staged[0]["proposal_id"]
    assert svc.decide(ctx, prop_id, "approve", "queue", clock).applied is True

    # Simulate a crash right after CAS approved -> executing (and adapter already saw the key once)
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            """
            UPDATE copilot.action_proposals
            SET status = 'executing', executing_since = %s
            WHERE proposal_id = %s
            """,
            (clock.now(), prop_id),
        )
    pre_crash_receipt = adapter.execute(staged.staged[0], prop_id)

    # Not yet > 5 minutes old -> recovery does nothing
    clock.advance(timedelta(minutes=3))
    assert svc.recover_executing(ctx, clock) == []

    # Past 5 minutes -> recovery completes with the exact same deterministic receipt
    clock.advance(timedelta(minutes=3))
    recovered = svc.recover_executing(ctx, clock)
    assert len(recovered) == 1
    assert recovered[0].status == "executed"
    assert recovered[0].receipt is not None
    assert recovered[0].receipt.receipt_id == pre_crash_receipt.receipt_id
    assert len(adapter.executed_keys) == 1

    # Running recovery again is a no-op (already executed)
    assert svc.recover_executing(ctx, clock) == []


def test_adapter_exception_retries_three_times_then_marks_failed_with_one_audit_event(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    adapter = MockAdapter()
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, adapter)

    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-retry-fail",
    )
    thread_id = "acme-001:retry-fail-thread"
    turn_id = "turn-retry-fail-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "upgrade")

    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["ram"], repo, policy_svc).evidence:
        ledger.add(ev)
    handles = [
        ev.handle for ev in ledger.all() if ev.handle is not None and ev.device_id == "M4XVHUV1MEPZ"
    ]

    staged = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="M4XVHUV1MEPZ",
                arguments={
                    "device_id": "M4XVHUV1MEPZ",
                    "component": "ram",
                    "spec": "32 GB DDR5 SODIMM",
                },
                rationale="Sustained RAM pressure",
                evidence_handles=handles,
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    prop_id = staged.staged[0]["proposal_id"]
    assert svc.decide(ctx, prop_id, "approve", "queue", clock).applied is True

    # Configure MockAdapter to raise RuntimeError on 3 consecutive calls
    adapter.raise_next(RuntimeError("upstream timeout"), count=3)

    # Attempt 1 via execute() -> stays executing (attempts=1), no action_failed yet
    r1 = svc.execute(ctx, prop_id, clock)
    assert r1.status == "executing"
    assert r1.proposal is not None and r1.proposal["attempts"] == 1

    # Attempt 2 via recover_executing() -> stays executing (attempts=2), no action_failed yet
    clock.advance(timedelta(minutes=6))
    rec2 = svc.recover_executing(ctx, clock)
    assert len(rec2) == 1
    assert rec2[0].status == "executing"
    assert rec2[0].proposal is not None and rec2[0].proposal["attempts"] == 2

    # Attempt 3 via recover_executing() -> marks failed (attempts=3) + exactly 1 action_failed
    clock.advance(timedelta(minutes=6))
    rec3 = svc.recover_executing(ctx, clock)
    assert len(rec3) == 1
    assert rec3[0].status == "failed"
    assert rec3[0].proposal is not None and rec3[0].proposal["attempts"] == 3

    # Subsequent recover_executing() does nothing (no infinite retry loop)
    clock.advance(timedelta(minutes=10))
    assert svc.recover_executing(ctx, clock) == []

    failed_events = [
        e
        for e in audit_svc.list(ctx)
        if e["payload_json"].get("proposal_id") == prop_id and e["event_type"] == "action_failed"
    ]
    assert len(failed_events) == 1
    assert audit_svc.verify(ctx)["ok"] is True
