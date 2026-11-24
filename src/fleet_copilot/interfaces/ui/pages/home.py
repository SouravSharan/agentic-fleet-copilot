"""Home page: instant deterministic Fleet Briefing ($0, zero audit) + Guided Tour."""

from __future__ import annotations

from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.charts_ui import prepare_chart_spec_for_render
from fleet_copilot.interfaces.ui.safe_md import safe_md
from fleet_copilot.interfaces.ui.services import (
    cached_personas,
    cached_telemetry_as_of,
    get_cached_briefing,
    get_resolved_policies,
)
from fleet_copilot.interfaces.ui.state import (
    clear_session,
    mark_tour_step_done,
    queue_copilot_prompt,
    require_session,
    safe_page_run,
    switch_to_page,
)
from fleet_copilot.interfaces.ui.theme import (
    CHART_PALETTE,
    STATUS_HEX,
    fmt_as_of_full,
    fmt_int,
    fmt_pct,
    render_status_badge,
)
from fleet_copilot.services.briefing import BriefingItem

_CATEGORY_CHART_Y_TITLE: dict[str, str] = {
    "battery": "Battery cycles",
    "disk": "Disk free (%)",
    "ram": "RAM used (%)",
    "compliance_drift": "Status",
    "compliance_chronic": "Status",
    "software": "Present",
    "stale_devices": "Days offline",
}

_CATEGORY_METRIC: dict[str, str] = {
    "battery": "battery_cycles",
    "disk": "disk_free_pct",
    "ram": "ram_used_pct",
}


def _mini_chart_spec(
    item: BriefingItem,
    *,
    policies: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a compact inline Vega-Lite v5 trend chart with threshold line from `chart_series`."""
    if not item.chart_series:
        return None
    dev_to_host = dict(zip(item.device_ids, item.hostnames, strict=False))
    rows: list[dict[str, Any]] = []
    for raw in item.chart_series:
        if "date" not in raw or "value" not in raw or raw["value"] is None:
            continue
        did = str(raw.get("device_id") or "")
        rows.append(
            {
                "date": str(raw["date"]),
                "value": float(raw["value"]),
                "device": dev_to_host.get(did, did or "fleet"),
            }
        )
    if not rows:
        return None

    distinct_dates = {r["date"] for r in rows}
    y_title = _CATEGORY_CHART_Y_TITLE.get(item.category, "Value")
    sev_color = STATUS_HEX.get(item.severity, CHART_PALETTE[0])

    if len(distinct_dates) <= 1:
        return {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "width": "container",
            "height": 140,
            "data": {"values": rows},
            "mark": {"type": "bar", "cornerRadiusTopLeft": 3, "cornerRadiusTopRight": 3},
            "encoding": {
                "x": {"field": "device", "type": "nominal", "title": None},
                "y": {"field": "value", "type": "quantitative", "title": y_title},
                "color": {"value": sev_color},
                "tooltip": [
                    {"field": "device", "type": "nominal", "title": "Device"},
                    {"field": "value", "type": "quantitative", "title": y_title},
                ],
            },
        }

    base_spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 175,
        "data": {"values": rows},
        "mark": {"type": "line", "point": False, "strokeWidth": 2},
        "encoding": {
            "x": {
                "field": "date",
                "type": "temporal",
                "title": None,
                "axis": {"format": "%b %d", "tickCount": 4},
            },
            "y": {
                "field": "value",
                "type": "quantitative",
                "title": y_title,
                "scale": {"zero": False},
                "axis": {"title": y_title, "titlePadding": 8},
            },
            "color": {
                "field": "device",
                "type": "nominal",
                "title": "Device",
                "scale": {"range": list(CHART_PALETTE)},
                "legend": {"orient": "bottom", "columns": 3, "title": "Device"},
            },
            "tooltip": [
                {"field": "date", "type": "temporal", "title": "Date"},
                {"field": "device", "type": "nominal", "title": "Device"},
                {"field": "value", "type": "quantitative", "title": y_title},
            ],
        },
    }
    return prepare_chart_spec_for_render(
        base_spec,
        metric=_CATEGORY_METRIC.get(item.category),
        policies=policies,
        height=175,
        strip_inner_title=True,
    )


def _render_briefing_card(
    item: BriefingItem,
    *,
    is_admin: bool,
    key_prefix: str,
    policies: dict[str, Any] | None = None,
) -> None:
    """Render one ranked fleet briefing attention card."""
    with st.container(border=True):
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="small",
        ):
            render_status_badge(item.severity)
            if item.recency:
                st.caption(f"Latest signal: {item.recency}")

        st.markdown(f"##### {safe_md(item.title)}")
        st.markdown(safe_md(item.explanation))

        if item.hostnames:
            with st.container(horizontal=True, wrap=True, gap="small"):
                for host in item.hostnames[:8]:
                    st.badge(safe_md(host), icon=":material/laptop_mac:", color="gray")
                if len(item.hostnames) > 8:
                    st.badge(f"+{len(item.hostnames) - 8} more", color="gray")

        spec = _mini_chart_spec(item, policies=policies)
        if spec is not None:
            st.vega_lite_chart(spec=spec, width="stretch", key=f"{key_prefix}_chart_{item.id}")

        with st.container(horizontal=True, gap="small"):
            if st.button(
                "Ask about this",
                key=f"{key_prefix}_ask_{item.id}",
                type="primary",
                icon=":material/chat:",
            ):
                if item.category == "battery":
                    mark_tour_step_done(2)
                queue_copilot_prompt(item.suggested_prompt, auto_send=True, new_chat=True)
                switch_to_page("copilot")

            if is_admin and item.suggested_action_prompt:
                if st.button(
                    "Propose this action",
                    key=f"{key_prefix}_propose_{item.id}",
                    type="secondary",
                    icon=":material/bolt:",
                ):
                    queue_copilot_prompt(
                        item.suggested_action_prompt,
                        auto_send=True,
                        new_chat=True,
                    )
                    switch_to_page("copilot")


def _render_guided_tour(*, company_id: str, is_admin: bool) -> None:
    """Render the collapsible reviewer guided tour adapted to the 6-page layout."""
    completed: set[int] = set(st.session_state.get("tour_completed_steps") or ())
    if 1 not in completed:
        completed.add(1)
        st.session_state["tour_completed_steps"] = completed
    used = bool(st.session_state.get("tour_used", False))

    steps: list[dict[str, Any]] = [
        {
            "num": 1,
            "title": "Review the fleet briefing on Home",
            "detail": "Instant $0 KPI tiles and ranked attention cards surfaced without typing.",
            "action_label": "Mark done",
            "kind": "mark",
        },
        {
            "num": 2,
            "title": "Ask about battery end-of-life risk",
            "detail": "Live progress updates, grounded claims with [E#] chips, and trend data.",
            "action_label": "Try it",
            "kind": "prompt",
            "prompt": "Which Acme laptops have batteries needing service, and what should we do?",
        },
        {
            "num": 3,
            "title": "Inspect a citation chip [E#]",
            "detail": (
                "Click any citation chip in Copilot to view snapshot ID, metric, and raw JSON."
            ),
            "action_label": "Open Copilot",
            "kind": "page",
            "page": "copilot",
        },
        {
            "num": 4,
            "title": "Open the Inspector drawer",
            "detail": (
                "Inspect the Plan, tool timeline with durations, evidence ledger, "
                "grounding check, and cost."
            ),
            "action_label": "Open Copilot",
            "kind": "page",
            "page": "copilot",
        },
        {
            "num": 5,
            "title": "Test hardware refusal & verified alternatives (Globex · IT admin)",
            "detail": (
                "Ask to upgrade RAM on globex-thinkpad-6 (soldered RAM -> refusal + alternatives)."
            ),
            "action_label": "Try it" if company_id == "globex-002" else "Switch to Globex",
            "kind": "prompt" if company_id == "globex-002" else "sign_out",
            "prompt": "Order a 32 GB RAM upgrade for globex-thinkpad-6",
        },
        {
            "num": 6,
            "title": "Stage & approve remediation tickets",
            "detail": (
                "Stage screen-lock tickets, review the policy checklist, and approve selected."
            ),
            "action_label": "Try it" if is_admin else "Switch persona",
            "kind": "prompt" if is_admin else "sign_out",
            "prompt": "Open screen_lock remediation tickets for all failing laptops",
        },
        {
            "num": 7,
            "title": "Generate & pin a 30-day low-disk chart",
            "detail": "Create a grounded Vega-Lite chart and pin it to Fleet -> Saved charts.",
            "action_label": "Try it",
            "kind": "prompt",
            "prompt": "Chart disk free % for our low-disk devices over 30 days",
        },
        {
            "num": 8,
            "title": "Run ad-hoc read-only SQL analysis",
            "detail": "Ask for average battery cycles by model and expand Read-only query.",
            "action_label": "Try it",
            "kind": "prompt",
            "prompt": "What is the average battery cycle count by hardware model?",
        },
        {
            "num": 9,
            "title": "Explore Approvals & Activity, Fleet, and How it works",
            "detail": (
                "Check the shared approvals queue, audit chain, saved charts, and guardrails."
            ),
            "action_label": "Open How it works",
            "kind": "page",
            "page": "how_it_works",
        },
        {
            "num": 10,
            "title": "Switch to Acme · Viewer to verify RBAC",
            "detail": (
                "Confirm viewers see the same briefing with zero Approve or Propose controls."
            ),
            "action_label": "Switch persona",
            "kind": "sign_out",
        },
    ]

    done_count = len(completed)
    with st.expander(
        f"Guided tour ({done_count}/{len(steps)} completed)",
        expanded=not used,
        icon=":material/explore:",
    ):
        st.caption(
            "Follow these steps to see proactive insights, grounding citations, policy guardrails, "
            "human-in-the-loop approvals, charts, and ad-hoc SQL."
        )
        for step in steps:
            num = int(step["num"])
            col_chk, col_txt, col_btn = st.columns([0.5, 5.5, 1.8])
            with col_chk:
                checked = st.checkbox(
                    f"Step {num}",
                    value=num in completed,
                    key=f"tour_chk_{num}",
                    label_visibility="collapsed",
                )
                if checked and num not in completed:
                    mark_tour_step_done(num)
                elif not checked and num in completed:
                    updated = set(st.session_state.get("tour_completed_steps") or ())
                    updated.discard(num)
                    st.session_state["tour_completed_steps"] = updated
            with col_txt:
                st.markdown(f"**{num}. {step['title']}**  \n{step['detail']}")
            with col_btn:
                if st.button(
                    str(step["action_label"]),
                    key=f"tour_try_{num}",
                    width="stretch",
                ):
                    mark_tour_step_done(num)
                    if step["kind"] == "prompt":
                        queue_copilot_prompt(str(step["prompt"]), auto_send=True, new_chat=True)
                        switch_to_page("copilot")
                    elif step["kind"] == "page":
                        switch_to_page(str(step["page"]))
                    elif step["kind"] == "sign_out":
                        clear_session()
                        st.rerun()
                    else:
                        st.rerun()


def render_home_page() -> None:
    """Render the instant deterministic Home Fleet Briefing page."""
    session = require_session()
    is_admin = session.role == "it_admin"

    as_of_iso = cached_telemetry_as_of(session.company_id)
    briefing = get_cached_briefing(
        session.company_id,
        as_of_iso,
        session.role,
        session.actor_id,
    )
    policies = get_resolved_policies(session.company_id, as_of_iso)

    head_left, head_right = st.columns([4, 2])
    with head_left:
        st.markdown(f"## {briefing.company_name} Fleet Briefing")
    with head_right:
        st.caption(f":material/schedule: Telemetry as of {fmt_as_of_full(briefing.as_of)}")

    # KPI tiles
    k = briefing.kpis
    c1, c2, c3, c4 = st.columns(4, gap="small")
    with c1:
        with st.container(border=True):
            st.metric("Devices", fmt_int(k.devices))
            st.caption(f"{fmt_int(k.laptops)} laptops")
    with c2:
        with st.container(border=True):
            st.metric("Compliance pass rate", fmt_pct(k.compliance_pass_pct))
            st.caption("Across latest fleet checks")
    with c3:
        with st.container(border=True):
            st.metric("Open approvals", fmt_int(k.open_approvals))
            st.caption("Pending human review")
    with c4:
        with st.container(border=True):
            st.metric("Devices needing attention", fmt_int(k.devices_needing_attention))
            st.caption("High or medium findings")

    # Needs attention (HIGH and MEDIUM) right above the fold
    high_med = [item for item in briefing.items if item.severity in ("high", "medium")]
    low_items = [item for item in briefing.items if item.severity == "low"]

    st.markdown(f"### Needs attention ({len(high_med)})")
    if not high_med:
        st.success(
            "No high or medium-severity issues detected across the fleet as of "
            f"{fmt_as_of_full(briefing.as_of)}.",
            icon=":material/check_circle:",
        )
    else:
        for item in high_med:
            _render_briefing_card(
                item,
                is_admin=is_admin,
                key_prefix="attn",
                policies=policies,
            )

    if low_items:
        with st.expander(
            f"Also worth a look ({len(low_items)})",
            expanded=False,
            icon=":material/info:",
        ):
            for item in low_items:
                _render_briefing_card(
                    item,
                    is_admin=is_admin,
                    key_prefix="low",
                    policies=policies,
                )

    # Ask box + persona suggested prompts
    personas = cached_personas()
    persona_spec = personas.get(session.persona)
    suggested = list(persona_spec.suggested_prompts[:3]) if persona_spec else []

    with st.container(border=True):
        st.markdown("#### :material/chat: Ask Fleet Copilot")
        with st.form("home_ask_form", clear_on_submit=True, border=False):
            q_col, btn_col = st.columns([5.5, 1.2])
            with q_col:
                user_q = st.text_input(
                    "Ask a question about your fleet",
                    placeholder="e.g. Which devices are running low on disk space?",
                    label_visibility="collapsed",
                    key="home_ask_input",
                )
            with btn_col:
                submitted = st.form_submit_button(
                    "Ask",
                    type="primary",
                    icon=":material/send:",
                    width="stretch",
                )
            if submitted and user_q.strip():
                queue_copilot_prompt(user_q.strip(), auto_send=True, new_chat=True)
                switch_to_page("copilot")

        if suggested:
            st.caption("Suggested prompts")
            p_cols = st.columns(len(suggested), gap="small")
            for idx, prompt_text in enumerate(suggested):
                with p_cols[idx]:
                    if st.button(
                        prompt_text,
                        key=f"home_suggested_{idx}",
                        width="stretch",
                    ):
                        queue_copilot_prompt(prompt_text, auto_send=True, new_chat=True)
                        switch_to_page("copilot")

    # Guided tour checklist
    _render_guided_tour(company_id=session.company_id, is_admin=is_admin)


def main() -> None:
    safe_page_run(render_home_page)


if __name__ == "__main__":
    main()
