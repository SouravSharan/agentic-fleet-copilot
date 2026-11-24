"""Answer card, compact citation popovers, inline charts, SQL results, insights, refusal cards."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.charts_ui import prepare_chart_spec_for_render
from fleet_copilot.interfaces.ui.humanize import (
    humanize_check_name,
    humanize_source,
    humanize_tool_name,
    split_and_humanize_caveats,
)
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
    """Render a compact clickable `[E#]`/`[C#]`/`[Q#]` citation chip popover."""
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
            st.text(str(ev["label"]))
        with st.expander("Raw evidence JSON", expanded=False):
            st.code(json.dumps(ev, indent=2, default=str), language="json")


def _render_chart_card(
    chart: dict[str, Any],
    *,
    session: DemoSession,
    turn_id: str,
    chart_idx: int,
    policies: Mapping[str, Any] | None = None,
    read_only: bool = False,
) -> None:
    """Render one inline Vega-Lite chart with full container width, threshold rule & Pin button."""
    chart_id = str(chart.get("chart_id") or f"{turn_id}_c{chart_idx}")
    title = str(chart.get("title") or "Chart")
    raw_spec = chart.get("vega_lite_spec")
    if not isinstance(raw_spec, dict):
        return

    render_spec = prepare_chart_spec_for_render(
        raw_spec,
        chart_meta=chart,
        policies=policies,
        height=260,
        strip_inner_title=True,
    )
    frag_key = f"chart_frag_{turn_id}_{chart_idx}"

    @st.fragment(key=frag_key)
    def _chart_fragment() -> None:
        with st.container(border=True):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
                gap="small",
            ):
                handle = chart.get("handle")
                handle_tag = f"`[{handle}]` " if handle else ""
                st.markdown(f"**{handle_tag}{title}**")
                if not read_only:
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
                            width="content",
                        ):
                            container = get_ui_container()
                            ctx = make_tenant_context(session)
                            try:
                                res = container.chart_svc.pin_chart(ctx, chart_id, pinned=True)
                            except Exception:  # noqa: BLE001
                                res = None
                            if res is not None:
                                updated: set[str] = set(
                                    st.session_state.get("pinned_chart_ids") or ()
                                )
                                updated.add(chart_id)
                                st.session_state["pinned_chart_ids"] = updated
                                st.rerun()
                            else:
                                st.caption("Saved chart unavailable for historical replay.")

            st.vega_lite_chart(
                spec=render_spec,
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
    return f"Stage {humanize_tool_name(tool).lower()} for {target}"


def _render_refusal_cards(
    resp: dict[str, Any],
    *,
    session: DemoSession,
    hostname_map: dict[str, str],
    turn_id: str,
    read_only: bool = False,
) -> None:
    """Render refusal cards with humanized policy/catalog sources and verified alternatives."""
    refused = [v for v in (resp.get("verdicts") or []) if v.get("decision") == "refuse"]
    if not refused:
        return

    is_admin = session.role == "it_admin" and not read_only
    for idx, verdict in enumerate(refused):
        tool_label = humanize_tool_name(str(verdict.get("tool_name") or "action"))
        target_id = str(verdict.get("target_id") or "")
        host = hostname_map.get(target_id)
        target_display = (
            f"{host} (`{target_id}`)" if host and host != target_id else f"`{target_id}`"
        )

        failed_checks = [c for c in (verdict.get("checks") or []) if not c.get("passed", True)]
        sources = sorted(
            {humanize_source(str(c.get("source"))) for c in failed_checks if c.get("source")}
        )
        reasons = [str(r) for r in (verdict.get("reasons") or []) if r]

        with st.container(border=True):
            render_status_badge("blocked", "Action refused by policy")
            st.markdown(f"**{tool_label}** · Target: {target_display}")
            for reason in reasons:
                st.markdown(f"- {reason}")
            if sources:
                st.caption(f"Source: {', '.join(sources)}")

            if failed_checks:
                with st.expander("Failed policy check details", expanded=False):
                    for chk in failed_checks:
                        chk_lbl = humanize_check_name(str(chk.get("name") or ""))
                        src_lbl = humanize_source(str(chk.get("source") or ""))
                        st.markdown(
                            f":material/cancel: **{chk_lbl}** — observed "
                            f"`{chk.get('observed')}`, required `{chk.get('threshold')}` "
                            f"({src_lbl})"
                        )

            alternatives = list(verdict.get("alternatives") or [])
            if alternatives:
                st.markdown("**Verified alternatives**")
                for a_idx, alt in enumerate(alternatives):
                    alt_tool = humanize_tool_name(str(alt.get("tool") or ""))
                    raw_hint = alt.get("args_hint")
                    hint: dict[str, Any] = raw_hint if isinstance(raw_hint, dict) else {}
                    hint_reason = str(
                        hint.get("reason") or hint.get("note") or hint.get("check_id") or ""
                    )
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                        gap="small",
                    ):
                        detail = f" — {hint_reason}" if hint_reason else ""
                        st.markdown(f":material/alt_route: **{alt_tool}**{detail}")
                        if is_admin:
                            prompt_text = _alternative_prompt(alt, host or target_id)
                            if st.button(
                                "Stage this instead",
                                key=f"stage_alt_{turn_id}_{idx}_{a_idx}",
                                icon=":material/send:",
                                width="content",
                            ):
                                queue_copilot_prompt(
                                    prompt_text,
                                    auto_send=True,
                                    new_chat=False,
                                )
                                st.rerun()
                if not is_admin and not read_only:
                    st.caption("Ask an IT admin to stage an alternative action.")


def render_answer_card(
    resp: dict[str, Any],
    *,
    session: DemoSession,
    hostname_map: dict[str, str],
    turn_id: str,
    policies: Mapping[str, Any] | None = None,
    is_selected: bool = False,
    on_inspect_click: Any | None = None,
    read_only: bool = False,
) -> None:
    """Render a structured Fleet Copilot Answer card from stored `TurnResponse` JSON."""
    status = str(resp.get("status") or "completed")
    answer = resp.get("answer") if isinstance(resp.get("answer"), dict) else None
    cite_map = _citation_map(resp)
    telemetry_as_of = resp.get("telemetry_as_of")
    has_refusal_card = any(v.get("decision") == "refuse" for v in (resp.get("verdicts") or []))

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
        # Header row: timestamp / optional Recorded example badge / Inspector button
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="small",
        ):
            with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                if read_only:
                    st.badge("Recorded example", icon=":material/menu_book:", color="gray")
                if telemetry_as_of:
                    st.caption(f":material/schedule: Data as of {fmt_as_of_full(telemetry_as_of)}")
                elif not read_only:
                    st.caption("Fleet Copilot")

            if on_inspect_click is not None:
                btn_type = "primary" if is_selected else "tertiary"
                if st.button(
                    "Inspector",
                    key=f"inspect_btn_{turn_id}",
                    icon=":material/search_insights:",
                    type=btn_type,
                    width="content",
                ):
                    perf_mark_start("toggle_inspector")
                    on_inspect_click(turn_id)

        # Summary + grounded claims with compact citation chips
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
                    with st.container(
                        horizontal=True,
                        wrap=True,
                        gap="small",
                        key=f"cites_row_{turn_id}_{c_idx}",
                    ):
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
            _render_chart_card(
                chart,
                session=session,
                turn_id=turn_id,
                chart_idx=ch_idx,
                policies=policies,
                read_only=read_only,
            )

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
                    policies=policies,
                    read_only=read_only,
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
                    st.code(sql_text, language="sql", wrap_lines=True)
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

        # Humanized footer policy thresholds + collapsible Notes for other caveats
        caveats = list((answer or {}).get("caveats") or [])
        policy_lines, other_notes = split_and_humanize_caveats(
            caveats,
            has_refusal_card=has_refusal_card,
        )
        footer_parts: list[str] = []
        if telemetry_as_of:
            footer_parts.append(f"As of {fmt_as_of_full(telemetry_as_of)}")
        footer_parts.extend(policy_lines)
        if footer_parts:
            st.caption(" · ".join(footer_parts))
        if other_notes:
            with st.expander(f"Notes ({len(other_notes)})", expanded=False):
                for note in other_notes:
                    st.markdown(f"- {note}")

    # Refusal cards (outside the main answer border for crisp visual separation)
    _render_refusal_cards(
        resp,
        session=session,
        hostname_map=hostname_map,
        turn_id=turn_id,
        read_only=read_only,
    )
