"""Immutable domain models for Fleet Copilot (Pydantic v2 + TenantContext dataclass)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class TenantContext:
    """Verified per-request tenant & actor identity propagated outside LLM tool args."""

    company_id: str
    actor_id: str
    role: Literal["viewer", "it_admin"]
    trace_id: str


class AmbiguousDevice(ValueError):
    """Raised when a hostname or serial lookup matches more than one device in a tenant."""

    def __init__(self, identifier: str, candidates: list[str]) -> None:
        self.identifier = identifier
        self.candidates = tuple(candidates)
        super().__init__(
            f"Ambiguous device identifier {identifier!r}; matches: {', '.join(self.candidates)}"
        )


class Evidence(_DomainModel):
    """Atomic evidence citation anchored to a single telemetry snapshot and metric."""

    handle: str | None = None
    snapshot_id: str
    device_id: str
    hostname: str | None = None
    collected_at: datetime
    metric: str
    value: float | int | str | bool | None
    unit: str | None = None
    label: str


class Coverage(_DomainModel):
    """Scope and denominator metadata reported alongside every read/analytics tool result."""

    telemetry_as_of: datetime
    evaluated: int
    matched: int
    notes: list[str] = Field(default_factory=list)


class PolicyValue(_DomainModel):
    """Resolved policy threshold paired with the scope level that supplied it."""

    value: Any
    source: Literal["global", "tenant", "device"]


class ToolResult(_DomainModel):
    """Standard deterministic output contract returned by all domain services."""

    tool_name: str
    ok: bool = True
    summary: str
    data: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    coverage: Coverage | None = None
    policy_applied: dict[str, PolicyValue] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    truncated: bool = False


class DeviceState(_DomainModel):
    """Sanitized latest device state (excludes hardware_uuid, serial_number, and network fields)."""

    snapshot_id: str
    company_id: str
    device_id: str
    employee_id: str
    collected_at: datetime
    agent_version: str
    os_platform: str | None = None
    os_product_name: str | None = None
    os_product_version: str | None = None
    os_build_version: str | None = None
    os_architecture: str | None = None
    os_hostname: str | None = None
    model_name: str | None = None
    model_identifier: str | None = None
    processor: str | None = None
    total_memory_label: str | None = None
    ram_total_bytes: int | None = None
    ram_used_bytes: int | None = None
    ram_free_bytes: int | None = None
    ram_total_gb: float | None = None
    ram_used_pct: float | None = None
    disk_free_pct: float | None = None
    disk_total_gb: float | None = None
    has_battery_block: bool = False
    battery_present: bool | None = None
    battery_charging_status: str | None = None
    battery_percentage: int | None = None
    battery_condition: str | None = None
    battery_cycle_count: int | None = None
    battery_full_charge_capacity: int | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    snapshot_count: int
    ever_had_battery: bool
    form_factor: Literal["laptop", "desktop", "unknown"]


class SuggestedAction(_DomainModel):
    """Advisory next action attached to an analytical insight (never auto-executed)."""

    tool: str
    args_hint: dict[str, Any] = Field(default_factory=dict)
    reason: str


class Insight(_DomainModel):
    """Structured fleet health insight backed by snapshot evidence and computed statistics."""

    insight_id: str
    category: str
    severity: Literal["low", "medium", "high"]
    device_id: str
    employee_id: str
    finding: str
    explanation: str
    evidence: list[Evidence]
    stats: dict[str, Any] = Field(default_factory=dict)
    suggested_action: SuggestedAction | None = None
    chart: dict[str, Any] | None = None


class DeviceFilters(_DomainModel):
    """Validated filter parameters for `find_devices`."""

    form_factor: Literal["laptop", "desktop", "unknown"] | None = None
    os_family: Literal["macos", "windows"] | None = None
    os_version_lt: str | None = None
    model: str | None = None
    employee_id: str | None = None
    hostname: str | None = None
    low_disk_only: bool = False
    max_disk_free_pct: float | None = None
    min_ram_used_pct: float | None = None
    battery_condition: str | None = None
    software_name: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ComplianceQuery(_DomainModel):
    """Validated filter parameters for `query_compliance`."""

    severity: Literal["low", "medium", "high"] | None = None
    status: Literal["pass", "fail"] | None = None
    check_id: str | None = None
    form_factor: Literal["laptop", "desktop", "unknown"] | None = None
    scope: Literal["latest", "window"] = "latest"


class SnapshotMetricPoint(_DomainModel):
    """Single time-series telemetry snapshot point for resource and battery analysis."""

    snapshot_id: str
    device_id: str
    employee_id: str
    hostname: str | None = None
    collected_at: datetime
    ram_used_pct: float | None = None
    disk_free_pct: float | None = None
    disk_total_gb: float | None = None
    has_battery_block: bool = False
    battery_present: bool | None = None
    battery_condition: str | None = None
    battery_cycle_count: int | None = None
    battery_full_charge_capacity: int | None = None
    battery_percentage: int | None = None


class ComplianceRecord(_DomainModel):
    """Single flattened compliance check observation."""

    snapshot_id: str
    device_id: str
    employee_id: str
    hostname: str | None = None
    collected_at: datetime
    check_id: str
    status: str
    severity: str


class SoftwareRecord(_DomainModel):
    """Single flattened installed software observation."""

    snapshot_id: str
    device_id: str
    employee_id: str
    hostname: str | None = None
    collected_at: datetime
    software_name: str
    version: str | None = None
    publisher: str | None = None


class PolicyRow(_DomainModel):
    """Raw policy row stored in `copilot.tenant_policies`."""

    company_id: str
    scope_type: Literal["global", "tenant", "device_type", "device"]
    scope_value: str
    metric_key: str
    value: Any


class ToolConfigRow(_DomainModel):
    """Raw tool configuration row stored in `copilot.tenant_tool_configs`."""

    company_id: str
    tool_name: str
    enabled: bool
    requires_approval: bool
    min_role: Literal["employee", "viewer", "it_admin"]
    adapter_config: dict[str, Any] = Field(default_factory=dict)
