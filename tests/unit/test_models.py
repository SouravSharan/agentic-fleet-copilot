"""Unit tests for domain Pydantic models and TenantContext."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fleet_copilot.domain.models import (
    Coverage,
    DeviceFilters,
    DeviceState,
    Evidence,
    Insight,
    PolicyValue,
    SuggestedAction,
    TenantContext,
    ToolResult,
)


def test_models_json_roundtrip() -> None:
    now = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)
    ev = Evidence(
        handle="E1",
        snapshot_id="snap_abc123",
        device_id="DEV001",
        hostname="acme-laptop-1",
        collected_at=now,
        metric="disk_free_pct",
        value=2.5,
        unit="%",
        label="Disk free 2.5%",
    )
    assert Evidence.model_validate_json(ev.model_dump_json()) == ev

    cov = Coverage(telemetry_as_of=now, evaluated=10, matched=2, notes=["3 Windows excluded"])
    assert Coverage.model_validate_json(cov.model_dump_json()) == cov

    pv = PolicyValue(value=15.0, source="global")
    assert PolicyValue.model_validate_json(pv.model_dump_json()) == pv

    dev = DeviceState(
        snapshot_id="snap_abc123",
        company_id="acme-001",
        device_id="DEV001",
        employee_id="emp-acme-1001",
        collected_at=now,
        agent_version="42.0.0",
        os_platform="darwin",
        os_product_name="macOS",
        os_product_version="14.5",
        os_hostname="acme-laptop-1",
        model_name="MacBook Pro",
        model_identifier="Mac14,5",
        ram_total_gb=16.0,
        ram_used_pct=55.2,
        disk_free_pct=2.5,
        disk_total_gb=512.0,
        has_battery_block=True,
        battery_present=True,
        battery_condition="Normal",
        battery_cycle_count=120,
        first_seen_at=now,
        last_seen_at=now,
        snapshot_count=30,
        ever_had_battery=True,
        form_factor="laptop",
    )
    assert DeviceState.model_validate_json(dev.model_dump_json()) == dev

    insight = Insight(
        insight_id="ins_disk_DEV001",
        category="disk",
        severity="high",
        device_id="DEV001",
        employee_id="emp-acme-1001",
        finding="Low disk free space (2.5%)",
        explanation="Free space fell below 15%.",
        evidence=[ev],
        stats={"latest_free_pct": 2.5},
        suggested_action=SuggestedAction(
            tool="open_remediation_ticket",
            args_hint={"device_id": "DEV001", "check_id": "low_disk_space"},
            reason="Free space below threshold",
        ),
    )
    assert Insight.model_validate_json(insight.model_dump_json()) == insight

    result = ToolResult(
        tool_name="find_devices",
        ok=True,
        summary="Found 2 devices",
        data=[dev.model_dump(mode="json")],
        evidence=[ev],
        coverage=cov,
        policy_applied={"low_disk_free_pct": pv},
        artifacts=[],
        error=None,
        truncated=False,
    )
    assert ToolResult.model_validate_json(result.model_dump_json()) == result


def test_models_forbid_extra_and_are_frozen() -> None:
    now = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)

    with pytest.raises(ValidationError):
        Evidence(
            snapshot_id="snap_1",
            device_id="DEV001",
            collected_at=now,
            metric="disk_free_pct",
            value=10.0,
            label="Disk free",
            unexpected="boom",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        DeviceFilters(unknown_field="x")  # type: ignore[call-arg]

    # Sensitive hardware/network fields must not exist on DeviceState
    forbidden_fields = {
        "hardware_uuid",
        "serial_number",
        "has_network_block",
        "mac",
        "address",
        "ip_address",
    }
    assert forbidden_fields.isdisjoint(DeviceState.model_fields.keys())

    # TenantContext is a frozen dataclass
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-123",
    )
    with pytest.raises(AttributeError):
        ctx.company_id = "globex-002"  # type: ignore[misc]
