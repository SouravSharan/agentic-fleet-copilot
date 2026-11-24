"""Sign in page: access code validation, persona cards from config/personas.yaml, lockout."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.services import cached_personas, get_ui_container
from fleet_copilot.interfaces.ui.state import (
    AUTH_ATTEMPTS_KEY,
    MAX_AUTH_ATTEMPTS,
    get_session,
    safe_page_run,
    set_session,
    switch_to_page,
)
from fleet_copilot.interfaces.ui.theme import fmt_role
from fleet_copilot.services.demo_auth import DemoAuthError, sign_in

_GENERIC_SIGNIN_ERROR = "Invalid access code or persona."


def render_sign_in_page() -> None:
    """Render the demo Sign in screen with access-code field and 4 persona cards."""
    existing = get_session()
    if existing is not None:
        st.info(
            f"Currently signed in as **{existing.label or existing.company_id}** "
            f"({fmt_role(existing.role)}).",
            icon=":material/check_circle:",
        )
        if st.button("Continue to Home", type="primary", icon=":material/home:"):
            switch_to_page("home")
        st.divider()

    st.markdown("## :material/devices: Fleet Copilot")
    st.markdown(
        "AI-assisted fleet telemetry intelligence, proactive health briefings, and "
        "policy-gated IT operations."
    )

    attempts = int(st.session_state.get(AUTH_ATTEMPTS_KEY, 0))
    locked = attempts >= MAX_AUTH_ATTEMPTS

    if locked:
        st.error(
            "Too many attempts. Sign-in is disabled for this session.",
            icon=":material/lock:",
        )

    def _clear_signin_error() -> None:
        st.session_state.pop("sign_in_error", None)

    code = st.text_input(
        "Access code",
        type="password",
        key="sign_in_access_code",
        placeholder="Enter your demo access code",
        disabled=locked,
        on_change=_clear_signin_error,
    )

    last_err = st.session_state.get("sign_in_error")
    if last_err and not locked:
        st.error(str(last_err), icon=":material/error:")

    st.markdown("#### Choose a demo persona")
    st.caption(
        "Each persona maps to an isolated company tenant and role. "
        "Select a card to sign in and open that company's fleet briefing."
    )

    personas = list(cached_personas().values())
    container = get_ui_container()

    cols = st.columns(2, gap="medium")
    for idx, spec in enumerate(personas):
        with cols[idx % 2]:
            with st.container(border=True):
                head_left, head_right = st.columns([3, 1.4])
                with head_left:
                    st.markdown(f"**{spec.label}**")
                with head_right:
                    is_admin = spec.role == "it_admin"
                    st.badge(
                        fmt_role(spec.role),
                        icon=(
                            ":material/admin_panel_settings:"
                            if is_admin
                            else ":material/visibility:"
                        ),
                        color="blue" if is_admin else "gray",
                    )
                st.caption(spec.what_to_try)
                btn_label = f"Sign in as {spec.label}"
                if st.button(
                    btn_label,
                    key=f"persona_card_{spec.id}",
                    type="primary" if idx == 0 else "secondary",
                    icon=":material/login:",
                    width="stretch",
                    disabled=locked,
                ):
                    if not code.strip():
                        st.session_state["sign_in_error"] = "Enter your access code first."
                        st.rerun()
                    try:
                        session = sign_in(code, spec.id, settings=container.settings)
                    except (DemoAuthError, Exception):  # noqa: BLE001
                        new_attempts = attempts + 1
                        st.session_state[AUTH_ATTEMPTS_KEY] = new_attempts
                        if new_attempts >= MAX_AUTH_ATTEMPTS:
                            st.session_state["sign_in_error"] = "Too many attempts."
                        else:
                            st.session_state["sign_in_error"] = _GENERIC_SIGNIN_ERROR
                        st.rerun()
                    else:
                        st.session_state.pop("sign_in_error", None)
                        set_session(session)
                        switch_to_page("home")
                        st.rerun()

    st.caption(
        ":material/info: Demo environment: simulated sign-in, isolated per-company telemetry, "
        "and mocked ticketing/procurement/email integrations."
    )


def main() -> None:
    safe_page_run(render_sign_in_page)


if __name__ == "__main__":
    main()
