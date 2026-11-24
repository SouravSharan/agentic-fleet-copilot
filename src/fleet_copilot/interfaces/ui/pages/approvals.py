"""Approvals & Activity page (full queue, audit chain, and outbox arrive in the next milestone)."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.state import require_session, safe_page_run, switch_to_page


def render_approvals_page() -> None:
    session = require_session()
    st.title("Approvals & Activity")
    st.caption(f"Workspace: {session.label or session.company_id}")
    st.info(
        "The full shared approvals queue, hash-chained audit timeline, and integration outbox "
        "preview are coming next. In the meantime, approve or review actions directly inside "
        "the Copilot conversation.",
        icon=":material/fact_check:",
    )
    if st.button("Go to Copilot", icon=":material/chat:", key="approvals_stub_go_copilot"):
        switch_to_page("copilot")


def main() -> None:
    safe_page_run(render_approvals_page)


if __name__ == "__main__":
    main()
