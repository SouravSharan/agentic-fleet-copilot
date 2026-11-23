"""Unit tests for PolicyService resolution order and source tracking."""

from __future__ import annotations

import pytest

from fleet_copilot.domain.models import PolicyRow, PolicyValue, TenantContext
from fleet_copilot.services.policy import PolicyService


class _FakePolicyStore:
    def __init__(self, rows: list[PolicyRow]) -> None:
        self._rows = rows

    def policies(self, company_id: str) -> list[PolicyRow]:
        return [r for r in self._rows if r.company_id in ("*", company_id)]


def _ctx(company_id: str = "acme-001") -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id="admin-1",
        role="it_admin",
        trace_id="trace-1",
    )


def test_policy_service_precedence_and_sources() -> None:
    store = _FakePolicyStore(
        [
            PolicyRow(
                company_id="*",
                scope_type="global",
                scope_value="",
                metric_key="low_disk_free_pct",
                value=15.0,
            ),
            PolicyRow(
                company_id="*",
                scope_type="global",
                scope_value="",
                metric_key="high_ram_used_pct",
                value=80.0,
            ),
            PolicyRow(
                company_id="*",
                scope_type="global",
                scope_value="",
                metric_key="stale_device_days",
                value=7,
            ),
            PolicyRow(
                company_id="acme-001",
                scope_type="tenant",
                scope_value="",
                metric_key="low_disk_free_pct",
                value=12.0,
            ),
            PolicyRow(
                company_id="acme-001",
                scope_type="device",
                scope_value="DEV-SPECIAL",
                metric_key="low_disk_free_pct",
                value=5.0,
            ),
            PolicyRow(
                company_id="globex-002",
                scope_type="tenant",
                scope_value="",
                metric_key="low_disk_free_pct",
                value=25.0,
            ),
        ]
    )
    svc = PolicyService(store)

    # Tenant-level resolution (no device_id)
    tenant_resolved = svc.resolve(_ctx("acme-001"))
    assert tenant_resolved["low_disk_free_pct"] == PolicyValue(value=12.0, source="tenant")
    assert tenant_resolved["high_ram_used_pct"] == PolicyValue(value=80.0, source="global")
    assert tenant_resolved["stale_device_days"] == PolicyValue(value=7, source="global")

    # Device-level override beats tenant and global
    dev_resolved = svc.resolve(_ctx("acme-001"), device_id="DEV-SPECIAL")
    assert dev_resolved["low_disk_free_pct"] == PolicyValue(value=5.0, source="device")
    assert dev_resolved["high_ram_used_pct"] == PolicyValue(value=80.0, source="global")

    # Another device in same tenant gets tenant override, not DEV-SPECIAL's override
    other_dev = svc.resolve(_ctx("acme-001"), device_id="DEV-NORMAL")
    assert other_dev["low_disk_free_pct"] == PolicyValue(value=12.0, source="tenant")

    # Unknown key raises KeyError
    with pytest.raises(KeyError):
        svc.get(_ctx("acme-001"), "does_not_exist")
