"""Fragment-isolated Action Proposal cards with HITL Approve / Reject / Defer controls."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.humanize import (
    humanize_actor_label,
    humanize_check_name,
    humanize_source,
    humanize_tool_name,
)
from fleet_copilot.interfaces.ui.safe_md import safe_code, safe_md
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


def summarize_proposal_args(args: dict[str, Any], rationale: str = "") -> str:
    """Format non-redundant action arguments (skipping `note` when it duplicates `rationale`)."""
    skip = {"device_id", "employee_id", "evidence"}
    rationale_norm = rationale.strip().lower()
    parts: list[str] = []
    for k, v in args.items():
        if k in skip or not v:
            continue
        val_str = str(v).strip()
        # Avoid repeating identical rationale/note copy on the proposal card
        if k in ("note", "reason") and (
            val_str.lower() == rationale_norm
            or (rationale_norm and val_str.lower() in rationale_norm)
            or (rationale_norm and rationale_norm in val_str.lower())
        ):
            continue
        label = k.replace("_", " ").capitalize()
        parts.append(f"{label}: {val_str}")
    return " · ".join(parts)


def format_already_decided_message(proposal_row: dict[str, Any]) -> str:
    """Build a crisp cross-path status message when a proposal was already decided elsewhere."""
    status = str(proposal_row.get("status") or "")
    reviewer = proposal_row.get("reviewer_id")
    via = proposal_row.get("decided_via") or "queue"
    rev_label = humanize_actor_label(str(reviewer)) if reviewer else "an IT admin"
    if status in ("approved", "executed"):
        return f"Already approved via {via} by {rev_label}."
    if status == "rejected":
        return f"Already rejected via {via} by {rev_label}."
    if status == "stale_blocked":
        return f"Already approved via {via} by {rev_label}, then blocked by fresh data check."
    if status == "expired":
        return "This approval request has expired."
    return f"Already decided ({status}) via {via} by {rev_label}."


def render_policy_checklist(checks: list[dict[str, Any]], *, expanded: bool = False) -> None:
    """Render a humanized policy checklist expander with pass/fail icons and readable sources."""
    if not checks:
        return
    passed_n = sum(1 for c in checks if c.get("passed", True))
    with st.expander(
        f"Policy checklist ({passed_n}/{len(checks)} passed)",
        expanded=expanded,
        icon=":material/verified_user:",
    ):
        for chk in checks:
            ok_flag = bool(chk.get("passed", True))
            icon = ":material/check_circle:" if ok_flag else ":material/cancel:"
            mark = "Pass" if ok_flag else "Fail"
            chk_label = humanize_check_name(str(chk.get("name") or ""))
            src_label = humanize_source(str(chk.get("source") or ""))
            obs_safe = safe_code(chk.get("observed")) or "—"
            thr_safe = safe_code(chk.get("threshold")) or "—"
            st.markdown(
                f"{icon} **{safe_md(chk_label)}** ({mark}) — "
                f"observed `{obs_safe}` vs "
                f"threshold `{thr_safe}` "
                f"({safe_md(src_label)})"
            )


def render_proposal_cards(
    resp: dict[str, Any],
    *,
    session: DemoSession,
    hostname_map: dict[str, str],
    thread_id: str,
    turn_id: str,
    read_only: bool = False,
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
        is_admin = session.role == "it_admin" and not read_only
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

            # Capture pre-resume DB statuses to detect cross-path decisions made via queue/API
            prior_db: dict[str, dict[str, Any]] = {}
            for pid_k in decisions:
                try:
                    row_before = container.store.get_proposal(session.company_id, pid_k)
                except Exception:  # noqa: BLE001
                    row_before = None
                if isinstance(row_before, dict):
                    prior_db[pid_k] = row_before

            res = container.guarded.resume(ctx, thread_id, decisions, note=note or None)
            outcomes = dict(st.session_state.get("last_resume_outcomes") or {})

            for pid_k, dec in decisions.items():
                prior = prior_db.get(pid_k)
                if prior is not None and str(prior.get("status") or "") != "pending_approval":
                    rcpt_json = prior.get("external_receipt_json")
                    rcpt_id = (
                        str(rcpt_json.get("receipt_id"))
                        if isinstance(rcpt_json, dict) and rcpt_json.get("receipt_id")
                        else None
                    )
                    outcomes[pid_k] = {
                        "status": str(prior.get("status") or "executed"),
                        "receipt_id": rcpt_id,
                        "message": format_already_decided_message(prior),
                        "already_decided": True,
                    }
                    continue

                p_res = next(
                    (p for p in res.proposals if str(p.get("proposal_id") or "") == pid_k),
                    None,
                )
                if p_res is not None:
                    st_r = str(p_res.get("status") or "")
                    outcomes[pid_k] = {
                        "status": "deferred" if dec == "defer" else st_r,
                        "receipt_id": _extract_receipt_from_markdown(res.answer_markdown, pid_k),
                        "message": _extract_stale_reason_from_markdown(res.answer_markdown, pid_k)
                        or res.answer_markdown.splitlines()[0],
                    }
                else:
                    outcomes[pid_k] = {
                        "status": "deferred" if dec == "defer" else dec,
                        "message": res.answer_markdown,
                    }

            st.session_state["last_resume_outcomes"] = outcomes
            cached_thread_turns.clear()
            bump_thread_list_version()

        for idx, (prop, eff_status, receipt_id, detail_msg) in enumerate(enriched):
            pid = str(prop.get("proposal_id") or f"prop_{idx}")
            tool_label = humanize_tool_name(str(prop.get("tool_name") or ""))
            target_id = str(prop.get("target_id") or "")
            host = hostname_map.get(target_id)
            target_display = (
                f"{safe_md(host)} (`{target_id}`)"
                if host and host != target_id
                else f"`{target_id}`"
            )
            raw_args = prop.get("arguments")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            rationale = str(prop.get("rationale") or "").strip()
            verdict = _match_verdict(prop, verdicts)
            checks = list((verdict or {}).get("checks") or [])
            expires_text = fmt_expires_in(prop.get("expires_at"))

            with st.container(border=True):
                with st.container(
                    horizontal=True,
                    horizontal_alignment="distribute",
                    vertical_alignment="center",
                    gap="small",
                ):
                    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                        if multi_pending and eff_status == "pending_approval":
                            checked = st.checkbox(
                                f"Select {pid}",
                                value=True,
                                key=f"chk_prop_{turn_id}_{pid}",
                                label_visibility="collapsed",
                            )
                            if checked:
                                selected_for_batch.append(pid)
                        st.markdown(f"**{tool_label}** · {target_display}")
                    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                        render_proposal_status_badge(eff_status)
                        if eff_status == "pending_approval" and expires_text:
                            st.caption(f":material/schedule: {expires_text}")

                arg_summary = summarize_proposal_args(args, rationale=rationale)
                if arg_summary:
                    st.caption(safe_md(arg_summary))
                if rationale:
                    st.markdown(safe_md(rationale))

                render_policy_checklist(
                    checks,
                    expanded=eff_status == "pending_approval" and len(enriched) <= 2,
                )

                # Decision buttons (it_admin ONLY, not read_only) OR outcome summary
                if eff_status == "pending_approval":
                    if is_admin and not multi_pending:
                        # Content-width buttons in a wrapping row: labels never truncate when
                        # the transcript column is narrow (e.g. with the Inspector open).
                        with st.container(horizontal=True, gap="small"):
                            st.button(
                                "Approve",
                                key=f"approve_{turn_id}_{pid}",
                                type="primary",
                                icon=":material/check:",
                                width="content",
                                on_click=_apply_decisions,
                                args=({pid: "approve"},),
                            )
                            st.button(
                                "Reject",
                                key=f"reject_{turn_id}_{pid}",
                                type="secondary",
                                icon=":material/close:",
                                width="content",
                                on_click=_apply_decisions,
                                args=({pid: "reject"},),
                            )
                            st.button(
                                "Defer",
                                key=f"defer_{turn_id}_{pid}",
                                type="tertiary",
                                icon=":material/schedule:",
                                width="content",
                                on_click=_apply_decisions,
                                args=({pid: "defer"},),
                            )
                    elif not is_admin and not read_only:
                        st.info(
                            "Ask an IT admin to approve this proposed action.",
                            icon=":material/visibility:",
                        )
                else:
                    outcome_rec = resume_outcomes.get(pid) or {}
                    if outcome_rec.get("already_decided") and detail_msg:
                        st.info(safe_md(detail_msg), icon=":material/info:")
                    elif eff_status == "executed":
                        rcpt_str = f" · Receipt `{receipt_id}`" if receipt_id else ""
                        st.success(f"Executed{rcpt_str}", icon=":material/check_circle:")
                        if not read_only:
                            if st.button(
                                "View in Approvals & Activity",
                                key=f"outbox_link_{turn_id}_{pid}",
                                type="tertiary",
                                icon=":material/open_in_new:",
                            ):
                                if receipt_id:
                                    st.session_state["approvals_outbox_receipt"] = receipt_id
                                switch_to_page("approvals")
                    elif eff_status == "stale_blocked":
                        reason = detail_msg or "Telemetry changed before execution."
                        st.warning(
                            f"Blocked by fresh data check: {safe_md(reason)}",
                            icon=":material/block:",
                        )
                    elif detail_msg:
                        st.caption(safe_md(detail_msg))

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
