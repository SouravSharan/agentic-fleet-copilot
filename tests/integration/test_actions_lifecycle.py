"""Integration tests for full action lifecycle across all four action tools."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

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


def _make_service(
    db: DatabaseManager,
) -> tuple[ActionService, PgTelemetryRepository, PolicyService, PgCopilotStore, AuditService]:
    repo = PgTelemetryRepository(db)
    store = PgCopilotStore(db)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db)
    adapter = MockAdapter()
    svc = ActionService(db, repo, store, policy_svc, audit_svc, adapter)
    return svc, repo, policy_svc, store, audit_svc


def test_four_action_lifecycles_end_to_end(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    svc, repo, policy_svc, store, audit_svc = _make_service(db_manager)
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-lifecycle-1",
    )

    thread_id = "acme-001:lifecycle-thread"
    turn_id = "turn-lifecycle-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id, title="Lifecycle test")
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "Run actions")

    # Populate ledger with real telemetry evidence from services
    ledger = EvidenceLedger()
    trends_res = analyze_fleet_trends(ctx, ["all"], repo, policy_svc)
    for ev in trends_res.evidence:
        ledger.add(ev)
    for dev_id in ("M4XVHUV1MEPZ", "1LYSSFD074BB", "8NM23J95R5I6", "MT7PJB7N5LRE"):
        det = get_device_details(ctx, dev_id, repo, policy_svc)
        for ev in det.evidence:
            ledger.add(ev)

    def _handles_for(device_id: str, metric_prefix: str) -> list[str]:
        return [
            ev.handle
            for ev in ledger.all()
            if ev.handle is not None
            and ev.device_id == device_id
            and ev.metric.startswith(metric_prefix)
        ]

    cases = [
        (
            "ORD-",
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="M4XVHUV1MEPZ",
                arguments={
                    "device_id": "M4XVHUV1MEPZ",
                    "component": "ram",
                    "spec": "32 GB DDR5 SODIMM",
                },
                rationale="Sustained 94% RAM usage on upgradable Dell XPS 15 9530",
                evidence_handles=_handles_for("M4XVHUV1MEPZ", "ram_used_pct"),
            ),
        ),
        (
            "TKT-",
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={
                    "device_id": "1LYSSFD074BB",
                    "check_id": "screen_lock",
                    "note": "Re-enable screen_lock compliance policy",
                },
                rationale="screen_lock drifted to fail",
                evidence_handles=_handles_for("1LYSSFD074BB", "compliance:screen_lock"),
            ),
        ),
        (
            "RPL-",
            DraftInput(
                tool_name="flag_device_for_replacement",
                target_type="device",
                target_id="8NM23J95R5I6",
                arguments={
                    "device_id": "8NM23J95R5I6",
                    "reason": "Battery Service Recommended plus low disk and screen_lock drift",
                },
                rationale="Multi-signal replacement candidate",
                evidence_handles=_handles_for("8NM23J95R5I6", "battery_"),
            ),
        ),
        (
            "NTF-",
            DraftInput(
                tool_name="notify_employee",
                target_type="employee",
                target_id="emp-acme-1003",
                arguments={
                    "employee_id": "emp-acme-1003",
                    "message": (
                        "Hi, your device MT7PJB7N5LRE (acme-macbook-4) is critically low "
                        "on disk space (2.0% free). Please free up space or contact IT."
                    ),
                },
                rationale="Low disk alert for employee device",
                evidence_handles=_handles_for("MT7PJB7N5LRE", "disk_free_pct"),
            ),
        ),
    ]

    for prefix, draft_in in cases:
        staged_res = svc.stage(ctx, [draft_in], thread_id, turn_id, ledger, clock)
        assert len(staged_res.staged) == 1, (
            f"Failed to stage {draft_in.tool_name}: {staged_res.refused}"
        )
        prop_id = staged_res.staged[0]["proposal_id"]

        dec = svc.decide(ctx, prop_id, "approve", "queue", clock, note="Approved in test")
        assert dec.applied is True
        assert dec.proposal["status"] == "approved"

        exe = svc.execute(ctx, prop_id, clock)
        assert exe.status == "executed"
        assert exe.receipt is not None
        assert exe.receipt.receipt_id.startswith(prefix)

        events = [
            e["event_type"]
            for e in audit_svc.list(ctx)
            if e["payload_json"].get("proposal_id") == prop_id
        ]
        assert events == ["proposal_staged", "proposal_decided", "action_executed"]

    verify = audit_svc.verify(ctx)
    assert verify["ok"] is True
    assert verify["rows_checked"] == 12
