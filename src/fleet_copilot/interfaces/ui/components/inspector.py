"""Technical Inspector drawer exposing raw Plan, Tools, Evidence, Verify, and Usage."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.humanize import format_device_record_summary
from fleet_copilot.interfaces.ui.safe_md import safe_code, safe_md
from fleet_copilot.interfaces.ui.state import perf_finish_if_active, perf_mark_start
from fleet_copilot.interfaces.ui.theme import (
    fmt_as_of_full,
    fmt_int,
    fmt_number,
    render_status_badge,
)

_SENSITIVE_ARG_KEYS: frozenset[str] = frozenset(
    {"api_key", "token", "secret", "password", "authorization", "dsn"}
)


def _redact_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {
            k: ("***" if str(k).lower() in _SENSITIVE_ARG_KEYS else _redact_args(v))
            for k, v in args.items()
        }
    if isinstance(args, list):
        return [_redact_args(v) for v in args]
    return args


def _grounding_summary(resp: dict[str, Any]) -> dict[str, Any]:
    raw_answer = resp.get("answer")
    answer: dict[str, Any] = raw_answer if isinstance(raw_answer, dict) else {}
    claims = list(answer.get("claims") or [])
    citations = list(resp.get("citations") or [])
    warnings = [str(w) for w in (resp.get("warnings") or [])]

    neutralized = any("unknown_handle_neutralized" in w for w in warnings)
    stripped = any("stripped" in w.lower() for w in warnings)
    warned = any("numeric grounding" in w.lower() for w in warnings)

    if stripped:
        result = "stripped"
    elif warned:
        result = "warned"
    elif claims or citations:
        result = "passed"
    else:
        result = "n/a"

    return {
        "claims_count": len(claims),
        "citations_count": len(citations),
        "result": result,
        "retries": 1 if (stripped or warned) else 0,
        "neutralized": neutralized,
    }


def render_inspector_panel() -> None:
    """Render the open right-hand technical Inspector panel (mounted only when open)."""

    @st.fragment(key="copilot_inspector")
    def _inspector_fragment() -> None:
        turns_by_id: dict[str, dict[str, Any]] = dict(
            st.session_state.get("_active_turns_by_id") or {}
        )
        turn_order: list[str] = list(st.session_state.get("_active_turn_order") or [])
        sel_id = str(st.session_state.get("selected_turn_id") or "")
        selected_turn: dict[str, Any] | None = turns_by_id.get(sel_id)
        if selected_turn is None and turn_order:
            selected_turn = turns_by_id.get(turn_order[-1])

        with st.container(border=True, key="inspector_card_wrap"):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
                gap="small",
            ):
                st.markdown("##### :material/search_insights: Inspector")
                if st.button(
                    "Close",
                    key="inspector_close_btn",
                    icon=":material/close:",
                    width="content",
                ):
                    perf_mark_start("toggle_inspector")
                    st.session_state["inspector_open"] = False
                    st.rerun(scope="app")

            if selected_turn is None:
                st.info(
                    "Send a message or click **Inspector** on any answer card to view its trace.",
                    icon=":material/info:",
                )
                perf_finish_if_active()
                return

            raw_resp = selected_turn.get("response_json")
            resp: dict[str, Any] = raw_resp if isinstance(raw_resp, dict) else selected_turn
            turn_id = str(resp.get("turn_id") or selected_turn.get("turn_id") or "")
            st.caption(
                f"Turn `{safe_code(turn_id) or '—'}` · status "
                f"`{safe_code(resp.get('status', 'completed')) or 'completed'}`"
            )

            # Short labels avoid horizontal tab overflow on desktop widths
            tab_plan, tab_tools, tab_ev, tab_verify, tab_usage = st.tabs(
                ["Plan", "Tools", "Evidence", "Verify", "Usage"]
            )

            with tab_plan:
                raw_plan = resp.get("plan")
                plan: dict[str, Any] | None = raw_plan if isinstance(raw_plan, dict) else None
                if plan is None:
                    st.caption("No planner output recorded for this turn.")
                else:
                    st.markdown(f"**Intent:** `{safe_code(plan.get('intent')) or '—'}`")
                    flags: list[str] = []
                    if plan.get("refers_to_other_org"):
                        flags.append("refers_to_other_org=true")
                    if plan.get("role_violation"):
                        flags.append("role_violation=true")
                    if plan.get("needs_clarification"):
                        flags.append(f"needs_clarification={safe_md(plan['needs_clarification'])}")
                    if flags:
                        st.caption(" · ".join(flags))
                    steps = list(plan.get("steps") or [])
                    if steps:
                        for i, step in enumerate(steps, start=1):
                            hint = safe_code(step.get("tool_hint"))
                            hint_tag = f" (`{hint}`)" if hint else ""
                            st.markdown(f"{i}. {safe_md(step.get('goal', ''))}{hint_tag}")
                    else:
                        st.caption("No tool steps planned.")

                # Inspector preserves raw unmodified caveats from the model
                raw_ans = resp.get("answer")
                raw_caveats = (
                    list(raw_ans.get("caveats") or []) if isinstance(raw_ans, dict) else []
                )
                if raw_caveats:
                    with st.expander("Raw answer caveats", expanded=False):
                        for c in raw_caveats:
                            st.text(str(c))

            with tab_tools:
                trace = list(resp.get("tool_trace") or [])
                if not trace:
                    st.caption("No tool calls executed in this turn.")
                else:
                    for idx, step in enumerate(trace, start=1):
                        tool_name = str(step.get("tool_name") or "unknown")
                        ok_flag = bool(step.get("ok", True))
                        dur = int(step.get("duration_ms") or 0)
                        matched = step.get("matched")
                        evaluated = step.get("evaluated")
                        with st.container(border=True):
                            st.markdown(f"**{idx}. `{safe_code(tool_name) or 'unknown'}`**")
                            render_status_badge(
                                "ok" if ok_flag else "blocked",
                                "ok" if ok_flag else "failed",
                            )
                            ratio = (
                                f"matched {matched}/{evaluated}"
                                if matched is not None and evaluated is not None
                                else ""
                            )
                            st.caption(f"{dur} ms" + (f" · {ratio}" if ratio else ""))
                            if step.get("summary"):
                                st.text(str(step["summary"]))
                            safe_args = _redact_args(step.get("args") or {})
                            st.code(json.dumps(safe_args, indent=2, default=str), language="json")

            with tab_ev:
                cites = list(resp.get("citations") or [])
                dev_summaries: dict[str, dict[str, Any]] = dict(
                    st.session_state.get("_active_device_summaries") or {}
                )
                if not cites:
                    st.caption("No evidence handles cited in this turn.")
                else:
                    for c in cites[:60]:
                        handle = str(c.get("handle") or "—")
                        dev_id = str(c.get("device_id") or "fleet")
                        host = str(c.get("hostname") or dev_id)
                        metric = str(c.get("metric") or "—")
                        snap_id = str(c.get("snapshot_id") or "—")
                        ts_short = fmt_as_of_full(c.get("collected_at"))
                        handle_safe = safe_code(handle) or "—"
                        metric_safe = safe_code(metric) or "—"
                        snap_safe = safe_code(snap_id) or "—"
                        with st.container(border=True):
                            if metric == "device_state":
                                summary = format_device_record_summary(c, dev_summaries.get(dev_id))
                                st.markdown(
                                    f"`[{handle_safe}]` **Device record** · {safe_md(summary)}"
                                )
                            else:
                                val_txt = fmt_number(c.get("value"), c.get("unit"))
                                st.markdown(
                                    f"`[{handle_safe}]` **{safe_md(host)}** · "
                                    f"`{metric_safe}` = {safe_md(val_txt)}"
                                )
                            st.caption(f"Snapshot `{snap_safe}` · {ts_short}")
                    if len(cites) > 60:
                        st.caption(f"Showing first 60 of {fmt_int(len(cites))} evidence items.")

            with tab_verify:
                g = _grounding_summary(resp)
                res_kind = (
                    "ok"
                    if g["result"] == "passed"
                    else ("medium" if g["result"] == "warned" else "low")
                )
                render_status_badge(res_kind, f"verify_grounding: {g['result']}")
                g1, g2 = st.columns(2)
                with g1:
                    st.metric("Claims", fmt_int(g["claims_count"]))
                    st.metric("Verifier retries", fmt_int(g["retries"]))
                with g2:
                    st.metric("Citations", fmt_int(g["citations_count"]))
                    st.metric(
                        "Neutralized handles",
                        "Yes" if g["neutralized"] else "No",
                    )

            with tab_usage:
                raw_usage = resp.get("usage")
                usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
                u1, u2 = st.columns(2)
                with u1:
                    st.metric("Input tokens", fmt_int(usage.get("input_tokens", 0)))
                    st.metric("Est. cost", f"${float(usage.get('cost_usd_est') or 0.0):.4f}")
                with u2:
                    st.metric("Output tokens", fmt_int(usage.get("output_tokens", 0)))
                    st.metric("Latency", f"{fmt_int(usage.get('latency_ms', 0))} ms")
                st.caption(f"Trace ID: `{safe_code(resp.get('trace_id')) or '—'}`")
                if resp.get("telemetry_as_of"):
                    st.caption(f"Telemetry as of: {fmt_as_of_full(resp.get('telemetry_as_of'))}")

        perf_finish_if_active()

    _inspector_fragment()
