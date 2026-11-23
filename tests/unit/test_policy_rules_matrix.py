"""Unit tests covering every row of the 05 §3.6 action policy rules matrix with synthetic facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fleet_copilot.clock import FixedClock
from fleet_copilot.domain.models import Evidence, PolicyValue
from fleet_copilot.domain.policy_rules import (
    DeviceFacts,
    EmployeeFacts,
    HardwareCatalogRow,
    ProposalDraft,
    evaluate,
)

AS_OF = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)
CLOCK = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))


def _policies(**overrides: Any) -> dict[str, PolicyValue]:
    base: dict[str, Any] = {
        "low_disk_free_pct": 15.0,
        "high_ram_used_pct": 80.0,
        "ram_sustained_ratio": 0.7,
        "battery_eol_cycles": 800,
        "battery_capacity_drop_pct": 3.0,
        "stale_device_days": 7,
        "max_telemetry_lag_days": None,
        "replacement_min_signals": 2,
        "notify_message_max_chars": 800,
    }
    base.update(overrides)
    return {k: PolicyValue(value=v, source="global") for k, v in base.items()}


def _ev(
    device_id: str,
    metric: str,
    value: Any = 2.0,
    *,
    collected_at: datetime = AS_OF,
    snap: str = "snap_1",
) -> Evidence:
    return Evidence(
        handle="E1",
        snapshot_id=snap,
        device_id=device_id,
        hostname=f"host-{device_id}",
        collected_at=collected_at,
        metric=metric,
        value=value,
        label=f"{metric}={value}",
    )


def _cat(
    model_id: str,
    *,
    form_factor: str = "laptop",
    ram: bool = True,
    ram_max: int | None = 64,
    storage: bool = True,
    battery: bool = True,
) -> HardwareCatalogRow:
    return HardwareCatalogRow(
        model_identifier=model_id,
        model_name=model_id,
        form_factor=form_factor,  # type: ignore[arg-type]
        ram_upgradable=ram,
        ram_max_gb=ram_max,
        storage_upgradable=storage,
        battery_serviceable=battery,
        source_urls=["https://example.test/spec"],
        confidence="high",
        verified_at="2026-11-23",
    )


def _device_facts(**overrides: Any) -> DeviceFacts:
    base: dict[str, Any] = {
        "device_id": "DEV1",
        "hostname": "acme-laptop-1",
        "model_identifier": "XPS-9530",
        "form_factor": "laptop",
        "ever_had_battery": True,
        "battery_condition": "Normal",
        "cycles": 200,
        "ram_sustained": False,
        "ram_total_gb": 16.0,
        "disk_latest_free_pct": 45.0,
        "disk_depleting": False,
        "days_to_low": None,
        "storage_total_gb": 512.0,
        "failing_checks": [],
        "non_baseline_apps": [],
        "active_signals": [],
        "latest_snapshot_at": AS_OF,
    }
    base.update(overrides)
    return DeviceFacts(**base)


def test_upgrade_battery_allowed_and_desktop_refused() -> None:
    laptop = _device_facts(
        device_id="LAP1",
        battery_condition="Service Recommended",
        cycles=1050,
        active_signals=["battery"],
    )
    draft = ProposalDraft(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="LAP1",
        arguments={"device_id": "LAP1", "component": "battery", "spec": "OEM Battery"},
        rationale="Battery SR",
        evidence=[_ev("LAP1", "battery_latest", "Service Recommended")],
    )
    verdict = evaluate(draft, laptop, _policies(), _cat("XPS-9530"), CLOCK, AS_OF)
    assert verdict.decision == "allow"

    desktop = _device_facts(
        device_id="MACMINI",
        model_identifier="Mac14,3",
        form_factor="desktop",
        ever_had_battery=False,
        battery_condition=None,
        cycles=None,
    )
    draft_desk = draft.model_copy(
        update={
            "target_id": "MACMINI",
            "arguments": {"device_id": "MACMINI", "component": "battery", "spec": "OEM Battery"},
            "evidence": [_ev("MACMINI", "device_state", "desktop")],
        }
    )
    v_desk = evaluate(
        draft_desk,
        desktop,
        _policies(),
        _cat(
            "Mac14,3", form_factor="desktop", ram=False, ram_max=None, storage=False, battery=False
        ),
        CLOCK,
        AS_OF,
    )
    assert v_desk.decision == "refuse"
    assert v_desk.alternatives == []


def test_upgrade_ram_allowed_soldered_refused_and_healthy_refused() -> None:
    # Upgradable + sustained RAM + valid spec -> allow
    xps = _device_facts(
        device_id="XPS1",
        ram_sustained=True,
        ram_total_gb=16.0,
        active_signals=["ram"],
    )
    draft = ProposalDraft(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="XPS1",
        arguments={"device_id": "XPS1", "component": "ram", "spec": "32 GB DDR5 SODIMM"},
        rationale="Sustained RAM pressure",
        evidence=[_ev("XPS1", "ram_used_pct", 94.0)],
    )
    v_ok = evaluate(draft, xps, _policies(), _cat("XPS-9530", ram=True, ram_max=64), CLOCK, AS_OF)
    assert v_ok.decision == "allow"

    # Spec exceeds ram_max_gb -> refuse
    draft_over = draft.model_copy(
        update={"arguments": {"device_id": "XPS1", "component": "ram", "spec": "128 GB DDR5"}}
    )
    v_over = evaluate(
        draft_over, xps, _policies(), _cat("XPS-9530", ram=True, ram_max=64), CLOCK, AS_OF
    )
    assert v_over.decision == "refuse"

    # Soldered RAM + sustained RAM -> refuse with replacement & memory_pressure alternatives
    x1 = _device_facts(
        device_id="X1",
        model_identifier="21CB",
        ram_sustained=True,
        ram_total_gb=16.0,
        active_signals=["ram"],
    )
    draft_x1 = draft.model_copy(
        update={
            "target_id": "X1",
            "arguments": {"device_id": "X1", "component": "ram", "spec": "32 GB"},
            "evidence": [_ev("X1", "ram_used_pct", 88.0)],
        }
    )
    v_soldered = evaluate(
        draft_x1, x1, _policies(), _cat("21CB", ram=False, ram_max=None, storage=True), CLOCK, AS_OF
    )
    assert v_soldered.decision == "refuse"
    alt_tools = {a.tool for a in v_soldered.alternatives}
    assert {"flag_device_for_replacement", "open_remediation_ticket"}.issubset(alt_tools)

    # Healthy laptop -> refuse with no alternatives
    healthy = _device_facts(device_id="HLTH", ram_sustained=False)
    draft_h = draft.model_copy(
        update={
            "target_id": "HLTH",
            "arguments": {"device_id": "HLTH", "component": "ram", "spec": "32 GB"},
            "evidence": [_ev("HLTH", "ram_used_pct", 45.0)],
        }
    )
    v_h = evaluate(draft_h, healthy, _policies(), _cat("XPS-9530"), CLOCK, AS_OF)
    assert v_h.decision == "refuse"
    assert v_h.alternatives == []

    # Unknown model in catalog -> allow_with_warnings
    v_unk = evaluate(draft, xps, _policies(), None, CLOCK, AS_OF)
    assert v_unk.decision == "allow_with_warnings"
    assert any("unknown" in w.lower() for w in v_unk.warnings)


def test_upgrade_storage_allowed_and_soldered_refused() -> None:
    x1 = _device_facts(
        device_id="X1",
        model_identifier="21CB",
        disk_latest_free_pct=2.5,
        disk_depleting=True,
        storage_total_gb=512.0,
        active_signals=["disk"],
    )
    draft = ProposalDraft(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="X1",
        arguments={"device_id": "X1", "component": "storage", "spec": "1 TB NVMe SSD"},
        rationale="Low disk space",
        evidence=[_ev("X1", "disk_free_pct", 2.5)],
    )
    v_ok = evaluate(
        draft, x1, _policies(), _cat("21CB", ram=False, ram_max=None, storage=True), CLOCK, AS_OF
    )
    assert v_ok.decision == "allow"

    # Soldered Mac storage -> refuse with ticket low_disk_space + replacement alternatives
    mbp = _device_facts(
        device_id="MBP1",
        model_identifier="Mac14,5",
        disk_latest_free_pct=2.0,
        disk_depleting=True,
        storage_total_gb=512.0,
        active_signals=["disk"],
    )
    draft_mbp = draft.model_copy(
        update={
            "target_id": "MBP1",
            "arguments": {"device_id": "MBP1", "component": "storage", "spec": "1 TB SSD"},
            "evidence": [_ev("MBP1", "disk_free_pct", 2.0)],
        }
    )
    v_mbp = evaluate(
        draft_mbp,
        mbp,
        _policies(),
        _cat("Mac14,5", ram=False, ram_max=None, storage=False),
        CLOCK,
        AS_OF,
    )
    assert v_mbp.decision == "refuse"
    assert {"open_remediation_ticket", "flag_device_for_replacement"}.issubset(
        {a.tool for a in v_mbp.alternatives}
    )


def test_remediation_ticket_and_replacement_rules() -> None:
    dev = _device_facts(
        device_id="DEV1",
        failing_checks=["screen_lock"],
        non_baseline_apps=["uTorrent"],
        active_signals=["compliance", "software"],
    )
    t_ok = evaluate(
        ProposalDraft(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="DEV1",
            arguments={"device_id": "DEV1", "check_id": "screen_lock", "note": "Fix screen lock"},
            rationale="screen_lock failed",
            evidence=[_ev("DEV1", "compliance:screen_lock", "fail")],
        ),
        dev,
        _policies(),
        _cat("Mac15,7", ram=False, ram_max=None, storage=False),
        CLOCK,
        AS_OF,
    )
    assert t_ok.decision == "allow"

    # Passing check refused
    t_pass = evaluate(
        ProposalDraft(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="DEV1",
            arguments={"device_id": "DEV1", "check_id": "disk_encryption", "note": "Check enc"},
            rationale="Test",
            evidence=[_ev("DEV1", "compliance:disk_encryption", "pass")],
        ),
        dev,
        _policies(),
        _cat("Mac15,7", ram=False, ram_max=None, storage=False),
        CLOCK,
        AS_OF,
    )
    assert t_pass.decision == "refuse"
    assert t_pass.alternatives == []

    # Non-baseline software ticket allowed
    t_sw = evaluate(
        ProposalDraft(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="DEV1",
            arguments={"device_id": "DEV1", "check_id": "non_baseline_software", "note": "Remove"},
            rationale="Unapproved software",
            evidence=[_ev("DEV1", "software:uTorrent", "installed")],
        ),
        dev,
        _policies(),
        _cat("Mac15,7", ram=False, ram_max=None, storage=False),
        CLOCK,
        AS_OF,
    )
    assert t_sw.decision == "allow"

    # Replacement allowed on >= 2 signals, refused on healthy device
    r_ok = evaluate(
        ProposalDraft(
            tool_name="flag_device_for_replacement",
            target_type="device",
            target_id="DEV1",
            arguments={"device_id": "DEV1", "reason": "Multiple active signals"},
            rationale="Multi-signal",
            evidence=[_ev("DEV1", "compliance:screen_lock", "fail")],
        ),
        dev,
        _policies(),
        _cat("Mac15,7", ram=False, ram_max=None, storage=False),
        CLOCK,
        AS_OF,
    )
    assert r_ok.decision == "allow"

    healthy = _device_facts(device_id="OK1")
    r_no = evaluate(
        ProposalDraft(
            tool_name="flag_device_for_replacement",
            target_type="device",
            target_id="OK1",
            arguments={"device_id": "OK1", "reason": "Feels slow"},
            rationale="No signals",
            evidence=[_ev("OK1", "device_state", "laptop")],
        ),
        healthy,
        _policies(),
        _cat("Mac14,5", ram=False, ram_max=None, storage=False),
        CLOCK,
        AS_OF,
    )
    assert r_no.decision == "refuse"


def test_notify_employee_and_common_invariants() -> None:
    d_low = _device_facts(
        device_id="DEV_OWN",
        hostname="acme-macbook-4",
        disk_latest_free_pct=2.0,
        disk_depleting=True,
        active_signals=["disk"],
    )
    emp = EmployeeFacts(
        employee_id="emp-acme-1003",
        devices=[d_low],
        hostnames=["acme-macbook-4"],
        other_device_ids=("DEV_OTHER",),
        other_hostnames=("acme-macbook-9",),
        other_employee_ids=("emp-acme-1002",),
    )

    good = ProposalDraft(
        tool_name="notify_employee",
        target_type="employee",
        target_id="emp-acme-1003",
        arguments={
            "employee_id": "emp-acme-1003",
            "message": "Your laptop DEV_OWN (acme-macbook-4) is low on disk space (2.0%).",
        },
        rationale="Low disk alert",
        evidence=[_ev("DEV_OWN", "disk_free_pct", 2.0)],
    )
    assert evaluate(good, emp, _policies(), None, CLOCK, AS_OF).decision == "allow"

    # Mentions another device -> refuse
    leak_dev = good.model_copy(
        update={
            "arguments": {
                "employee_id": "emp-acme-1003",
                "message": "Check DEV_OWN and also DEV_OTHER please.",
            }
        }
    )
    assert evaluate(leak_dev, emp, _policies(), None, CLOCK, AS_OF).decision == "refuse"

    # Mentions another employee -> refuse
    leak_emp = good.model_copy(
        update={
            "arguments": {
                "employee_id": "emp-acme-1003",
                "message": "DEV_OWN disk low; compare with emp-acme-1002.",
            }
        }
    )
    assert evaluate(leak_emp, emp, _policies(), None, CLOCK, AS_OF).decision == "refuse"

    # Stale-only evidence (> stale_device_days before latest_snapshot_at) -> refuse
    stale = good.model_copy(
        update={
            "evidence": [
                _ev("DEV_OWN", "disk_free_pct", 2.0, collected_at=AS_OF - timedelta(days=10))
            ]
        }
    )
    assert evaluate(stale, emp, _policies(), None, CLOCK, AS_OF).decision == "refuse"

    # Multi-point trend evidence [first (day 0) + latest (fresh)] -> allow
    trend_with_latest = good.model_copy(
        update={
            "evidence": [
                _ev(
                    "DEV_OWN",
                    "disk_free_pct_first",
                    12.0,
                    collected_at=AS_OF - timedelta(days=25),
                    snap="snap_0",
                ),
                _ev("DEV_OWN", "disk_free_pct", 2.0, collected_at=AS_OF, snap="snap_latest"),
            ]
        }
    )
    assert evaluate(trend_with_latest, emp, _policies(), None, CLOCK, AS_OF).decision == "allow"

    # Viewer role, disabled tool, lag exceeded, duplicate, cooldown -> all refuse
    assert (
        evaluate(good, emp, _policies(), None, CLOCK, AS_OF, actor_role="viewer").decision
        == "refuse"
    )
    assert (
        evaluate(good, emp, _policies(), None, CLOCK, AS_OF, tool_enabled=False).decision
        == "refuse"
    )
    assert (
        evaluate(good, emp, _policies(), None, CLOCK, AS_OF, actions_enabled=False).decision
        == "refuse"
    )
    assert (
        evaluate(good, emp, _policies(), None, CLOCK, AS_OF, has_active_duplicate=True).decision
        == "refuse"
    )
    assert (
        evaluate(good, emp, _policies(), None, CLOCK, AS_OF, in_cooldown=True).decision == "refuse"
    )
    assert (
        evaluate(good, emp, _policies(max_telemetry_lag_days=1), None, CLOCK, AS_OF).decision
        == "refuse"
    )
