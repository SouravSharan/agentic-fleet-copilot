"""Integration test for TOCTOU freshness recheck blocking execution after issue resolves."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

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
from scripts.telemetry_sim import app as sim_app
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


def test_toctou_stale_block_when_issue_resolved_before_execute(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    restore_telemetry_baseline: None,
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
        trace_id="trace-toctou-1",
    )
    thread_id = "acme-001:toctou-thread"
    turn_id = "turn-toctou-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "ticket")

    ledger = EvidenceLedger()
    tr = analyze_fleet_trends(ctx, ["compliance_drift"], repo, policy_svc)
    for ev in tr.evidence:
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
                arguments={
                    "device_id": "1LYSSFD074BB",
                    "check_id": "screen_lock",
                    "note": "Remediate screen_lock",
                },
                rationale="screen_lock is failing",
                evidence_handles=handles,
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    assert len(staged.staged) == 1
    prop_id = staged.staged[0]["proposal_id"]

    # Approve while failing
    assert svc.decide(ctx, prop_id, "approve", "queue", clock).applied is True

    # Simulate screen_lock resolving in live telemetry before execute runs
    runner = CliRunner()
    res = runner.invoke(sim_app, ["scenario", "toctou-resolved"])
    assert res.exit_code == 0, res.output

    # Execute must detect fresh passing telemetry -> stale_blocked, adapter NOT called
    exe = svc.execute(ctx, prop_id, clock)
    assert exe.status == "stale_blocked"
    assert exe.receipt is None
    assert adapter.call_count == 0

    events = [e for e in audit_svc.list(ctx) if e["payload_json"].get("proposal_id") == prop_id]
    assert [e["event_type"] for e in events] == [
        "proposal_staged",
        "proposal_decided",
        "freshness_blocked",
    ]
    assert len(events[-1]["payload_json"]["new_snapshot_ids"]) >= 1
