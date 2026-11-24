"""Unit tests for pure plain-English UI/briefing humanizer (`presentation/humanize.py`)."""

from __future__ import annotations

import re
from pathlib import Path

from fleet_copilot.presentation.humanize import (
    SEEDED_POLICY_KEYS,
    format_device_record_summary,
    humanize_actor_label,
    humanize_audit_event,
    humanize_caveat_text,
    humanize_chart_title,
    humanize_check_name,
    humanize_policy,
    humanize_policy_source,
    humanize_source,
    humanize_tool_name,
    split_and_humanize_caveats,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_seeded_policy_key_in_sql_has_human_label() -> None:
    sql_text = (REPO_ROOT / "db" / "02_copilot.sql").read_text(encoding="utf-8")
    sql_keys = sorted(
        set(
            re.findall(
                r"\('\*',\s*'global',\s*'',\s*'([a-z0-9_]+)'",
                sql_text,
            )
        )
    )
    assert sql_keys, "Expected seeded global policy keys in db/02_copilot.sql"
    for key in sql_keys:
        assert key in SEEDED_POLICY_KEYS, f"Seeded policy key {key!r} missing from humanize.py"
        rendered = humanize_policy(key, 15, "global")
        assert key not in rendered, f"Raw snake_case key {key!r} leaked into {rendered!r}"
        assert "(default policy)" in rendered


def test_policy_examples_and_sources() -> None:
    assert (
        humanize_policy("battery_eol_cycles", 800, "global")
        == "Battery end-of-life: 800 cycles (default policy)"
    )
    assert humanize_policy("low_disk_free_pct", 15.0) == "Low disk: under 15% free"
    assert (
        humanize_policy("high_ram_used_pct", 80.0, "tenant")
        == "High memory use: over 80% (company policy)"
    )
    assert humanize_policy("cooldown_days_upgrade", 30) == "Upgrade cool-down: 30 days"
    assert humanize_policy_source("global") == "default policy"
    assert humanize_policy_source("tenant") == "company policy"


def test_unknown_policy_key_falls_back_gracefully() -> None:
    assert (
        humanize_policy("custom_future_metric", 42, "tenant")
        == "Custom future metric: 42 (company policy)"
    )


def test_tool_names_sources_checks_and_audit_events() -> None:
    assert humanize_tool_name("create_upgrade_order") == "Order a hardware upgrade"
    assert humanize_tool_name("open_remediation_ticket") == "Open a remediation ticket"
    assert humanize_tool_name("flag_device_for_replacement") == "Flag device for replacement"
    assert humanize_tool_name("notify_employee") == "Notify the employee"
    assert humanize_tool_name("unknown_future_tool") == "Unknown future tool"

    assert humanize_source("catalog:21CB") == "Hardware catalog: ThinkPad X1 Carbon Gen 11"
    assert humanize_source("catalog:Mac14,5") == "Hardware catalog: MacBook Pro (M2 Pro)"
    assert humanize_source("catalog:UNKNOWN-99") == "Hardware catalog: UNKNOWN-99"
    assert humanize_source("analyzer:ram") == "Memory trend analyzer"
    assert humanize_check_name("ram_upgradable") == "Model RAM is upgradable"
    assert humanize_audit_event("action_executed") == "Approved action executed"
    assert humanize_actor_label("rev_01:acme_admin") == "Acme · IT admin"


def test_caveat_splitting_hides_as_of_and_refusal_duplicates_and_humanizes() -> None:
    caveats = [
        "telemetry_as_of: 2026-06-12T10:47:00+00:00.",
        (
            "Policy thresholds applied: high_ram_used_pct = 80.0% (global), "
            "cooldown_days_upgrade = 30 (global)."
        ),
        "Action refused: create_upgrade_order was refused because RAM is soldered.",
        "Alternative 1: Flag the device for replacement using flag_device_for_replacement.",
        "Excluded 1 non-laptop desktop device from battery counts.",
    ]
    policy_lines, notes = split_and_humanize_caveats(caveats, has_refusal_card=True)
    assert len(policy_lines) == 1
    assert "High memory use: over 80% (default policy)" in policy_lines[0]
    assert "Upgrade cool-down: 30 days (default policy)" in policy_lines[0]
    assert "high_ram_used_pct" not in policy_lines[0]
    assert notes == ["Excluded 1 non-laptop desktop device from battery counts."]

    # Without refusal card, tool names in notes are humanized cleanly
    assert "Order a hardware upgrade" in humanize_caveat_text(
        "Action create_upgrade_order requires approval."
    )


def test_compliance_check_chart_title_and_device_record_helpers() -> None:
    # compliance check labels have clean plain-English names
    assert humanize_check_name("disk_encryption") == "Disk encryption"
    assert humanize_check_name("os_up_to_date") == "OS up to date"
    assert humanize_check_name("screen_lock") == "Screen lock"
    assert humanize_check_name("endpoint_protection") == "Endpoint protection"

    # chart title device_id -> hostname render-time helper
    hmap = {"dev-001": "acme-macbook-1", "dev-002": "acme-dell-9"}
    assert humanize_chart_title("dev-001 · Battery", hmap) == "acme-macbook-1 · Battery"
    assert humanize_chart_title("Device dev-002 battery health", hmap) == (
        "Device acme-dell-9 battery health"
    )
    assert humanize_chart_title("Low disk fleet devices", hmap) == "Low disk fleet devices"

    # device_state evidence summary builds from evidence first, labels cached gaps 'current'
    ev_with_payload = {
        "metric": "device_state",
        "value": {"hostname": "acme-macbook-1", "battery_cycles": 850, "os": "macOS 15"},
    }
    cur = {
        "hostname": "acme-macbook-1",
        "os": "macOS 15",
        "ram_used_pct": 62.0,
        "disk_free_pct": 31.0,
        "battery_cycles": 910,
    }
    summary = format_device_record_summary(ev_with_payload, cur)
    assert "acme-macbook-1" in summary
    assert "850 cycles" in summary  # citation-time value wins over current 910!
    assert "910 cycles" not in summary
    assert "current: 62% RAM" in summary
    assert "31% disk free" in summary

    # Sparse evidence value -> current cached fields are explicitly labeled
    ev_sparse = {"metric": "device_state", "value": None, "device_id": "dev-001"}
    sparse_summary = format_device_record_summary(ev_sparse, cur)
    assert "current:" in sparse_summary
    assert "acme-macbook-1" in sparse_summary
    assert "910 cycles" in sparse_summary
