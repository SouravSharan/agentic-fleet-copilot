"""Trend analyzer registry and unified `analyze_fleet_trends()` orchestration service."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fleet_copilot.domain.models import (
    Coverage,
    Evidence,
    Insight,
    PolicyValue,
    TenantContext,
    ToolResult,
)
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends.base import Analyzer, AnalyzerOutput
from fleet_copilot.services.trends.battery import BatteryAnalyzer
from fleet_copilot.services.trends.compliance import ComplianceDriftAnalyzer
from fleet_copilot.services.trends.disk import DiskAnalyzer
from fleet_copilot.services.trends.ram import RamAnalyzer
from fleet_copilot.services.trends.rollup import build_device_rollup
from fleet_copilot.services.trends.software import SoftwareAnalyzer

ANALYZER_ORDER: tuple[str, ...] = (
    "battery",
    "ram",
    "disk",
    "compliance_drift",
    "software",
)

ANALYZER_REGISTRY: dict[str, Analyzer] = {
    "battery": BatteryAnalyzer(),
    "ram": RamAnalyzer(),
    "disk": DiskAnalyzer(),
    "compliance_drift": ComplianceDriftAnalyzer(),
    "software": SoftwareAnalyzer(),
}


def analyze_fleet_trends(
    ctx: TenantContext,
    categories: Sequence[str],
    repo: TelemetryRepository,
    policy_svc: PolicyService,
    *,
    device_ids: Sequence[str] | None = None,
    window_days: int = 30,
    catalog_by_model: dict[str, Any] | None = None,
) -> ToolResult:
    """Run one or more trend analyzers and return insights, flattened evidence, and chart series."""
    valid_names = set(ANALYZER_REGISTRY.keys()) | {"all"}
    requested = list(categories) if categories else ["all"]
    unknown = [c for c in requested if c not in valid_names]
    if unknown:
        return ToolResult(
            tool_name="analyze_fleet_trends",
            ok=False,
            summary=f"Unknown trend category: {', '.join(unknown)}.",
            error=(f"Unknown category {unknown!r}. Valid: {', '.join((*ANALYZER_ORDER, 'all'))}"),
        )

    selected = (
        list(ANALYZER_ORDER)
        if "all" in requested
        else [c for c in ANALYZER_ORDER if c in set(requested)]
    )

    as_of = repo.tenant_as_of(ctx.company_id)
    policies = policy_svc.resolve(ctx)
    devices = sorted(
        repo.latest_states(ctx.company_id),
        key=lambda d: (d.device_id, d.collected_at),
    )
    if device_ids is not None:
        allowed = set(device_ids)
        devices = [d for d in devices if d.device_id in allowed]

    all_insights: list[Insight] = []
    all_evidence: list[Evidence] = []
    all_artifacts: list[dict[str, Any]] = []
    merged_policy: dict[str, PolicyValue] = {}
    notes: list[str] = []

    for cat in selected:
        analyzer = ANALYZER_REGISTRY[cat]
        out: AnalyzerOutput = analyzer.analyze(
            ctx,
            repo,
            policies,
            as_of,
            window_days=window_days,
            device_ids=device_ids,
        )
        all_insights.extend(out.insights)
        for ins in out.insights:
            all_evidence.extend(ins.evidence)
        all_artifacts.extend(out.artifacts)
        merged_policy.update(out.policy_applied)
        notes.extend(out.coverage.notes)

    rollup_rows = build_device_rollup(
        devices,
        all_insights,
        policies,
        catalog_by_model=catalog_by_model,
    )
    all_artifacts.append({"type": "device_signal_rollup", "rows": rollup_rows})

    summary = (
        f"Analyzed {len(devices)} device(s) across {', '.join(selected)} "
        f"as of {as_of.date().isoformat()}: {len(all_insights)} insight(s)."
    )

    return ToolResult(
        tool_name="analyze_fleet_trends",
        ok=True,
        summary=summary,
        data=[ins.model_dump(mode="json") for ins in all_insights],
        evidence=all_evidence,
        coverage=Coverage(
            telemetry_as_of=as_of,
            evaluated=len(devices),
            matched=len({ins.device_id for ins in all_insights}),
            notes=notes,
        ),
        policy_applied=merged_policy,
        artifacts=all_artifacts,
    )


__all__ = [
    "ANALYZER_ORDER",
    "ANALYZER_REGISTRY",
    "Analyzer",
    "AnalyzerOutput",
    "BatteryAnalyzer",
    "ComplianceDriftAnalyzer",
    "DiskAnalyzer",
    "RamAnalyzer",
    "SoftwareAnalyzer",
    "analyze_fleet_trends",
    "build_device_rollup",
]
