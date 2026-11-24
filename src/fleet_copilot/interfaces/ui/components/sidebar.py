"""Always-visible sidebar chrome + Copilot chat history rail under navigation."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.services import (
    cached_telemetry_as_of,
    cached_thread_list,
    get_ui_container,
)
from fleet_copilot.interfaces.ui.state import (
    bump_thread_list_version,
    clear_session,
    get_session,
    make_tenant_context,
    perf_mark_start,
)
from fleet_copilot.interfaces.ui.theme import fmt_as_of_date, fmt_relative_time, fmt_role
from fleet_copilot.services.demo_auth import DemoSession


def _render_copilot_history_in_sidebar(session: DemoSession) -> None:
    """Render New chat, search, and actor's saved threads in the app sidebar."""
    container = get_ui_container()

    def _on_select_thread(target_tid: str | None) -> None:
        perf_mark_start("switch_thread")
        st.session_state["active_thread_id"] = target_tid
        st.session_state["selected_turn_id"] = None

    st.divider()
    st.markdown("##### :material/forum: Chats")
    st.button(
        "New chat",
        key="copilot_new_chat_btn",
        type="primary",
        icon=":material/add:",
        width="stretch",
        on_click=_on_select_thread,
        args=(None,),
    )

    st.text_input(
        "Search chats",
        key="thread_search_query",
        placeholder="Search by title...",
        label_visibility="collapsed",
    )

    search_q = str(st.session_state.get("thread_search_query") or "").strip() or None
    thread_ver = int(st.session_state.get("thread_list_version", 0))
    threads = cached_thread_list(
        session.company_id,
        session.actor_id,
        session.role,
        thread_ver,
        search_q,
    )

    if not threads:
        st.caption("No saved conversations yet.")
        return

    active_tid = st.session_state.get("active_thread_id")
    for t in threads:
        tid = str(t["thread_id"])
        title = str(t.get("title") or "Untitled chat")
        rel_time = fmt_relative_time(t.get("updated_at"))
        is_active = tid == active_tid

        row_main, row_menu = st.columns([4.4, 1.2], gap="small")
        with row_main:
            short = title if len(title) <= 28 else title[:27].rstrip() + "…"
            btn_label = short + (f" · {rel_time}" if rel_time else "")
            st.button(
                btn_label,
                key=f"thread_select_{tid}",
                type="primary" if is_active else "tertiary",
                help=title,
                width="stretch",
                on_click=_on_select_thread,
                args=(tid,),
            )
        with row_menu:
            with st.popover(
                "",
                icon=":material/more_horiz:",
                type="tertiary",
                key=f"thread_menu_{tid}",
            ):
                new_title = st.text_input(
                    "Rename chat",
                    value=title,
                    key=f"rename_input_{tid}",
                )
                if st.button("Save title", key=f"rename_save_{tid}", width="stretch"):
                    if new_title.strip():
                        ctx_r = make_tenant_context(session)
                        container.guarded.rename_thread(ctx_r, tid, new_title.strip())
                        bump_thread_list_version()
                        st.rerun()
                if st.button(
                    "Hide chat",
                    key=f"hide_thread_{tid}",
                    icon=":material/visibility_off:",
                    width="stretch",
                ):
                    ctx_h = make_tenant_context(session)
                    container.guarded.hide_thread(ctx_h, tid, hidden=True)
                    if st.session_state.get("active_thread_id") == tid:
                        st.session_state["active_thread_id"] = None
                        st.session_state["selected_turn_id"] = None
                    bump_thread_list_version()
                    st.rerun()


def render_sidebar(*, active_slug: str = "") -> None:
    """Render the persistent Fleet Copilot sidebar with session metadata, chats, and Sign out."""
    session = get_session()
    with st.sidebar:
        st.markdown("### :material/devices: Fleet Copilot")
        if session is None:
            st.caption("Sign in with a demo access code to inspect a company's fleet.")
            st.divider()
            st.caption("Demo: simulated sign-in · integrations are mocked")
            return

        container = get_ui_container()
        try:
            as_of_iso = cached_telemetry_as_of(session.company_id)
        except Exception:  # noqa: BLE001
            as_of_iso = ""
        mode = container.guarded.mode(session.company_id)

        role_icon = (
            ":material/admin_panel_settings:"
            if session.role == "it_admin"
            else ":material/visibility:"
        )
        role_color = "blue" if session.role == "it_admin" else "gray"

        with st.container(border=True):
            st.markdown(f"**{session.label or session.company_id}**")
            st.badge(fmt_role(session.role), icon=role_icon, color=role_color)
            if as_of_iso:
                st.caption(f":material/calendar_today: Data as of {fmt_as_of_date(as_of_iso)}")

        if mode == "live":
            st.badge("Live AI", icon=":material/bolt:", color="green")
        else:
            st.badge(
                "Examples only (live budget used)",
                icon=":material/menu_book:",
                color="gray",
            )

        if active_slug == "copilot":
            _render_copilot_history_in_sidebar(session)

        st.divider()
        st.caption("Demo: simulated sign-in · integrations are mocked")
        if st.button(
            "Sign out",
            key="sidebar_sign_out",
            icon=":material/logout:",
            width="stretch",
        ):
            clear_session()
            st.rerun()
