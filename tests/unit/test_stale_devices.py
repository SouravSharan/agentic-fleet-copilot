"""Unit tests for per-device staleness flagging across fleet and trend read services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fleet_copilot.domain.models import (
    ComplianceQuery,
    ComplianceRecord,
    DeviceFilters,
    DeviceState,
    PolicyRow,
    TenantContext,
)
from fleet_copilot.services.fleet import (
    find_devices,
    get_device_details,
    get_employee_devices,
    get_fleet_overview,
    query_compliance,
)
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from tests.graph.fakes import InMemoryCopilotStore, InMemoryTelemetryRepo

AS_OF = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


def _ctx() -> TenantContext:
    return TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-stale-test",
    )


def _make_device(
    *,
    device_id: str,
    hostname: str,
    employee_id: str,
    collected_at: datetime,
) -> DeviceState:
    return DeviceState(
        snapshot_id=f"snap-{device_id}",
        company_id="acme-001",
        device_id=device_id,
        employee_id=employee_id,
        collected_at=collected_at,
        agent_version="42.0.0",
        os_platform="darwin",
        model_identifier="MacBookPro18,1",
        model_name="MacBook Pro 16",
        form_factor="laptop",
        os_product_name="macOS",
        os_product_version="15.1",
        os_hostname=hostname,
        ram_total_gb=16.0,
        ram_used_pct=55.0,
        disk_total_gb=500.0,
        disk_free_pct=42.0,
        has_battery_block=True,
        battery_present=True,
        battery_condition="Normal",
        battery_cycle_count=120,
        first_seen_at=collected_at - timedelta(days=10),
        last_seen_at=collected_at,
        snapshot_count=11,
        ever_had_battery=True,
    )


class _SyntheticRepo(InMemoryTelemetryRepo):
    def __init__(self, *, include_stale: bool) -> None:
        fresh = _make_device(
            device_id="FRESHDEV0001",
            hostname="acme-fresh-1",
            employee_id="emp-acme-2001",
            collected_at=AS_OF,
        )
        self._devices = [fresh]
        self._comp_rows = [
            ComplianceRecord(
                snapshot_id=fresh.snapshot_id,
                device_id=fresh.device_id,
                employee_id=fresh.employee_id,
                hostname=fresh.os_hostname,
                collected_at=fresh.collected_at,
                check_id="screen_lock",
                severity="medium",
                status="pass",
            )
        ]
        if include_stale:
            stale = _make_device(
                device_id="STALEDEV0002",
                hostname="acme-lost-laptop",
                employee_id="emp-acme-2001",
                collected_at=AS_OF - timedelta(days=14),
            )
            self._devices.append(stale)
            self._comp_rows.append(
                ComplianceRecord(
                    snapshot_id=stale.snapshot_id,
                    device_id=stale.device_id,
                    employee_id=stale.employee_id,
                    hostname=stale.os_hostname,
                    collected_at=stale.collected_at,
                    check_id="screen_lock",
                    severity="medium",
                    status="fail",
                )
            )

    def tenant_as_of(self, company_id: str) -> datetime:
        return AS_OF

    def compliance(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[ComplianceRecord]:
        return [r for r in self._comp_rows if device_ids is None or r.device_id in device_ids]


class _OverridePolicyStore(InMemoryCopilotStore):
    def __init__(self, extra_rows: list[PolicyRow]) -> None:
        super().__init__()
        self._extra = extra_rows

    def policies(self, company_id: str) -> list[PolicyRow]:
        return [*super().policies(company_id), *self._extra]


def test_stale_device_flagged_across_fleet_and_trend_services() -> None:
    repo = _SyntheticRepo(include_stale=True)
    policy_svc = PolicyService(InMemoryCopilotStore())
    ctx = _ctx()

    expected_note = (
        "1 device(s) not seen for more than 7 days: acme-lost-laptop (last seen 2026-05-29)"
    )

    # 1. get_fleet_overview
    ov = get_fleet_overview(ctx, repo, policy_svc)
    assert ov.coverage is not None
    assert expected_note in ov.coverage.notes

    # 2. find_devices
    found = find_devices(ctx, DeviceFilters(), repo, policy_svc)
    assert found.coverage is not None
    assert expected_note in found.coverage.notes
    by_id = {row["device_id"]: row for row in found.data}
    assert by_id["STALEDEV0002"]["last_seen_days_ago"] == 14
    assert "last_seen_days_ago" not in by_id["FRESHDEV0001"]
    assert "stale_device_days" not in found.policy_applied

    # 3. query_compliance
    comp = query_compliance(ctx, ComplianceQuery(status="fail"), repo, policy_svc)
    assert comp.coverage is not None
    assert expected_note in comp.coverage.notes
    assert comp.data[0]["device_id"] == "STALEDEV0002"
    assert comp.data[0]["last_seen_days_ago"] == 14

    # 4. get_device_details (stale device vs fresh device)
    det_stale = get_device_details(ctx, "STALEDEV0002", repo, policy_svc)
    assert det_stale.coverage is not None
    assert expected_note in det_stale.coverage.notes
    assert det_stale.data[0]["profile"]["last_seen_days_ago"] == 14

    det_fresh = get_device_details(ctx, "FRESHDEV0001", repo, policy_svc)
    assert det_fresh.coverage is not None
    assert det_fresh.coverage.notes == []
    assert "last_seen_days_ago" not in det_fresh.data[0]["profile"]

    # 5. get_employee_devices
    emp = get_employee_devices(ctx, "emp-acme-2001", repo, policy_svc)
    assert emp.coverage is not None
    assert expected_note in emp.coverage.notes
    emp_by_id = {row["device_id"]: row for row in emp.data}
    assert emp_by_id["STALEDEV0002"]["last_seen_days_ago"] == 14
    assert "last_seen_days_ago" not in emp_by_id["FRESHDEV0001"]

    # 6. analyze_fleet_trends (single note across all categories + rollup field)
    trends = analyze_fleet_trends(ctx, ["all"], repo, policy_svc)
    assert trends.coverage is not None
    assert trends.coverage.notes.count(expected_note) == 1
    rollup = next(a for a in trends.artifacts if a["type"] == "device_signal_rollup")
    rollup_by_id = {row["device_id"]: row for row in rollup["rows"]}
    assert rollup_by_id["STALEDEV0002"]["last_seen_days_ago"] == 14
    assert "last_seen_days_ago" not in rollup_by_id["FRESHDEV0001"]


def test_stale_device_respects_tenant_policy_override() -> None:
    repo = _SyntheticRepo(include_stale=True)
    store = _OverridePolicyStore(
        [
            PolicyRow(
                company_id="acme-001",
                scope_type="tenant",
                scope_value="",
                metric_key="stale_device_days",
                value=21,
            )
        ]
    )
    policy_svc = PolicyService(store)
    ctx = _ctx()

    # 14 days old is NOT stale under a 21-day tenant threshold -> no note, no field
    found = find_devices(ctx, DeviceFilters(), repo, policy_svc)
    assert found.coverage is not None
    assert not any("not seen for more than" in n for n in found.coverage.notes)
    assert all("last_seen_days_ago" not in row for row in found.data)


def test_zero_stale_devices_leaves_outputs_without_new_keys_notes_or_policies() -> None:
    repo = _SyntheticRepo(include_stale=False)
    policy_svc = PolicyService(InMemoryCopilotStore())
    ctx = _ctx()

    ov = get_fleet_overview(ctx, repo, policy_svc)
    assert ov.coverage is not None and ov.coverage.notes == []

    found = find_devices(ctx, DeviceFilters(), repo, policy_svc)
    assert found.coverage is not None and found.coverage.notes == []
    assert found.policy_applied == {}
    assert all("last_seen_days_ago" not in row for row in found.data)

    comp = query_compliance(ctx, ComplianceQuery(), repo, policy_svc)
    assert comp.coverage is not None
    assert not any("not seen for more than" in n for n in comp.coverage.notes)
    assert all("last_seen_days_ago" not in row for row in comp.data)

    emp = get_employee_devices(ctx, "emp-acme-2001", repo, policy_svc)
    assert emp.coverage is not None and emp.coverage.notes == []
    assert emp.policy_applied == {}
    assert all("last_seen_days_ago" not in row for row in emp.data)

    trends = analyze_fleet_trends(ctx, ["all"], repo, policy_svc)
    assert trends.coverage is not None
    assert not any("not seen for more than" in n for n in trends.coverage.notes)
    assert "stale_device_days" not in trends.policy_applied
    rollup = next(a for a in trends.artifacts if a["type"] == "device_signal_rollup")
    assert all("last_seen_days_ago" not in row for row in rollup["rows"])
