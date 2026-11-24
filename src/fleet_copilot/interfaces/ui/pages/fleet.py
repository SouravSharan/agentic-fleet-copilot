"""Fleet & Saved charts page (device explorer and saved charts arrive in the next milestone)."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.state import require_session, safe_page_run, switch_to_page


def render_fleet_page() -> None:
    session = require_session()
    st.title("Fleet & Saved charts")
    st.caption(f"Workspace: {session.label or session.company_id}")
    st.info(
        "The interactive fleet device inventory, per-device telemetry profiles, and personal "
        "Saved charts board are coming next. You can already pin charts from Copilot answers.",
        icon=":material/laptop_mac:",
    )
    if st.button("Back to Home briefing", icon=":material/home:", key="fleet_stub_go_home"):
        switch_to_page("home")


def main() -> None:
    safe_page_run(render_fleet_page)


if __name__ == "__main__":
    main()
