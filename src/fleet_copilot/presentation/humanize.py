"""Pure plain-English formatters shared by the Fleet Briefing service and Streamlit UI.

Zero Streamlit imports so backend services and unit tests can import this module cleanly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "hardware_catalog.yaml"

_FALLBACK_CATALOG_NAMES: dict[str, str] = {
    "21CB": "ThinkPad X1 Carbon Gen 11",
    "XPS-9530": "Dell XPS 15 9530",
    "Mac14,2": "MacBook Air (M2)",
    "Mac14,5": "MacBook Pro (M2 Pro)",
    "Mac15,7": "MacBook Pro (M3 Pro)",
    "Mac14,3": "Mac mini (M2)",
}

# All 19 global policy keys seeded in db/02_copilot.sql
SEEDED_POLICY_KEYS: tuple[str, ...] = (
    "low_disk_free_pct",
    "disk_burn_min_slope_pct_per_day",
    "disk_trend_min_r2",
    "high_ram_used_pct",
    "ram_sustained_ratio",
    "battery_eol_cycles",
    "battery_capacity_drop_pct",
    "software_baseline_min_share",
    "flagged_software_list",
    "replacement_min_signals",
    "stale_device_days",
    "max_telemetry_lag_days",
    "proposal_ttl_hours",
    "notify_message_max_chars",
    "cooldown_days_upgrade",
    "cooldown_days_replacement",
    "cooldown_days_ticket",
    "cooldown_hours_notify",
    "require_distinct_approver",
)

_TOOL_PLAIN_VERBS: dict[str, str] = {
    "create_upgrade_order": "Order a hardware upgrade",
    "open_remediation_ticket": "Open a remediation ticket",
    "flag_device_for_replacement": "Flag device for replacement",
    "notify_employee": "Notify the employee",
    "get_fleet_overview": "Inspect fleet overview",
    "find_devices": "Search devices",
    "query_compliance": "Query compliance checks",
    "get_device_details": "Inspect device details",
    "get_employee_devices": "List employee devices",
    "analyze_fleet_trends": "Analyze 30-day fleet trends",
    "list_action_proposals": "List action proposals",
    "create_chart": "Generate a chart",
    "run_readonly_sql": "Run read-only fleet SQL",
}

_CHECK_PLAIN_NAMES: dict[str, str] = {
    "actor_role": "IT admin permission",
    "actions_enabled": "Actions enabled",
    "tool_enabled": "Action enabled for company",
    "target_exists": "Target exists in this company",
    "evidence_present_and_valid": "Backed by verified telemetry",
    "evidence_matches_target": "Evidence matches target device",
    "evidence_recent": "Telemetry is recent",
    "no_active_duplicate": "No duplicate open action",
    "not_in_cooldown": "Outside action cool-down window",
    "valid_component": "Valid hardware component",
    "valid_spec": "Valid upgrade specification",
    "ram_sustained": "Sustained high memory pressure",
    "spec_capacity_parseable": "Valid target memory/storage size",
    "spec_gt_current_ram": "Upgrade exceeds current RAM",
    "spec_lte_ram_max": "Within model maximum RAM",
    "ram_upgradable": "Model RAM is upgradable",
    "storage_upgradable": "Model storage is upgradable",
    "battery_serviceable": "Laptop battery is serviceable",
    "has_battery": "Device is battery-equipped",
    "valid_check_id": "Valid compliance/remediation check",
    "valid_note": "Ticket note provided",
    "telemetry_check_failing": "Compliance check is currently failing",
    "disk_or_burn_condition": "Low or rapidly depleting disk",
    "non_baseline_software_present": "Non-baseline app still installed",
    "replacement_signal_threshold": "Meets multi-signal replacement threshold",
    "employee_owns_referenced_devices": "Employee owns all mentioned devices",
    "notify_message_within_limit": "Message within length limit",
    "disk_encryption": "Disk encryption",
    "os_up_to_date": "OS up to date",
    "screen_lock": "Screen lock",
    "antivirus_enabled": "Antivirus enabled",
    "firewall_enabled": "Firewall enabled",
    "low_disk_space": "Low disk space",
    "memory_pressure": "Memory pressure",
    "non_baseline_software": "Non-baseline software",
}

_AUDIT_EVENT_LABELS: dict[str, str] = {
    "turn_started": "Conversation turn started",
    "plan_created": "Investigation plan created",
    "tool_called": "Fleet tool executed",
    "access_denied": "Access denied by guardrail",
    "refusal": "Request refused by policy",
    "grounding_violation": "Grounding check flagged issue",
    "proposal_refused": "Proposed action refused by policy",
    "proposal_staged": "Action staged for human approval",
    "approval_requested": "Approval requested",
    "proposal_decided": "Approval decision recorded",
    "freshness_blocked": "Execution blocked by fresh data check",
    "action_executed": "Approved action executed",
    "action_failed": "Action execution failed",
    "proposal_expired": "Approval request expired",
    "turn_completed": "Conversation turn completed",
    "demo_reset": "Company demo state reset",
    "chart_created": "Grounded chart created",
    "chart_pinned": "Chart pin status updated",
    "chart_refreshed": "Saved chart refreshed on latest telemetry",
    "chart_deleted": "Saved chart deleted",
}

_SOURCE_LABELS: dict[str, str] = {
    "global": "Default policy",
    "tenant": "Company policy",
    "device": "Device override",
    "rbac": "Role permissions",
    "env": "System action switch",
    "tool_config": "Company tool settings",
    "dedupe": "Duplicate action guard",
    "cooldown": "Action cool-down policy",
    "ledger": "Verified telemetry evidence",
    "telemetry": "Latest device telemetry",
    "schema": "Request validation",
    "analyzer:ram": "Memory trend analyzer",
    "analyzer:battery": "Battery wear analyzer",
    "analyzer:disk": "Disk trend analyzer",
    "analyzer:compliance_drift": "Compliance drift analyzer",
    "analyzer:software": "Software baseline analyzer",
}

_POLICY_KV_RE = re.compile(
    r"\b([a-z][a-z0-9_]{3,})\s*=\s*([^\s,;()]+(?:\s*(?:%|days?|hours?|cycles?))?)"
    r"(?:\s*\((global|tenant|device)\))?",
    re.IGNORECASE,
)

_TOOL_TOKEN_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _TOOL_PLAIN_VERBS) + r")\b")


@lru_cache(maxsize=1)
def _load_catalog_names() -> dict[str, str]:
    names = dict(_FALLBACK_CATALOG_NAMES)
    if _CATALOG_PATH.exists():
        try:
            raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
            for row in raw.get("models") or []:
                mid = str(row.get("model_identifier") or "").strip()
                mname = str(row.get("model_name") or "").strip()
                if mid and mname:
                    names[mid] = mname
        except Exception:  # noqa: BLE001
            pass
    return names


def _fmt_num(value: Any, *, decimals: int | None = None) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        try:
            value = float(stripped)
        except ValueError:
            return value.strip()
    if isinstance(value, (int, float)):
        fval = float(value)
        if decimals is not None:
            return f"{fval:,.{decimals}f}"
        if abs(fval - round(fval)) < 1e-6:
            return f"{int(round(fval)):,}"
        return f"{fval:,.1f}"
    return str(value)


def _fmt_ratio_pct(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%"))
        except ValueError:
            return value
    if isinstance(value, (int, float)):
        fval = float(value)
        pct = fval * 100.0 if 0.0 < fval <= 1.0 else fval
        if abs(pct - round(pct)) < 1e-6:
            return f"{int(round(pct))}%"
        return f"{pct:.1f}%"
    return str(value)


def humanize_policy_source(source: str | None) -> str:
    """Translate a policy scope (`global`/`tenant`/`device`) into plain English."""
    if not source:
        return ""
    key = source.strip().lower()
    if key == "global":
        return "default policy"
    if key == "tenant":
        return "company policy"
    if key == "device":
        return "device policy"
    return source.replace("_", " ")


def humanize_policy(key: str, value: Any, source: str | None = None) -> str:
    """Render a policy metric key, threshold value, and source in customer-friendly copy."""
    norm = (key or "").strip().lower()
    src_text = humanize_policy_source(source)
    suffix = f" ({src_text})" if src_text else ""

    if norm == "low_disk_free_pct":
        body = f"Low disk: under {_fmt_num(value)}% free"
    elif norm == "disk_burn_min_slope_pct_per_day":
        body = f"Disk burn rate: {_fmt_num(value, decimals=1)}%/day or more"
    elif norm == "disk_trend_min_r2":
        body = f"Disk trend confidence (R²): at least {_fmt_num(value, decimals=2)}"
    elif norm == "high_ram_used_pct":
        body = f"High memory use: over {_fmt_num(value)}%"
    elif norm == "ram_sustained_ratio":
        body = f"Sustained memory window: at least {_fmt_ratio_pct(value)} of snapshots"
    elif norm == "battery_eol_cycles":
        body = f"Battery end-of-life: {_fmt_num(value)} cycles"
    elif norm == "battery_capacity_drop_pct":
        body = f"Battery capacity drop: {_fmt_num(value, decimals=1)}% or more"
    elif norm == "software_baseline_min_share":
        body = f"Fleet software baseline: under {_fmt_ratio_pct(value)} of devices"
    elif norm == "flagged_software_list":
        if isinstance(value, (list, tuple)):
            items = ", ".join(str(x) for x in value)
        else:
            items = str(value)
        body = f"Flagged software: {items}"
    elif norm == "replacement_min_signals":
        body = f"Replacement trigger: at least {_fmt_num(value)} active signals"
    elif norm == "stale_device_days":
        body = f"Offline check-in alert: over {_fmt_num(value)} days"
    elif norm == "max_telemetry_lag_days":
        if value is None or str(value).lower() == "null":
            body = "Maximum telemetry lag: no fixed limit"
        else:
            body = f"Maximum telemetry lag: {_fmt_num(value)} days"
    elif norm == "proposal_ttl_hours":
        body = f"Approval expiry: {_fmt_num(value)} hours"
    elif norm == "notify_message_max_chars":
        body = f"Employee notice limit: {_fmt_num(value)} characters"
    elif norm == "cooldown_days_upgrade":
        body = f"Upgrade cool-down: {_fmt_num(value)} days"
    elif norm == "cooldown_days_replacement":
        body = f"Replacement cool-down: {_fmt_num(value)} days"
    elif norm == "cooldown_days_ticket":
        body = f"Ticket cool-down: {_fmt_num(value)} days"
    elif norm == "cooldown_hours_notify":
        body = f"Notification cool-down: {_fmt_num(value)} hours"
    elif norm == "require_distinct_approver":
        enabled = str(value).strip().lower() in ("true", "1", "yes")
        body = f"Two-person approval: {'required' if enabled else 'not required'}"
    else:
        label = norm.replace("_", " ").capitalize() if norm else "Policy"
        body = f"{label}: {_fmt_num(value)}"

    return f"{body}{suffix}"


def humanize_tool_name(tool_name: str) -> str:
    """Convert an internal tool name into a plain-English verb phrase."""
    key = (tool_name or "").strip()
    return _TOOL_PLAIN_VERBS.get(key, key.replace("_", " ").capitalize())


def humanize_source(
    source: str | None,
    *,
    catalog_names: Mapping[str, str] | None = None,
) -> str:
    """Convert a policy/check source token (e.g. `catalog:21CB`) into readable copy."""
    if not source:
        return "Policy check"
    raw = source.strip()
    if raw.lower().startswith("catalog:"):
        model_id = raw.split(":", 1)[1].strip()
        lookup = dict(_load_catalog_names())
        if catalog_names:
            lookup.update(catalog_names)
        model_name = lookup.get(model_id, model_id)
        return f"Hardware catalog: {model_name}"
    if raw.lower() in _SOURCE_LABELS:
        return _SOURCE_LABELS[raw.lower()]
    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        return f"{prefix.replace('_', ' ').capitalize()}: {rest.replace('_', ' ')}"
    return raw.replace("_", " ").capitalize()


def humanize_check_name(check_name: str) -> str:
    """Convert a policy check identifier into a readable checklist label."""
    key = (check_name or "").strip()
    return _CHECK_PLAIN_NAMES.get(key, key.replace("_", " ").capitalize())


def humanize_audit_event(event_type: str) -> str:
    """Convert an audit event type identifier into a plain-English timeline label."""
    key = (event_type or "").strip()
    return _AUDIT_EVENT_LABELS.get(key, key.replace("_", " ").capitalize())


def humanize_actor_label(actor_id: str | None) -> str:
    """Format an actor identifier like `rev_01:acme_admin` cleanly for display."""
    if not actor_id:
        return "System"
    raw = str(actor_id).strip()
    if ":" in raw:
        _, persona = raw.split(":", 1)
        parts = persona.split("_")
        if len(parts) >= 2:
            company = parts[0].title()
            role = "IT admin" if parts[1] == "admin" else parts[1].title()
            return f"{company} · {role}"
        return persona.replace("_", " ").title()
    return raw


def humanize_caveat_text(text: str) -> str:
    """Rewrite known internal `policy_key=value (scope)` and tool tokens inside free-text notes."""
    cleaned = text.strip()
    # Strip redundant leading prefixes like "Policy thresholds applied:" before expanding nicely
    prefix_match = re.match(
        r"^(?:Policy thresholds? (?:applied|used):\s*)", cleaned, flags=re.IGNORECASE
    )
    prefix = ""
    rest = cleaned
    if prefix_match:
        prefix = "Thresholds applied: "
        rest = cleaned[prefix_match.end() :]

    def _replace_kv(match: re.Match[str]) -> str:
        k = match.group(1)
        if k.lower() not in SEEDED_POLICY_KEYS:
            return match.group(0)
        val_raw = match.group(2).strip()
        # Strip trailing unit words that our formatter adds natively
        val_clean = re.sub(r"\s*(?:days?|hours?|cycles?|%)$", "", val_raw, flags=re.IGNORECASE)
        src = match.group(3)
        return humanize_policy(k, val_clean, src)

    out = _POLICY_KV_RE.sub(_replace_kv, rest)
    out = _TOOL_TOKEN_RE.sub(lambda m: humanize_tool_name(m.group(1)), out)
    return (prefix + out).strip()


def split_and_humanize_caveats(
    caveats: list[str],
    *,
    has_refusal_card: bool = False,
) -> tuple[list[str], list[str]]:
    """Filter and humanize LLM caveats for Answer card display (Inspector keeps raw text).

    Returns `(policy_lines, other_notes)`.
    """
    policy_lines: list[str] = []
    other_notes: list[str] = []
    for raw in caveats:
        text = str(raw or "").strip()
        if not text:
            continue
        lower = text.lower()
        # Banner already displays telemetry_as_of
        if lower.startswith("telemetry as of") or lower.startswith("telemetry_as_of"):
            continue
        # Hide caveats that duplicate the refusal card + its verified alternatives
        if has_refusal_card and (
            lower.startswith("action refused")
            or lower.startswith("action ")
            and " refused" in lower
            or lower.startswith("alternative ")
        ):
            continue
        humanized = humanize_caveat_text(text)
        if "threshold" in lower or "policy" in lower or any(k in lower for k in SEEDED_POLICY_KEYS):
            policy_lines.append(humanized)
        else:
            other_notes.append(humanized)
    return policy_lines, other_notes


def humanize_chart_title(title: str, hostname_map: Mapping[str, str] | None = None) -> str:
    """Replace raw device IDs in a displayed chart title with hostnames at render time."""
    if not title or not hostname_map:
        return title
    out = title
    # Replace longer IDs first to avoid partial collisions
    for dev_id in sorted(hostname_map.keys(), key=len, reverse=True):
        host = hostname_map.get(dev_id)
        if not host or host == dev_id or len(dev_id) < 6:
            continue
        out = re.sub(rf"\b{re.escape(dev_id)}\b", host, out)
    return out


def format_device_record_summary(
    ev: Mapping[str, Any],
    current_device: Mapping[str, Any] | None = None,
) -> str:
    """Format a readable one-line Device record summary from evidence fields first.

    Any supplemental fields taken from current cached device state are explicitly labelled
    `current:` so reviewers never mistake current values for values at citation time.
    """
    raw_val_obj = ev.get("value")
    val_map: Mapping[str, Any] = raw_val_obj if isinstance(raw_val_obj, Mapping) else {}
    raw_val = "" if isinstance(raw_val_obj, Mapping) else str(raw_val_obj or "").strip()

    hostname = str(ev.get("hostname") or val_map.get("hostname") or "").strip()
    device_id = str(ev.get("device_id") or val_map.get("device_id") or "").strip()
    ev_model = str(ev.get("model") or val_map.get("model") or "").strip()
    ev_ff = str(ev.get("form_factor") or val_map.get("form_factor") or "").strip()
    ev_os = str(ev.get("os") or val_map.get("os") or "").strip()
    ev_batt_cond = str(
        ev.get("battery_condition") or val_map.get("battery_condition") or ""
    ).strip()
    ev_batt_cyc = (
        ev.get("battery_cycles")
        if ev.get("battery_cycles") is not None
        else val_map.get("battery_cycles")
    )
    ev_ram = (
        ev.get("ram_used_pct")
        if ev.get("ram_used_pct") is not None
        else val_map.get("ram_used_pct")
    )
    ev_disk = (
        ev.get("disk_free_pct")
        if ev.get("disk_free_pct") is not None
        else val_map.get("disk_free_pct")
    )

    ev_parts: list[str] = []
    if hostname:
        ev_parts.append(hostname)
    elif device_id:
        ev_parts.append(device_id)
    if ev_model:
        ev_parts.append(ev_model)
    if ev_ff:
        ev_parts.append(ev_ff)
    if ev_os:
        ev_parts.append(ev_os)
    if ev_batt_cond and ev_batt_cyc is not None:
        ev_parts.append(f"battery {ev_batt_cond} ({int(ev_batt_cyc):,} cycles)")
    elif ev_batt_cond:
        ev_parts.append(f"battery {ev_batt_cond}")
    elif ev_batt_cyc is not None:
        ev_parts.append(f"{int(ev_batt_cyc):,} cycles")
    if ev_ram is not None:
        ev_parts.append(f"{float(ev_ram):.0f}% RAM")
    if ev_disk is not None:
        ev_parts.append(f"{float(ev_disk):.0f}% disk free")
    if raw_val and raw_val.lower() not in (hostname.lower(), device_id.lower()):
        ev_parts.append(raw_val)

    ev_summary = " · ".join(ev_parts) if ev_parts else "Device record"
    if not current_device:
        return ev_summary

    cur_parts: list[str] = []
    cur_host = str(current_device.get("hostname") or "").strip()
    if cur_host and not hostname and cur_host.lower() != device_id.lower():
        cur_parts.append(cur_host)
    model = str(current_device.get("model") or "").strip()
    if model and not ev_model and model.lower() not in raw_val.lower():
        cur_parts.append(model)
    ff = str(current_device.get("form_factor") or "").strip()
    if ff and not ev_ff and ff.lower() not in raw_val.lower():
        cur_parts.append(ff)
    cur_os = str(current_device.get("os") or "").strip()
    if cur_os and not ev_os and cur_os.lower() not in raw_val.lower():
        cur_parts.append(cur_os)

    has_ev_battery = bool(ev_batt_cond) or ev_batt_cyc is not None
    if not has_ev_battery and current_device.get("battery_present", True):
        cond = str(current_device.get("battery_condition") or "").strip()
        cyc = current_device.get("battery_cycles")
        if cond and cyc is not None:
            cur_parts.append(f"battery {cond} ({int(cyc):,} cycles)")
        elif cond:
            cur_parts.append(f"battery {cond}")
        elif cyc is not None:
            cur_parts.append(f"{int(cyc):,} cycles")

    cur_ram = current_device.get("ram_used_pct")
    if ev_ram is None and cur_ram is not None:
        cur_parts.append(f"{float(cur_ram):.0f}% RAM")
    cur_disk = current_device.get("disk_free_pct")
    if ev_disk is None and cur_disk is not None:
        cur_parts.append(f"{float(cur_disk):.0f}% disk free")

    if cur_parts:
        return f"{ev_summary} · current: {', '.join(cur_parts)}"
    return ev_summary
