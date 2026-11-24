"""Main Streamlit multipage entrypoint for Fleet Copilot (`st.navigation` with 6 pages)."""

from __future__ import annotations

import streamlit as st

from fleet_copilot.interfaces.ui.components.sidebar import render_sidebar
from fleet_copilot.interfaces.ui.pages.approvals import render_approvals_page
from fleet_copilot.interfaces.ui.pages.copilot import render_copilot_page
from fleet_copilot.interfaces.ui.pages.fleet import render_fleet_page
from fleet_copilot.interfaces.ui.pages.home import render_home_page
from fleet_copilot.interfaces.ui.pages.how_it_works import render_how_it_works_page
from fleet_copilot.interfaces.ui.pages.sign_in import render_sign_in_page
from fleet_copilot.interfaces.ui.state import (
    get_session,
    perf_enabled,
    register_pages,
    safe_page_run,
)
from fleet_copilot.interfaces.ui.theme import inject_base_css


def run_app() -> None:
    """Configure page chrome, register navigation pages (hiding Sign in after auth), and run."""
    st.set_page_config(
        page_title="Fleet Copilot",
        page_icon=":material/devices:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_base_css()
    perf_enabled()

    signed_in = get_session() is not None

    pages = {
        "sign_in": st.Page(
            lambda: safe_page_run(render_sign_in_page),
            title="Sign in",
            icon=":material/login:",
            url_path="sign-in",
            default=not signed_in,
        ),
        "home": st.Page(
            lambda: safe_page_run(render_home_page),
            title="Home",
            icon=":material/home:",
            url_path="home",
            default=signed_in,
        ),
        "copilot": st.Page(
            lambda: safe_page_run(render_copilot_page),
            title="Copilot",
            icon=":material/chat:",
            url_path="copilot",
        ),
        "approvals": st.Page(
            lambda: safe_page_run(render_approvals_page),
            title="Approvals & Activity",
            icon=":material/fact_check:",
            url_path="approvals",
        ),
        "fleet": st.Page(
            lambda: safe_page_run(render_fleet_page),
            title="Fleet",
            icon=":material/laptop_mac:",
            url_path="fleet",
        ),
        "how_it_works": st.Page(
            lambda: safe_page_run(render_how_it_works_page),
            title="How it works",
            icon=":material/psychology:",
            url_path="how-it-works",
        ),
    }
    register_pages(pages)

    if signed_in:
        ordered_pages = [
            pages["home"],
            pages["copilot"],
            pages["approvals"],
            pages["fleet"],
            pages["how_it_works"],
        ]
        nav_position = "sidebar"
    else:
        # Register all routes even when signed out (hidden nav) so browser refresh
        # on /copilot, /fleet, /approvals, /home, or /how-it-works redirects cleanly to Sign in
        # instead of showing Streamlit's "Page not found" modal.
        ordered_pages = [
            pages["sign_in"],
            pages["home"],
            pages["copilot"],
            pages["approvals"],
            pages["fleet"],
            pages["how_it_works"],
        ]
        nav_position = "hidden"

    active_page = st.navigation(ordered_pages, position=nav_position)
    active_slug = next((slug for slug, pg in pages.items() if pg == active_page), "")
    render_sidebar(active_slug=active_slug)
    active_page.run()


run_app()
