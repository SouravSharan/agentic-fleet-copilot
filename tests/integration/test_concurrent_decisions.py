"""Integration test proving compare-and-set decisions let exactly one concurrent reviewer win."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService, DecideResult, DraftInput
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
        min_size=2,
        max_size=8,
    ) as manager:
        yield manager


def test_two_threads_deciding_concurrently_exactly_one_wins(
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
        trace_id="trace-conc-1",
    )
    thread_id = "acme-001:conc-thread"
    turn_id = "turn-conc-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "ticket")

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

    def _decide(actor: str, decision: str) -> DecideResult:
        r_ctx = TenantContext(
            company_id="acme-001",
            actor_id=actor,
            role="it_admin",
            trace_id=f"trace-{actor}",
        )
        return svc.decide(r_ctx, prop_id, decision, "queue", clock)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_decide, "reviewer_a", "approve")
        f2 = pool.submit(_decide, "reviewer_b", "reject")
        results = [f1.result(), f2.result()]

    winners = [r for r in results if r.applied]
    losers = [r for r in results if not r.applied]
    assert len(winners) == 1
    assert len(losers) == 1
    assert "already decided" in losers[0].message.lower()

    verify = audit_svc.verify(ctx)
    assert verify["ok"] is True
