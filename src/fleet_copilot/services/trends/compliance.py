"""Compliance drift (pass -> fail) and chronic-failure analyzer with daily pass-rate series."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from fleet_copilot.domain.models import (
    ComplianceRecord,
    Coverage,
    Evidence,
    Insight,
    PolicyValue,
    SuggestedAction,
    TenantContext,
)
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.trends.base import AnalyzerOutput


class ComplianceDriftAnalyzer:
    """Detects compliance pass->fail drift (with recovery tracking) and chronic failures."""

    category: str = "compliance_drift"

    def analyze(
        self,
        ctx: TenantContext,
        repo: TelemetryRepository,
        policies: dict[str, PolicyValue],  # noqa: ARG002 - uniform analyzer protocol
        as_of: datetime,
        window_days: int = 30,
        device_ids: Sequence[str] | None = None,
    ) -> AnalyzerOutput:
        """Analyze compliance drift and chronic failures over `[as_of - window_days, as_of]`."""
        devices = sorted(
            repo.latest_states(ctx.company_id),
            key=lambda d: (d.device_id, d.collected_at),
        )
        if device_ids is not None:
            allowed = set(device_ids)
            devices = [d for d in devices if d.device_id in allowed]
        dev_map = {d.device_id: d for d in devices}

        since = as_of - timedelta(days=window_days)
        rows = repo.compliance(
            ctx.company_id,
            since=since,
            device_ids=[d.device_id for d in devices] if device_ids is not None else None,
        )

        # Group by (device_id, check_id) and collect tenant daily pass rates
        grouped: dict[tuple[str, str], list[ComplianceRecord]] = defaultdict(list)
        daily_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"pass": 0, "fail": 0})
        )

        for r in rows:
            if r.device_id in dev_map:
                grouped[(r.device_id, r.check_id)].append(r)
            day = r.collected_at.date().isoformat()
            if r.status in ("pass", "fail"):
                daily_counts[r.check_id][day][r.status] += 1

        drift_insights: list[Insight] = []
        chronic_insights: list[Insight] = []

        for (device_id, check_id), recs in sorted(grouped.items()):
            recs.sort(key=lambda r: r.collected_at)
            dev = dev_map[device_id]
            first_rec = recs[0]
            latest_rec = recs[-1]
            check_severity = latest_rec.severity

            first_pass: ComplianceRecord | None = None
            transition_fail: ComplianceRecord | None = None
            recovered_rec: ComplianceRecord | None = None

            for rec in recs:
                if rec.status == "pass":
                    if first_pass is None:
                        first_pass = rec
                    elif transition_fail is not None and recovered_rec is None:
                        recovered_rec = rec
                elif rec.status == "fail":
                    if first_pass is not None and transition_fail is None:
                        transition_fail = rec

            fail_count = sum(1 for r in recs if r.status == "fail")

            if transition_fail is not None and first_pass is not None:
                recovered = latest_rec.status == "pass"
                first_fail_date = transition_fail.collected_at.date().isoformat()
                recovered_date = (
                    recovered_rec.collected_at.date().isoformat()
                    if recovered_rec is not None
                    else None
                )
                sev: Literal["low", "medium", "high"]
                if recovered:
                    sev = "low"
                elif check_severity == "high":
                    sev = "high"
                else:
                    sev = "medium"

                evidence = [
                    Evidence(
                        handle=None,
                        snapshot_id=first_pass.snapshot_id,
                        device_id=device_id,
                        hostname=dev.os_hostname,
                        collected_at=first_pass.collected_at,
                        metric=f"compliance:{check_id}:first",
                        value=first_pass.status,
                        unit=None,
                        label=f"First pass for {check_id} on {first_pass.collected_at.date()}",
                    ),
                    Evidence(
                        handle=None,
                        snapshot_id=transition_fail.snapshot_id,
                        device_id=device_id,
                        hostname=dev.os_hostname,
                        collected_at=transition_fail.collected_at,
                        metric=f"compliance:{check_id}:transition",
                        value=transition_fail.status,
                        unit=None,
                        label=f"Transition pass->fail for {check_id} on {first_fail_date}",
                    ),
                    Evidence(
                        handle=None,
                        snapshot_id=latest_rec.snapshot_id,
                        device_id=device_id,
                        hostname=dev.os_hostname,
                        collected_at=latest_rec.collected_at,
                        metric=f"compliance:{check_id}",
                        value=latest_rec.status,
                        unit=None,
                        label=f"Latest {check_id} status: {latest_rec.status}",
                    ),
                ]

                state_desc = (
                    f"recovered on {recovered_date}"
                    if recovered
                    else "remains failing as of latest snapshot"
                )
                drift_insights.append(
                    Insight(
                        insight_id=f"compliance_drift:{device_id}:{check_id}",
                        category="compliance_drift",
                        severity=sev,
                        device_id=device_id,
                        employee_id=dev.employee_id,
                        finding=(
                            f"Compliance drift on {dev.os_hostname or device_id}: "
                            f"{check_id} ({check_severity}) flipped pass->fail "
                            f"on {first_fail_date}."
                        ),
                        explanation=(
                            f"{check_id} passed on {first_pass.collected_at.date().isoformat()}, "
                            f"flipped to fail on {first_fail_date} ({fail_count}/{len(recs)} "
                            f"failing snapshots), and {state_desc}."
                        ),
                        evidence=evidence,
                        stats={
                            "kind": "drift",
                            "check_id": check_id,
                            "check_severity": check_severity,
                            "first_fail_date": first_fail_date,
                            "recovered": recovered,
                            "recovered_date": recovered_date,
                            "fail_count": fail_count,
                            "total_snapshots": len(recs),
                        },
                        suggested_action=(
                            None
                            if recovered
                            else SuggestedAction(
                                tool="open_remediation_ticket",
                                args_hint={"device_id": device_id, "check_id": check_id},
                                reason=f"{check_id} drifted to failing and has not recovered",
                            )
                        ),
                    )
                )
            elif fail_count == len(recs) and len(recs) > 0:
                chronic_sev: Literal["low", "medium", "high"] = (
                    "high" if check_severity == "high" else "low"
                )
                evidence = [
                    Evidence(
                        handle=None,
                        snapshot_id=first_rec.snapshot_id,
                        device_id=device_id,
                        hostname=dev.os_hostname,
                        collected_at=first_rec.collected_at,
                        metric=f"compliance:{check_id}:first",
                        value=first_rec.status,
                        unit=None,
                        label=f"First window {check_id} status: {first_rec.status}",
                    ),
                    Evidence(
                        handle=None,
                        snapshot_id=latest_rec.snapshot_id,
                        device_id=device_id,
                        hostname=dev.os_hostname,
                        collected_at=latest_rec.collected_at,
                        metric=f"compliance:{check_id}",
                        value=latest_rec.status,
                        unit=None,
                        label=f"Latest {check_id} status: {latest_rec.status}",
                    ),
                ]
                chronic_insights.append(
                    Insight(
                        insight_id=f"compliance_chronic:{device_id}:{check_id}",
                        category="compliance_chronic",
                        severity=chronic_sev,
                        device_id=device_id,
                        employee_id=dev.employee_id,
                        finding=(
                            f"Chronic compliance failure on {dev.os_hostname or device_id}: "
                            f"{check_id} ({check_severity}) failed all {len(recs)} snapshots."
                        ),
                        explanation=(
                            f"{check_id} failed on every snapshot in the {window_days}-day window "
                            f"(chronic failure, not pass->fail drift)."
                        ),
                        evidence=evidence,
                        stats={
                            "kind": "chronic",
                            "check_id": check_id,
                            "check_severity": check_severity,
                            "fail_count": fail_count,
                            "total_snapshots": len(recs),
                        },
                        suggested_action=SuggestedAction(
                            tool="open_remediation_ticket",
                            args_hint={"device_id": device_id, "check_id": check_id},
                            reason=f"{check_id} is chronically failing",
                        ),
                    )
                )

        daily_series: dict[str, list[dict[str, Any]]] = {}
        for check_id, by_day in sorted(daily_counts.items()):
            series_rows: list[dict[str, Any]] = []
            for day, counts in sorted(by_day.items()):
                total = counts["pass"] + counts["fail"]
                rate = round(counts["pass"] / total, 4) if total > 0 else 0.0
                series_rows.append(
                    {
                        "date": day,
                        "pass": counts["pass"],
                        "fail": counts["fail"],
                        "pass_rate": rate,
                    }
                )
            daily_series[check_id] = series_rows

        all_insights = drift_insights + chronic_insights
        return AnalyzerOutput(
            category=self.category,
            insights=all_insights,
            coverage=Coverage(
                telemetry_as_of=as_of,
                evaluated=len(devices),
                matched=len(drift_insights),
                notes=[
                    f"Detected {len(drift_insights)} drift event(s) and "
                    f"{len(chronic_insights)} chronic failure(s)"
                ],
            ),
            artifacts=[
                {
                    "type": "daily_compliance_pass_rate",
                    "series": daily_series,
                }
            ],
            policy_applied={},
        )
