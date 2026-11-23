"""Disk capacity and depletion trajectory analyzer (OLS slope + R^2 gate + floor check)."""

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
from fleet_copilot.domain.stats import days_until, ols
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.trends.base import AnalyzerOutput, resolve_policy_float


class DiskAnalyzer:
    """Flags devices that are low on disk space or actively depleting on an OLS trajectory."""

    category: str = "disk"

    def analyze(
        self,
        ctx: TenantContext,
        repo: TelemetryRepository,
        policies: dict[str, PolicyValue],
        as_of: datetime,
        window_days: int = 30,
        device_ids: Sequence[str] | None = None,
    ) -> AnalyzerOutput:
        """Analyze low-disk and depleting-disk signals over `[as_of - window_days, as_of]`."""
        low_pct, pv_low = resolve_policy_float(policies, "low_disk_free_pct", 15.0)
        min_slope, pv_slope = resolve_policy_float(policies, "disk_burn_min_slope_pct_per_day", 0.1)
        min_r2, pv_r2 = resolve_policy_float(policies, "disk_trend_min_r2", 0.8)
        policy_applied = {
            "low_disk_free_pct": pv_low,
            "disk_burn_min_slope_pct_per_day": pv_slope,
            "disk_trend_min_r2": pv_r2,
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
            if p.disk_free_pct is not None:
                by_dev[p.device_id].append(p)

        insights: list[Insight] = []
        notes: list[str] = []

        for dev in devices:
            dev_pts = sorted(by_dev.get(dev.device_id, []), key=lambda p: p.collected_at)
            if not dev_pts:
                continue

            first_pt = dev_pts[0]
            latest_pt = dev_pts[-1]
            first_val = float(first_pt.disk_free_pct or 0.0)
            latest_val = float(latest_pt.disk_free_pct or 0.0)

            t0 = first_pt.collected_at
            xs = [(p.collected_at - t0).total_seconds() / 86400.0 for p in dev_pts]
            ys = [float(p.disk_free_pct or 0.0) for p in dev_pts]
            fit = ols(xs, ys)

            is_low = latest_val < low_pct
            is_depleting = fit is not None and fit.slope < -min_slope and fit.r2 >= min_r2

            if not (is_low or is_depleting):
                continue

            slope_val = round(fit.slope, 4) if fit is not None else None
            r2_val = round(fit.r2, 4) if fit is not None else None
            d_low = (
                round(d, 2)
                if fit is not None and (d := days_until(latest_val, fit.slope, low_pct)) is not None
                else None
            )
            d_zero = (
                round(d, 2)
                if fit is not None and (d := days_until(latest_val, fit.slope, 0.0)) is not None
                else None
            )

            # Check last-7-day flattening when depleting
            last_7d_slope: float | None = None
            flattened_near_floor = False
            if is_depleting:
                cutoff_7d = latest_pt.collected_at - timedelta(days=7)
                recent = [p for p in dev_pts if p.collected_at >= cutoff_7d]
                if len(recent) >= 3:
                    t7 = recent[0].collected_at
                    x7 = [(p.collected_at - t7).total_seconds() / 86400.0 for p in recent]
                    y7 = [float(p.disk_free_pct or 0.0) for p in recent]
                    fit7 = ols(x7, y7)
                    if fit7 is not None:
                        last_7d_slope = round(fit7.slope, 4)
                        if abs(fit7.slope) < 0.1:
                            flattened_near_floor = True

            first_below = next(
                (p for p in dev_pts if p.disk_free_pct is not None and p.disk_free_pct < low_pct),
                None,
            )

            evidence: list[Evidence] = [
                Evidence(
                    handle=None,
                    snapshot_id=first_pt.snapshot_id,
                    device_id=dev.device_id,
                    hostname=dev.os_hostname,
                    collected_at=first_pt.collected_at,
                    metric="disk_free_pct_first",
                    value=first_val,
                    unit="%",
                    label=f"First window disk free: {first_val}%",
                )
            ]
            if first_below is not None and first_below.snapshot_id not in {
                first_pt.snapshot_id,
                latest_pt.snapshot_id,
            }:
                evidence.append(
                    Evidence(
                        handle=None,
                        snapshot_id=first_below.snapshot_id,
                        device_id=dev.device_id,
                        hostname=dev.os_hostname,
                        collected_at=first_below.collected_at,
                        metric="disk_free_pct_below_threshold",
                        value=first_below.disk_free_pct,
                        unit="%",
                        label=(
                            f"First reading below {low_pct}% threshold: "
                            f"{first_below.disk_free_pct}%"
                        ),
                    )
                )
            evidence.append(
                Evidence(
                    handle=None,
                    snapshot_id=latest_pt.snapshot_id,
                    device_id=dev.device_id,
                    hostname=dev.os_hostname,
                    collected_at=latest_pt.collected_at,
                    metric="disk_free_pct",
                    value=latest_val,
                    unit="%",
                    label=f"Latest disk free: {latest_val}%",
                )
            )

            severity: Literal["low", "medium", "high"] = (
                "high" if (latest_val < 5.0 or (d_zero is not None and d_zero < 14.0)) else "medium"
            )

            reasons: list[str] = []
            if is_low:
                reasons.append(f"latest free space {latest_val}% < {low_pct}%")
            if is_depleting:
                reasons.append(f"depleting at {slope_val}%/day (R^2={r2_val})")

            finding = (
                f"Disk capacity risk on {dev.os_hostname or dev.device_id}: {'; '.join(reasons)}."
            )
            explanation = (
                f"Primary disk free space moved from {first_val}% to {latest_val}% over "
                f"{len(dev_pts)} snapshots."
            )
            if fit is not None and is_depleting:
                explanation += (
                    f" Linear fit slope={slope_val}%/day (R^2={r2_val}), "
                    f"estimated days_to_low={d_low}, days_to_zero={d_zero}."
                )
            if flattened_near_floor:
                caveat = f"values flattened near {latest_val:.1f}%; projection may overstate"
                explanation += f" Note: {caveat}."
                notes.append(f"{dev.device_id}: {caveat}")

            insights.append(
                Insight(
                    insight_id=f"disk:{dev.device_id}",
                    category=self.category,
                    severity=severity,
                    device_id=dev.device_id,
                    employee_id=dev.employee_id,
                    finding=finding,
                    explanation=explanation,
                    evidence=evidence,
                    stats={
                        "is_low": is_low,
                        "is_depleting": is_depleting,
                        "first_free_pct": first_val,
                        "latest_free_pct": latest_val,
                        "slope_pct_per_day": slope_val,
                        "r2": r2_val,
                        "days_to_low": d_low,
                        "days_to_zero": d_zero,
                        "last_7d_slope_pct_per_day": last_7d_slope,
                        "flattened_near_floor": flattened_near_floor,
                        "disk_total_gb": dev.disk_total_gb,
                    },
                    suggested_action=SuggestedAction(
                        tool="create_upgrade_order",
                        args_hint={"device_id": dev.device_id, "component": "storage"},
                        reason="Low or rapidly depleting primary disk free space",
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
                notes=notes,
            ),
            policy_applied=policy_applied,
        )
