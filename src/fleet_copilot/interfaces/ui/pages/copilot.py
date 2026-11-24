"""Copilot chat page: centred transcript (~820 px), sidebar history, on-demand Inspector."""

from __future__ import annotations

from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.components.answer_card import render_answer_card
from fleet_copilot.interfaces.ui.components.inspector import render_inspector_panel
from fleet_copilot.interfaces.ui.components.proposal_card import render_proposal_cards
from fleet_copilot.interfaces.ui.services import (
    cached_personas,
    cached_telemetry_as_of,
    cached_thread_list,
    cached_thread_turns,
    get_hostname_map,
    get_resolved_policies,
    get_ui_container,
)
from fleet_copilot.interfaces.ui.state import (
    bump_thread_list_version,
    make_tenant_context,
    perf_finish_if_active,
    perf_mark_start,
    require_session,
    safe_page_run,
    switch_to_page,
)
from fleet_copilot.services.copilot import collapse_progress_events
from fleet_copilot.services.demo_auth import DemoSession

_DEFAULT_VISIBLE_TURNS = 8
_USER_AVATAR = ":material/person:"
_ASSISTANT_AVATAR = ":material/devices:"

_CAPABILITY_EXAMPLES: list[tuple[str, str, str]] = [
    (
        "Fleet questions",
        ":material/devices:",
        "Which devices are running low on disk space right now?",
    ),
    (
        "Trends & insights",
        ":material/trending_up:",
        "Which laptops have batteries needing service, and what should we do?",
    ),
    (
        "Safe actions",
        ":material/verified_user:",
        "Open screen_lock remediation tickets for all failing laptops",
    ),
    (
        "Grounded charts",
        ":material/show_chart:",
        "Chart disk free % for our low-disk devices over 30 days",
    ),
    (
        "Ad-hoc SQL",
        ":material/database:",
        "What is the average battery cycle count by hardware model?",
    ),
]


def _start_turn(session: DemoSession, prompt: str, *, new_chat: bool = False) -> None:
    """Submit a turn to `TurnJobs` so reruns or navigation never interrupt execution."""
    clean = prompt.strip()
    if not clean:
        return
    container = get_ui_container()
    ctx = make_tenant_context(session)
    target_tid = None if new_chat else st.session_state.get("active_thread_id")
    job_id = container.turn_jobs.submit(container.guarded, ctx, target_tid, clean)
    snap = container.turn_jobs.get(job_id)
    resolved_tid = snap.thread_id if snap is not None else target_tid

    st.session_state["active_job_id"] = job_id
    st.session_state["active_job_prompt"] = clean
    st.session_state["active_job_thread_id"] = resolved_tid
    st.session_state["active_thread_id"] = resolved_tid


def _load_thread_turns(
    session: DemoSession,
    thread_id: str | None,
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Load stored turns for `thread_id` (plus any ephemeral limited/failed turns)."""
    db_turns: list[dict[str, Any]] = []
    if thread_id:
        match = next((t for t in threads if t.get("thread_id") == thread_id), None)
        updated_key = str((match or {}).get("updated_at") or "")
        version_key = f"{updated_key}:{st.session_state.get('thread_list_version', 0)}"
        data = cached_thread_turns(
            session.company_id,
            session.actor_id,
            session.role,
            thread_id,
            version_key,
        )
        if data is not None:
            db_turns = list(data.get("turns") or [])

    ephemeral_by_thread: dict[str, list[dict[str, Any]]] = dict(
        st.session_state.get("ephemeral_turns") or {}
    )
    extra = list(ephemeral_by_thread.get(thread_id or "__new__", []))
    seen_ids = {str(t.get("turn_id") or "") for t in db_turns}
    merged = list(db_turns)
    for item in extra:
        if str(item.get("turn_id") or "") not in seen_ids:
            merged.append(item)
    return merged


def _render_empty_state(session: DemoSession, *, input_disabled: bool) -> None:
    """Render the clean New Chat onboarding state with 3 persona prompts + capability examples."""
    personas = cached_personas()
    spec = personas.get(session.persona)
    suggested = list(spec.suggested_prompts[:3]) if spec else []

    with st.container(border=True):
        st.markdown("### :material/chat: Start a conversation")
        st.caption(
            "Ask about device health, compliance posture, 30-day trends, charts, or "
            "policy-checked remediation actions."
        )
        if suggested:
            st.markdown("**Suggested for this persona**")
            for idx, prompt in enumerate(suggested):
                if st.button(
                    prompt,
                    key=f"empty_suggested_{idx}",
                    icon=":material/arrow_forward:",
                    width="stretch",
                    disabled=input_disabled,
                ):
                    _start_turn(session, prompt, new_chat=True)
                    st.rerun()

    with st.expander("What can I ask?", expanded=True, icon=":material/help_outline:"):
        for idx, (cap_title, icon, example_q) in enumerate(_CAPABILITY_EXAMPLES):
            c_left, c_right = st.columns([1.8, 4.2])
            with c_left:
                st.markdown(f"{icon} **{cap_title}**")
            with c_right:
                if st.button(
                    example_q,
                    key=f"cap_example_{idx}",
                    type="tertiary",
                    disabled=input_disabled,
                ):
                    _start_turn(session, example_q, new_chat=True)
                    st.rerun()


def _render_active_job_poller(session: DemoSession) -> None:
    """Poll `TurnJobs` every 0.5 s until done, rendering friendly collapsed progress labels."""
    job_id = st.session_state.get("active_job_id")
    if not job_id:
        return

    @st.fragment(run_every=0.5, key="copilot_job_poller")
    def _poller() -> None:
        current_job_id = st.session_state.get("active_job_id")
        if not current_job_id:
            return
        container = get_ui_container()
        snap = container.turn_jobs.get(str(current_job_id))
        prompt_text = str(st.session_state.get("active_job_prompt") or "")

        if prompt_text:
            with st.chat_message("user", avatar=_USER_AVATAR):
                st.markdown(prompt_text)

        if snap is None:
            st.session_state.pop("active_job_id", None)
            st.rerun(scope="app")
            return

        collapsed = collapse_progress_events(snap.events)
        if snap.status in ("queued", "running"):
            current_label = collapsed[-1].label if collapsed else "Reading your fleet context"
            with st.chat_message("assistant", avatar=_ASSISTANT_AVATAR):
                with st.status(current_label, state="running", expanded=True):
                    if not collapsed:
                        st.markdown(":material/hourglass_top: Reading your fleet context")
                    else:
                        for ev in collapsed:
                            st.markdown(f":material/check_small: {ev.label}")
            return

        # Completed or failed: clear poller state, refresh caches, and re-render full view
        st.session_state.pop("active_job_id", None)
        st.session_state.pop("active_job_prompt", None)
        st.session_state.pop("active_job_thread_id", None)

        if snap.result is not None:
            resp_dict = snap.result.model_dump(mode="json")
            tid = snap.result.thread_id
            st.session_state["active_thread_id"] = tid
            st.session_state["selected_turn_id"] = snap.result.turn_id
            if snap.result.status in ("limited", "failed"):
                ephemeral = dict(st.session_state.get("ephemeral_turns") or {})
                bucket = list(ephemeral.get(tid, []))
                bucket.append(
                    {
                        "turn_id": snap.result.turn_id,
                        "user_message": prompt_text,
                        "response_json": resp_dict,
                        "status": snap.result.status,
                    }
                )
                ephemeral[tid] = bucket
                st.session_state["ephemeral_turns"] = ephemeral
        elif snap.error:
            tid = snap.thread_id
            ephemeral = dict(st.session_state.get("ephemeral_turns") or {})
            bucket = list(ephemeral.get(tid, []))
            bucket.append(
                {
                    "turn_id": f"err_{current_job_id}",
                    "user_message": prompt_text,
                    "response_json": {
                        "turn_id": f"err_{current_job_id}",
                        "status": "failed",
                        "answer_markdown": snap.error,
                        "answer": {"summary": snap.error},
                        "warnings": [],
                    },
                    "status": "failed",
                }
            )
            ephemeral[tid] = bucket
            st.session_state["ephemeral_turns"] = ephemeral

        cached_thread_turns.clear()
        bump_thread_list_version()
        st.rerun(scope="app")

    _poller()


def _turn_to_response_dict(turn_row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a stored DB turn row into a `TurnResponse`-shaped dict without running graphs."""
    rj = turn_row.get("response_json")
    if isinstance(rj, dict) and rj:
        return rj
    return {
        "turn_id": str(turn_row.get("turn_id") or ""),
        "trace_id": str(turn_row.get("trace_id") or ""),
        "status": str(turn_row.get("status") or "completed"),
        "answer_markdown": str(turn_row.get("assistant_response") or ""),
        "plan": turn_row.get("plan_json"),
        "tool_trace": list(turn_row.get("tool_calls_json") or []),
        "citations": [],
        "charts": [],
        "queries": [],
        "insights": [],
        "proposals": [],
        "verdicts": [],
        "usage": {
            "input_tokens": int(turn_row.get("input_tokens") or 0),
            "output_tokens": int(turn_row.get("output_tokens") or 0),
            "cost_usd_est": float(turn_row.get("cost_usd_est") or 0.0),
            "latency_ms": int(turn_row.get("latency_ms") or 0),
        },
        "warnings": [],
    }


def _render_transcript_zone(
    *,
    session: DemoSession,
    turns: list[dict[str, Any]],
    active_tid: str | None,
    selected_turn_id: str | None,
    hostname_map: dict[str, str],
    policies: dict[str, Any],
    examples_only: bool,
    inspector_open: bool,
) -> None:
    """Render the Copilot header, examples-only banner, paginated transcript, poller, and input."""
    with st.container(
        horizontal=True,
        horizontal_alignment="distribute",
        vertical_alignment="center",
        gap="small",
    ):
        st.markdown("### :material/chat: Copilot")
        if turns and not inspector_open:
            if st.button(
                "Open Inspector",
                key="copilot_header_open_inspector",
                icon=":material/search_insights:",
                type="secondary",
                width="content",
            ):
                perf_mark_start("toggle_inspector")
                st.session_state["inspector_open"] = True
                if not st.session_state.get("selected_turn_id"):
                    st.session_state["selected_turn_id"] = str(turns[-1].get("turn_id") or "")
                st.rerun(scope="app")

    if examples_only:
        with st.container(border=True):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
                gap="small",
            ):
                st.info(
                    "Examples only (live budget used). Live chat input is disabled.",
                    icon=":material/menu_book:",
                )
                if st.button(
                    "View Examples",
                    key="copilot_go_examples",
                    icon=":material/arrow_forward:",
                    width="content",
                ):
                    st.session_state["how_it_works_default_tab"] = "Examples"
                    switch_to_page("how_it_works")

    @st.fragment(key="copilot_transcript")
    def _transcript_fragment() -> None:
        def _on_inspect(tid_sel: str) -> None:
            # Always rerun at app scope so Inspector mounts cleanly (fixes U11)
            st.session_state["selected_turn_id"] = tid_sel
            st.session_state["inspector_open"] = True
            st.rerun(scope="app")

        active_job = st.session_state.get("active_job_id")
        if not turns and not active_job:
            _render_empty_state(session, input_disabled=examples_only)
        else:
            expanded_threads: set[str] = set(st.session_state.get("expanded_thread_ids") or ())
            tid_key = active_tid or "__active__"
            show_all = tid_key in expanded_threads or len(turns) <= _DEFAULT_VISIBLE_TURNS
            hidden_count = 0 if show_all else len(turns) - _DEFAULT_VISIBLE_TURNS
            visible_turns = turns if show_all else turns[-_DEFAULT_VISIBLE_TURNS:]

            if hidden_count > 0:
                if st.button(
                    f"Show {hidden_count} earlier turns",
                    key=f"show_earlier_{tid_key}",
                    icon=":material/history:",
                    type="secondary",
                    width="stretch",
                ):
                    expanded_threads.add(tid_key)
                    st.session_state["expanded_thread_ids"] = expanded_threads
                    st.rerun(scope="fragment")

            for idx, turn_row in enumerate(visible_turns):
                turn_id = str(turn_row.get("turn_id") or f"turn_{idx}")
                user_msg = str(turn_row.get("user_message") or "").strip()
                if user_msg:
                    with st.chat_message("user", avatar=_USER_AVATAR):
                        st.markdown(user_msg)

                resp_dict = _turn_to_response_dict(turn_row)
                with st.chat_message("assistant", avatar=_ASSISTANT_AVATAR):
                    render_answer_card(
                        resp_dict,
                        session=session,
                        hostname_map=hostname_map,
                        turn_id=turn_id,
                        policies=policies,
                        is_selected=(inspector_open and selected_turn_id == turn_id),
                        on_inspect_click=_on_inspect,
                    )
                    render_proposal_cards(
                        resp_dict,
                        session=session,
                        hostname_map=hostname_map,
                        thread_id=active_tid or str(resp_dict.get("thread_id") or ""),
                        turn_id=turn_id,
                    )

        perf_finish_if_active()

    _transcript_fragment()

    # Live streaming progress poller (mounts ONLY while a TurnJobs job is active)
    _render_active_job_poller(session)

    chat_disabled = examples_only or bool(st.session_state.get("active_job_id"))
    placeholder = (
        "Examples only mode — browse recorded examples in How it works"
        if examples_only
        else "Ask about your fleet, request a chart, or propose an action..."
    )
    new_msg = st.chat_input(placeholder, disabled=chat_disabled, key="copilot_chat_input")
    if new_msg and not chat_disabled:
        _start_turn(session, new_msg, new_chat=False)
        st.rerun()


def render_copilot_page() -> None:
    """Render the Copilot page (chat history lives in sidebar; Inspector mounts only when open)."""
    session = require_session()
    container = get_ui_container()

    mode = container.guarded.mode(session.company_id)
    examples_only = mode == "examples_only"

    # Handle incoming prompt handoffs (from Home cards, Guided Tour, Fleet, or refusal alternatives)
    pending_prompt = st.session_state.pop("pending_prompt", None)
    pending_auto = bool(st.session_state.pop("pending_auto_send", False))
    pending_new = bool(st.session_state.pop("pending_new_chat", False))
    if pending_prompt and pending_auto and not examples_only:
        _start_turn(session, str(pending_prompt), new_chat=pending_new)

    as_of_iso = cached_telemetry_as_of(session.company_id)
    hostname_map = get_hostname_map(session.company_id, as_of_iso)
    policies = get_resolved_policies(session.company_id, as_of_iso)

    search_q = str(st.session_state.get("thread_search_query") or "").strip() or None
    thread_ver = int(st.session_state.get("thread_list_version", 0))
    threads = cached_thread_list(
        session.company_id,
        session.actor_id,
        session.role,
        thread_ver,
        search_q,
    )

    active_tid: str | None = st.session_state.get("active_thread_id")
    turns = _load_thread_turns(session, active_tid, threads)

    # Populate lookup map in session state so Inspector renders the chosen turn cleanly
    turns_by_id = {str(t.get("turn_id") or f"turn_{idx}"): t for idx, t in enumerate(turns)}
    st.session_state["_active_turns_by_id"] = turns_by_id
    st.session_state["_active_turn_order"] = list(turns_by_id.keys())
    selected_turn_id: str | None = st.session_state.get("selected_turn_id")
    if selected_turn_id not in turns_by_id:
        selected_turn_id = next(reversed(turns_by_id), None)

    inspector_open = bool(st.session_state.get("inspector_open", False))

    if inspector_open:
        col_main, col_insp = st.columns([6.4, 3.6], gap="medium")
        with col_main:
            _render_transcript_zone(
                session=session,
                turns=turns,
                active_tid=active_tid,
                selected_turn_id=selected_turn_id,
                hostname_map=hostname_map,
                policies=policies,
                examples_only=examples_only,
                inspector_open=True,
            )
        with col_insp:
            render_inspector_panel()
    else:
        with st.container(key="copilot_centered_wrap"):
            _render_transcript_zone(
                session=session,
                turns=turns,
                active_tid=active_tid,
                selected_turn_id=selected_turn_id,
                hostname_map=hostname_map,
                policies=policies,
                examples_only=examples_only,
                inspector_open=False,
            )


def main() -> None:
    safe_page_run(render_copilot_page)


if __name__ == "__main__":
    main()
