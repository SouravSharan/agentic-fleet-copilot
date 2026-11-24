"""Deterministic read services returning structured, evidence-backed `ToolResult`s."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from fleet_copilot.domain.models import (
    AmbiguousDevice,
    ComplianceQuery,
    Coverage,
    DeviceFilters,
    DeviceState,
    Evidence,
    PolicyValue,
    TenantContext,
    ToolResult,
)
from fleet_copilot.domain.os_version import older_than, parse_os, parse_version_for_family
from fleet_copilot.domain.stats import percentile
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.staleness import (
    resolve_stale_device_days,
    stale_days_ago,
    stale_devices_note,
)


def _device_row_dict(
    d: DeviceState,
    *,
    as_of: Any = None,
    stale_days: int | None = None,
) -> dict[str, Any]:
    row = d.model_dump(mode="json")
    if as_of is not None and stale_days is not None:
        days = stale_days_ago(d, as_of, stale_days)
        if days is not None:
            row["last_seen_days_ago"] = days
    return row


def _evidence_for_device(
    d: DeviceState,
    *,
    metric: str,
    value: float | int | str | bool | None,
    unit: str | None,
    label: str,
) -> Evidence:
    return Evidence(
        handle=None,
        snapshot_id=d.snapshot_id,
        device_id=d.device_id,
        hostname=d.os_hostname,
        collected_at=d.collected_at,
        metric=metric,
        value=value,
        unit=unit,
        label=label,
    )


def get_fleet_overview(
    ctx: TenantContext,
    repo: TelemetryRepository,
    policy_svc: PolicyService,
) -> ToolResult:
    """Return tenant-scoped fleet counts by form factor, OS, model, and latest compliance."""
    as_of = repo.tenant_as_of(ctx.company_id)
    devices = repo.latest_states(ctx.company_id)
    latest_comp = repo.compliance(ctx.company_id, latest_only=True)
    policies = policy_svc.resolve(ctx)

    ff_counts: Counter[str] = Counter()
    os_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    for d in devices:
        ff_counts[d.form_factor] += 1
        os_label = f"{d.os_product_name or 'unknown'} {d.os_product_version or ''}".strip()
        os_counts[os_label] += 1
        model_counts[d.model_name or "unknown"] += 1

    comp_summary: dict[str, dict[str, Any]] = {}
    for row in latest_comp:
        bucket = comp_summary.setdefault(
            row.check_id,
            {"severity": row.severity, "pass": 0, "fail": 0},
        )
        if row.status in ("pass", "fail"):
            bucket[row.status] += 1

    overview_data = {
        "total_devices": len(devices),
        "by_form_factor": dict(sorted(ff_counts.items())),
        "by_os": dict(sorted(os_counts.items())),
        "by_model": dict(sorted(model_counts.items())),
        "compliance": dict(sorted(comp_summary.items())),
        "telemetry_as_of": as_of.isoformat(),
    }

    evidence = [
        _evidence_for_device(
            d,
            metric="device_state",
            value=d.form_factor,
            unit=None,
            label=f"{d.os_hostname or d.device_id} ({d.model_name or 'unknown'}, {d.form_factor})",
        )
        for d in sorted(devices, key=lambda x: (x.device_id, x.collected_at))
    ]

    stale_days = resolve_stale_device_days(policies)
    notes: list[str] = []
    if (stale_note := stale_devices_note(devices, as_of, stale_days)) is not None:
        notes.append(stale_note)

    summary = (
        f"Fleet overview as of {as_of.date().isoformat()}: {len(devices)} devices "
        f"({', '.join(f'{k}={v}' for k, v in sorted(ff_counts.items()))})."
    )
    return ToolResult(
        tool_name="get_fleet_overview",
        ok=True,
        summary=summary,
        data=[overview_data],
        evidence=evidence,
        coverage=Coverage(
            telemetry_as_of=as_of,
            evaluated=len(devices),
            matched=len(devices),
            notes=notes,
        ),
        policy_applied=policies,
    )


def find_devices(
    ctx: TenantContext,
    filters: DeviceFilters,
    repo: TelemetryRepository,
    policy_svc: PolicyService,
) -> ToolResult:
    """Filter latest device states with explicit coverage notes and policy transparency."""
    if filters.os_version_lt is not None and filters.os_family is None:
        return ToolResult(
            tool_name="find_devices",
            ok=False,
            summary="os_version_lt requires os_family.",
            error="os_version_lt requires os_family (valid: 'macos', 'windows')",
        )

    if filters.os_version_lt is not None and filters.os_family is not None:
        if parse_version_for_family(filters.os_family, filters.os_version_lt) is None:
            return ToolResult(
                tool_name="find_devices",
                ok=False,
                summary=f"Invalid OS version threshold {filters.os_version_lt!r}.",
                error=(
                    f"Invalid os_version_lt {filters.os_version_lt!r} "
                    f"for os_family={filters.os_family!r}"
                ),
            )

    as_of = repo.tenant_as_of(ctx.company_id)
    all_devices = sorted(
        repo.latest_states(ctx.company_id), key=lambda d: (d.device_id, d.collected_at)
    )
    notes: list[str] = []
    policy_applied: dict[str, PolicyValue] = {}

    low_disk_threshold: float | None = None
    if filters.low_disk_only:
        pv = policy_svc.get(ctx, "low_disk_free_pct")
        policy_applied["low_disk_free_pct"] = pv
        low_disk_threshold = float(pv.value)
        notes.append(f"low_disk_free_pct threshold={low_disk_threshold}% ({pv.source})")

    sw_by_device: dict[str, set[str]] = defaultdict(set)
    if filters.software_name is not None:
        for sw in repo.software(ctx.company_id, latest_only=True):
            sw_by_device[sw.device_id].add(sw.software_name.lower())

    excluded_platforms: Counter[str] = Counter()
    matched: list[DeviceState] = []

    for d in all_devices:
        if filters.form_factor is not None and d.form_factor != filters.form_factor:
            continue

        if filters.os_family is not None:
            parsed = parse_os(d.os_product_name, d.os_product_version)
            if parsed is None or parsed.family != filters.os_family:
                excluded_platforms[d.os_product_name or "unknown"] += 1
                continue
            if filters.os_version_lt is not None:
                is_older = older_than(parsed, filters.os_family, filters.os_version_lt)
                if is_older is not True:
                    continue

        if filters.model is not None:
            needle = filters.model.lower()
            hay_name = (d.model_name or "").lower()
            hay_id = (d.model_identifier or "").lower()
            if needle not in hay_name and needle not in hay_id:
                continue

        if filters.employee_id is not None and d.employee_id != filters.employee_id:
            continue

        if filters.hostname is not None:
            if (d.os_hostname or "").lower() != filters.hostname.lower():
                continue

        if low_disk_threshold is not None:
            if d.disk_free_pct is None or d.disk_free_pct >= low_disk_threshold:
                continue

        if filters.max_disk_free_pct is not None:
            if d.disk_free_pct is None or d.disk_free_pct > filters.max_disk_free_pct:
                continue

        if filters.min_ram_used_pct is not None:
            if d.ram_used_pct is None or d.ram_used_pct < filters.min_ram_used_pct:
                continue

        if filters.battery_condition is not None:
            if (d.battery_condition or "").lower() != filters.battery_condition.lower():
                continue

        if filters.software_name is not None:
            needle_sw = filters.software_name.lower()
            if not any(needle_sw in app for app in sw_by_device.get(d.device_id, set())):
                continue

        matched.append(d)

    if excluded_platforms:
        parts = [f"{cnt} {plat}" for plat, cnt in sorted(excluded_platforms.items())]
        notes.append(f"Excluded non-{filters.os_family} platforms: {', '.join(parts)}")

    stale_days = resolve_stale_device_days(policy_svc.resolve(ctx))
    if (stale_note := stale_devices_note(all_devices, as_of, stale_days)) is not None:
        notes.append(stale_note)

    truncated = len(matched) > filters.limit
    page = matched[: filters.limit]

    evidence: list[Evidence] = []
    for d in page:
        dev_ev: list[Evidence] = []
        if filters.low_disk_only or filters.max_disk_free_pct is not None:
            dev_ev.append(
                _evidence_for_device(
                    d,
                    metric="disk_free_pct",
                    value=d.disk_free_pct,
                    unit="%",
                    label=f"Disk free {d.disk_free_pct}%",
                )
            )
        if filters.os_family is not None or filters.os_version_lt is not None:
            ver_str = f"{d.os_product_name or ''} {d.os_product_version or ''}".strip()
            dev_ev.append(
                _evidence_for_device(
                    d,
                    metric="os_version",
                    value=ver_str,
                    unit=None,
                    label=f"OS {ver_str}",
                )
            )
        if filters.min_ram_used_pct is not None:
            dev_ev.append(
                _evidence_for_device(
                    d,
                    metric="ram_used_pct",
                    value=d.ram_used_pct,
                    unit="%",
                    label=f"RAM used {d.ram_used_pct}%",
                )
            )
        if filters.battery_condition is not None:
            dev_ev.append(
                _evidence_for_device(
                    d,
                    metric="battery_condition",
                    value=d.battery_condition,
                    unit=None,
                    label=f"Battery condition {d.battery_condition}",
                )
            )
        if filters.software_name is not None:
            dev_ev.append(
                _evidence_for_device(
                    d,
                    metric=f"software:{filters.software_name}",
                    value=True,
                    unit=None,
                    label=f"Installed software matches {filters.software_name}",
                )
            )
        if not dev_ev:
            dev_ev.append(
                _evidence_for_device(
                    d,
                    metric="device_state",
                    value=d.form_factor,
                    unit=None,
                    label=f"{d.os_hostname or d.device_id} ({d.form_factor})",
                )
            )
        evidence.extend(dev_ev)

    summary = (
        f"Matched {len(matched)} of {len(all_devices)} devices as of {as_of.date().isoformat()}."
    )
    return ToolResult(
        tool_name="find_devices",
        ok=True,
        summary=summary,
        data=[_device_row_dict(d, as_of=as_of, stale_days=stale_days) for d in page],
        evidence=evidence,
        coverage=Coverage(
            telemetry_as_of=as_of,
            evaluated=len(all_devices),
            matched=len(matched),
            notes=notes,
        ),
        policy_applied=policy_applied,
        truncated=truncated,
    )


def query_compliance(
    ctx: TenantContext,
    query: ComplianceQuery,
    repo: TelemetryRepository,
    policy_svc: PolicyService,
) -> ToolResult:
    """Query compliance check results with explicit zero-match pass evidence and check inventory."""
    as_of = repo.tenant_as_of(ctx.company_id)
    all_devices = sorted(
        repo.latest_states(ctx.company_id), key=lambda d: (d.device_id, d.collected_at)
    )
    evaluated_devices = [
        d for d in all_devices if query.form_factor is None or d.form_factor == query.form_factor
    ]
    eval_ids = {d.device_id for d in evaluated_devices}

    since = as_of - timedelta(days=30) if query.scope == "window" else None
    all_comp = repo.compliance(
        ctx.company_id,
        since=since,
        latest_only=(query.scope == "latest"),
    )

    existing_pairs = sorted({(c.check_id, c.severity) for c in all_comp})
    existing_check_ids = sorted({cid for cid, _ in existing_pairs})

    if query.check_id is not None and query.check_id not in existing_check_ids:
        return ToolResult(
            tool_name="query_compliance",
            ok=False,
            summary=f"Unknown check_id {query.check_id!r}.",
            error=f"Unknown check_id {query.check_id!r}. Valid: {', '.join(existing_check_ids)}",
        )

    scoped_comp = [c for c in all_comp if c.device_id in eval_ids]
    matched_rows = [
        c
        for c in scoped_comp
        if (query.severity is None or c.severity == query.severity)
        and (query.status is None or c.status == query.status)
        and (query.check_id is None or c.check_id == query.check_id)
    ]
    matched_rows.sort(key=lambda c: (c.device_id, c.collected_at, c.check_id))

    pairs_note = "Existing checks (check_id, severity): " + ", ".join(
        f"{cid} ({sev})" for cid, sev in existing_pairs
    )
    notes = [pairs_note]
    stale_days = resolve_stale_device_days(policy_svc.resolve(ctx))
    if (stale_note := stale_devices_note(evaluated_devices, as_of, stale_days)) is not None:
        notes.append(stale_note)
    stale_by_dev = {
        d.device_id: days
        for d in evaluated_devices
        if (days := stale_days_ago(d, as_of, stale_days)) is not None
    }

    if matched_rows:
        data: list[dict[str, Any]] = []
        for c in matched_rows:
            row = c.model_dump(mode="json")
            if c.device_id in stale_by_dev:
                row["last_seen_days_ago"] = stale_by_dev[c.device_id]
            data.append(row)
        evidence = [
            Evidence(
                handle=None,
                snapshot_id=c.snapshot_id,
                device_id=c.device_id,
                hostname=c.hostname,
                collected_at=c.collected_at,
                metric=f"compliance:{c.check_id}",
                value=c.status,
                unit=None,
                label=f"{c.check_id} ({c.severity}): {c.status}",
            )
            for c in matched_rows
        ]
        summary = (
            f"Found {len(matched_rows)} compliance record(s) across "
            f"{len(evaluated_devices)} evaluated device(s) as of {as_of.date().isoformat()}."
        )
        matched_count = len(matched_rows)
    else:
        # Zero matches: provide one pass-evidence row per evaluated device
        latest_comp = (
            scoped_comp
            if query.scope == "latest"
            else [
                c
                for c in repo.compliance(ctx.company_id, latest_only=True)
                if c.device_id in eval_ids
            ]
        )
        by_dev: dict[str, list[Any]] = defaultdict(list)
        for c in latest_comp:
            by_dev[c.device_id].append(c)

        evidence = []
        for d in evaluated_devices:
            dev_checks = [
                c
                for c in by_dev.get(d.device_id, [])
                if (query.severity is None or c.severity == query.severity)
                and (query.check_id is None or c.check_id == query.check_id)
            ]
            if dev_checks:
                chk = sorted(dev_checks, key=lambda x: x.check_id)[0]
                evidence.append(
                    Evidence(
                        handle=None,
                        snapshot_id=chk.snapshot_id,
                        device_id=chk.device_id,
                        hostname=chk.hostname or d.os_hostname,
                        collected_at=chk.collected_at,
                        metric=f"compliance:{chk.check_id}",
                        value=chk.status,
                        unit=None,
                        label=f"{chk.check_id} ({chk.severity}): {chk.status}",
                    )
                )
            else:
                evidence.append(
                    _evidence_for_device(
                        d,
                        metric="compliance:none_matching",
                        value="pass",
                        unit=None,
                        label="No matching failing compliance checks",
                    )
                )

        data = []
        matched_count = 0
        summary = (
            f"0 matching compliance records across {len(evaluated_devices)} evaluated "
            f"device(s) as of {as_of.date().isoformat()}."
        )

    return ToolResult(
        tool_name="query_compliance",
        ok=True,
        summary=summary,
        data=data,
        evidence=evidence,
        coverage=Coverage(
            telemetry_as_of=as_of,
            evaluated=len(evaluated_devices),
            matched=matched_count,
            notes=notes,
        ),
    )


def get_device_details(
    ctx: TenantContext,
    identifier: str,
    repo: TelemetryRepository,
    policy_svc: PolicyService,
) -> ToolResult:
    """Return detailed profile, 30-day stats, compliance history, and software for one device."""
    try:
        device = repo.resolve_device(ctx.company_id, identifier)
    except AmbiguousDevice as exc:
        return ToolResult(
            tool_name="get_device_details",
            ok=False,
            summary=str(exc),
            error="ambiguous_identifier",
            data=[{"candidates": list(exc.candidates)}],
        )

    if device is None:
        return ToolResult(
            tool_name="get_device_details",
            ok=False,
            summary="Device not found in this tenant.",
            error="not_found",
        )

    as_of = repo.tenant_as_of(ctx.company_id)
    since = as_of - timedelta(days=30)
    pts = repo.series(ctx.company_id, since=since, device_ids=[device.device_id])
    comp_rows = repo.compliance(ctx.company_id, since=since, device_ids=[device.device_id])
    latest_sw = repo.software(ctx.company_id, latest_only=True, device_ids=[device.device_id])
    policies = policy_svc.resolve(ctx, device_id=device.device_id)

    ram_vals = [p.ram_used_pct for p in pts if p.ram_used_pct is not None]
    disk_vals = [p.disk_free_pct for p in pts if p.disk_free_pct is not None]
    bat_pts = [p for p in pts if p.has_battery_block and p.battery_present]

    stats_30d: dict[str, Any] = {
        "snapshots_in_window": len(pts),
        "ram_avg_pct": round(sum(ram_vals) / len(ram_vals), 2) if ram_vals else None,
        "ram_p50_pct": round(percentile(ram_vals, 50.0) or 0.0, 2) if ram_vals else None,
        "ram_p95_pct": round(percentile(ram_vals, 95.0) or 0.0, 2) if ram_vals else None,
        "ram_max_pct": max(ram_vals) if ram_vals else None,
        "disk_first_free_pct": disk_vals[0] if disk_vals else None,
        "disk_latest_free_pct": disk_vals[-1] if disk_vals else None,
        "disk_min_free_pct": min(disk_vals) if disk_vals else None,
        "battery_snapshots": len(bat_pts),
    }

    latest_comp = [c for c in comp_rows if c.snapshot_id == device.snapshot_id]
    comp_history: dict[str, dict[str, int]] = {}
    for c in comp_rows:
        bucket = comp_history.setdefault(c.check_id, {"pass": 0, "fail": 0})
        if c.status in ("pass", "fail"):
            bucket[c.status] += 1

    stale_days = resolve_stale_device_days(policies)
    detail_notes: list[str] = []
    if (stale_note := stale_devices_note([device], as_of, stale_days)) is not None:
        detail_notes.append(stale_note)

    detail_record: dict[str, Any] = {
        "device_id": device.device_id,
        "profile": _device_row_dict(device, as_of=as_of, stale_days=stale_days),
        "stats_30d": stats_30d,
        "latest_compliance": [c.model_dump(mode="json") for c in latest_comp],
        "compliance_history_30d": dict(sorted(comp_history.items())),
        "installed_software": [
            {"name": s.software_name, "version": s.version, "publisher": s.publisher}
            for s in latest_sw
        ],
    }

    evidence = [
        _evidence_for_device(
            device,
            metric="device_state",
            value=f"{device.model_name or 'unknown'} ({device.form_factor})",
            unit=None,
            label=f"Latest snapshot for {device.os_hostname or device.device_id}",
        )
    ]
    if device.disk_free_pct is not None:
        evidence.append(
            _evidence_for_device(
                device,
                metric="disk_free_pct",
                value=device.disk_free_pct,
                unit="%",
                label=f"Disk free {device.disk_free_pct}%",
            )
        )
    if device.ram_used_pct is not None:
        evidence.append(
            _evidence_for_device(
                device,
                metric="ram_used_pct",
                value=device.ram_used_pct,
                unit="%",
                label=f"RAM used {device.ram_used_pct}%",
            )
        )
    if device.battery_condition is not None:
        evidence.append(
            _evidence_for_device(
                device,
                metric="battery_condition",
                value=device.battery_condition,
                unit=None,
                label=f"Battery {device.battery_condition} ({device.battery_cycle_count} cycles)",
            )
        )

    summary = (
        f"Device {device.device_id} ({device.os_hostname or 'unknown-host'}): "
        f"{device.model_name or 'unknown'} running {device.os_product_name or ''} "
        f"{device.os_product_version or ''}, disk free {device.disk_free_pct}%, "
        f"RAM used {device.ram_used_pct}%."
    )
    return ToolResult(
        tool_name="get_device_details",
        ok=True,
        summary=summary,
        data=[detail_record],
        evidence=evidence,
        coverage=Coverage(
            telemetry_as_of=as_of,
            evaluated=1,
            matched=1,
            notes=detail_notes,
        ),
        policy_applied=policies,
    )


def get_employee_devices(
    ctx: TenantContext,
    employee_id: str,
    repo: TelemetryRepository,
    policy_svc: PolicyService,
) -> ToolResult:
    """Return all devices assigned to `employee_id` within the tenant."""
    as_of = repo.tenant_as_of(ctx.company_id)
    devices = sorted(
        repo.employee_devices(ctx.company_id, employee_id),
        key=lambda d: (d.device_id, d.collected_at),
    )

    if not devices:
        return ToolResult(
            tool_name="get_employee_devices",
            ok=True,
            summary=(
                f"No devices found for employee {employee_id} as of {as_of.date().isoformat()}."
            ),
            data=[],
            evidence=[],
            coverage=Coverage(
                telemetry_as_of=as_of,
                evaluated=0,
                matched=0,
                notes=[],
            ),
        )

    latest_comp = repo.compliance(
        ctx.company_id,
        latest_only=True,
        device_ids=[d.device_id for d in devices],
    )
    failing_by_dev: dict[str, list[str]] = defaultdict(list)
    for c in latest_comp:
        if c.status == "fail":
            failing_by_dev[c.device_id].append(c.check_id)

    stale_days = resolve_stale_device_days(policy_svc.resolve(ctx))
    emp_notes: list[str] = []
    if (stale_note := stale_devices_note(devices, as_of, stale_days)) is not None:
        emp_notes.append(stale_note)

    data: list[dict[str, Any]] = []
    evidence: list[Evidence] = []
    for d in devices:
        row = _device_row_dict(d, as_of=as_of, stale_days=stale_days)
        row["failing_checks"] = sorted(failing_by_dev.get(d.device_id, []))
        data.append(row)
        evidence.append(
            _evidence_for_device(
                d,
                metric="device_state",
                value=d.form_factor,
                unit=None,
                label=f"{d.os_hostname or d.device_id} ({d.model_name or 'unknown'})",
            )
        )

    summary = (
        f"Employee {employee_id} owns {len(devices)} device(s) as of {as_of.date().isoformat()}."
    )
    return ToolResult(
        tool_name="get_employee_devices",
        ok=True,
        summary=summary,
        data=data,
        evidence=evidence,
        coverage=Coverage(
            telemetry_as_of=as_of,
            evaluated=len(devices),
            matched=len(devices),
            notes=emp_notes,
        ),
    )
