"""Non-baseline installed software change analyzer."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from fleet_copilot.domain.models import (
    Coverage,
    Evidence,
    Insight,
    PolicyValue,
    SoftwareRecord,
    SuggestedAction,
    TenantContext,
)
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.trends.base import (
    AnalyzerOutput,
    resolve_policy_float,
    resolve_policy_str_list,
)


class SoftwareAnalyzer:
    """Identifies software absent from the tenant baseline (first in-window snapshot)."""

    category: str = "software"

    def analyze(
        self,
        ctx: TenantContext,
        repo: TelemetryRepository,
        policies: dict[str, PolicyValue],
        as_of: datetime,
        window_days: int = 30,
        device_ids: Sequence[str] | None = None,
    ) -> AnalyzerOutput:
        """Compute tenant baseline from first in-window snapshots and flag non-baseline apps."""
        min_share, pv_share = resolve_policy_float(policies, "software_baseline_min_share", 0.8)
        flagged_list, pv_flagged = resolve_policy_str_list(policies, "flagged_software_list", ())
        flagged_lower = {name.lower() for name in flagged_list}
        policy_applied = {
            "software_baseline_min_share": pv_share,
            "flagged_software_list": pv_flagged,
        }

        all_devices = sorted(
            repo.latest_states(ctx.company_id),
            key=lambda d: (d.device_id, d.collected_at),
        )
        since = as_of - timedelta(days=window_days)
        # Always load tenant-wide software so the baseline share reflects the full tenant.
        all_sw = repo.software(ctx.company_id, since=since)

        by_dev_snaps: dict[str, dict[datetime, list[SoftwareRecord]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in all_sw:
            by_dev_snaps[row.device_id][row.collected_at].append(row)

        n_tenant_devices = len(all_devices)
        first_snap_counts: Counter[str] = Counter()
        for dev in all_devices:
            snaps = by_dev_snaps.get(dev.device_id)
            if not snaps:
                continue
            first_ts = min(snaps.keys())
            first_names = {r.software_name for r in snaps[first_ts]}
            for name in first_names:
                first_snap_counts[name] += 1

        baseline = (
            {
                name
                for name, count in first_snap_counts.items()
                if (count / n_tenant_devices) >= min_share
            }
            if n_tenant_devices > 0
            else set()
        )

        target_devices = (
            [d for d in all_devices if d.device_id in set(device_ids)]
            if device_ids is not None
            else all_devices
        )

        insights: list[Insight] = []
        for dev in target_devices:
            snaps = by_dev_snaps.get(dev.device_id)
            if not snaps:
                continue
            ordered_ts = sorted(snaps.keys())
            latest_ts = ordered_ts[-1]
            latest_names = {r.software_name for r in snaps[latest_ts]}

            first_seen_by_app: dict[str, SoftwareRecord] = {}
            latest_rec_by_app: dict[str, SoftwareRecord] = {}
            for ts in ordered_ts:
                for rec in snaps[ts]:
                    if rec.software_name in baseline:
                        continue
                    if rec.software_name not in first_seen_by_app:
                        first_seen_by_app[rec.software_name] = rec
                    latest_rec_by_app[rec.software_name] = rec

            for app_name, first_rec in sorted(first_seen_by_app.items()):
                in_flagged = app_name.lower() in flagged_lower
                severity: Literal["low", "medium", "high"] = "high" if in_flagged else "medium"
                still_present = app_name in latest_names
                latest_rec = latest_rec_by_app[app_name]
                first_seen_date = first_rec.collected_at.date().isoformat()

                evidence = [
                    Evidence(
                        handle=None,
                        snapshot_id=first_rec.snapshot_id,
                        device_id=dev.device_id,
                        hostname=dev.os_hostname,
                        collected_at=first_rec.collected_at,
                        metric=f"software:{app_name}:first_seen",
                        value=first_rec.version or "installed",
                        unit=None,
                        label=f"Non-baseline software '{app_name}' first seen on {first_seen_date}",
                    )
                ]
                if latest_rec.snapshot_id != first_rec.snapshot_id:
                    evidence.append(
                        Evidence(
                            handle=None,
                            snapshot_id=latest_rec.snapshot_id,
                            device_id=dev.device_id,
                            hostname=dev.os_hostname,
                            collected_at=latest_rec.collected_at,
                            metric=f"software:{app_name}",
                            value=latest_rec.version or "installed",
                            unit=None,
                            label=(
                                f"Latest observation of '{app_name}' on "
                                f"{latest_rec.collected_at.date().isoformat()}"
                            ),
                        )
                    )

                flag_note = " (listed in flagged_software_list)" if in_flagged else ""
                finding = (
                    f"Non-baseline software '{app_name}' on {dev.os_hostname or dev.device_id}"
                    f"{flag_note}, first seen {first_seen_date}."
                )
                presence_text = (
                    "still installed on latest snapshot"
                    if still_present
                    else "not present on latest snapshot"
                )
                explanation = (
                    f"'{app_name}' is not part of the tenant baseline (apps present on >= "
                    f"{min_share * 100:.0f}% of {n_tenant_devices} devices at window start). "
                    f"First observed on {first_seen_date}; {presence_text}."
                )

                app_hash = hashlib.sha256(app_name.encode("utf-8")).hexdigest()[:10]
                insights.append(
                    Insight(
                        insight_id=f"software:{dev.device_id}:{app_hash}",
                        category=self.category,
                        severity=severity,
                        device_id=dev.device_id,
                        employee_id=dev.employee_id,
                        finding=finding,
                        explanation=explanation,
                        evidence=evidence,
                        stats={
                            "software_name": app_name,
                            "version": first_rec.version,
                            "publisher": first_rec.publisher,
                            "first_seen_date": first_seen_date,
                            "still_present_latest": still_present,
                            "in_flagged_list": in_flagged,
                            "baseline_apps": sorted(baseline),
                        },
                        suggested_action=SuggestedAction(
                            tool="open_remediation_ticket",
                            args_hint={
                                "device_id": dev.device_id,
                                "check_id": "non_baseline_software",
                            },
                            reason=f"Review and remediate non-baseline software '{app_name}'",
                        ),
                    )
                )

        return AnalyzerOutput(
            category=self.category,
            insights=insights,
            coverage=Coverage(
                telemetry_as_of=as_of,
                evaluated=len(target_devices),
                matched=len(insights),
                notes=[
                    f"Tenant baseline ({len(baseline)} apps at >= {min_share * 100:.0f}%): "
                    + ", ".join(sorted(baseline))
                ],
            ),
            policy_applied=policy_applied,
        )
