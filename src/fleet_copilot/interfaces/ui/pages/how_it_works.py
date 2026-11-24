"""How it works page (architecture, guardrails playground, evals, and examples arrive next)."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.state import require_session, safe_page_run, switch_to_page


def render_how_it_works_page() -> None:
    session = require_session()
    st.title("How it works")
    st.caption(f"Workspace: {session.label or session.company_id}")
    st.info(
        "The architecture diagram, live Guardrails playground, evaluation report, recorded "
        "Examples gallery, and demo reset controls are coming next.",
        icon=":material/psychology:",
    )
    if st.button("Open Copilot", icon=":material/chat:", key="how_stub_go_copilot"):
        switch_to_page("copilot")


def main() -> None:
    safe_page_run(render_how_it_works_page)


if __name__ == "__main__":
    main()
