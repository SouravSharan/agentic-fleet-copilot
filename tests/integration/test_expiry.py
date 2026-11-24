"""Integration test for proposal TTL expiration job and audit trail."""

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


def test_pending_proposal_expires_after_ttl_and_cannot_be_approved(
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
        trace_id="trace-expiry-1",
    )
    thread_id = "acme-001:expiry-thread"
    turn_id = "turn-expiry-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "expire")

    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["compliance_drift"], repo, policy_svc).evidence:
        ledger.add(ev)
    handles = [
        ev.handle for ev in ledger.all() if ev.handle is not None and ev.device_id == "1LYSSFD074BB"
    ]

    staged = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
                rationale="screen_lock failed",
                evidence_handles=handles,
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    prop_id = staged.staged[0]["proposal_id"]

    # Before 72h TTL -> not expired
    clock.advance(timedelta(hours=71))
    assert svc.expire_due(ctx, clock) == []

    # Past 72h TTL -> expired + audited
    clock.advance(timedelta(hours=2))
    expired = svc.expire_due(ctx, clock)
    assert expired == [prop_id]

    # Attempting to decide after expiry loses CAS
    dec = svc.decide(ctx, prop_id, "approve", "queue", clock)
    assert dec.applied is False
    assert "expired" in dec.message.lower()

    events = [
        e["event_type"]
        for e in audit_svc.list(ctx)
        if e["payload_json"].get("proposal_id") == prop_id
    ]
    assert events == ["proposal_staged", "proposal_expired"]


def _stage_sample_ticket(
    db_manager: DatabaseManager,
    clock: FixedClock,
    suffix: str,
) -> tuple[ActionService, PgCopilotStore, AuditService, TenantContext, str]:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, MockAdapter())

    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id=f"trace-expiry-{suffix}",
    )
    thread_id = f"acme-001:expiry-thread-{suffix}"
    turn_id = f"turn-expiry-{suffix}"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "expire")

    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["compliance_drift"], repo, policy_svc).evidence:
        ledger.add(ev)
    handles = [
        ev.handle for ev in ledger.all() if ev.handle is not None and ev.device_id == "1LYSSFD074BB"
    ]
    staged = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
                rationale="screen_lock failed",
                evidence_handles=handles,
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    return svc, store, audit_svc, ctx, str(staged.staged[0]["proposal_id"])


def test_approve_after_ttl_enforces_expiry_without_sweep(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    svc, store, audit_svc, ctx, prop_id = _stage_sample_ticket(db_manager, clock, "approve-ttl")

    # Advance past 72h TTL without running expire_due sweep
    clock.advance(timedelta(hours=73))
    dec = svc.decide(ctx, prop_id, "approve", "queue", clock)

    assert dec.applied is False
    assert dec.proposal is not None
    assert dec.proposal["status"] == "expired"
    assert dec.message == "This proposal expired; ask the copilot again for a fresh one."

    stored = store.get_proposal(ctx.company_id, prop_id)
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reviewer_id"] is None

    # A second attempt stays expired and does not double-audit proposal_expired
    dec2 = svc.decide(ctx, prop_id, "approve", "queue", clock)
    assert dec2.applied is False
    assert dec2.proposal is not None and dec2.proposal["status"] == "expired"

    events = [
        e["event_type"]
        for e in audit_svc.list(ctx)
        if e["payload_json"].get("proposal_id") == prop_id
    ]
    assert events == ["proposal_staged", "proposal_expired"]
    assert audit_svc.verify(ctx)["ok"] is True


def test_reject_after_ttl_marks_expired_not_rejected(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    svc, store, audit_svc, ctx, prop_id = _stage_sample_ticket(db_manager, clock, "reject-ttl")

    clock.advance(timedelta(hours=72, seconds=1))
    dec = svc.decide(ctx, prop_id, "reject", "queue", clock)

    assert dec.applied is False
    assert dec.proposal is not None
    assert dec.proposal["status"] == "expired"
    assert dec.message == "This proposal expired; ask the copilot again for a fresh one."
    stored = store.get_proposal(ctx.company_id, prop_id)
    assert stored is not None and stored["status"] == "expired"

    events = [
        e["event_type"]
        for e in audit_svc.list(ctx)
        if e["payload_json"].get("proposal_id") == prop_id
    ]
    assert events == ["proposal_staged", "proposal_expired"]


def test_defer_after_ttl_marks_expired(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    svc, store, audit_svc, ctx, prop_id = _stage_sample_ticket(db_manager, clock, "defer-ttl")

    clock.advance(timedelta(hours=73))
    dec = svc.decide(ctx, prop_id, "defer", "chat", clock)

    assert dec.applied is False
    assert dec.proposal is not None
    assert dec.proposal["status"] == "expired"
    assert dec.message == "This proposal expired; ask the copilot again for a fresh one."
    stored = store.get_proposal(ctx.company_id, prop_id)
    assert stored is not None and stored["status"] == "expired"

    events = [
        e["event_type"]
        for e in audit_svc.list(ctx)
        if e["payload_json"].get("proposal_id") == prop_id
    ]
    assert events == ["proposal_staged", "proposal_expired"]


def test_approve_before_ttl_still_succeeds(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    svc, _, audit_svc, ctx, prop_id = _stage_sample_ticket(db_manager, clock, "before-ttl")

    clock.advance(timedelta(hours=71))
    dec = svc.decide(ctx, prop_id, "approve", "queue", clock)

    assert dec.applied is True
    assert dec.proposal is not None
    assert dec.proposal["status"] == "approved"
    events = [
        e["event_type"]
        for e in audit_svc.list(ctx)
        if e["payload_json"].get("proposal_id") == prop_id
    ]
    assert events == ["proposal_staged", "proposal_decided"]
