"""Always-visible sidebar chrome: identity chip, data timestamp, AI mode, and Sign out."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.services import cached_telemetry_as_of, get_ui_container
from fleet_copilot.interfaces.ui.state import clear_session, get_session, switch_to_page
from fleet_copilot.interfaces.ui.theme import fmt_as_of_date, fmt_role


def render_sidebar() -> None:
    """Render the persistent Fleet Copilot sidebar with session metadata and mode status."""
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

        st.divider()
        st.caption("Demo: simulated sign-in · integrations are mocked")
        if st.button(
            "Sign out",
            key="sidebar_sign_out",
            icon=":material/logout:",
            width="stretch",
        ):
            clear_session()
            switch_to_page("sign_in")
            st.rerun()
