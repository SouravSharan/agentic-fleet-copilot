"""Technical Inspector drawer (`@st.fragment`) exposing Plan, Steps, Evidence, Grounding, Usage."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.state import perf_finish_if_active, perf_mark_start
from fleet_copilot.interfaces.ui.theme import fmt_as_of_full, fmt_int, render_status_badge

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
    """Render the toggleable right-hand Inspector inside its own `@st.fragment`."""

    @st.fragment(key="copilot_inspector")
    def _inspector_fragment() -> None:
        is_open = bool(st.session_state.get("inspector_open", False))
        turns_by_id: dict[str, dict[str, Any]] = dict(
            st.session_state.get("_active_turns_by_id") or {}
        )
        turn_order: list[str] = list(st.session_state.get("_active_turn_order") or [])
        sel_id = str(st.session_state.get("selected_turn_id") or "")
        selected_turn: dict[str, Any] | None = turns_by_id.get(sel_id)
        if selected_turn is None and turn_order:
            selected_turn = turns_by_id.get(turn_order[-1])

        with st.container(border=True):
            hdr_left, hdr_right = st.columns([3.5, 1.5])
            with hdr_left:
                st.markdown("#### :material/search_insights: Inspector")
            with hdr_right:
                toggle_label = "Close" if is_open else "Open"
                toggle_icon = ":material/close:" if is_open else ":material/open_in_full:"

                def _on_toggle_inspector() -> None:
                    perf_mark_start("toggle_inspector")
                    st.session_state["inspector_open"] = not bool(
                        st.session_state.get("inspector_open", False)
                    )

                st.button(
                    toggle_label,
                    key="inspector_toggle_btn",
                    icon=toggle_icon,
                    width="stretch",
                    on_click=_on_toggle_inspector,
                )

            if not is_open:
                st.caption(
                    "Open the Inspector to inspect the agent Plan, tool timeline, "
                    "evidence ledger, grounding check, and token usage."
                )
                perf_finish_if_active()
                return

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
            st.caption(f"Turn `{turn_id}` · status `{resp.get('status', 'completed')}`")

            tab_plan, tab_steps, tab_ev, tab_ground, tab_usage = st.tabs(
                ["Plan", "Steps", "Evidence", "Grounding", "Usage"]
            )

            with tab_plan:
                raw_plan = resp.get("plan")
                plan: dict[str, Any] | None = raw_plan if isinstance(raw_plan, dict) else None
                if plan is None:
                    st.caption("No planner output recorded for this turn.")
                else:
                    st.markdown(f"**Intent:** `{plan.get('intent', '—')}`")
                    flags: list[str] = []
                    if plan.get("refers_to_other_org"):
                        flags.append("refers_to_other_org=true")
                    if plan.get("role_violation"):
                        flags.append("role_violation=true")
                    if plan.get("needs_clarification"):
                        flags.append(f"needs_clarification={plan['needs_clarification']}")
                    if flags:
                        st.caption(" · ".join(flags))
                    steps = list(plan.get("steps") or [])
                    if steps:
                        for i, step in enumerate(steps, start=1):
                            hint = step.get("tool_hint")
                            hint_tag = f" (`{hint}`)" if hint else ""
                            st.markdown(f"{i}. {step.get('goal', '')}{hint_tag}")
                    else:
                        st.caption("No tool steps planned.")

            with tab_steps:
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
                            st.markdown(f"**{idx}. `{tool_name}`**")
                            render_status_badge(
                                "ok" if ok_flag else "blocked", "ok" if ok_flag else "failed"
                            )
                            ratio = (
                                f"matched {matched}/{evaluated}"
                                if matched is not None and evaluated is not None
                                else ""
                            )
                            st.caption(f"{dur} ms" + (f" · {ratio}" if ratio else ""))
                            if step.get("summary"):
                                st.caption(str(step["summary"]))
                            safe_args = _redact_args(step.get("args") or {})
                            st.code(json.dumps(safe_args, indent=2, default=str), language="json")

            with tab_ev:
                cites = list(resp.get("citations") or [])
                if not cites:
                    st.caption("No evidence handles cited in this turn.")
                else:
                    rows = [
                        {
                            "handle": c.get("handle"),
                            "device": c.get("hostname") or c.get("device_id"),
                            "metric": c.get("metric"),
                            "value": c.get("value"),
                            "unit": c.get("unit") or "",
                            "snapshot_id": c.get("snapshot_id"),
                            "collected_at": str(c.get("collected_at") or "")[:19],
                        }
                        for c in cites[:200]
                    ]
                    st.dataframe(rows, width="stretch", hide_index=True)

            with tab_ground:
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
                st.caption(f"Trace ID: `{resp.get('trace_id', '—')}`")
                if resp.get("telemetry_as_of"):
                    st.caption(f"Telemetry as of: {fmt_as_of_full(resp.get('telemetry_as_of'))}")

        perf_finish_if_active()

    _inspector_fragment()
