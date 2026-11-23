"""Pure domain models, version parsing, statistics, and evidence ledger (no I/O)."""

from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import (
    AmbiguousDevice,
    ComplianceQuery,
    ComplianceRecord,
    Coverage,
    DeviceFilters,
    DeviceState,
    Evidence,
    Insight,
    PolicyRow,
    PolicyValue,
    SnapshotMetricPoint,
    SoftwareRecord,
    SuggestedAction,
    TenantContext,
    ToolConfigRow,
    ToolResult,
)
from fleet_copilot.domain.os_version import OsVersion, older_than, parse_os
from fleet_copilot.domain.stats import Ols, days_until, ols, percentile, share_at_or_above

__all__ = [
    "AmbiguousDevice",
    "ComplianceQuery",
    "ComplianceRecord",
    "Coverage",
    "DeviceFilters",
    "DeviceState",
    "Evidence",
    "EvidenceLedger",
    "Insight",
    "Ols",
    "OsVersion",
    "PolicyRow",
    "PolicyValue",
    "SnapshotMetricPoint",
    "SoftwareRecord",
    "SuggestedAction",
    "TenantContext",
    "ToolConfigRow",
    "ToolResult",
    "days_until",
    "older_than",
    "ols",
    "parse_os",
    "percentile",
    "share_at_or_above",
]
