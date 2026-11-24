"""Deterministic Fleet Briefing service for the Home page and CLI (`$0`, zero LLM, zero audit)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.domain.models import DeviceState, PolicyValue, TenantContext
from fleet_copilot.interfaces.ui.humanize import humanize_policy
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.fleet import get_fleet_overview
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.staleness import resolve_stale_device_days, stale_days_ago
from fleet_copilot.services.trends import analyze_fleet_trends

_COMPANY_NAMES: dict[str, str] = {
    "acme-001": "Acme",
    "globex-002": "Globex",
    "initech-003": "Initech",
}

_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


class _BriefingModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BriefingKpis(_BriefingModel):
    """Top-level fleet health KPIs shown at the top of the Home page."""

    devices: int
    laptops: int
    compliance_pass_pct: float
    open_approvals: int
    devices_needing_attention: int


class BriefingItem(_BriefingModel):
    """Single prioritized insight card surfaced deterministically from fleet analyzers."""

    id: str
    severity: Literal["high", "medium", "low"]
    category: str
    title: str
    explanation: str
    device_ids: list[str] = Field(default_factory=list)
    hostnames: list[str] = Field(default_factory=list)
    evidence_snapshot_ids: list[str] = Field(default_factory=list)
    chart_series: list[dict[str, Any]] = Field(default_factory=list)
    suggested_prompt: str
    suggested_action_prompt: str | None = None
    recency: str = ""


class Briefing(_BriefingModel):
    """Complete deterministic Fleet Briefing for one tenant."""

    company_id: str
    company_name: str
    as_of: str
    kpis: BriefingKpis
    items: list[BriefingItem] = Field(default_factory=list)


def _company_display_name(company_id: str, override: str | None = None) -> str:
    if override and override.strip():
        return override.strip()
    return _COMPANY_NAMES.get(company_id, company_id.split("-")[0].title())


def _highest_severity(values: list[str]) -> Literal["high", "medium", "low"]:
    best: Literal["high", "medium", "low"] = "low"
    for v in values:
        if v == "high":
            return "high"
        if v == "medium" and best == "low":
            best = "medium"
    return best


def _policy_note(policies: dict[str, PolicyValue], key: str, default: Any) -> str:
    pv = policies.get(key)
    val = pv.value if pv is not None else default
    src = pv.source if pv is not None else "global"
    return humanize_policy(key, val, src)


def _a_or_an(word: str) -> str:
    return "an" if word and word[0].lower() in ("a", "e", "i", "o", "u") else "a"


def _format_host_list(hostnames: list[str]) -> str:
    if not hostnames:
        return ""
    if len(hostnames) == 1:
        return hostnames[0]
    if len(hostnames) == 2:
        return f"{hostnames[0]} and {hostnames[1]}"
    return ", ".join(hostnames[:-1]) + f", and {hostnames[-1]}"


def _ticket_action_prompt(check_id: str, hostnames: list[str]) -> str:
    if len(hostnames) == 1:
        return f"Open {_a_or_an(check_id)} {check_id} remediation ticket for {hostnames[0]}"
    if len(hostnames) <= 5:
        return f"Open {check_id} remediation tickets for {_format_host_list(hostnames)}"
    return f"Open {_a_or_an(check_id)} {check_id} remediation ticket for {hostnames[0]}"


def _recency_for_insights(insights: list[dict[str, Any]], as_of_date: str) -> str:
    dates: list[str] = []
    for ins in insights:
        stats = ins.get("stats") or {}
        for k in ("transition_date", "first_fail_date", "first_seen_date"):
            v = stats.get(k)
            if isinstance(v, str) and v:
                dates.append(v[:10])
        for ev in ins.get("evidence") or []:
            ts = ev.get("collected_at")
            if isinstance(ts, str) and ts:
                dates.append(ts[:10])
    return max(dates) if dates else as_of_date


def _build_category_item(
    *,
    category: str,
    insights: list[dict[str, Any]],
    dev_by_id: dict[str, DeviceState],
    policies: dict[str, PolicyValue],
    company_name: str,
    as_of_date: str,
    is_admin: bool,
) -> BriefingItem:
    device_ids = sorted({str(ins["device_id"]) for ins in insights})
    hostnames = [
        dev_by_id[did].os_hostname or did if did in dev_by_id else did for did in device_ids
    ]
    sev = _highest_severity([str(ins.get("severity") or "low") for ins in insights])
    recency = _recency_for_insights(insights, as_of_date)

    snap_ids: set[str] = set()
    chart_series: list[dict[str, Any]] = []
    for ins in insights:
        for ev in ins.get("evidence") or []:
            sid = ev.get("snapshot_id")
            if sid:
                snap_ids.add(str(sid))
        chart = ins.get("chart")
        if isinstance(chart, dict) and isinstance(chart.get("data_rows"), list):
            for row in chart["data_rows"]:
                if isinstance(row, dict):
                    chart_series.append(dict(row))

    n = len(device_ids)
    lead_host = hostnames[0] if hostnames else device_ids[0]
    plural = "device" if n == 1 else "devices"
    laptop_plural = "laptop" if n == 1 else "laptops"

    if category == "battery":
        p_cyc = _policy_note(policies, "battery_eol_cycles", 800)
        p_drop = _policy_note(policies, "battery_capacity_drop_pct", 3.0)
        title = f"{n} {laptop_plural} with battery end-of-life risk"
        explanation = (
            f"{n} {company_name} {laptop_plural} ({', '.join(hostnames)}) show Service Recommended "
            f"status or battery wear exceeding thresholds ({p_cyc}; {p_drop})."
        )
        prompt = (
            f"Which {company_name} laptops have batteries needing service, and what should we do?"
        )
        action_prompt = (
            f"Create a battery replacement order or flag {lead_host} for replacement"
            if is_admin
            else None
        )
    elif category == "disk":
        p_low = _policy_note(policies, "low_disk_free_pct", 15.0)
        p_slope = _policy_note(policies, "disk_burn_min_slope_pct_per_day", 0.1)
        title = f"{n} {plural} low or rapidly depleting on disk space"
        explanation = (
            f"{n} {company_name} {plural} ({', '.join(hostnames)}) are below free-space policy or "
            f"burning disk space steadily ({p_low}; {p_slope})."
        )
        prompt = (
            f"Which {company_name} devices are low or depleting on disk space, "
            "and what remediation do you recommend?"
        )
        action_prompt = _ticket_action_prompt("low_disk_space", hostnames) if is_admin else None
    elif category == "ram":
        p_ram = _policy_note(policies, "high_ram_used_pct", 80.0)
        p_ratio = _policy_note(policies, "ram_sustained_ratio", 0.7)
        title = f"{n} {plural} under sustained high RAM pressure"
        explanation = (
            f"{n} {company_name} {plural} ({', '.join(hostnames)}) exceeded memory utilization "
            f"thresholds across the 30-day window ({p_ram}; {p_ratio})."
        )
        prompt = (
            f"Which {company_name} devices have sustained high RAM usage, "
            "and which ones can be upgraded?"
        )
        action_prompt = f"Create a RAM upgrade order for {lead_host}" if is_admin else None
    elif category == "compliance_drift":
        checks = sorted(
            {str((ins.get("stats") or {}).get("check_id") or "compliance") for ins in insights}
        )
        lead_check = checks[0] if checks else "screen_lock"
        title = f"{n} {plural} drifted into compliance failure ({', '.join(checks)})"
        explanation = (
            f"{n} {company_name} {plural} ({', '.join(hostnames)}) transitioned from pass to fail "
            f"on {', '.join(checks)} within the 30-day window (Compliance drift · default policy)."
        )
        prompt = (
            f"Which {company_name} devices drifted into compliance failure in the last 30 days?"
        )
        action_prompt = _ticket_action_prompt(lead_check, hostnames) if is_admin else None
    elif category == "compliance_chronic":
        checks = sorted(
            {str((ins.get("stats") or {}).get("check_id") or "compliance") for ins in insights}
        )
        lead_check = checks[0] if checks else "os_up_to_date"
        title = f"{n} {plural} with chronic compliance failures ({', '.join(checks)})"
        explanation = (
            f"{n} {company_name} {plural} ({', '.join(hostnames)}) failed {', '.join(checks)} "
            "on every snapshot in the 30-day window (Chronic compliance · default policy)."
        )
        prompt = f"Which {company_name} devices have chronic compliance check failures?"
        action_prompt = _ticket_action_prompt(lead_check, hostnames) if is_admin else None
    elif category == "software":
        p_share = _policy_note(policies, "software_baseline_min_share", 0.8)
        apps = sorted(
            {
                str((ins.get("stats") or {}).get("software_name") or "")
                for ins in insights
                if (ins.get("stats") or {}).get("software_name")
            }
        )
        apps_text = ", ".join(apps) if apps else "non-baseline apps"
        title = f"{n} {plural} with non-baseline software ({apps_text})"
        explanation = (
            f"{n} {company_name} {plural} ({', '.join(hostnames)}) installed software outside the "
            f"tenant baseline ({apps_text}; {p_share})."
        )
        prompt = (
            f"Which {company_name} devices installed non-baseline software over the last 30 days?"
        )
        action_prompt = (
            _ticket_action_prompt("non_baseline_software", hostnames) if is_admin else None
        )
    else:
        title = f"{n} {plural} with {category.replace('_', ' ')} findings"
        explanation = f"{n} {company_name} {plural} ({', '.join(hostnames)}) flagged by {category}."
        prompt = f"Tell me more about {category.replace('_', ' ')} findings at {company_name}."
        action_prompt = None

    return BriefingItem(
        id=f"brief:{category}",
        severity=sev,
        category=category,
        title=title,
        explanation=explanation,
        device_ids=device_ids,
        hostnames=hostnames,
        evidence_snapshot_ids=sorted(snap_ids),
        chart_series=chart_series,
        suggested_prompt=prompt,
        suggested_action_prompt=action_prompt,
        recency=recency,
    )


def build_briefing(
    ctx: TenantContext,
    repo: TelemetryRepository,
    store: Any,
    policy_svc: PolicyService,
    *,
    company_label: str | None = None,
) -> Briefing:
    """Build a deterministic, read-only Fleet Briefing without LLM calls or audit writes."""
    company_name = _company_display_name(ctx.company_id, override=company_label)
    is_admin = ctx.role == "it_admin"

    overview_res = get_fleet_overview(ctx, repo, policy_svc)
    overview = overview_res.data[0] if overview_res.data else {}
    as_of: datetime = (
        overview_res.coverage.telemetry_as_of
        if overview_res.coverage is not None
        else repo.tenant_as_of(ctx.company_id)
    )
    as_of_date = as_of.date().isoformat()

    devices = sorted(
        repo.latest_states(ctx.company_id),
        key=lambda d: (d.device_id, d.collected_at),
    )
    dev_by_id = {d.device_id: d for d in devices}
    policies = policy_svc.resolve(ctx)

    catalog = store.hardware_catalog(ctx.company_id) if hasattr(store, "hardware_catalog") else None
    trends_res = analyze_fleet_trends(
        ctx,
        ["all"],
        repo,
        policy_svc,
        catalog_by_model=catalog,
    )

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ins in trends_res.data:
        by_category[str(ins["category"])].append(ins)

    items: list[BriefingItem] = []
    for cat in sorted(by_category.keys()):
        items.append(
            _build_category_item(
                category=cat,
                insights=by_category[cat],
                dev_by_id=dev_by_id,
                policies=policies,
                company_name=company_name,
                as_of_date=as_of_date,
                is_admin=is_admin,
            )
        )

    # F4 stale devices item (added only when >= 1 device stopped reporting beyond stale_device_days)
    stale_days = resolve_stale_device_days(policies)
    stale_devs = [d for d in devices if stale_days_ago(d, as_of, stale_days) is not None]
    if stale_devs:
        stale_ids = [d.device_id for d in stale_devs]
        stale_hosts = [d.os_hostname or d.device_id for d in stale_devs]
        stale_snaps = sorted({d.snapshot_id for d in stale_devs})
        p_stale = _policy_note(policies, "stale_device_days", stale_days)
        n_stale = len(stale_devs)
        plural = "device" if n_stale == 1 else "devices"
        lead_host = stale_hosts[0]
        recency_stale = max(d.collected_at.date().isoformat() for d in stale_devs)
        items.append(
            BriefingItem(
                id="brief:stale_devices",
                severity="medium",
                category="stale_devices",
                title=f"{n_stale} {plural} stopped reporting (> {stale_days} days)",
                explanation=(
                    f"{n_stale} {company_name} {plural} ({', '.join(stale_hosts)}) have not "
                    f"reported telemetry for more than {stale_days} days ({p_stale})."
                ),
                device_ids=stale_ids,
                hostnames=stale_hosts,
                evidence_snapshot_ids=stale_snaps,
                chart_series=[
                    {
                        "date": d.collected_at.date().isoformat(),
                        "value": float(stale_days_ago(d, as_of, stale_days) or 0),
                        "device_id": d.device_id,
                        "snapshot_id": d.snapshot_id,
                    }
                    for d in stale_devs
                ],
                suggested_prompt=(
                    f"Which {company_name} devices have stopped reporting telemetry recently?"
                ),
                suggested_action_prompt=(
                    (
                        f"Notify the owner of {stale_hosts[0]} about missing telemetry check-ins"
                        if len(stale_hosts) == 1
                        else (
                            f"Notify the owners of {_format_host_list(stale_hosts)} "
                            "about missing telemetry check-ins"
                            if len(stale_hosts) <= 5
                            else (
                                f"Notify the owner of {lead_host} about missing telemetry check-ins"
                            )
                        )
                    )
                    if is_admin
                    else None
                ),
                recency=recency_stale,
            )
        )

    # Deterministic rank: severity (high -> low), recency (newest -> oldest),
    # device count (largest -> smallest), then category/id ascending.
    def _sort_key(item: BriefingItem) -> tuple[int, str, int, str]:
        return (
            _SEVERITY_RANK.get(item.severity, 9),
            # Invert ISO date lexicographically by mapping digits (9 - d) for descending order
            "".join(str(9 - int(ch)) if ch.isdigit() else ch for ch in (item.recency or "0000")),
            -len(item.device_ids),
            item.id,
        )

    items.sort(key=_sort_key)

    comp_map = overview.get("compliance") or {}
    total_pass = sum(int(v.get("pass", 0)) for v in comp_map.values())
    total_fail = sum(int(v.get("fail", 0)) for v in comp_map.values())
    total_checks = total_pass + total_fail
    pass_pct = round((total_pass / total_checks) * 100.0, 1) if total_checks > 0 else 100.0

    pending = (
        store.list_proposals(ctx.company_id, status="pending_approval")
        if hasattr(store, "list_proposals")
        else []
    )
    # Count only devices in HIGH or MEDIUM items so LOW chronic items don't inflate the headline
    attention_ids = {
        did for item in items if item.severity in ("high", "medium") for did in item.device_ids
    }

    kpis = BriefingKpis(
        devices=int(overview.get("total_devices", len(devices))),
        laptops=int((overview.get("by_form_factor") or {}).get("laptop", 0)),
        compliance_pass_pct=pass_pct,
        open_approvals=len(pending),
        devices_needing_attention=len(attention_ids),
    )

    return Briefing(
        company_id=ctx.company_id,
        company_name=company_name,
        as_of=as_of.isoformat(),
        kpis=kpis,
        items=items,
    )
