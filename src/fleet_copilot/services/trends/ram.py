"""Sustained high RAM utilization trend analyzer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from fleet_copilot.domain.models import (
    Coverage,
    Evidence,
    Insight,
    PolicyValue,
    SnapshotMetricPoint,
    SuggestedAction,
    TenantContext,
)
from fleet_copilot.domain.stats import percentile, share_at_or_above
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.trends.base import AnalyzerOutput, resolve_policy_float


class RamAnalyzer:
    """Flags devices whose RAM utilization meets or exceeds threshold on >= sustained ratio."""

    category: str = "ram"

    def analyze(
        self,
        ctx: TenantContext,
        repo: TelemetryRepository,
        policies: dict[str, PolicyValue],
        as_of: datetime,
        window_days: int = 30,
        device_ids: Sequence[str] | None = None,
    ) -> AnalyzerOutput:
        """Analyze sustained RAM pressure over `[as_of - window_days, as_of]`."""
        high_ram_pct, pv_ram = resolve_policy_float(policies, "high_ram_used_pct", 80.0)
        sustained_ratio, pv_ratio = resolve_policy_float(policies, "ram_sustained_ratio", 0.7)
        policy_applied = {
            "high_ram_used_pct": pv_ram,
            "ram_sustained_ratio": pv_ratio,
        }

        devices = sorted(
            repo.latest_states(ctx.company_id),
            key=lambda d: (d.device_id, d.collected_at),
        )
        if device_ids is not None:
            allowed = set(device_ids)
            devices = [d for d in devices if d.device_id in allowed]

        since = as_of - timedelta(days=window_days)
        pts = repo.series(
            ctx.company_id,
            since=since,
            device_ids=[d.device_id for d in devices],
        )
        by_dev: dict[str, list[SnapshotMetricPoint]] = defaultdict(list)
        for p in pts:
            if p.ram_used_pct is not None:
                by_dev[p.device_id].append(p)

        insights: list[Insight] = []
        for dev in devices:
            dev_pts = sorted(by_dev.get(dev.device_id, []), key=lambda p: p.collected_at)
            if not dev_pts:
                continue

            vals = [p.ram_used_pct for p in dev_pts if p.ram_used_pct is not None]
            share = share_at_or_above(vals, high_ram_pct)
            if share < sustained_ratio:
                continue

            avg_val = round(sum(vals) / len(vals), 2)
            p50_val = round(percentile(vals, 50.0) or 0.0, 2)
            p95_val = round(percentile(vals, 95.0) or 0.0, 2)
            max_val = round(max(vals), 2)

            peak_pt = max(dev_pts, key=lambda p: (p.ram_used_pct or -1.0, p.collected_at))
            latest_pt = dev_pts[-1]

            evidence = [
                Evidence(
                    handle=None,
                    snapshot_id=peak_pt.snapshot_id,
                    device_id=dev.device_id,
                    hostname=dev.os_hostname,
                    collected_at=peak_pt.collected_at,
                    metric="ram_used_pct_peak",
                    value=peak_pt.ram_used_pct,
                    unit="%",
                    label=f"Peak RAM utilization {peak_pt.ram_used_pct}%",
                ),
                Evidence(
                    handle=None,
                    snapshot_id=latest_pt.snapshot_id,
                    device_id=dev.device_id,
                    hostname=dev.os_hostname,
                    collected_at=latest_pt.collected_at,
                    metric="ram_used_pct",
                    value=latest_pt.ram_used_pct,
                    unit="%",
                    label=f"Latest RAM utilization {latest_pt.ram_used_pct}%",
                ),
            ]

            severity: Literal["low", "medium", "high"] = "high" if p50_val >= 90.0 else "medium"
            finding = (
                f"Sustained high RAM usage on {dev.os_hostname or dev.device_id}: "
                f"{share * 100:.0f}% of snapshots >= {high_ram_pct:.0f}% (p50={p50_val}%)."
            )
            explanation = (
                f"Across {len(vals)} snapshots in the {window_days}-day window, RAM utilization "
                f"averaged {avg_val}% (p50={p50_val}%, p95={p95_val}%, max={max_val}%), "
                f"meeting or exceeding {high_ram_pct}% on {share * 100:.1f}% of snapshots "
                f"(policy threshold >= {sustained_ratio * 100:.0f}%)."
            )

            insights.append(
                Insight(
                    insight_id=f"ram:{dev.device_id}",
                    category=self.category,
                    severity=severity,
                    device_id=dev.device_id,
                    employee_id=dev.employee_id,
                    finding=finding,
                    explanation=explanation,
                    evidence=evidence,
                    stats={
                        "avg": avg_val,
                        "p50": p50_val,
                        "p95": p95_val,
                        "max": max_val,
                        "share": round(share, 4),
                        "snapshots": len(vals),
                        "ram_total_gb": dev.ram_total_gb,
                    },
                    suggested_action=SuggestedAction(
                        tool="create_upgrade_order",
                        args_hint={"device_id": dev.device_id, "component": "ram"},
                        reason="Sustained high RAM utilization over the analysis window",
                    ),
                )
            )

        return AnalyzerOutput(
            category=self.category,
            insights=insights,
            coverage=Coverage(
                telemetry_as_of=as_of,
                evaluated=len(devices),
                matched=len(insights),
                notes=[],
            ),
            policy_applied=policy_applied,
        )
