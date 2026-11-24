"""Answer card, citation popovers, inline charts, SQL results, insights, and refusal cards."""

from __future__ import annotations

import json
import re
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.services import get_ui_container
from fleet_copilot.interfaces.ui.state import (
    make_tenant_context,
    perf_mark_start,
    queue_copilot_prompt,
)
from fleet_copilot.interfaces.ui.theme import (
    fmt_as_of_full,
    fmt_int,
    fmt_number,
    plain_tool_name,
    plain_warning,
    render_status_badge,
)
from fleet_copilot.services.demo_auth import DemoSession

_MAX_SQL_DISPLAY_ROWS = 200
_INLINE_HANDLE_RE = re.compile(r"\s*\[([ECQ]\d+(?:\s*,\s*[ECQ]\d+)*)\]")


def _citation_map(resp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ev in resp.get("citations") or []:
        h = str(ev.get("handle") or "")
        if h:
            out[h] = ev
    return out


def _render_citation_popover(
    handle: str,
    ev: dict[str, Any],
    *,
    key: str,
) -> None:
    """Render a clickable `[E#]`/`[C#]`/`[Q#]` citation chip popover with raw JSON expander."""
    with st.popover(f"[{handle}]", type="tertiary", key=key):
        hostname = ev.get("hostname")
        device_id = str(ev.get("device_id") or "fleet")
        dev_label = f"{hostname} (`{device_id}`)" if hostname else f"`{device_id}`"
        st.markdown(f"**Evidence `[{handle}]`**")
        st.markdown(f"**Device:** {dev_label}")
        st.markdown(f"**Metric:** `{ev.get('metric', '—')}`")
        st.markdown(f"**Value:** {fmt_number(ev.get('value'), ev.get('unit'))}")
        st.markdown(f"**Snapshot ID:** `{ev.get('snapshot_id', '—')}`")
        st.markdown(f"**Collected at:** {fmt_as_of_full(ev.get('collected_at'))}")
        if ev.get("label"):
            st.caption(str(ev["label"]))
        with st.expander("Raw evidence JSON", expanded=False):
            st.code(json.dumps(ev, indent=2, default=str), language="json")


def _extract_policy_lines(caveats: list[str]) -> tuple[list[str], list[str]]:
    """Split policy/threshold caveats from other contextual notes for crisp footer display."""
    policy_lines: list[str] = []
    other_lines: list[str] = []
    for cav in caveats:
        lower = cav.lower().strip()
        if lower.startswith("telemetry as of") or lower.startswith("telemetry_as_of"):
            continue
        if "threshold" in lower or "policy" in lower or "evaluated" in lower or "devices" in lower:
            policy_lines.append(cav)
        else:
            other_lines.append(cav)
    return policy_lines, other_lines


def _render_chart_card(
    chart: dict[str, Any],
    *,
    session: DemoSession,
    turn_id: str,
    chart_idx: int,
) -> None:
    """Render one inline Vega-Lite chart with a personal Pin button inside its own fragment."""
    chart_id = str(chart.get("chart_id") or f"{turn_id}_c{chart_idx}")
    title = str(chart.get("title") or "Chart")
    spec = chart.get("vega_lite_spec")
    if not isinstance(spec, dict):
        return

    frag_key = f"chart_frag_{turn_id}_{chart_idx}"

    @st.fragment(key=frag_key)
    def _chart_fragment() -> None:
        with st.container(border=True):
            hdr_left, hdr_right = st.columns([4.2, 1.8])
            with hdr_left:
                handle = chart.get("handle")
                handle_tag = f"`[{handle}]` " if handle else ""
                st.markdown(f"**{handle_tag}{title}**")
            with hdr_right:
                pinned_ids: set[str] = set(st.session_state.get("pinned_chart_ids") or ())
                is_pinned = chart_id in pinned_ids or bool(chart.get("pinned"))
                can_pin = bool(chart.get("chart_id")) and str(chart["chart_id"]).startswith(
                    "chart_"
                )
                if is_pinned:
                    st.badge("Pinned", icon=":material/push_pin:", color="green")
                elif can_pin:
                    if st.button(
                        "Pin to Saved charts",
                        key=f"pin_{turn_id}_{chart_idx}_{chart_id}",
                        icon=":material/push_pin:",
                        width="stretch",
                    ):
                        container = get_ui_container()
                        ctx = make_tenant_context(session)
                        try:
                            res = container.chart_svc.pin_chart(ctx, chart_id, pinned=True)
                        except Exception:  # noqa: BLE001
                            res = None
                        if res is not None:
                            updated: set[str] = set(st.session_state.get("pinned_chart_ids") or ())
                            updated.add(chart_id)
                            st.session_state["pinned_chart_ids"] = updated
                            st.rerun(scope="fragment")
                        else:
                            st.caption("Saved chart unavailable for historical replay.")

            st.vega_lite_chart(
                spec=spec,
                width="stretch",
                key=f"vega_{turn_id}_{chart_idx}_{chart_id}",
            )
            as_of = chart.get("telemetry_as_of")
            row_count = chart.get("row_count")
            meta_parts: list[str] = []
            if isinstance(row_count, int):
                meta_parts.append(f"{fmt_int(row_count)} points")
            if as_of:
                meta_parts.append(f"as of {fmt_as_of_full(as_of)}")
            if meta_parts:
                st.caption(" · ".join(meta_parts))

    _chart_fragment()


def _alternative_prompt(alt: dict[str, Any], target_label: str) -> str:
    """Build a natural-language Copilot prompt to stage a verified refusal alternative."""
    tool = str(alt.get("tool") or "")
    raw_hint = alt.get("args_hint")
    hint: dict[str, Any] = raw_hint if isinstance(raw_hint, dict) else {}
    target = str(hint.get("device_id") or hint.get("employee_id") or target_label)
    if tool == "flag_device_for_replacement":
        reason = str(hint.get("reason") or "hardware limitation")
        return f"Flag {target} for replacement due to {reason}"
    if tool == "open_remediation_ticket":
        check_id = str(hint.get("check_id") or "remediation")
        return f"Open a {check_id} remediation ticket for {target}"
    if tool == "create_upgrade_order":
        component = str(hint.get("component") or "hardware")
        spec = str(hint.get("spec") or "")
        return f"Create a {spec} {component} upgrade order for {target}".strip()
    if tool == "notify_employee":
        return f"Notify employee {target} about device remediation"
    return f"Stage {plain_tool_name(tool).lower()} for {target}"


def _render_refusal_cards(
    resp: dict[str, Any],
    *,
    session: DemoSession,
    hostname_map: dict[str, str],
    turn_id: str,
) -> None:
    """Render refusal cards with policy/catalog source and verified alternatives."""
    refused = [v for v in (resp.get("verdicts") or []) if v.get("decision") == "refuse"]
    if not refused:
        return

    is_admin = session.role == "it_admin"
    for idx, verdict in enumerate(refused):
        tool_label = plain_tool_name(str(verdict.get("tool_name") or "action"))
        target_id = str(verdict.get("target_id") or "")
        host = hostname_map.get(target_id)
        target_display = (
            f"{host} (`{target_id}`)" if host and host != target_id else f"`{target_id}`"
        )

        failed_checks = [c for c in (verdict.get("checks") or []) if not c.get("passed", True)]
        sources = sorted({str(c.get("source")) for c in failed_checks if c.get("source")})
        reasons = [str(r) for r in (verdict.get("reasons") or []) if r]

        with st.container(border=True):
            render_status_badge("blocked", "Action refused by policy")
            st.markdown(f"**{tool_label}** · Target: {target_display}")
            for reason in reasons:
                st.markdown(f"- {reason}")
            if sources:
                st.caption(f"Policy source: {', '.join(sources)}")

            if failed_checks:
                with st.expander("Failed policy check details", expanded=False):
                    for chk in failed_checks:
                        st.markdown(
                            f":material/cancel: **{chk.get('name')}** — observed "
                            f"`{chk.get('observed')}`, required `{chk.get('threshold')}` "
                            f"(source: `{chk.get('source')}`)"
                        )

            alternatives = list(verdict.get("alternatives") or [])
            if alternatives:
                st.markdown("**Verified alternatives**")
                for a_idx, alt in enumerate(alternatives):
                    alt_tool = plain_tool_name(str(alt.get("tool") or ""))
                    hint = alt.get("args_hint") if isinstance(alt.get("args_hint"), dict) else {}
                    hint_reason = str(
                        hint.get("reason") or hint.get("note") or hint.get("check_id") or ""
                    )
                    a_left, a_right = st.columns([4.2, 1.8])
                    with a_left:
                        detail = f" — {hint_reason}" if hint_reason else ""
                        st.markdown(f":material/alt_route: **{alt_tool}**{detail}")
                    with a_right:
                        if is_admin:
                            prompt_text = _alternative_prompt(alt, host or target_id)
                            if st.button(
                                "Stage this instead",
                                key=f"stage_alt_{turn_id}_{idx}_{a_idx}",
                                icon=":material/send:",
                                width="stretch",
                            ):
                                queue_copilot_prompt(
                                    prompt_text,
                                    auto_send=True,
                                    new_chat=False,
                                )
                                st.rerun()
                if not is_admin:
                    st.caption("Ask an IT admin to stage an alternative action.")


def render_answer_card(
    resp: dict[str, Any],
    *,
    session: DemoSession,
    hostname_map: dict[str, str],
    turn_id: str,
    is_selected: bool = False,
    on_inspect_click: Any | None = None,
) -> None:
    """Render a structured Fleet Copilot Answer card from stored `TurnResponse` JSON."""
    status = str(resp.get("status") or "completed")
    answer = resp.get("answer") if isinstance(resp.get("answer"), dict) else None
    cite_map = _citation_map(resp)
    telemetry_as_of = resp.get("telemetry_as_of")

    # Calm info box for limited or failed turns
    if status in ("limited", "failed"):
        msg = (answer.get("summary") if answer else None) or str(
            resp.get("answer_markdown") or "This request could not be completed."
        )
        warnings = [plain_warning(str(w)) for w in (resp.get("warnings") or [])]
        with st.container(border=True):
            if status == "limited":
                st.info(msg, icon=":material/info:")
            else:
                st.warning(msg, icon=":material/warning:")
            for w in warnings:
                if w and w not in msg:
                    st.caption(w)
        return

    with st.container(border=True):
        # Header row: status / timestamp / Inspect trigger
        h_left, h_right = st.columns([4.6, 1.4])
        with h_left:
            if telemetry_as_of:
                st.caption(f":material/schedule: Data as of {fmt_as_of_full(telemetry_as_of)}")
        with h_right:
            btn_type = "primary" if is_selected else "tertiary"
            if st.button(
                "Inspector",
                key=f"inspect_btn_{turn_id}",
                icon=":material/search_insights:",
                type=btn_type,
                width="stretch",
            ):
                perf_mark_start("toggle_inspector")
                if on_inspect_click is not None:
                    on_inspect_click(turn_id)

        # Summary + grounded claims
        if answer is not None:
            summary = str(answer.get("summary") or "").strip()
            if summary:
                st.markdown(summary)

            claims = list(answer.get("claims") or [])
            for c_idx, claim in enumerate(claims):
                raw_text = str(claim.get("text") or "").strip()
                clean_text = _INLINE_HANDLE_RE.sub("", raw_text).strip()
                if clean_text.endswith(" ."):
                    clean_text = clean_text[:-2] + "."
                handles = [str(h) for h in (claim.get("citations") or []) if str(h) in cite_map]
                st.markdown(f"- {clean_text}")
                if handles:
                    with st.container(horizontal=True, wrap=True, gap="small"):
                        for h_idx, h in enumerate(handles):
                            _render_citation_popover(
                                h,
                                cite_map[h],
                                key=f"cite_{turn_id}_{c_idx}_{h_idx}_{h}",
                            )
        else:
            md = str(resp.get("answer_markdown") or "").strip()
            if md:
                st.markdown(md)

        # Charts (explicit C handles, plus auto-attached insight charts if no explicit chart exists)
        charts = list(resp.get("charts") or [])
        insights = list(resp.get("insights") or [])
        for ch_idx, chart in enumerate(charts):
            _render_chart_card(chart, session=session, turn_id=turn_id, chart_idx=ch_idx)

        if not charts:
            insight_charts = [
                ins["chart"]
                for ins in insights
                if isinstance(ins.get("chart"), dict)
                and isinstance(ins["chart"].get("vega_lite_spec"), dict)
            ]
            for ic_idx, ichart in enumerate(insight_charts[:2]):
                _render_chart_card(
                    ichart,
                    session=session,
                    turn_id=turn_id,
                    chart_idx=100 + ic_idx,
                )

        # Compact insight cards
        if insights:
            st.markdown("**Detected insights**")
            for ins in insights[:5]:
                with st.container(border=True):
                    sev = str(ins.get("severity") or "low")
                    render_status_badge(sev)
                    st.markdown(f"**{ins.get('finding', '')}**")
                    if ins.get("explanation"):
                        st.caption(str(ins["explanation"]))

        # Read-only SQL query results (Q handles)
        queries = list(resp.get("queries") or [])
        for q_idx, q in enumerate(queries):
            q_handle = str(q.get("handle") or f"Q{q_idx + 1}")
            row_count = int(q.get("row_count") or len(q.get("rows") or []))
            purpose = str(q.get("purpose") or "Read-only query")
            with st.expander(
                f"Read-only query [{q_handle}] · {purpose} ({fmt_int(row_count)} rows)",
                expanded=False,
                icon=":material/database:",
            ):
                sql_text = str(q.get("sql") or "")
                if sql_text:
                    st.code(sql_text, language="sql")
                rows = list(q.get("rows") or [])[:_MAX_SQL_DISPLAY_ROWS]
                if rows:
                    st.dataframe(rows, width="stretch", hide_index=True)
                else:
                    st.caption("Query returned 0 rows.")
                if q.get("truncated"):
                    st.caption(f"Showing up to {_MAX_SQL_DISPLAY_ROWS} rows.")

        # Warnings in plain English
        warnings = [str(w) for w in (resp.get("warnings") or []) if w]
        if warnings:
            for w in warnings:
                st.warning(plain_warning(w), icon=":material/warning:")

        # Footer: policy thresholds & caveats
        caveats = list((answer or {}).get("caveats") or [])
        policy_lines, other_caveats = _extract_policy_lines(caveats)
        footer_parts: list[str] = []
        if telemetry_as_of:
            footer_parts.append(f"As of {fmt_as_of_full(telemetry_as_of)}")
        footer_parts.extend(policy_lines)
        if footer_parts:
            st.caption(" · ".join(footer_parts))
        for note in other_caveats:
            if not note.lower().startswith(("action refused:", "alternative ")):
                st.caption(note)

    # Refusal cards (outside the main answer border for crisp visual separation)
    _render_refusal_cards(resp, session=session, hostname_map=hostname_map, turn_id=turn_id)
