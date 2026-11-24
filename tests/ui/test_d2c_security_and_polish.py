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
    # Active HTML tags, markdown links, and images must NEVER appear
    assert any("x" in v for v in md_values)
    for v in md_values:
        assert "<b>" not in v
        assert "<img" not in v
        assert "onerror" not in v
        assert "![" not in v
        assert "](" not in v
        assert "href" not in v


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
        assert "](" not in val
        assert "![" not in val
        assert "<script>" not in val
        assert "<img" not in val
        assert "<b>" not in val
        assert "href" not in val
    # Code span and citation handle preserved cleanly in summary (CommonMark-escaped [E1])
    assert any("`disk_free_pct < 15`" in m.value and r"\[E1\]" in m.value for m in at.markdown)
    # Bare URL neutralized in backticks
    assert any("`https://evil.example/c`" in m.value for m in at.markdown)

    # Sidebar chat title button label truncates BEFORE sanitizing so code spans never leak open
    tid = f"{session.company_id}:thr_evil_title"
    offline_container.store.ensure_thread(
        session.company_id,
        tid,
        session.actor_id,
        title="Check `code_span_that_gets_cut_midway` ![x](https://evil.example/s)",
    )
    at_sb = AppTest.from_function(_app_render_sidebar, default_timeout=15)
    at_sb.session_state["demo_session"] = session
    at_sb.run()
    assert not at_sb.exception
    for b in at_sb.button:
        lbl = str(b.label)
        assert "](" not in lbl
        assert "![" not in lbl
        assert "<img" not in lbl
        assert "href" not in lbl
        # An unmatched backtick cut by 28-char truncation must be backslash-escaped, not open
        if "code_span" in lbl:
            assert r"\`" in lbl


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


# ---------------------------------------------------------------------------
# Phase D2c walkthrough polish tests (L2, L3, W1-W11)
# ---------------------------------------------------------------------------


def test_w1_fleet_row_selection_updates_detail_panel_and_picker(
    offline_container: UIContainer,
) -> None:
    """W1: selecting row 1 in Fleet table switches selected_device_id & detail panel to row 1."""
    _ = offline_container
    session = _demo_session_for("acme_admin")

    def _run_fleet() -> None:
        from fleet_copilot.interfaces.ui.pages.fleet import render_fleet_page

        render_fleet_page()

    at = AppTest.from_function(_run_fleet, default_timeout=20)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception
    # Row 0 sorted alphabetically is acme-macbook-4; row 1 is acme-thinkpad-8
    assert at.session_state["selected_device_id"] == "MT7PJB7N5LRE"
    assert any("acme-macbook-4" in s.value for s in at.subheader)

    # Simulate selecting row 1 in the single-row selectable st.dataframe
    at.session_state["fleet_devices_table"] = {
        "selection": {"rows": [1], "columns": [], "cells": []}
    }
    at.run()
    assert not at.exception
    assert at.session_state["selected_device_id"] == "8NM23J95R5I6"
    assert at.session_state["fleet_device_picker"] == "8NM23J95R5I6"
    assert any("acme-thinkpad-8" in s.value for s in at.subheader)


def test_l3_fleet_compliance_heatmap_humanizes_check_ids() -> None:
    """L3: Fleet compliance heatmap maps raw check IDs to plain English."""
    from fleet_copilot.interfaces.ui.pages.fleet import _compliance_timeline_spec

    spec = _compliance_timeline_spec(
        [
            {"date": "2026-06-12", "check_id": "os_up_to_date", "status": "fail"},
            {"date": "2026-06-12", "check_id": "disk_encryption", "status": "pass"},
            {"date": "2026-06-12", "check_id": "screen_lock", "status": "pass"},
        ]
    )
    assert spec is not None
    checks = {v["check"] for v in spec["data"]["values"]}
    assert checks == {"OS up to date", "Disk encryption", "Screen lock"}


def test_w2_architecture_diagrams_theme_aware_and_readable() -> None:
    """W2: Graphviz DOT sets transparent bg, font size >= 12, and theme-contrasting edge fonts."""
    from fleet_copilot.interfaces.ui.pages.how_it_works import _agent_graph_dot, _system_graph_dot

    dark_agent = _agent_graph_dot({"load_context", "planner"}, dark=True)
    light_agent = _agent_graph_dot({"load_context", "planner"}, dark=False)
    dark_sys = _system_graph_dot(dark=True)

    for dot in (dark_agent, light_agent, dark_sys):
        assert 'bgcolor="transparent"' in dot
        assert "fontsize=12" in dot
        assert "rankdir=TB" in dot
    assert 'fontcolor="#E2E8F0"' in dark_agent
    assert 'fontcolor="#E2E8F0"' in dark_sys
    assert 'fontcolor="#1E293B"' in light_agent


def test_w3_examples_gallery_defaults_to_showcase_and_shows_question_and_grade(
    offline_container: UIContainer,
) -> None:
    """W3: Examples defaults to ins-acme-battery (Passed) and shows question + Passed/Missed."""
    _ = offline_container
    session = _demo_session_for("acme_admin")

    def _run_how() -> None:
        from fleet_copilot.interfaces.ui.pages.how_it_works import render_how_it_works_page

        render_how_it_works_page()

    at = AppTest.from_function(_run_how, default_timeout=25)
    at.session_state["demo_session"] = session
    at.session_state["how_it_works_default_tab"] = "Examples"
    at.run()
    assert not at.exception
    assert at.session_state["examples_group_picker"] == "Insights"
    assert at.session_state["examples_case_picker"] == "ins-acme-battery"
    assert any("Question asked:" in m.value for m in at.markdown)

    # Switching to Q&A -> qa-acme-desktop-battery shows Missed badge and reason
    at.selectbox(key="examples_group_picker").select("Q&A").run()
    assert not at.exception
    at.selectbox(key="examples_case_picker").select("qa-acme-desktop-battery").run()
    assert not at.exception
    assert any("Strict device-ID set grader expected" in c.value for c in at.caption)


def test_w4_home_mini_charts_software_table_and_compliance_heatmap() -> None:
    """W4: Software omits meaningless chart, compliance renders heatmap, axes use short titles."""
    from fleet_copilot.interfaces.ui.pages.home import _CATEGORY_CHART_Y_TITLE, _mini_chart_spec

    assert _CATEGORY_CHART_Y_TITLE["battery"] == "Cycles"
    assert _CATEGORY_CHART_Y_TITLE["disk"] == "Disk %"
    assert _CATEGORY_CHART_Y_TITLE["ram"] == "RAM %"

    sw_item = BriefingItem(
        id="brief:software",
        severity="medium",
        category="software",
        title="1 device with non-baseline software",
        explanation="Found uTorrent",
        device_ids=["MT7PJB7N5LRE"],
        hostnames=["acme-macbook-4"],
        chart_series=[{"date": "2026-06-12", "value": 1.0, "device_id": "MT7PJB7N5LRE"}],
        suggested_prompt="Check software",
    )
    assert _mini_chart_spec(sw_item) is None

    drift_item = BriefingItem(
        id="brief:compliance_drift",
        severity="high",
        category="compliance_drift",
        title="1 device drifted into compliance failure",
        explanation="screen_lock failed",
        device_ids=["MT7PJB7N5LRE"],
        hostnames=["acme-macbook-4"],
        chart_series=[
            {"date": "2026-06-10", "value": 1.0, "device_id": "MT7PJB7N5LRE"},
            {"date": "2026-06-11", "value": 0.0, "device_id": "MT7PJB7N5LRE"},
        ],
        suggested_prompt="Check drift",
    )
    hm = _mini_chart_spec(drift_item)
    assert hm is not None
    assert hm["mark"]["type"] == "rect"
    assert {v["status"] for v in hm["data"]["values"]} == {"pass", "fail"}


def test_w5_w6_w7_chart_title_hostname_and_device_record_evidence_in_answer_and_inspector(
    offline_container: UIContainer,
) -> None:
    """W5/W6/W7: Answer chart uses hostname, device_state renders Device record with current:."""
    _ = offline_container
    session = _demo_session_for("acme_admin")
    resp = {
        "turn_id": "turn_w56",
        "trace_id": "trace-w56",
        "status": "completed",
        "answer": {
            "summary": "acme-thinkpad-8 needs battery service.",
            "claims": [{"text": "Device needs service [E1]", "citations": ["E1"]}],
        },
        "citations": [
            {
                "handle": "E1",
                "snapshot_id": "snap_8nm2_latest",
                "device_id": "8NM23J95R5I6",
                "hostname": "acme-thinkpad-8",
                "collected_at": AS_OF.isoformat(),
                "metric": "device_state",
                "value": "laptop",
                "unit": None,
                "label": "acme-thinkpad-8 (laptop)",
            }
        ],
        "charts": [
            {
                "chart_id": "chart_w5_test",
                "handle": "C1",
                "title": "8NM23J95R5I6 · Battery",
                "vega_lite_spec": {
                    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                    "data": {"values": [{"date": "2026-06-12", "value": 1157}]},
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "date", "type": "temporal"},
                        "y": {"field": "value", "type": "quantitative", "title": "Cycles"},
                    },
                },
            }
        ],
        "queries": [],
        "insights": [],
        "proposals": [],
        "verdicts": [],
        "warnings": [],
        "telemetry_as_of": AS_OF.isoformat(),
    }

    def _run_answer_and_insp(resp_in, sess_in):  # type: ignore[no-untyped-def]
        import streamlit as st

        from fleet_copilot.interfaces.ui.components.answer_card import render_answer_card
        from fleet_copilot.interfaces.ui.components.inspector import render_inspector_panel

        hmap = {"8NM23J95R5I6": "acme-thinkpad-8"}
        summaries = {
            "8NM23J95R5I6": {
                "hostname": "acme-thinkpad-8",
                "model": "ThinkPad X1 Carbon",
                "form_factor": "laptop",
                "battery_present": True,
                "battery_condition": "Service Recommended",
                "battery_cycles": 1157,
            }
        }
        st.session_state["_active_turns_by_id"] = {
            "turn_w56": {"turn_id": "turn_w56", "response_json": resp_in}
        }
        st.session_state["_active_turn_order"] = ["turn_w56"]
        st.session_state["selected_turn_id"] = "turn_w56"
        st.session_state["_active_device_summaries"] = summaries
        render_answer_card(
            resp_in,
            session=sess_in,
            hostname_map=hmap,
            turn_id="turn_w56",
            device_summaries=summaries,
        )
        render_inspector_panel()

    at = AppTest.from_function(_run_answer_and_insp, args=(resp, session), default_timeout=20)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception
    md_all = "\n".join(m.value for m in at.markdown)
    # W5: chart title uses hostname, raw chart dict stays untouched
    assert "acme-thinkpad-8 · Battery" in md_all
    assert resp["charts"][0]["title"] == "8NM23J95R5I6 · Battery"
    # W6 & W7: Device record summary with `current:` label rendered in compact Evidence cards
    from markdown_it import MarkdownIt

    rendered_md = MarkdownIt("commonmark").render(md_all)
    assert "Device record" in rendered_md
    assert "current: ThinkPad X1 Carbon" in rendered_md
    assert "1,157 cycles" in rendered_md


def test_w8_viewer_empty_approvals_queue_uses_viewer_copy(
    offline_container: UIContainer,
) -> None:
    """W8: Viewer with empty approvals queue sees role-appropriate copy."""
    _ = offline_container
    session = _demo_session_for("acme_viewer")

    def _run_approvals() -> None:
        from fleet_copilot.interfaces.ui.pages.approvals import render_approvals_page

        render_approvals_page()

    at = AppTest.from_function(_run_approvals, default_timeout=15)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception
    info_texts = [i.value for i in at.info]
    assert any("IT admins stage actions from Home or Copilot." in t for t in info_texts)


def test_w9_sign_in_empty_code_does_not_count_toward_lockout_and_typing_clears_error(
    offline_container: UIContainer,
) -> None:
    """W9: Empty access code prompts user without incrementing lockout; typing clears error."""
    _ = offline_container

    def _run_sign_in() -> None:
        from fleet_copilot.interfaces.ui.pages.sign_in import render_sign_in_page

        render_sign_in_page()

    at = AppTest.from_function(_run_sign_in, default_timeout=15)
    at.run()
    assert not at.exception

    # Click persona card with empty access code -> no lockout increment!
    at.button(key="persona_card_acme_admin").click().run()
    assert not at.exception
    assert any("Enter your access code first." in e.value for e in at.error)
    assert int(at.session_state.get("auth_failed_attempts", 0)) == 0

    # Typing into Access code clears the error via on_change
    at.text_input(key="sign_in_access_code").set_value("t").run()
    assert not at.exception
    assert not at.error


def test_w10_outbox_email_word_boundary_ellipsis_hostname_subject_and_safe_md(
    offline_container: UIContainer,
) -> None:
    """W10: Outbox trims cleanly with ellipsis, shows hostname in subject, and sanitizes message."""
    session = _demo_session_for("acme_admin")
    long_msg = (
        "Hello, your laptop acme-macbook-4 is failing screen_lock compliance. "
        "Please enable screen lock immediately to comply with company security guidelines "
        "and avoid ticket escalation. See ![beacon](https://evil.example/px) and "
        "[phish](https://evil.example/click) for additional details."
    )
    offline_container.store.proposals["prop_email_1"] = {
        "proposal_id": "prop_email_1",
        "company_id": "acme-001",
        "tool_name": "notify_employee",
        "target_type": "employee",
        "target_id": "emp-acme-1003",
        "arguments_json": {"employee_id": "emp-acme-1003", "message": long_msg},
        "status": "executed",
    }
    offline_container.store.outbox.append(
        {
            "outbox_id": 1,
            "company_id": "acme-001",
            "proposal_id": "prop_email_1",
            "channel": "email",
            "receipt_id": "NTF-1234567890",
            "payload_json": {
                "system": "email_mock",
                "recipient_employee_id": "emp-acme-1003",
                "subject": "Action needed on your managed device (acme-001)",
                "body_preview": long_msg[:240],
            },
            "created_at": AS_OF,
        }
    )

    def _run_approvals() -> None:
        from fleet_copilot.interfaces.ui.pages.approvals import render_approvals_page

        render_approvals_page()

    at = AppTest.from_function(_run_approvals, default_timeout=20)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception

    # Subject replaced raw (acme-001) with matched hostname (acme-macbook-4) with no literal escapes
    assert any(t.value == "Action needed on your managed device (acme-macbook-4)" for t in at.text)
    # Preview ends cleanly with ellipsis and full message has zero active links/images
    md_values = [m.value for m in at.markdown]
    assert any(v.endswith("…") for v in md_values)
    for v in md_values:
        assert "](" not in v
        assert "![" not in v
        assert "href" not in v


def test_s5_backtick_interpolation_and_untrusted_call_sites(
    offline_container: UIContainer,
) -> None:
    """S5: inspector tool_hint, policy observed/threshold, citation ids, and outbox dev_id safe."""
    session = _demo_session_for("acme_admin")
    hostile_bt = "x` ![px](//evil/p.png) `y"
    resp = {
        "turn_id": "turn_s5",
        "trace_id": "trace-s5",
        "status": "completed",
        "plan": {
            "intent": "qa",
            "steps": [{"goal": "Check devices", "tool_hint": hostile_bt}],
        },
        "answer": {"summary": "Done [E1].", "claims": []},
        "citations": [
            {
                "handle": "E1",
                "snapshot_id": hostile_bt,
                "device_id": hostile_bt,
                "hostname": "acme-macbook-4",
                "collected_at": AS_OF.isoformat(),
                "metric": hostile_bt,
                "value": 5,
                "unit": "%",
            }
        ],
        "verdicts": [
            {
                "allowed": False,
                "tool_name": "create_upgrade_order",
                "target_id": "MT7PJB7N5LRE",
                "reasons": ["Blocked"],
                "checks": [
                    {
                        "name": "ram_upgradable",
                        "passed": False,
                        "observed": hostile_bt,
                        "threshold": hostile_bt,
                        "source": "catalog",
                    }
                ],
            }
        ],
        "proposals": [
            {
                "proposal_id": "prop_s5",
                "tool_name": "open_remediation_ticket",
                "target_id": "MT7PJB7N5LRE",
                "status": "pending",
                "policy_verdict": {
                    "allowed": True,
                    "checks": [
                        {
                            "name": "disk_encryption",
                            "passed": True,
                            "observed": hostile_bt,
                            "threshold": hostile_bt,
                            "source": "policy",
                        }
                    ],
                },
            }
        ],
        "telemetry_as_of": AS_OF.isoformat(),
    }

    def _run_s5(resp_in, sess_in):  # type: ignore[no-untyped-def]
        import streamlit as st

        from fleet_copilot.interfaces.ui.components.answer_card import render_answer_card
        from fleet_copilot.interfaces.ui.components.inspector import render_inspector_panel
        from fleet_copilot.interfaces.ui.components.proposal_card import render_proposal_cards

        st.session_state["_active_turns_by_id"] = {
            "turn_s5": {"turn_id": "turn_s5", "response_json": resp_in}
        }
        st.session_state["_active_turn_order"] = ["turn_s5"]
        st.session_state["selected_turn_id"] = "turn_s5"
        render_answer_card(resp_in, session=sess_in, hostname_map={}, turn_id="turn_s5")
        render_proposal_cards(
            resp_in, session=sess_in, hostname_map={}, thread_id="acme-001:t1", turn_id="turn_s5"
        )
        render_inspector_panel()

    at = AppTest.from_function(_run_s5, args=(resp, session), default_timeout=20)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception

    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark")
    for text_val in [m.value for m in at.markdown] + [c.value for c in at.caption]:
        for tok in parser.parse(text_val):
            for ch in tok.children or ():
                assert ch.type not in ("image", "link_open"), (
                    f"Backtick breakout leaked active {ch.type!r} in {text_val!r}"
                )

    # Approvals procurement dev_id and plain st.text email subject (no literal \< escapes)
    offline_container.store.outbox.append(
        {
            "outbox_id": 99,
            "company_id": "acme-001",
            "proposal_id": "prop_proc_s5",
            "channel": "procurement",
            "receipt_id": "ORD-99",
            "payload_json": {"device_id": hostile_bt, "sku": "RAM-32", "quantity": 1},
            "created_at": AS_OF,
        }
    )
    offline_container.store.outbox.append(
        {
            "outbox_id": 100,
            "company_id": "acme-001",
            "proposal_id": "prop_mail_s5",
            "channel": "email",
            "receipt_id": "NTF-100",
            "payload_json": {
                "recipient_employee_id": hostile_bt,
                "subject": "Disk < 15% alert (acme-001)",
                "body_preview": "Short body",
            },
            "created_at": AS_OF,
        }
    )

    def _run_app_s5() -> None:
        from fleet_copilot.interfaces.ui.pages.approvals import render_approvals_page

        render_approvals_page()

    at_ap = AppTest.from_function(_run_app_s5, default_timeout=20)
    at_ap.session_state["demo_session"] = session
    at_ap.run()
    assert not at_ap.exception
    # Plain st.text email subject must NOT show escaped '\<' or '&lt;'
    assert any(t.value == "Disk < 15% alert (Acme)" for t in at_ap.text)
    for text_val in [m.value for m in at_ap.markdown] + [c.value for c in at_ap.caption]:
        for tok in parser.parse(text_val):
            for ch in tok.children or ():
                assert ch.type not in ("image", "link_open"), (
                    f"Outbox leaked active {ch.type!r} in {text_val!r}"
                )


def test_l2_w11_guardrails_uses_turn_jobs_guarded_and_company_aware_copy(
    offline_container: UIContainer,
) -> None:
    """L2 & W11: Guardrails has full-label buttons, company-aware copy, and TurnJobs."""
    session = _demo_session_for("acme_admin")

    def _run_how() -> None:
        from fleet_copilot.interfaces.ui.pages.how_it_works import render_how_it_works_page

        render_how_it_works_page()

    at = AppTest.from_function(_run_how, default_timeout=20)
    at.session_state["demo_session"] = session
    at.session_state["how_it_works_default_tab"] = "Guardrails"
    at.run()
    assert not at.exception

    # W11: Acme-aware explanation for cross-tenant recorded run
    assert any("You are signed in as **Acme**" in c.value for c in at.caption)

    # L2: Selecting an Acme-eligible attack and clicking Run live submits via TurnJobs + Guarded
    at.button(key="guardrail_pick_ignore_instructions").click().run()
    assert not at.exception
    at.button(key="guardrail_live_ignore_instructions").click().run()
    assert not at.exception
    job_meta = at.session_state.get("guardrail_active_job")
    assert isinstance(job_meta, dict)
    assert job_meta.get("attack_id") == "ignore_instructions"
    # Wait briefly for offline ScriptedChatModel TurnJob to finish cleanly
    snap = offline_container.turn_jobs.get(str(job_meta["job_id"]))
    assert snap is not None
