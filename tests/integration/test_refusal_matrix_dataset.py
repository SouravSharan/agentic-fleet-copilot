"""Integration tests covering every refused example in 05 §3.6 on the real dataset."""

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


def _build(
    db: DatabaseManager,
    company_id: str,
    role: str = "it_admin",
) -> tuple[ActionService, TenantContext, EvidenceLedger, FixedClock]:
    repo = PgTelemetryRepository(db)
    store = PgCopilotStore(db)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db)
    svc = ActionService(db, repo, store, policy_svc, audit_svc, MockAdapter())
    ctx = TenantContext(
        company_id=company_id,
        actor_id=f"{company_id}_{role}",
        role=role,  # type: ignore[arg-type]
        trace_id=f"trace-refuse-{company_id}",
    )
    ledger = EvidenceLedger()
    tr = analyze_fleet_trends(ctx, ["all"], repo, policy_svc)
    for ev in tr.evidence:
        ledger.add(ev)
    for dev in repo.latest_states(company_id):
        det = get_device_details(ctx, dev.device_id, repo, policy_svc)
        for ev in det.evidence:
            ledger.add(ev)
    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    return svc, ctx, ledger, clock


def _h(ledger: EvidenceLedger, device_id: str, prefix: str = "") -> list[str]:
    return [
        ev.handle
        for ev in ledger.all()
        if ev.handle is not None
        and ev.device_id == device_id
        and (not prefix or ev.metric.startswith(prefix))
    ]


def test_refusal_matrix_on_dataset(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    svc_acme, ctx_acme, led_acme, clock = _build(db_manager, "acme-001")
    svc_globex, ctx_globex, led_globex, _ = _build(db_manager, "globex-002")

    # 1. Mac mini battery upgrade -> refused, no alternatives
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="DLT35MHGTH2D",
            arguments={"device_id": "DLT35MHGTH2D", "component": "battery", "spec": "OEM Battery"},
            rationale="Battery upgrade on Mac mini",
            evidence_handles=_h(led_acme, "DLT35MHGTH2D"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert v.alternatives == []

    # 2. Soldered RAM (JRZSGXVMKE6M in Globex) -> refused with replacement & memory_pressure ticket
    v = svc_globex.precheck(
        ctx_globex,
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="JRZSGXVMKE6M",
            arguments={"device_id": "JRZSGXVMKE6M", "component": "ram", "spec": "32 GB DDR5"},
            rationale="Upgrade soldered RAM",
            evidence_handles=_h(led_globex, "JRZSGXVMKE6M", "ram_used_pct"),
        ),
        led_globex,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert {"flag_device_for_replacement", "open_remediation_ticket"}.issubset(
        {a.tool for a in v.alternatives}
    )

    # 3. Healthy laptop RAM upgrade (XSU9UUDGU6AC) -> refused, no alternatives
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="XSU9UUDGU6AC",
            arguments={"device_id": "XSU9UUDGU6AC", "component": "ram", "spec": "32 GB"},
            rationale="Healthy device",
            evidence_handles=_h(led_acme, "XSU9UUDGU6AC"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert v.alternatives == []

    # 4. Soldered storage (MT7PJB7N5LRE) -> refused with low_disk_space ticket & replacement
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="MT7PJB7N5LRE",
            arguments={"device_id": "MT7PJB7N5LRE", "component": "storage", "spec": "1 TB SSD"},
            rationale="Upgrade soldered Mac SSD",
            evidence_handles=_h(led_acme, "MT7PJB7N5LRE", "disk_free_pct"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert {"open_remediation_ticket", "flag_device_for_replacement"}.issubset(
        {a.tool for a in v.alternatives}
    )

    # 5. Passing check ticket (disk_encryption) -> refused, no alternatives
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="1LYSSFD074BB",
            arguments={"device_id": "1LYSSFD074BB", "check_id": "disk_encryption", "note": "Check"},
            rationale="Check disk_encryption",
            evidence_handles=_h(led_acme, "1LYSSFD074BB"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert v.alternatives == []

    # 6. Healthy replacement (Q3VUITEGQ400) -> refused, no alternatives
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="flag_device_for_replacement",
            target_type="device",
            target_id="Q3VUITEGQ400",
            arguments={"device_id": "Q3VUITEGQ400", "reason": "Just in case"},
            rationale="Healthy device",
            evidence_handles=_h(led_acme, "Q3VUITEGQ400"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert v.alternatives == []

    # 7. Notify employee mentioning device they don't own -> refused
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="notify_employee",
            target_type="employee",
            target_id="emp-acme-1003",
            arguments={
                "employee_id": "emp-acme-1003",
                "message": "Your device MT7PJB7N5LRE and also M4XVHUV1MEPZ need attention.",
            },
            rationale="Cross-employee mention",
            evidence_handles=_h(led_acme, "MT7PJB7N5LRE", "disk_free_pct"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert v.alternatives == []

    # 8. Fabricated evidence handle -> refused
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="1LYSSFD074BB",
            arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
            rationale="Fabricated evidence",
            evidence_handles=["E999999"],
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"

    # 9. Cross-tenant device (Acme ctx -> Globex E4V0SB5I0MYU) -> refused with not_found
    v = svc_acme.precheck(
        ctx_acme,
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="E4V0SB5I0MYU",
            arguments={"device_id": "E4V0SB5I0MYU", "component": "storage", "spec": "1 TB SSD"},
            rationale="Cross tenant",
            evidence_handles=_h(led_acme, "MT7PJB7N5LRE"),
        ),
        led_acme,
        clock,
    ).verdict
    assert v.decision == "refuse"
    assert any("not_found" in r for r in v.reasons)

    # 10. Viewer role -> refused
    svc_view, ctx_view, led_view, _ = _build(db_manager, "acme-001", role="viewer")
    v = svc_view.precheck(
        ctx_view,
        DraftInput(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="1LYSSFD074BB",
            arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
            rationale="Viewer attempt",
            evidence_handles=_h(led_view, "1LYSSFD074BB", "compliance:screen_lock"),
        ),
        led_view,
        clock,
    ).verdict
    assert v.decision == "refuse"
