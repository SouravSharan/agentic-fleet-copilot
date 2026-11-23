"""Battery wear and end-of-life trend analyzer (laptops only, latest-cycle rule)."""

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
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.trends.base import (
    AnalyzerOutput,
    resolve_policy_float,
    resolve_policy_int,
)


class BatteryAnalyzer:
    """Detects laptop battery degradation via condition, latest cycle count, or capacity drop."""

    category: str = "battery"

    def analyze(
        self,
        ctx: TenantContext,
        repo: TelemetryRepository,
        policies: dict[str, PolicyValue],
        as_of: datetime,
        window_days: int = 30,
        device_ids: Sequence[str] | None = None,
    ) -> AnalyzerOutput:
        """Analyze laptop batteries over `[as_of - window_days, as_of]`."""
        eol_cycles, pv_cycles = resolve_policy_int(policies, "battery_eol_cycles", 800)
        drop_threshold, pv_drop = resolve_policy_float(policies, "battery_capacity_drop_pct", 3.0)
        policy_applied = {
            "battery_eol_cycles": pv_cycles,
            "battery_capacity_drop_pct": pv_drop,
        }

        all_devices = sorted(
            repo.latest_states(ctx.company_id),
            key=lambda d: (d.device_id, d.collected_at),
        )
        if device_ids is not None:
            allowed = set(device_ids)
            all_devices = [d for d in all_devices if d.device_id in allowed]

        laptops = [d for d in all_devices if d.form_factor == "laptop"]
        skipped_non_laptops = len(all_devices) - len(laptops)
        laptop_ids = [d.device_id for d in laptops]

        since = as_of - timedelta(days=window_days)
        pts = repo.series(ctx.company_id, since=since, device_ids=laptop_ids)
        by_dev: dict[str, list[SnapshotMetricPoint]] = defaultdict(list)
        for p in pts:
            by_dev[p.device_id].append(p)

        insights: list[Insight] = []
        missing_battery_rows = 0

        for dev in laptops:
            dev_pts = sorted(by_dev.get(dev.device_id, []), key=lambda p: p.collected_at)
            bat_pts = [p for p in dev_pts if p.has_battery_block and p.battery_present]
            missing_battery_rows += len(dev_pts) - len(bat_pts)
            if not bat_pts:
                continue

            first_pt = bat_pts[0]
            latest_pt = bat_pts[-1]
            latest_cond = latest_pt.battery_condition

            cycle_pts = [p for p in bat_pts if p.battery_cycle_count is not None]
            cycles_first = cycle_pts[0].battery_cycle_count if cycle_pts else None
            cycles_last = cycle_pts[-1].battery_cycle_count if cycle_pts else None
            cycles_max = (
                max(p.battery_cycle_count for p in cycle_pts if p.battery_cycle_count is not None)
                if cycle_pts
                else None
            )

            cap_pts = [
                p
                for p in bat_pts
                if p.battery_full_charge_capacity is not None and p.battery_full_charge_capacity > 0
            ]
            cap_first = cap_pts[0].battery_full_charge_capacity if cap_pts else None
            cap_last = cap_pts[-1].battery_full_charge_capacity if cap_pts else None
            drop_pct: float | None = None
            if cap_first is not None and cap_last is not None and cap_first > 0:
                drop_pct = round(((cap_first - cap_last) / cap_first) * 100.0, 2)

            cond_flagged = latest_cond is not None and latest_cond != "Normal"
            cycles_flagged = cycles_last is not None and cycles_last >= eol_cycles
            drop_flagged = drop_pct is not None and drop_pct >= drop_threshold

            if not (cond_flagged or cycles_flagged or drop_flagged):
                continue

            transition_pt = next(
                (p for p in bat_pts if p.battery_condition and p.battery_condition != "Normal"),
                None,
            )
            days_since_transition: float | None = None
            transition_date: str | None = None
            if transition_pt is not None:
                transition_date = transition_pt.collected_at.date().isoformat()
                days_since_transition = round(
                    (as_of - transition_pt.collected_at).total_seconds() / 86400.0,
                    2,
                )

            evidence: list[Evidence] = [
                Evidence(
                    handle=None,
                    snapshot_id=first_pt.snapshot_id,
                    device_id=dev.device_id,
                    hostname=dev.os_hostname,
                    collected_at=first_pt.collected_at,
                    metric="battery_window_first",
                    value=first_pt.battery_cycle_count,
                    unit="cycles",
                    label=(
                        f"First window battery reading: condition={first_pt.battery_condition}, "
                        f"cycles={first_pt.battery_cycle_count}, "
                        f"capacity={first_pt.battery_full_charge_capacity} mAh"
                    ),
                )
            ]
            if transition_pt is not None:
                evidence.append(
                    Evidence(
                        handle=None,
                        snapshot_id=transition_pt.snapshot_id,
                        device_id=dev.device_id,
                        hostname=dev.os_hostname,
                        collected_at=transition_pt.collected_at,
                        metric="battery_condition_transition",
                        value=transition_pt.battery_condition,
                        unit=None,
                        label=(
                            f"Transition to {transition_pt.battery_condition} on "
                            f"{transition_date} (cycles={transition_pt.battery_cycle_count})"
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
                    metric="battery_latest",
                    value=latest_pt.battery_condition or latest_pt.battery_cycle_count,
                    unit=None if latest_pt.battery_condition else "cycles",
                    label=(
                        f"Latest battery reading: condition={latest_cond}, "
                        f"cycles={cycles_last}, capacity={cap_last} mAh"
                    ),
                )
            )

            reasons: list[str] = []
            if cond_flagged:
                reasons.append(f"condition is {latest_cond}")
            if cycles_flagged:
                reasons.append(f"latest cycle count {cycles_last} >= {eol_cycles}")
            if drop_flagged:
                reasons.append(f"capacity dropped {drop_pct}% >= {drop_threshold}%")

            severity: Literal["low", "medium", "high"] = "high" if cond_flagged else "medium"
            finding = (
                f"Battery end-of-life risk on {dev.os_hostname or dev.device_id}: "
                f"{'; '.join(reasons)}."
            )
            explanation = (
                f"Over {len(bat_pts)} battery snapshots in the {window_days}-day window, "
                f"cycles moved {cycles_first}->{cycles_last} (max {cycles_max}) and full-charge "
                f"capacity moved {cap_first}->{cap_last} mAh (drop {drop_pct}%)."
            )
            if transition_date is not None:
                explanation += f" Condition first changed from Normal on {transition_date}."

            insights.append(
                Insight(
                    insight_id=f"battery:{dev.device_id}",
                    category=self.category,
                    severity=severity,
                    device_id=dev.device_id,
                    employee_id=dev.employee_id,
                    finding=finding,
                    explanation=explanation,
                    evidence=evidence,
                    stats={
                        "cycles_first": cycles_first,
                        "cycles_last": cycles_last,
                        "cycles_max": cycles_max,
                        "capacity_first": cap_first,
                        "capacity_last": cap_last,
                        "drop_pct": drop_pct,
                        "latest_condition": latest_cond,
                        "transition_date": transition_date,
                        "days_since_transition": days_since_transition,
                    },
                    suggested_action=SuggestedAction(
                        tool="create_upgrade_order",
                        args_hint={
                            "device_id": dev.device_id,
                            "component": "battery",
                            "spec": "OEM replacement battery",
                        },
                        reason="Laptop battery exhibits end-of-life degradation",
                    ),
                )
            )

        notes: list[str] = []
        if skipped_non_laptops > 0:
            notes.append(f"Excluded {skipped_non_laptops} non-laptop device(s) from battery check")
        if missing_battery_rows > 0:
            notes.append(
                f"Skipped {missing_battery_rows} snapshot(s) with missing battery telemetry block"
            )

        return AnalyzerOutput(
            category=self.category,
            insights=insights,
            coverage=Coverage(
                telemetry_as_of=as_of,
                evaluated=len(laptops),
                matched=len(insights),
                notes=notes,
            ),
            policy_applied=policy_applied,
        )
