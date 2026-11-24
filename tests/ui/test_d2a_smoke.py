"""Streamlit AppTest smoke tests for Phase D2a pages (Sign in, Home, Copilot) x all 4 personas."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from streamlit.testing.v1 import AppTest

from fleet_copilot.agent.schemas import Answer, Plan
from fleet_copilot.clock import FixedClock
from fleet_copilot.config import Settings
from fleet_copilot.interfaces.ui.services import UIContainer, set_ui_container_override
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.charts import ChartService
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.demo_auth import DemoSession, load_personas
from fleet_copilot.services.guarded import GuardedCopilot
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.turn_jobs import TurnJobs
from tests.graph.fakes import (
    AS_OF,
    InMemoryAuditService,
    InMemoryCopilotStore,
    InMemoryTelemetryRepo,
    ScriptedChatModel,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "evals" / "reports" / "runs"

ALL_PERSONAS = ("acme_admin", "acme_viewer", "globex_admin", "initech_admin")

_ADMIN_ONLY_BUTTON_SUBSTRINGS = (
    "propose this action",
    "approve",
    "reject",
    "defer",
    "stage this instead",
)


def _make_offline_container(*, spend_usd: float = 0.0) -> UIContainer:
    repo = InMemoryTelemetryRepo()
    store = InMemoryCopilotStore()
    policy_svc = PolicyService(store)
    audit_svc = InMemoryAuditService()
    clock = FixedClock(AS_OF)
    settings = Settings(
        app_env="dev",
        demo_access_codes="testcode:rev_test",
        demo_spend_cap_usd=2.00,
        demo_turns_per_code=25,
        demo_turns_per_hour=20,
        llm_replay_mode="replay",
    )

    def _model_factory(node: str) -> ScriptedChatModel:
        if node == "planner":
            return ScriptedChatModel(script=[Plan(intent="qa", steps=[])])
        if node == "synthesizer":
            return ScriptedChatModel(script=[Answer(summary="Offline smoke response.")])
        return ScriptedChatModel(script=["Done."])

    copilot = CopilotService(
        telemetry_repo=repo,
        copilot_store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        adapter=MockAdapter(),
        checkpointer=MemorySaver(),
        clock=clock,
        settings=settings,
        model_factory=_model_factory,
    )
    if spend_usd > 0.0:
        store.record_spend(
            "acme-001",
            actor_id="rev_test:acme_admin",
            rev_id="rev_test",
            turn_id="turn_prior_budget",
            cost_usd=spend_usd,
            created_at=AS_OF,
        )
    guarded = GuardedCopilot(copilot, settings=settings, clock=clock, require_api_key=False)
    chart_svc = ChartService(
        telemetry_repo=repo,
        policy_svc=policy_svc,
        store=store,
        audit_svc=audit_svc,
    )
    turn_jobs = TurnJobs(max_workers=2, clock=clock)
    return UIContainer(
        settings=settings,
        db=None,
        telemetry_repo=repo,
        store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        copilot=copilot,
        guarded=guarded,
        chart_svc=chart_svc,
        turn_jobs=turn_jobs,
        clock=clock,
    )


def _demo_session_for(persona_id: str) -> DemoSession:
    spec = load_personas()[persona_id]
    return DemoSession(
        rev_id="rev_test",
        persona=spec.id,
        company_id=spec.company_id,
        role=spec.role,
        actor_id=f"rev_test:{spec.id}",
        label=spec.label,
    )


@pytest.fixture()
def offline_ui_container() -> Iterator[UIContainer]:
    container = _make_offline_container()
    set_ui_container_override(container)
    try:
        yield container
    finally:
        set_ui_container_override(None)
        container.turn_jobs.shutdown(wait=False)


def _all_button_labels(at: AppTest) -> list[str]:
    return [str(b.label) for b in at.button]


def _seed_rich_copilot_thread(container: UIContainer, session: DemoSession) -> str:
    """Seed one stored thread with chart, SQL, pending proposal, and refusal turns."""
    tid = f"{session.company_id}:smoke_rich"
    store: InMemoryCopilotStore = container.store
    store.ensure_thread(session.company_id, tid, session.actor_id, title="Rich smoke thread")

    # Turn 1: chart + citations
    chart_turn = json.loads((RUNS_DIR / "chart-acme-low-disk-30d.json").read_text(encoding="utf-8"))
    chart_turn["thread_id"] = tid
    chart_turn["turn_id"] = "turn_smoke_chart"
    store.ensure_turn(
        session.company_id,
        tid,
        "turn_smoke_chart",
        "trace-smoke-1",
        session.actor_id,
        "Chart low disk over 30 days",
        status="completed",
    )
    store.update_turn(session.company_id, "turn_smoke_chart", response_json=chart_turn)

    # Turn 2: SQL result + pending proposal
    prop_turn = json.loads(
        (RUNS_DIR / "act-ram-upgrade-allow-approve.json").read_text(encoding="utf-8")
    )
    sql_turn = json.loads(
        (RUNS_DIR / "sql-acme-avg-cycles-by-model.json").read_text(encoding="utf-8")
    )
    prop_turn["thread_id"] = tid
    prop_turn["turn_id"] = "turn_smoke_prop"
    prop_turn["queries"] = sql_turn["queries"]
    prop_turn["status"] = "waiting_approval"
    for p in prop_turn["proposals"]:
        p["status"] = "pending_approval"
        p["expires_at"] = (datetime.now(UTC) + timedelta(hours=48)).isoformat()
    prop_turn["approval_packet"] = {
        "batch_id": "batch_smoke",
        "thread_id": tid,
        "turn_id": "turn_smoke_prop",
        "proposals": prop_turn["proposals"],
        "fixture_mode": True,
    }
    store.ensure_turn(
        session.company_id,
        tid,
        "turn_smoke_prop",
        "trace-smoke-2",
        session.actor_id,
        "Upgrade RAM on acme-dell-9",
        status="waiting_approval",
    )
    store.update_turn(session.company_id, "turn_smoke_prop", response_json=prop_turn)

    # Turn 3: Refusal with verified alternatives
    refuse_turn = json.loads(
        (RUNS_DIR / "act-ram-upgrade-soldered-refuse-alt.json").read_text(encoding="utf-8")
    )
    refuse_turn["thread_id"] = tid
    refuse_turn["turn_id"] = "turn_smoke_refuse"
    store.ensure_turn(
        session.company_id,
        tid,
        "turn_smoke_refuse",
        "trace-smoke-3",
        session.actor_id,
        "Upgrade RAM on soldered laptop",
        status="completed",
    )
    store.update_turn(session.company_id, "turn_smoke_refuse", response_json=refuse_turn)
    return tid


def test_sign_in_renders_personas_rejects_wrong_code_locks_at_10_and_accepts_valid(
    offline_ui_container: UIContainer,
) -> None:
    _ = offline_ui_container

    def _run_sign_in() -> None:
        from fleet_copilot.interfaces.ui.pages.sign_in import render_sign_in_page

        render_sign_in_page()

    at = AppTest.from_function(_run_sign_in, default_timeout=15)
    at.run()
    assert not at.exception
    labels = _all_button_labels(at)
    assert any("Acme · IT admin" in lbl for lbl in labels)
    assert any("Acme · Viewer" in lbl for lbl in labels)
    assert any("Globex · IT admin" in lbl for lbl in labels)
    assert any("Initech · IT admin" in lbl for lbl in labels)

    # Wrong code produces the generic error
    at.text_input(key="sign_in_access_code").set_value("wrong-code").run()
    at.button(key="persona_card_acme_admin").click().run()
    assert not at.exception
    assert any("Invalid access code or persona." in e.value for e in at.error)
    assert at.session_state["auth_failed_attempts"] == 1

    # 10 failed attempts lock out the form for the session
    at.session_state["auth_failed_attempts"] = 9
    at.run()
    at.button(key="persona_card_acme_admin").click().run()
    assert not at.exception
    assert any("Too many attempts" in e.value for e in at.error)
    assert at.text_input(key="sign_in_access_code").disabled is True
    assert at.button(key="persona_card_acme_admin").disabled is True

    # Valid sign-in in a fresh session populates DemoSession
    at_ok = AppTest.from_function(_run_sign_in, default_timeout=15)
    at_ok.run()
    at_ok.text_input(key="sign_in_access_code").set_value("testcode").run()
    at_ok.button(key="persona_card_acme_viewer").click().run()
    assert not at_ok.exception
    sess = at_ok.session_state["demo_session"]
    assert isinstance(sess, DemoSession)
    assert sess.company_id == "acme-001"
    assert sess.role == "viewer"


@pytest.mark.parametrize("persona_id", ALL_PERSONAS)
def test_home_page_renders_all_personas_and_hides_propose_for_viewer(
    offline_ui_container: UIContainer,
    persona_id: str,
) -> None:
    _ = offline_ui_container
    session = _demo_session_for(persona_id)

    def _run_home() -> None:
        from fleet_copilot.interfaces.ui.pages.home import render_home_page

        render_home_page()

    at = AppTest.from_function(_run_home, default_timeout=15)
    at.session_state["demo_session"] = session
    at.run()
    assert not at.exception

    metric_labels = [m.label for m in at.metric]
    assert "Devices" in metric_labels
    assert "Compliance pass rate" in metric_labels
    assert "Open approvals" in metric_labels
    assert "Devices needing attention" in metric_labels

    btn_labels = [lbl.lower() for lbl in _all_button_labels(at)]
    if session.role == "viewer":
        for forbidden in _ADMIN_ONLY_BUTTON_SUBSTRINGS:
            assert not any(forbidden in lbl for lbl in btn_labels), (
                f"Viewer must not render button matching {forbidden!r}: {btn_labels}"
            )
    elif session.company_id == "acme-001":
        assert any("propose this action" in lbl for lbl in btn_labels)


@pytest.mark.parametrize("persona_id", ALL_PERSONAS)
def test_copilot_page_empty_and_rich_thread_rbac(
    offline_ui_container: UIContainer,
    persona_id: str,
) -> None:
    session = _demo_session_for(persona_id)

    def _run_copilot() -> None:
        from fleet_copilot.interfaces.ui.pages.copilot import render_copilot_page

        render_copilot_page()

    # 1. Empty state
    at_empty = AppTest.from_function(_run_copilot, default_timeout=15)
    at_empty.session_state["demo_session"] = session
    at_empty.run()
    assert not at_empty.exception
    assert any("Start a conversation" in m.value for m in at_empty.markdown)

    # 2. Rich stored thread with chart, SQL, pending proposal, and refusal alternatives
    tid = _seed_rich_copilot_thread(offline_ui_container, session)
    at_rich = AppTest.from_function(_run_copilot, default_timeout=15)
    at_rich.session_state["demo_session"] = session
    at_rich.session_state["active_thread_id"] = tid
    at_rich.session_state["inspector_open"] = True
    at_rich.run()
    assert not at_rich.exception

    btn_labels = [lbl.lower() for lbl in _all_button_labels(at_rich)]
    if session.role == "viewer":
        for forbidden in _ADMIN_ONLY_BUTTON_SUBSTRINGS:
            assert not any(forbidden in lbl for lbl in btn_labels), (
                f"Viewer rendered forbidden action control {forbidden!r}: {btn_labels}"
            )
        assert any("ask an it admin" in info.value.lower() for info in at_rich.info)
    else:
        assert any(lbl == "approve" for lbl in btn_labels)
        assert any("stage this instead" in lbl for lbl in btn_labels)


def test_copilot_examples_only_mode_disables_chat_input() -> None:
    container = _make_offline_container(spend_usd=2.50)
    set_ui_container_override(container)
    try:
        session = _demo_session_for("acme_admin")

        def _run_copilot() -> None:
            from fleet_copilot.interfaces.ui.pages.copilot import render_copilot_page

            render_copilot_page()

        at = AppTest.from_function(_run_copilot, default_timeout=15)
        at.session_state["demo_session"] = session
        at.run()
        assert not at.exception
        assert any("examples only" in info.value.lower() for info in at.info)
        assert at.chat_input[0].disabled is True
    finally:
        set_ui_container_override(None)
        container.turn_jobs.shutdown(wait=False)


def test_unauthenticated_home_and_copilot_require_session(
    offline_ui_container: UIContainer,
) -> None:
    _ = offline_ui_container

    def _run_home() -> None:
        from fleet_copilot.interfaces.ui.pages.home import render_home_page

        render_home_page()

    at = AppTest.from_function(_run_home, default_timeout=15)
    at.run()
    assert not at.exception
    assert not at.metric
    assert any("sign in" in info.value.lower() for info in at.info)
