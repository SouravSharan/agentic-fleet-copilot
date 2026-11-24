"""Unified visual design system, status palette, and plain-English formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import streamlit as st

BadgeColor = Literal[
    "red", "orange", "yellow", "blue", "green", "violet", "gray", "grey", "primary"
]

# Single unified status palette across badges, cards, and Vega-Lite charts
STATUS_HEX: dict[str, str] = {
    "high": "#DC2626",
    "medium": "#D97706",
    "low": "#2563EB",
    "ok": "#059669",
    "stale": "#64748B",
    "blocked": "#64748B",
}

CHART_PALETTE: tuple[str, ...] = (
    "#2563EB",  # blue (low / primary)
    "#D97706",  # amber (medium)
    "#DC2626",  # red (high)
    "#059669",  # green (ok)
    "#7C3AED",  # violet
    "#0891B2",  # cyan
    "#64748B",  # grey (stale / blocked)
)

_SEVERITY_STYLE: dict[str, tuple[str, str, BadgeColor]] = {
    "high": ("High", ":material/error:", "red"),
    "medium": ("Medium", ":material/warning:", "orange"),
    "low": ("Low", ":material/info:", "blue"),
    "ok": ("Healthy", ":material/check_circle:", "green"),
    "stale": ("Stale", ":material/schedule:", "gray"),
    "blocked": ("Blocked", ":material/block:", "gray"),
}

_PROPOSAL_STATUS_STYLE: dict[str, tuple[str, str, BadgeColor]] = {
    "pending_approval": ("Awaiting approval", ":material/pending_actions:", "orange"),
    "approved": ("Approved", ":material/check_circle:", "green"),
    "executed": ("Executed", ":material/check_circle:", "green"),
    "rejected": ("Rejected", ":material/cancel:", "gray"),
    "expired": ("Expired", ":material/timer_off:", "gray"),
    "stale_blocked": ("Blocked by fresh data check", ":material/block:", "gray"),
    "failed": ("Execution failed", ":material/error:", "red"),
    "deferred": ("Deferred to queue", ":material/schedule:", "blue"),
}

_TOOL_PLAIN_NAMES: dict[str, str] = {
    "open_remediation_ticket": "Open remediation ticket",
    "create_upgrade_order": "Create hardware upgrade order",
    "flag_device_for_replacement": "Flag device for replacement",
    "notify_employee": "Notify employee",
}

_WARNING_PLAIN_TEXT: dict[str, str] = {
    "unknown_handle_neutralized": "A reference the copilot could not verify was removed.",
    "demo_budget_exhausted": (
        "This demo has used its live AI budget. Browse recorded examples in How it works."
    ),
    "live_unavailable": (
        "Live AI turns are unavailable right now. Browse recorded examples in How it works."
    ),
    "turn_cap_reached": (
        "This access code has reached its demo turn limit. "
        "Browse recorded examples in How it works."
    ),
    "rate_limited": "Rate limit reached for this hour. Please wait a moment before sending again.",
    "thread_busy": "Another request is still running in this chat. Please wait a moment.",
    "thread_turn_limit": "This conversation reached its 30-turn limit. Please start a new chat.",
}

_BASE_CSS = """
<style>
.block-container {
    padding-top: 3.25rem;
    padding-bottom: 2rem;
    max-width: 1480px;
}
[data-testid="stSidebar"] {
    min-width: 248px;
}
.fc-muted {
    color: #64748B;
    font-size: 0.85rem;
}
.fc-meta-row {
    color: #64748B;
    font-size: 0.82rem;
    margin-top: 0.25rem;
}
.fc-card-title {
    font-weight: 600;
    font-size: 1.02rem;
    margin: 0.15rem 0 0.25rem 0;
}
.fc-claim-text {
    line-height: 1.5;
    margin: 0.1rem 0;
}
.fc-sidebar-chip {
    border: 1px solid rgba(148, 163, 184, 0.35);
    border-radius: 0.5rem;
    padding: 0.5rem 0.65rem;
    margin-bottom: 0.5rem;
    font-size: 0.86rem;
}
</style>
"""


def inject_base_css() -> None:
    """Inject subtle layout spacing and typography utility classes."""
    st.markdown(_BASE_CSS, unsafe_allow_html=True)


def render_status_badge(kind: str, label: str | None = None) -> None:
    """Render an accessible icon + text badge using the unified status palette."""
    key = (kind or "low").strip().lower()
    default_label, icon, color = _SEVERITY_STYLE.get(
        key,
        (key.replace("_", " ").title(), ":material/info:", "blue"),
    )
    st.badge(label or default_label, icon=icon, color=color)


def render_proposal_status_badge(status: str) -> None:
    """Render an icon + text badge for an action proposal lifecycle state."""
    key = (status or "pending_approval").strip().lower()
    lbl, icon, color = _PROPOSAL_STATUS_STYLE.get(
        key,
        (key.replace("_", " ").title(), ":material/info:", "gray"),
    )
    st.badge(lbl, icon=icon, color=color)


def plain_tool_name(tool_name: str) -> str:
    """Convert an internal action tool identifier into plain English."""
    return _TOOL_PLAIN_NAMES.get(tool_name, tool_name.replace("_", " ").capitalize())


def plain_warning(raw: str) -> str:
    """Translate an internal warning token into clear customer-facing copy."""
    if raw in _WARNING_PLAIN_TEXT:
        return _WARNING_PLAIN_TEXT[raw]
    lower = raw.lower()
    for code, text in _WARNING_PLAIN_TEXT.items():
        if code in lower:
            return text
    return raw


def fmt_pct(value: float | int | None) -> str:
    """Format a percentage consistently with 1 decimal place."""
    if value is None:
        return "—"
    return f"{float(value):.1f}%"


def fmt_int(value: float | int | None) -> str:
    """Format an integer with thousands separators."""
    if value is None:
        return "—"
    return f"{int(round(float(value))):,}"


def fmt_number(value: Any, unit: str | None = None) -> str:
    """Format a metric value cleanly with its unit when present."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        if unit == "%":
            return f"{value:.1f}%"
        if abs(value - round(value)) < 1e-6:
            rendered = f"{int(round(value)):,}"
        else:
            rendered = f"{value:,.2f}"
        return f"{rendered} {unit}" if unit else rendered
    if isinstance(value, int):
        rendered = f"{value:,}"
        return f"{rendered} {unit}" if unit else rendered
    text = str(value)
    return f"{text} {unit}" if unit and unit not in text else text


def parse_iso_datetime(raw: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string or datetime safely."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def fmt_as_of_date(raw: str | datetime | None) -> str:
    """Format a timestamp as a concise date chip (e.g. '12 Jun 2026')."""
    dt = parse_iso_datetime(raw)
    if dt is None:
        return str(raw or "—")[:10]
    return dt.astimezone(UTC).strftime("%-d %b %Y")


def fmt_as_of_full(raw: str | datetime | None) -> str:
    """Format a timestamp with UTC time (e.g. '12 Jun 2026 · 11:00 UTC')."""
    dt = parse_iso_datetime(raw)
    if dt is None:
        return str(raw or "—")
    return dt.astimezone(UTC).strftime("%-d %b %Y · %H:%M UTC")


def fmt_relative_time(raw: str | datetime | None, *, now: datetime | None = None) -> str:
    """Format a timestamp relative to `now` for compact history lists."""
    dt = parse_iso_datetime(raw)
    if dt is None:
        return ""
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    delta_s = int((ref - dt).total_seconds())
    if delta_s < 60:
        return "just now"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    if delta_s < 7 * 86400:
        return f"{delta_s // 86400}d ago"
    return dt.astimezone(UTC).strftime("%-d %b")


def fmt_expires_in(raw: str | datetime | None, *, now: datetime | None = None) -> str:
    """Format an expiry timestamp as 'Expires in N h' or 'Expired'."""
    dt = parse_iso_datetime(raw)
    if dt is None:
        return ""
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    secs = int((dt - ref).total_seconds())
    if secs <= 0:
        return "Expired"
    hours = max(1, round(secs / 3600))
    return f"Expires in {hours} h"


def fmt_role(role: str) -> str:
    """Format an internal role identifier cleanly."""
    return "IT admin" if role == "it_admin" else "Viewer"
