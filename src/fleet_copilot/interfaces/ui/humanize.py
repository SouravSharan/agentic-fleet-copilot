"""Backward-compatible re-export of `fleet_copilot.presentation.humanize` for UI modules."""

from __future__ import annotations

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

__all__ = [
    "SEEDED_POLICY_KEYS",
    "format_device_record_summary",
    "humanize_actor_label",
    "humanize_audit_event",
    "humanize_caveat_text",
    "humanize_chart_title",
    "humanize_check_name",
    "humanize_policy",
    "humanize_policy_source",
    "humanize_source",
    "humanize_tool_name",
    "split_and_humanize_caveats",
]
