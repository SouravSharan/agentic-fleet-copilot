"""Parametrized integration tests covering every refused example in 05 §3.6 on the dataset."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

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


@pytest.fixture(scope="module")
def ledgers_by_tenant(db_manager: DatabaseManager) -> dict[str, EvidenceLedger]:
    repo = PgTelemetryRepository(db_manager)
    policy_svc = PolicyService(PgCopilotStore(db_manager))
    out: dict[str, EvidenceLedger] = {}
    for company_id in ("acme-001", "globex-002"):
        ctx = TenantContext(
            company_id=company_id,
            actor_id=f"{company_id}_admin",
            role="it_admin",
            trace_id=f"trace-ledger-{company_id}",
        )
        ledger = EvidenceLedger()
        for ev in analyze_fleet_trends(ctx, ["all"], repo, policy_svc).evidence:
            ledger.add(ev)
        for dev in repo.latest_states(company_id):
            for ev in get_device_details(ctx, dev.device_id, repo, policy_svc).evidence:
                ledger.add(ev)
        out[company_id] = ledger
    return out


def _handles(ledger: EvidenceLedger, device_id: str, prefix: str = "") -> list[str]:
    return [
        ev.handle
        for ev in ledger.all()
        if ev.handle is not None
        and ev.device_id == device_id
        and (not prefix or ev.metric.startswith(prefix))
    ]


REFUSAL_CASES: list[tuple[str, str, str, dict[str, Any], str, str, set[str], str | None]] = [
    (
        "mac_mini_battery_upgrade",
        "acme-001",
        "it_admin",
        {
            "tool_name": "create_upgrade_order",
            "target_type": "device",
            "target_id": "DLT35MHGTH2D",
            "arguments": {
                "device_id": "DLT35MHGTH2D",
                "component": "battery",
                "spec": "OEM Battery",
            },
            "rationale": "Battery upgrade on Mac mini",
        },
        "DLT35MHGTH2D",
        "",
        set(),
        None,
    ),
    (
        "soldered_ram_thinkpad_x1",
        "globex-002",
        "it_admin",
        {
            "tool_name": "create_upgrade_order",
            "target_type": "device",
            "target_id": "JRZSGXVMKE6M",
            "arguments": {"device_id": "JRZSGXVMKE6M", "component": "ram", "spec": "32 GB DDR5"},
            "rationale": "Upgrade soldered RAM",
        },
        "JRZSGXVMKE6M",
        "ram_used_pct",
        {"flag_device_for_replacement", "open_remediation_ticket"},
        None,
    ),
    (
        "healthy_laptop_ram_upgrade",
        "acme-001",
        "it_admin",
        {
            "tool_name": "create_upgrade_order",
            "target_type": "device",
            "target_id": "XSU9UUDGU6AC",
            "arguments": {"device_id": "XSU9UUDGU6AC", "component": "ram", "spec": "32 GB"},
            "rationale": "Healthy device",
        },
        "XSU9UUDGU6AC",
        "",
        set(),
        None,
    ),
    (
        "soldered_storage_macbook_pro",
        "acme-001",
        "it_admin",
        {
            "tool_name": "create_upgrade_order",
            "target_type": "device",
            "target_id": "MT7PJB7N5LRE",
            "arguments": {"device_id": "MT7PJB7N5LRE", "component": "storage", "spec": "1 TB SSD"},
            "rationale": "Upgrade soldered Mac SSD",
        },
        "MT7PJB7N5LRE",
        "disk_free_pct",
        {"open_remediation_ticket", "flag_device_for_replacement"},
        None,
    ),
    (
        "passing_disk_encryption_ticket",
        "acme-001",
        "it_admin",
        {
            "tool_name": "open_remediation_ticket",
            "target_type": "device",
            "target_id": "1LYSSFD074BB",
            "arguments": {
                "device_id": "1LYSSFD074BB",
                "check_id": "disk_encryption",
                "note": "Check disk_encryption",
            },
            "rationale": "Check passing policy",
        },
        "1LYSSFD074BB",
        "",
        set(),
        None,
    ),
    (
        "healthy_device_replacement",
        "acme-001",
        "it_admin",
        {
            "tool_name": "flag_device_for_replacement",
            "target_type": "device",
            "target_id": "Q3VUITEGQ400",
            "arguments": {"device_id": "Q3VUITEGQ400", "reason": "Just in case"},
            "rationale": "Healthy device",
        },
        "Q3VUITEGQ400",
        "",
        set(),
        None,
    ),
    (
        "notify_employee_other_device",
        "acme-001",
        "it_admin",
        {
            "tool_name": "notify_employee",
            "target_type": "employee",
            "target_id": "emp-acme-1003",
            "arguments": {
                "employee_id": "emp-acme-1003",
                "message": "Your device MT7PJB7N5LRE and also M4XVHUV1MEPZ need attention.",
            },
            "rationale": "Cross-employee mention",
        },
        "MT7PJB7N5LRE",
        "disk_free_pct",
        set(),
        None,
    ),
    (
        "fabricated_evidence_handle",
        "acme-001",
        "it_admin",
        {
            "tool_name": "open_remediation_ticket",
            "target_type": "device",
            "target_id": "1LYSSFD074BB",
            "arguments": {"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
            "rationale": "Fabricated evidence",
        },
        "__FABRICATED__",
        "",
        set(),
        None,
    ),
    (
        "cross_tenant_device_not_found",
        "acme-001",
        "it_admin",
        {
            "tool_name": "create_upgrade_order",
            "target_type": "device",
            "target_id": "E4V0SB5I0MYU",
            "arguments": {"device_id": "E4V0SB5I0MYU", "component": "storage", "spec": "1 TB SSD"},
            "rationale": "Cross tenant",
        },
        "MT7PJB7N5LRE",
        "",
        set(),
        "not_found",
    ),
    (
        "viewer_role_refused",
        "acme-001",
        "viewer",
        {
            "tool_name": "open_remediation_ticket",
            "target_type": "device",
            "target_id": "1LYSSFD074BB",
            "arguments": {"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
            "rationale": "Viewer attempt",
        },
        "1LYSSFD074BB",
        "compliance:screen_lock",
        set(),
        None,
    ),
]


@pytest.mark.parametrize(
    (
        "case_id",
        "company_id",
        "role",
        "draft_fields",
        "evidence_device",
        "metric_prefix",
        "expected_alt_tools",
        "expected_reason_substr",
    ),
    REFUSAL_CASES,
    ids=[c[0] for c in REFUSAL_CASES],
)
def test_refusal_matrix_on_dataset(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    ledgers_by_tenant: dict[str, EvidenceLedger],
    case_id: str,
    company_id: str,
    role: str,
    draft_fields: dict[str, Any],
    evidence_device: str,
    metric_prefix: str,
    expected_alt_tools: set[str],
    expected_reason_substr: str | None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    svc = ActionService(
        db_manager,
        repo,
        store,
        PolicyService(store),
        AuditService(db_manager),
        MockAdapter(),
    )
    ctx = TenantContext(
        company_id=company_id,
        actor_id=f"{company_id}_{role}",
        role=role,  # type: ignore[arg-type]
        trace_id=f"trace-refuse-{case_id}",
    )
    ledger = ledgers_by_tenant[company_id]
    handles = (
        ["E999999"]
        if evidence_device == "__FABRICATED__"
        else _handles(ledger, evidence_device, metric_prefix)
    )
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))

    verdict = svc.precheck(
        ctx,
        DraftInput(**draft_fields, evidence_handles=handles),
        ledger,
        clock,
    ).verdict

    assert verdict.decision == "refuse"
    if expected_alt_tools:
        assert expected_alt_tools.issubset({a.tool for a in verdict.alternatives})
    else:
        assert verdict.alternatives == []
    if expected_reason_substr is not None:
        assert any(expected_reason_substr in r for r in verdict.reasons)
