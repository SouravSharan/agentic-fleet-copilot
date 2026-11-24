"""Integration tests for copilot.integration_outbox transactional writes, RLS, and redaction."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.ops.redact_turns import redact_expired_turns, scan_redacted_outbox
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService, DraftInput
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.fleet import get_device_details
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset, run_scenario


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


def _setup_ledger(
    ctx: TenantContext,
    repo: PgTelemetryRepository,
    policy_svc: PolicyService,
    device_ids: tuple[str, ...],
) -> EvidenceLedger:
    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["all"], repo, policy_svc).evidence:
        ledger.add(ev)
    for did in device_ids:
        for ev in get_device_details(ctx, did, repo, policy_svc).evidence:
            ledger.add(ev)
    return ledger


def _handles(ledger: EvidenceLedger, device_id: str, prefix: str) -> list[str]:
    return [
        ev.handle
        for ev in ledger.all()
        if ev.handle is not None and ev.device_id == device_id and ev.metric.startswith(prefix)
    ]


def test_outbox_records_expected_payload_for_all_four_channels_and_enforces_rls(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    adapter = MockAdapter()
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, adapter)

    clock = FixedClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))
    acme_ctx = TenantContext(
        company_id="acme-001",
        actor_id="rev_01:acme_admin",
        role="it_admin",
        trace_id="trace-outbox-1",
    )
    thread_id = "acme-001:outbox-thread"
    turn_id = "turn-outbox-1"
    store.ensure_thread(acme_ctx.company_id, thread_id, acme_ctx.actor_id)
    store.ensure_turn(
        acme_ctx.company_id, thread_id, turn_id, acme_ctx.trace_id, acme_ctx.actor_id, "run"
    )

    ledger = _setup_ledger(
        acme_ctx, repo, policy_svc, ("M4XVHUV1MEPZ", "1LYSSFD074BB", "8NM23J95R5I6", "MT7PJB7N5LRE")
    )

    drafts = [
        (
            "procurement",
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="M4XVHUV1MEPZ",
                arguments={"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB DDR5"},
                rationale="Sustained RAM pressure",
                evidence_handles=_handles(ledger, "M4XVHUV1MEPZ", "ram_used_pct"),
            ),
        ),
        (
            "ticketing",
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={
                    "device_id": "1LYSSFD074BB",
                    "check_id": "screen_lock",
                    "note": "Enable lock",
                },
                rationale="screen_lock failed",
                evidence_handles=_handles(ledger, "1LYSSFD074BB", "compliance:screen_lock"),
            ),
        ),
        (
            "asset_management",
            DraftInput(
                tool_name="flag_device_for_replacement",
                target_type="device",
                target_id="8NM23J95R5I6",
                arguments={"device_id": "8NM23J95R5I6", "reason": "Battery Service Recommended"},
                rationale="Multi-signal replacement candidate",
                evidence_handles=_handles(ledger, "8NM23J95R5I6", "battery_"),
            ),
        ),
        (
            "email",
            DraftInput(
                tool_name="notify_employee",
                target_type="employee",
                target_id="emp-acme-1003",
                arguments={
                    "employee_id": "emp-acme-1003",
                    "message": "Your laptop MT7PJB7N5LRE is critically low on disk space.",
                },
                rationale="Disk warning",
                evidence_handles=_handles(ledger, "MT7PJB7N5LRE", "disk_free_pct"),
            ),
        ),
    ]

    for expected_channel, draft in drafts:
        staged = svc.stage(acme_ctx, [draft], thread_id, turn_id, ledger, clock)
        pid = staged.staged[0]["proposal_id"]
        assert svc.decide(acme_ctx, pid, "approve", "api", clock).applied is True
        exe = svc.execute(acme_ctx, pid, clock)
        assert exe.status == "executed"
        assert exe.receipt is not None
        assert exe.receipt.channel == expected_channel

        rows = store.list_outbox("acme-001", proposal_id=pid)
        assert len(rows) == 1
        row = rows[0]
        assert row["receipt_id"] == exe.receipt.receipt_id
        assert row["channel"] == expected_channel
        payload = row["payload_json"]
        if expected_channel == "ticketing":
            assert payload["configuration_item"] == "1LYSSFD074BB"
            assert payload["short_description"]
            assert payload["priority"]
        elif expected_channel == "procurement":
            assert payload["sku"]
            assert payload["spec"] == "32 GB DDR5"
            assert payload["quantity"] == 1
            assert payload["ship_to_employee_id"].startswith("emp-acme-")
        elif expected_channel == "email":
            assert payload["subject"]
            assert "MT7PJB7N5LRE" in payload["body_preview"]
        elif expected_channel == "asset_management":
            assert payload["device_id"] == "8NM23J95R5I6"
            assert payload["action"] == "flag_for_replacement"

    # Cross-tenant RLS: globex-002 sees 0 Acme outbox rows
    assert len(store.list_outbox("acme-001")) == 4
    assert store.list_outbox("globex-002") == []

    # Retention redaction clears expired outbox payload_json
    redacted_turns = redact_expired_turns(
        db_manager,
        cutoff=clock.now() + timedelta(days=8),
        now=clock.now() + timedelta(days=8),
        company_id="acme-001",
    )
    assert redacted_turns >= 0
    assert scan_redacted_outbox(db_manager, company_id="acme-001") == []
    redacted_rows = store.list_outbox("acme-001")
    assert all(r["payload_json"] == {} and r["redacted_at"] is not None for r in redacted_rows)


def test_failed_and_stale_blocked_executions_write_no_outbox_row(
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

    clock = FixedClock(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="rev_01:acme_admin",
        role="it_admin",
        trace_id="trace-outbox-fail",
    )
    thread_id = "acme-001:outbox-fail-thread"
    turn_id = "turn-outbox-fail"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "fail")

    ledger = _setup_ledger(ctx, repo, policy_svc, ("M4XVHUV1MEPZ", "1LYSSFD074BB"))

    # 1. Failed adapter execution -> 0 outbox rows
    adapter.fail_next("downstream_503")
    st_fail = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="M4XVHUV1MEPZ",
                arguments={"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB DDR5"},
                rationale="Upgrade",
                evidence_handles=_handles(ledger, "M4XVHUV1MEPZ", "ram_used_pct"),
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    pid_fail = st_fail.staged[0]["proposal_id"]
    assert svc.decide(ctx, pid_fail, "approve", "api", clock).applied is True
    assert svc.execute(ctx, pid_fail, clock).status == "failed"
    assert store.list_outbox("acme-001", proposal_id=pid_fail) == []

    # 2. Stale-blocked (TOCTOU resolved before execution) -> 0 outbox rows
    st_stale = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
                rationale="Fix screen_lock",
                evidence_handles=_handles(ledger, "1LYSSFD074BB", "compliance:screen_lock"),
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    pid_stale = st_stale.staged[0]["proposal_id"]
    assert svc.decide(ctx, pid_stale, "approve", "api", clock).applied is True
    run_scenario(
        "toctou-resolved",
        device_id="1LYSSFD074BB",
        writer_url=get_settings().telemetry_writer_database_url,
    )
    assert svc.execute(ctx, pid_stale, clock).status == "stale_blocked"
    assert store.list_outbox("acme-001", proposal_id=pid_stale) == []
