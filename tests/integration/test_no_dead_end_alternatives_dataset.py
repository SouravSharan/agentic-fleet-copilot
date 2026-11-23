"""Integration test proving zero dead-end alternatives across all tenants x devices x actions."""

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

TENANTS = ("acme-001", "globex-002", "initech-003")


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


@pytest.mark.parametrize("company_id", TENANTS)
def test_no_dead_end_alternatives_across_all_devices(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    company_id: str,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, MockAdapter())
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id=company_id,
        actor_id=f"{company_id}_admin",
        role="it_admin",
        trace_id=f"trace-alt-{company_id}",
    )

    ledger = EvidenceLedger()
    tr = analyze_fleet_trends(ctx, ["all"], repo, policy_svc)
    for ev in tr.evidence:
        ledger.add(ev)
    devices = repo.latest_states(company_id)
    for d in devices:
        det = get_device_details(ctx, d.device_id, repo, policy_svc)
        for ev in det.evidence:
            ledger.add(ev)

    for dev in devices:
        handles = [
            ev.handle
            for ev in ledger.all()
            if ev.handle is not None and ev.device_id == dev.device_id
        ]
        assert handles

        candidate_drafts = [
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id=dev.device_id,
                arguments={"device_id": dev.device_id, "component": "ram", "spec": "32 GB DDR5"},
                rationale="Check RAM upgrade",
                evidence_handles=handles,
            ),
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id=dev.device_id,
                arguments={"device_id": dev.device_id, "component": "storage", "spec": "2 TB SSD"},
                rationale="Check storage upgrade",
                evidence_handles=handles,
            ),
            DraftInput(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id=dev.device_id,
                arguments={
                    "device_id": dev.device_id,
                    "component": "battery",
                    "spec": "OEM Battery",
                },
                rationale="Check battery upgrade",
                evidence_handles=handles,
            ),
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id=dev.device_id,
                arguments={
                    "device_id": dev.device_id,
                    "check_id": "screen_lock",
                    "note": "Check screen_lock",
                },
                rationale="Check ticket",
                evidence_handles=handles,
            ),
            DraftInput(
                tool_name="flag_device_for_replacement",
                target_type="device",
                target_id=dev.device_id,
                arguments={"device_id": dev.device_id, "reason": "Check replacement"},
                rationale="Check replacement",
                evidence_handles=handles,
            ),
        ]

        for draft_in in candidate_drafts:
            verdict = svc.precheck(ctx, draft_in, ledger, clock).verdict
            if verdict.decision == "refuse":
                for alt in verdict.alternatives:
                    alt_in = DraftInput(
                        tool_name=alt.tool,  # type: ignore[arg-type]
                        target_type="device",
                        target_id=dev.device_id,
                        arguments=dict(alt.args_hint),
                        rationale="Alternative from refusal",
                        evidence_handles=handles,
                    )
                    alt_verdict = svc.precheck(ctx, alt_in, ledger, clock).verdict
                    assert alt_verdict.decision in ("allow", "allow_with_warnings"), (
                        f"{company_id}/{dev.device_id}: dead-end alt {alt} -> {alt_verdict.reasons}"
                    )
