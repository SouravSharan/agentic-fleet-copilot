"""Approvals & Activity page: Pending queue, History, Audit timeline, and Integration outbox."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.components.proposal_card import (
    render_policy_checklist,
    summarize_proposal_args,
)
from fleet_copilot.interfaces.ui.humanize import (
    humanize_actor_label,
    humanize_audit_event,
    humanize_tool_name,
)
from fleet_copilot.interfaces.ui.safe_md import safe_code, safe_md
from fleet_copilot.interfaces.ui.services import (
    cached_telemetry_as_of,
    cached_thread_turns,
    get_hostname_map,
    get_ui_container,
)
from fleet_copilot.interfaces.ui.state import (
    bump_thread_list_version,
    make_tenant_context,
    open_copilot_thread,
    perf_finish_if_active,
    perf_mark_start,
    require_session,
    safe_page_run,
)
from fleet_copilot.interfaces.ui.theme import (
    fmt_as_of_full,
    fmt_expires_in,
    fmt_int,
    render_proposal_status_badge,
    render_status_badge,
)
from fleet_copilot.services.demo_auth import DemoSession

_CHANNEL_LABELS: dict[str, tuple[str, str]] = {
    "ticketing": ("ServiceNow ticket", ":material/confirmation_number:"),
    "procurement": ("Hardware purchase order", ":material/shopping_cart:"),
    "email": ("Employee email", ":material/mail:"),
    "asset_management": ("Asset lifecycle flag", ":material/recycling:"),
}


def _extract_receipt_id(prop: dict[str, Any]) -> str | None:
    rcpt = prop.get("external_receipt_json")
    if isinstance(rcpt, dict) and rcpt.get("receipt_id"):
        return str(rcpt["receipt_id"])
    return None


def _proposal_checks(prop: dict[str, Any]) -> list[dict[str, Any]]:
    verdict = prop.get("policy_verdict_json")
    if isinstance(verdict, dict) and isinstance(verdict.get("checks"), list):
        return list(verdict["checks"])
    return []


def _render_pending_tab(
    session: DemoSession,
    hostname_map: dict[str, str],
) -> None:
    """Render company-wide pending proposals with queue Approve/Reject (it_admin only)."""
    container = get_ui_container()
    is_admin = session.role == "it_admin"

    @st.fragment(key="approvals_pending_frag")
    def _pending_fragment() -> None:
        pending = list(
            container.store.list_proposals(session.company_id, status="pending_approval")
        )
        outcomes: dict[str, dict[str, Any]] = dict(
            st.session_state.get("queue_decision_outcomes") or {}
        )

        # Show recent queue decision outcomes banner cleanly
        if outcomes:
            with st.container(border=True):
                with st.container(
                    horizontal=True,
                    horizontal_alignment="distribute",
                    vertical_alignment="center",
                ):
                    st.markdown("**Recent approval outcomes**")
                    if st.button("Dismiss", key="queue_dismiss_outcomes", type="tertiary"):
                        st.session_state.pop("queue_decision_outcomes", None)
                        st.rerun()
                for pid_out, info in outcomes.items():
                    st_out = str(info.get("status") or "")
                    rcpt_id = info.get("receipt_id")
                    msg = str(info.get("message") or "")
                    if st_out == "executed":
                        rcpt_str = f" · Receipt `{rcpt_id}`" if rcpt_id else ""
                        st.success(
                            f"`{pid_out[:8]}` executed{rcpt_str}",
                            icon=":material/check_circle:",
                        )
                    elif st_out == "stale_blocked":
                        st.warning(
                            f"`{pid_out[:8]}` blocked by fresh data check: {safe_md(msg)}",
                            icon=":material/block:",
                        )
                    elif st_out == "rejected":
                        st.info(f"`{pid_out[:8]}` rejected.", icon=":material/cancel:")
                    elif st_out == "expired":
                        st.warning(f"`{pid_out[:8]}` expired.", icon=":material/timer_off:")
                    else:
                        st.caption(f"`{pid_out[:8]}` -> {st_out}: {safe_md(msg)}")

        if not pending:
            empty_msg = (
                "No pending approvals for this company right now. Stage an action from Home or "
                "Copilot to review it here."
                if is_admin
                else "Nothing is waiting for approval. IT admins stage actions from Home or "
                "Copilot."
            )
            st.info(empty_msg, icon=":material/check_circle:")
            perf_finish_if_active()
            return

        if not is_admin:
            st.info(
                "Read-only view: ask an IT admin to approve or reject pending actions.",
                icon=":material/visibility:",
            )

        selected_ids: list[str] = []
        note_text = ""
        if is_admin:
            note_text = st.text_input(
                "Optional review note",
                key="approvals_queue_note",
                placeholder="Add an optional approval or rejection note...",
            )

        def _decide_batch(target_ids: list[str], decision: str) -> None:
            perf_mark_start("approve_queue")
            if not is_admin or not target_ids:
                return
            action_svc = container.guarded.action_service
            ctx = make_tenant_context(session)
            new_outcomes = dict(st.session_state.get("queue_decision_outcomes") or {})
            for pid_item in target_ids:
                dec = action_svc.decide(
                    ctx,
                    pid_item,
                    "approve" if decision == "approve" else "reject",
                    "queue",
                    container.clock,
                    note=note_text.strip() or None,
                )
                if not dec.applied:
                    st_final = str((dec.proposal or {}).get("status") or "skipped")
                    new_outcomes[pid_item] = {
                        "status": st_final,
                        "message": dec.message,
                    }
                    continue
                if decision == "approve":
                    exe = action_svc.execute(ctx, pid_item, container.clock)
                    new_outcomes[pid_item] = {
                        "status": exe.status,
                        "receipt_id": exe.receipt.receipt_id if exe.receipt else None,
                        "message": exe.message,
                    }
                else:
                    new_outcomes[pid_item] = {
                        "status": "rejected",
                        "message": "Rejected via approvals queue.",
                    }
            st.session_state["queue_decision_outcomes"] = new_outcomes
            cached_thread_turns.clear()
            bump_thread_list_version()

        for prop in pending:
            pid = str(prop.get("proposal_id") or "")
            tool_label = humanize_tool_name(str(prop.get("tool_name") or ""))
            target_id = str(prop.get("target_id") or "")
            host = hostname_map.get(target_id)
            target_display = (
                f"{safe_md(host)} (`{target_id}`)"
                if host and host != target_id
                else f"`{target_id}`"
            )
            raw_args = prop.get("arguments_json") or prop.get("arguments")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            rationale = str(prop.get("rationale") or "").strip()
            expires_text = fmt_expires_in(prop.get("expires_at"))
            checks = _proposal_checks(prop)
            ev_snaps = list(prop.get("evidence_snapshot_ids") or [])
            proposer = str(prop.get("proposed_by_actor_id") or "")
            thread_id = str(prop.get("thread_id") or "")

            with st.container(border=True):
                with st.container(
                    horizontal=True,
                    horizontal_alignment="distribute",
                    vertical_alignment="center",
                    gap="small",
                ):
                    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                        if is_admin:
                            checked = st.checkbox(
                                f"Select {pid}",
                                value=True,
                                key=f"queue_chk_{pid}",
                                label_visibility="collapsed",
                            )
                            if checked:
                                selected_ids.append(pid)
                        st.markdown(f"**{tool_label}** · {target_display}")
                    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                        render_proposal_status_badge("pending_approval")
                        if expires_text:
                            st.caption(f":material/schedule: {expires_text}")

                arg_summary = summarize_proposal_args(args, rationale=rationale)
                if arg_summary:
                    st.caption(safe_md(arg_summary))
                if rationale:
                    st.markdown(safe_md(rationale))

                # Evidence snapshot chips
                if ev_snaps:
                    with st.container(horizontal=True, wrap=True, gap="small"):
                        for snap in ev_snaps[:6]:
                            st.badge(str(snap), icon=":material/verified:", color="gray")
                        if len(ev_snaps) > 6:
                            st.badge(f"+{len(ev_snaps) - 6} snapshots", color="gray")

                render_policy_checklist(checks, expanded=len(pending) <= 2)

                with st.container(
                    horizontal=True,
                    horizontal_alignment="distribute",
                    vertical_alignment="center",
                    gap="small",
                ):
                    if thread_id and proposer == session.actor_id:
                        if st.button(
                            "Open chat",
                            key=f"queue_open_chat_{pid}",
                            type="tertiary",
                            icon=":material/chat:",
                        ):
                            open_copilot_thread(thread_id)
                    else:
                        st.caption(f"From a chat by {humanize_actor_label(proposer)}")

                    if is_admin:
                        with st.container(horizontal=True, gap="small"):
                            st.button(
                                "Approve",
                                key=f"queue_approve_one_{pid}",
                                type="primary",
                                icon=":material/check:",
                                on_click=_decide_batch,
                                args=([pid], "approve"),
                            )
                            st.button(
                                "Reject",
                                key=f"queue_reject_one_{pid}",
                                type="secondary",
                                icon=":material/close:",
                                on_click=_decide_batch,
                                args=([pid], "reject"),
                            )

        if is_admin and len(pending) > 1:
            with st.container(horizontal=True, gap="small"):
                st.button(
                    f"Approve selected ({len(selected_ids)})",
                    key="queue_approve_selected",
                    type="primary",
                    icon=":material/done_all:",
                    disabled=not selected_ids,
                    on_click=_decide_batch,
                    args=(selected_ids, "approve"),
                )
                st.button(
                    f"Reject selected ({len(selected_ids)})",
                    key="queue_reject_selected",
                    type="secondary",
                    icon=":material/close:",
                    disabled=not selected_ids,
                    on_click=_decide_batch,
                    args=(selected_ids, "reject"),
                )

        perf_finish_if_active()

    _pending_fragment()


def _render_history_tab(
    session: DemoSession,
    hostname_map: dict[str, str],
) -> None:
    """Render decided action proposals with reviewer, channel, timestamps, and receipt links."""
    container = get_ui_container()
    all_props = list(container.store.list_proposals(session.company_id))
    decided = [p for p in all_props if str(p.get("status") or "") != "pending_approval"]

    if not decided:
        st.info("No decided actions yet for this company.", icon=":material/history:")
        return

    for prop in decided:
        pid = str(prop.get("proposal_id") or "")
        status = str(prop.get("status") or "")
        tool_label = humanize_tool_name(str(prop.get("tool_name") or ""))
        target_id = str(prop.get("target_id") or "")
        host = hostname_map.get(target_id)
        target_display = (
            f"{safe_md(host)} (`{target_id}`)" if host and host != target_id else f"`{target_id}`"
        )
        reviewer = humanize_actor_label(prop.get("reviewer_id"))
        via = str(prop.get("decided_via") or "—")
        reviewed_at = fmt_as_of_full(prop.get("reviewed_at")) if prop.get("reviewed_at") else "—"
        executed_at = fmt_as_of_full(prop.get("executed_at")) if prop.get("executed_at") else "—"
        receipt_id = _extract_receipt_id(prop)

        with st.container(border=True):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
                gap="small",
            ):
                st.markdown(f"**{tool_label}** · {target_display}")
                render_proposal_status_badge(status)

            if prop.get("rationale"):
                st.caption(safe_md(prop["rationale"]))

            meta = (
                f"Reviewer: **{reviewer}** · Via: `{via}` · "
                f"Reviewed: {reviewed_at} · Executed: {executed_at}"
            )
            if receipt_id:
                meta += f" · Receipt: `{receipt_id}`"
            st.caption(meta)

            if receipt_id:
                if st.button(
                    f"Filter outbox by {receipt_id}",
                    key=f"hist_outbox_{pid}",
                    type="tertiary",
                    icon=":material/outbox:",
                ):
                    st.session_state["approvals_outbox_receipt"] = receipt_id
                    st.rerun()


def _render_audit_tab(session: DemoSession) -> None:
    """Render hash-chain verification banner and filterable audit event timeline."""
    container = get_ui_container()
    ctx = make_tenant_context(session)

    # Hash chain verification from AuditService.verify()
    verify_res: dict[str, Any]
    if hasattr(container.audit_svc, "verify"):
        verify_res = dict(container.audit_svc.verify(ctx))
    else:
        verify_res = {"ok": True, "rows_checked": 0, "first_bad_seq": None}

    if verify_res.get("ok", False):
        checked = fmt_int(verify_res.get("rows_checked", 0))
        st.success(
            f"Audit chain verified ✓ ({checked} events)",
            icon=":material/verified:",
        )
    else:
        st.error(
            f"Audit chain verification failed at sequence #{verify_res.get('first_bad_seq')}.",
            icon=":material/gpp_bad:",
        )

    events: list[dict[str, Any]] = (
        list(container.audit_svc.list(ctx, limit=200))
        if hasattr(container.audit_svc, "list")
        else []
    )
    if not events:
        st.info("No audit events recorded yet for this company.", icon=":material/receipt_long:")
        return

    event_types = sorted({str(e.get("event_type") or "") for e in events if e.get("event_type")})
    thread_ids = sorted({str(e.get("thread_id") or "") for e in events if e.get("thread_id")})

    f1, f2 = st.columns(2, gap="small")
    with f1:
        sel_type = st.selectbox(
            "Filter by event type",
            options=["All"] + event_types,
            format_func=lambda x: "All event types" if x == "All" else humanize_audit_event(x),
            key="audit_filter_type",
        )
    with f2:
        sel_thread = st.selectbox(
            "Filter by chat thread",
            options=["All"] + thread_ids,
            format_func=lambda x: "All chats" if x == "All" else x,
            key="audit_filter_thread",
        )

    filtered = [
        e
        for e in reversed(events)
        if (sel_type == "All" or e.get("event_type") == sel_type)
        and (sel_thread == "All" or e.get("thread_id") == sel_thread)
    ]
    st.caption(f"Showing {fmt_int(len(filtered))} of {fmt_int(len(events))} audit events")

    for ev in filtered[:100]:
        ev_type = str(ev.get("event_type") or "")
        seq_no = ev.get("seq_no", "")
        actor = humanize_actor_label(ev.get("actor_id"))
        created = fmt_as_of_full(ev.get("created_at"))
        label = humanize_audit_event(ev_type)
        payload = ev.get("payload_json") or {}

        with st.container(border=True):
            with st.container(
                horizontal=True,
                horizontal_alignment="distribute",
                vertical_alignment="center",
                gap="small",
            ):
                st.markdown(f"**#{seq_no} · {label}**")
                st.caption(f"{actor} · {created}")
            if ev.get("thread_id"):
                st.caption(f"Chat: `{ev['thread_id']}` · Trace: `{ev.get('trace_id', '—')}`")
            with st.expander("Payload JSON", expanded=False):
                st.code(json.dumps(payload, indent=2, default=str), language="json")


def _format_email_preview(full_message: str, raw_preview: str) -> tuple[str, bool]:
    """Return `(clean_preview, was_truncated)` trimmed on a word boundary with ellipsis."""
    src = full_message.strip() or raw_preview.strip()
    if not src:
        return "", False
    # Treat either long full message or 240-char adapter preview cap as truncated
    max_len = 220
    if len(full_message.strip()) > max_len:
        cut = src[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return f"{cut or src[:max_len]}…", True
    if not full_message.strip() and len(raw_preview) >= 240:
        cut = raw_preview[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return f"{cut or raw_preview[:max_len]}…", True
    return src, False


def _display_email_subject(
    raw_subject: str,
    *,
    company_id: str,
    company_label: str,
    message_text: str,
    hostname_map: dict[str, str],
) -> str:
    """Replace raw `(company_id)` in displayed email subject with matched device hostname or org."""
    token = f"({company_id})"
    if token not in raw_subject:
        return raw_subject
    msg_l = message_text.lower()
    matched_host = next(
        (
            h
            for h in sorted(hostname_map.values(), key=len, reverse=True)
            if h and h.lower() in msg_l
        ),
        None,
    )
    replacement = matched_host or company_label.split("·")[0].strip() or company_id
    return raw_subject.replace(token, f"({replacement})")


def _render_outbox_card(
    row: dict[str, Any],
    hostname_map: dict[str, str],
    *,
    company_id: str,
    company_label: str,
    proposals_by_id: dict[str, dict[str, Any]],
) -> None:
    """Render one outbox record formatted for its destination integration channel."""
    channel = str(row.get("channel") or "ticketing")
    receipt_id = str(row.get("receipt_id") or "")
    created_at = fmt_as_of_full(row.get("created_at"))
    raw_payload = row.get("payload_json")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    ch_label, ch_icon = _CHANNEL_LABELS.get(channel, (channel.title(), ":material/outbox:"))
    full_email_msg = ""
    email_truncated = False

    with st.container(border=True):
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="small",
        ):
            st.markdown(f"{ch_icon} **{ch_label}** · Receipt `{receipt_id}`")
            st.caption(created_at)

        if channel == "ticketing":
            ci = str(payload.get("configuration_item") or "")
            ci_host = hostname_map.get(ci)
            ci_display = f"{ci_host} ({ci})" if ci_host and ci_host != ci else ci
            c1, c2, c3 = st.columns(3)
            with c1:
                st.caption("Priority")
                priority = str(payload.get("priority") or "3 - Moderate")
                render_status_badge("high" if "high" in priority.lower() else "medium", priority)
            with c2:
                st.caption("Configuration item (CI)")
                st.text(ci_display or "—")
            with c3:
                st.caption("Category")
                st.text(str(payload.get("category") or "endpoint_compliance"))
            st.markdown("**Short description**")
            st.text(str(payload.get("short_description") or ""))
            if payload.get("description"):
                st.markdown("**Details**")
                st.text(str(payload["description"]))

        elif channel == "procurement":
            dev_id = str(payload.get("device_id") or "")
            dev_host = hostname_map.get(dev_id)
            p1, p2, p3, p4 = st.columns(4)
            with p1:
                st.caption("SKU")
                st.text(str(payload.get("sku") or "—"))
            with p2:
                st.caption("Specification")
                st.text(str(payload.get("spec") or "—"))
            with p3:
                st.caption("Quantity")
                st.text(str(payload.get("quantity") or 1))
            with p4:
                st.caption("Ship-to employee")
                st.text(str(payload.get("ship_to_employee_id") or "—"))
            st.caption(
                f"Target device: {safe_md(dev_host or dev_id)} (`{safe_code(dev_id) or '—'}`)"
            )
            if payload.get("justification"):
                st.text(str(payload["justification"]))

        elif channel == "email":
            prop_row = proposals_by_id.get(str(row.get("proposal_id") or ""))
            prop_args = (prop_row or {}).get("arguments_json") or (prop_row or {}).get("arguments")
            if isinstance(prop_args, dict) and prop_args.get("message"):
                full_email_msg = str(prop_args["message"])
            raw_preview = str(payload.get("body_preview") or "")
            preview_txt, email_truncated = _format_email_preview(full_email_msg, raw_preview)
            raw_subject = str(payload.get("subject") or "Action needed on your managed device")
            display_subject = _display_email_subject(
                raw_subject,
                company_id=company_id,
                company_label=company_label,
                message_text=full_email_msg or raw_preview,
                hostname_map=hostname_map,
            )
            st.markdown(f"**To:** `{safe_code(payload.get('recipient_employee_id')) or '—'}`")
            st.markdown("**Subject**")
            # st.text never renders markdown; pass plain text so no escapes appear literally (S5)
            st.text(display_subject)
            st.markdown("**Message preview**")
            with st.container(border=True):
                st.markdown(safe_md(preview_txt))

        else:  # asset_management / replacement
            dev_id = str(payload.get("device_id") or "")
            dev_host = hostname_map.get(dev_id)
            a1, a2, a3 = st.columns(3)
            with a1:
                st.caption("Action")
                st.text("Flag for replacement")
            with a2:
                st.caption("Device")
                st.text(f"{dev_host} ({dev_id})" if dev_host else dev_id)
            with a3:
                st.caption("Priority")
                render_status_badge("high", str(payload.get("priority") or "High").title())
            if payload.get("reason"):
                st.markdown("**Justification**")
                st.text(str(payload["reason"]))

        exp_label = (
            "Full message & raw payload JSON"
            if (channel == "email" and full_email_msg and email_truncated)
            else "Raw payload JSON"
        )
        with st.expander(exp_label, expanded=False):
            if channel == "email" and full_email_msg:
                st.markdown("**Full message**")
                st.markdown(safe_md(full_email_msg))
                st.markdown("**Raw payload JSON**")
            st.code(json.dumps(payload, indent=2, default=str), language="json")


def _render_outbox_tab(
    session: DemoSession,
    hostname_map: dict[str, str],
) -> None:
    """Render integration outbox entries with channel-specific previews and mock notice."""
    container = get_ui_container()
    st.info(
        "Mock integration: this is the exact payload that would be sent to ServiceNow, "
        "procurement, email, or asset lifecycle systems.",
        icon=":material/hub:",
    )

    outbox_rows = (
        list(container.store.list_outbox(session.company_id, limit=100))
        if hasattr(container.store, "list_outbox")
        else []
    )
    if not outbox_rows:
        st.caption(
            "No outbound payloads sent yet. Approve a staged action to inspect its exact payload."
        )
        return

    receipt_filter = str(st.session_state.get("approvals_outbox_receipt") or "").strip()
    if receipt_filter:
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.badge(f"Filtered by receipt: {receipt_filter}", icon=":material/filter_alt:")
            if st.button("Clear filter", key="outbox_clear_receipt_filter", type="tertiary"):
                st.session_state.pop("approvals_outbox_receipt", None)
                st.rerun()

    ch_options = ["All", "ticketing", "procurement", "email", "asset_management"]
    sel_channel = st.selectbox(
        "Channel",
        options=ch_options,
        format_func=lambda c: "All channels" if c == "All" else _CHANNEL_LABELS.get(c, (c, ""))[0],
        key="outbox_channel_filter",
    )

    visible = [
        r
        for r in outbox_rows
        if (sel_channel == "All" or r.get("channel") == sel_channel)
        and (not receipt_filter or str(r.get("receipt_id") or "") == receipt_filter)
    ]
    if not visible:
        st.caption("No integration outbox entries match the current filter.")
        return

    proposals_by_id = {
        str(p.get("proposal_id") or ""): p
        for p in container.store.list_proposals(session.company_id)
        if p.get("proposal_id")
    }
    for row in visible:
        _render_outbox_card(
            row,
            hostname_map,
            company_id=session.company_id,
            company_label=session.label or session.company_id,
            proposals_by_id=proposals_by_id,
        )


def render_approvals_page() -> None:
    """Render the 4-tab Approvals & Activity page."""
    session = require_session()
    as_of_iso = cached_telemetry_as_of(session.company_id)
    hostname_map = get_hostname_map(session.company_id, as_of_iso)

    st.markdown("## :material/fact_check: Approvals & Activity")
    st.caption(
        "Review company-wide pending approvals, verify the tamper-evident audit chain, "
        "and inspect exact outbound integration payloads."
    )

    tab_pending, tab_history, tab_audit, tab_outbox = st.tabs(
        ["Pending approvals", "History", "Audit log", "Integration outbox"]
    )

    with tab_pending:
        _render_pending_tab(session, hostname_map)
    with tab_history:
        _render_history_tab(session, hostname_map)
    with tab_audit:
        _render_audit_tab(session)
    with tab_outbox:
        _render_outbox_tab(session, hostname_map)


def main() -> None:
    safe_page_run(render_approvals_page)


if __name__ == "__main__":
    main()
