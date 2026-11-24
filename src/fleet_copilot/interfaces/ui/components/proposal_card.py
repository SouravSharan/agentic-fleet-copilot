"""Fragment-isolated Action Proposal cards with HITL Approve / Reject / Defer controls."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.services import cached_thread_turns, get_ui_container
from fleet_copilot.interfaces.ui.state import (
    bump_thread_list_version,
    make_tenant_context,
    perf_finish_if_active,
    perf_mark_start,
    switch_to_page,
)
from fleet_copilot.interfaces.ui.theme import (
    fmt_expires_in,
    plain_tool_name,
    render_proposal_status_badge,
)
from fleet_copilot.services.demo_auth import DemoSession

_RECEIPT_RE = re.compile(r"receipt\s+`?([A-Za-z0-9_-]+)`?", re.IGNORECASE)


def _match_verdict(prop: dict[str, Any], verdicts: list[dict[str, Any]]) -> dict[str, Any] | None:
    pid = str(prop.get("proposal_id") or "")
    for v in verdicts:
        if pid and str(v.get("proposal_id") or "") == pid:
            return v
    for v in verdicts:
        if (
            v.get("tool_name") == prop.get("tool_name")
            and v.get("target_id") == prop.get("target_id")
            and v.get("decision") != "refuse"
        ):
            return v
    return None


def _extract_receipt_from_markdown(markdown: str, proposal_id: str) -> str | None:
    for line in markdown.splitlines():
        if proposal_id and proposal_id in line:
            m = _RECEIPT_RE.search(line)
            if m:
                return m.group(1)
    return None


def _extract_stale_reason_from_markdown(markdown: str, proposal_id: str) -> str | None:
    for line in markdown.splitlines():
        if proposal_id and proposal_id in line and "stale_blocked" in line.lower():
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


def _summarize_args(args: dict[str, Any]) -> str:
    skip = {"device_id", "employee_id", "evidence"}
    parts = [f"{k.replace('_', ' ')}: {v}" for k, v in args.items() if k not in skip and v]
    return " · ".join(parts)


def render_proposal_cards(
    resp: dict[str, Any],
    *,
    session: DemoSession,
    hostname_map: dict[str, str],
    thread_id: str,
    turn_id: str,
) -> None:
    """Render all staged action proposals for one turn inside an isolated `@st.fragment`."""
    packet = resp.get("approval_packet") if isinstance(resp.get("approval_packet"), dict) else None
    proposals: list[dict[str, Any]] = list(
        (packet.get("proposals") if packet else None) or resp.get("proposals") or []
    )
    if not proposals:
        return

    verdicts = list(resp.get("verdicts") or [])
    batch_id = str(
        (packet or {}).get("batch_id") or proposals[0].get("batch_id") or f"batch_{turn_id}"
    )
    is_fixture = bool((packet or {}).get("fixture_mode", False))
    answer_md = str(resp.get("answer_markdown") or "")
    frag_key = f"proposal_batch_{turn_id}_{batch_id}"

    @st.fragment(key=frag_key)
    def _proposal_batch_fragment() -> None:
        is_admin = session.role == "it_admin"
        fixture_map: dict[str, dict[str, Any]] = dict(
            st.session_state.get("fixture_decisions") or {}
        )
        resume_outcomes: dict[str, dict[str, Any]] = dict(
            st.session_state.get("last_resume_outcomes") or {}
        )

        # Determine effective status of each proposal
        enriched: list[tuple[dict[str, Any], str, str | None, str | None]] = []
        pending_ids: list[str] = []
        for prop in proposals:
            pid = str(prop.get("proposal_id") or "")
            eff_status = str(prop.get("status") or "pending_approval")
            receipt_id = _extract_receipt_from_markdown(answer_md, pid)
            detail_msg = _extract_stale_reason_from_markdown(answer_md, pid)

            if pid in fixture_map:
                eff_status = str(fixture_map[pid].get("status") or eff_status)
                receipt_id = fixture_map[pid].get("receipt_id") or receipt_id
                detail_msg = fixture_map[pid].get("message") or detail_msg
            elif pid in resume_outcomes:
                eff_status = str(resume_outcomes[pid].get("status") or eff_status)
                receipt_id = resume_outcomes[pid].get("receipt_id") or receipt_id
                detail_msg = resume_outcomes[pid].get("message") or detail_msg

            if eff_status == "pending_approval":
                pending_ids.append(pid)
            enriched.append((prop, eff_status, receipt_id, detail_msg))

        multi_pending = is_admin and len(pending_ids) > 1
        selected_for_batch: list[str] = []

        def _apply_decisions(decisions: dict[str, str], note: str | None = None) -> None:
            perf_mark_start("approve_card")
            if is_fixture:
                updated = dict(st.session_state.get("fixture_decisions") or {})
                for pid_k, dec in decisions.items():
                    if dec == "approve":
                        updated[pid_k] = {
                            "status": "executed",
                            "receipt_id": f"FIX-{pid_k[:8].upper()}",
                            "message": "Approved on fixture proposal card.",
                        }
                    elif dec == "reject":
                        updated[pid_k] = {
                            "status": "rejected",
                            "message": "Rejected on fixture proposal card.",
                        }
                    else:
                        updated[pid_k] = {
                            "status": "deferred",
                            "message": "Deferred to approvals queue.",
                        }
                st.session_state["fixture_decisions"] = updated
                return

            container = get_ui_container()
            ctx = make_tenant_context(session)
            res = container.guarded.resume(ctx, thread_id, decisions, note=note or None)
            outcomes = dict(st.session_state.get("last_resume_outcomes") or {})
            for p_res in res.proposals:
                pid_r = str(p_res.get("proposal_id") or "")
                st_r = str(p_res.get("status") or "")
                if pid_r:
                    outcomes[pid_r] = {
                        "status": "deferred" if decisions.get(pid_r) == "defer" else st_r,
                        "receipt_id": _extract_receipt_from_markdown(res.answer_markdown, pid_r),
                        "message": _extract_stale_reason_from_markdown(res.answer_markdown, pid_r)
                        or res.answer_markdown.splitlines()[0],
                    }
            # Fallback if resume reported no-op message
            if not res.proposals:
                for pid_k in decisions:
                    outcomes[pid_k] = {
                        "status": "deferred" if decisions[pid_k] == "defer" else decisions[pid_k],
                        "message": res.answer_markdown,
                    }
            st.session_state["last_resume_outcomes"] = outcomes
            cached_thread_turns.clear()
            bump_thread_list_version()

        for idx, (prop, eff_status, receipt_id, detail_msg) in enumerate(enriched):
            pid = str(prop.get("proposal_id") or f"prop_{idx}")
            tool_label = plain_tool_name(str(prop.get("tool_name") or ""))
            target_id = str(prop.get("target_id") or "")
            host = hostname_map.get(target_id)
            target_display = (
                f"{host} (`{target_id}`)" if host and host != target_id else f"`{target_id}`"
            )
            raw_args = prop.get("arguments")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            verdict = _match_verdict(prop, verdicts)
            checks = list((verdict or {}).get("checks") or [])
            expires_text = fmt_expires_in(prop.get("expires_at"))

            with st.container(border=True):
                top_cols = st.columns([0.5, 4.0, 2.0]) if multi_pending else st.columns([4.5, 2.0])
                if multi_pending:
                    with top_cols[0]:
                        if eff_status == "pending_approval":
                            checked = st.checkbox(
                                f"Select {pid}",
                                value=True,
                                key=f"chk_prop_{turn_id}_{pid}",
                                label_visibility="collapsed",
                            )
                            if checked:
                                selected_for_batch.append(pid)
                    hdr_col, right_col = top_cols[1], top_cols[2]
                else:
                    hdr_col, right_col = top_cols[0], top_cols[1]

                with hdr_col:
                    st.markdown(f"**{tool_label}** · {target_display}")
                    arg_summary = _summarize_args(args)
                    if arg_summary:
                        st.caption(arg_summary)
                with right_col:
                    render_proposal_status_badge(eff_status)
                    if eff_status == "pending_approval" and expires_text:
                        st.caption(f":material/schedule: {expires_text}")

                if prop.get("rationale"):
                    st.markdown(str(prop["rationale"]))

                # Policy checklist (✓ / ✗ with observed vs threshold and source)
                if checks:
                    passed_n = sum(1 for c in checks if c.get("passed", True))
                    with st.expander(
                        f"Policy checklist ({passed_n}/{len(checks)} passed)",
                        expanded=eff_status == "pending_approval" and len(enriched) <= 2,
                        icon=":material/verified_user:",
                    ):
                        for chk in checks:
                            ok_flag = bool(chk.get("passed", True))
                            icon = ":material/check_circle:" if ok_flag else ":material/cancel:"
                            mark = "Pass" if ok_flag else "Fail"
                            st.markdown(
                                f"{icon} **{chk.get('name')}** ({mark}) — "
                                f"observed `{chk.get('observed')}` vs "
                                f"threshold `{chk.get('threshold')}` "
                                f"(source: `{chk.get('source')}`)"
                            )

                # Decision buttons (it_admin ONLY) OR outcome summary
                if eff_status == "pending_approval":
                    if is_admin and not multi_pending:
                        b1, b2, b3 = st.columns(3, gap="small")
                        with b1:
                            st.button(
                                "Approve",
                                key=f"approve_{turn_id}_{pid}",
                                type="primary",
                                icon=":material/check:",
                                width="stretch",
                                on_click=_apply_decisions,
                                args=({pid: "approve"},),
                            )
                        with b2:
                            st.button(
                                "Reject",
                                key=f"reject_{turn_id}_{pid}",
                                type="secondary",
                                icon=":material/close:",
                                width="stretch",
                                on_click=_apply_decisions,
                                args=({pid: "reject"},),
                            )
                        with b3:
                            st.button(
                                "Defer",
                                key=f"defer_{turn_id}_{pid}",
                                type="tertiary",
                                icon=":material/schedule:",
                                width="stretch",
                                on_click=_apply_decisions,
                                args=({pid: "defer"},),
                            )
                    elif not is_admin:
                        st.info(
                            "Ask an IT admin to approve this proposed action.",
                            icon=":material/visibility:",
                        )
                else:
                    if eff_status == "executed":
                        rcpt_str = f" · Receipt `{receipt_id}`" if receipt_id else ""
                        st.success(f"Executed{rcpt_str}", icon=":material/check_circle:")
                        if st.button(
                            "View in Approvals & Activity",
                            key=f"outbox_link_{turn_id}_{pid}",
                            type="tertiary",
                            icon=":material/open_in_new:",
                        ):
                            switch_to_page("approvals")
                    elif eff_status == "stale_blocked":
                        reason = detail_msg or "Telemetry changed before execution."
                        st.warning(
                            f"Blocked by fresh data check: {reason}",
                            icon=":material/block:",
                        )
                    elif detail_msg:
                        st.caption(detail_msg)

        # Batch controls for multiple pending proposals (it_admin ONLY)
        if multi_pending:
            approve_decs = {
                pid: ("approve" if pid in selected_for_batch else "defer") for pid in pending_ids
            }
            reject_decs = {
                pid: ("reject" if pid in selected_for_batch else "defer") for pid in pending_ids
            }
            defer_decs = {pid: "defer" for pid in pending_ids}
            with st.container(horizontal=True, gap="small"):
                st.button(
                    f"Approve selected ({len(selected_for_batch)})",
                    key=f"batch_approve_{turn_id}_{batch_id}",
                    type="primary",
                    icon=":material/done_all:",
                    disabled=not selected_for_batch,
                    on_click=_apply_decisions,
                    args=(approve_decs,),
                )
                st.button(
                    "Reject selected",
                    key=f"batch_reject_{turn_id}_{batch_id}",
                    type="secondary",
                    icon=":material/close:",
                    disabled=not selected_for_batch,
                    on_click=_apply_decisions,
                    args=(reject_decs,),
                )
                st.button(
                    "Defer all",
                    key=f"batch_defer_{turn_id}_{batch_id}",
                    type="tertiary",
                    icon=":material/schedule:",
                    on_click=_apply_decisions,
                    args=(defer_decs,),
                )

        perf_finish_if_active()

    _proposal_batch_fragment()
