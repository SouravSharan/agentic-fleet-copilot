"""Phase D2c UI security hygiene (H1, H2) and walkthrough polish AppTests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from streamlit.testing.v1 import AppTest

from fleet_copilot.domain.models import SoftwareRecord
from fleet_copilot.interfaces.ui.services import UIContainer, set_ui_container_override
from fleet_copilot.services.briefing import BriefingItem
from tests.graph.fakes import AS_OF
from tests.ui.test_d2a_smoke import _demo_session_for, _make_offline_container


def _app_render_briefing_card(item_dict, is_admin):  # type: ignore[no-untyped-def]
    from fleet_copilot.interfaces.ui.pages.home import _render_briefing_card
    from fleet_copilot.services.briefing import BriefingItem

    item = BriefingItem.model_validate(item_dict)
    _render_briefing_card(item, is_admin=is_admin, key_prefix="sec")


def _app_render_answer_card(resp_dict, session, hostname_map):  # type: ignore[no-untyped-def]
    from fleet_copilot.interfaces.ui.components.answer_card import render_answer_card

    render_answer_card(
        resp_dict,
        session=session,
        hostname_map=hostname_map,
        turn_id="turn_sec_test",
    )


def _app_render_sidebar():  # type: ignore[no-untyped-def]
    from fleet_copilot.interfaces.ui.components.sidebar import render_sidebar

    render_sidebar(active_slug="copilot")


@pytest.fixture()
def offline_container() -> Iterator[UIContainer]:
    container = _make_offline_container()
    set_ui_container_override(container)
    try:
        yield container
    finally:
        set_ui_container_override(None)
        container.turn_jobs.shutdown(wait=False)


def test_h1_home_briefing_title_escapes_html_tags(offline_container: UIContainer) -> None:
    """H1: briefing card title with raw HTML tags renders as literal escaped text, never HTML."""
    _ = offline_container
    hostile_title = "<b>x</b><img src=x onerror=alert(1)>"
    item = BriefingItem(
        id="brief:software",
        severity="high",
        category="software",
        title=hostile_title,
        explanation="Detected ![img](https://evil.example/px) and [click](https://evil.example)",
        device_ids=["MT7PJB7N5LRE"],
        hostnames=["acme-macbook-4"],
        suggested_prompt="Check software",
        recency="2026-06-12",
    )

    at = AppTest.from_function(
        _app_render_briefing_card,
        args=(item.model_dump(mode="json"), True),
        default_timeout=15,
    )
    at.run()
    assert not at.exception

    md_values = [m.value for m in at.markdown]
    # Escaped HTML must be present; live <b> or <img tags must NEVER appear
    assert any("&lt;b&gt;x&lt;/b&gt;&lt;img src=x onerror=alert(1)&gt;" in v for v in md_values)
    for v in md_values:
        assert "<b>" not in v
        assert "<img" not in v
        assert "](https://evil.example" not in v


def test_h2_answer_card_and_sidebar_neutralise_markdown_images_and_links(
    offline_container: UIContainer,
) -> None:
    """H2: untrusted model summary, claims, and sidebar chat titles never render links/images."""
    session = _demo_session_for("acme_admin")
    resp = {
        "turn_id": "turn_sec_test",
        "trace_id": "trace-sec",
        "status": "completed",
        "answer": {
            "summary": (
                "See ![x](https://evil.example/a) and [click](https://evil.example/b) "
                "plus https://evil.example/c and `disk_free_pct < 15` [E1]"
            ),
            "claims": [
                {
                    "text": "Host ![p](https://evil.example/p) has <script>bad</script> [E1]",
                    "citations": ["E1"],
                }
            ],
            "caveats": ["Note [link](https://evil.example/note) and <b>raw</b>"],
        },
        "citations": [
            {
                "handle": "E1",
                "snapshot_id": "snap_mt7p_latest",
                "device_id": "MT7PJB7N5LRE",
                "hostname": "acme-macbook-4",
                "collected_at": AS_OF.isoformat(),
                "metric": "disk_free_pct",
                "value": 2.0,
                "unit": "%",
                "label": "Low disk reading",
            }
        ],
        "charts": [],
        "queries": [],
        "insights": [
            {
                "severity": "high",
                "finding": "Found ![bad](https://evil.example/ins) on host",
                "explanation": "Visit [evil](https://evil.example/exp) now",
            }
        ],
        "proposals": [],
        "verdicts": [],
        "warnings": [],
        "telemetry_as_of": AS_OF.isoformat(),
    }

    at = AppTest.from_function(
        _app_render_answer_card,
        args=(resp, session, {"MT7PJB7N5LRE": "acme-macbook-4"}),
        default_timeout=15,
    )
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception

    all_text = [m.value for m in at.markdown] + [c.value for c in at.caption]
    for val in all_text:
        assert "](https://evil.example" not in val
        assert "<script>" not in val
        assert "<b>" not in val
    # Code span and citation handle preserved cleanly in summary
    assert any("`disk_free_pct < 15`" in m.value and "[E1]" in m.value for m in at.markdown)
    # Bare URL neutralized in backticks
    assert any("`https://evil.example/c`" in m.value for m in at.markdown)

    # Sidebar chat title button label also neutralizes markdown images/links
    tid = f"{session.company_id}:thr_evil_title"
    offline_container.store.ensure_thread(
        session.company_id,
        tid,
        session.actor_id,
        title="![x](https://evil.example/s) [bad](https://evil.example/t)",
    )
    at_sb = AppTest.from_function(_app_render_sidebar, default_timeout=15)
    at_sb.session_state["demo_session"] = session
    at_sb.run()
    assert not at_sb.exception
    for b in at_sb.button:
        assert "](https://evil.example" not in str(b.label)
        assert "![" not in str(b.label)


def test_h1_telemetry_software_name_with_html_is_escaped_on_home(
    offline_container: UIContainer,
) -> None:
    """Hostile software name from telemetry rendered on Home briefing has zero active HTML tags."""
    offline_container.telemetry_repo._software.append(
        SoftwareRecord(
            snapshot_id="snap_mt7p_latest",
            device_id="MT7PJB7N5LRE",
            employee_id="emp-acme-1003",
            hostname="acme-macbook-4",
            collected_at=AS_OF,
            software_name="<a href='https://evil.example'>Re-auth</a>",
            version="1.0",
            publisher="Unknown",
        )
    )
    session = _demo_session_for("acme_admin")

    def _run_home() -> None:
        from fleet_copilot.interfaces.ui.pages.home import render_home_page

        render_home_page()

    at = AppTest.from_function(_run_home, default_timeout=20)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception
    for m in at.markdown:
        assert "<a href=" not in m.value
