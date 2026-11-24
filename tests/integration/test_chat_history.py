"""Integration tests for response_json, titles/hidden, 30-turn cap, restart & redaction."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from typer.testing import CliRunner

from fleet_copilot.agent.schemas import Answer, Plan, PlanStep, TurnResponse
from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.interfaces.cli import app as cli_app
from fleet_copilot.ops.redact_turns import redact_expired_turns, scan_redacted_turns
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.services.copilot import CopilotService, ThreadNotOwnedError
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset
from tests.graph.fakes import ScriptedChatModel, make_tool_call_ai

FIXED_NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)
    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=2,
        max_size=6,
    ) as manager:
        yield manager


def _ctx(
    *,
    company_id: str = "acme-001",
    actor: str = "rev_01:acme_admin",
    role: str = "it_admin",
    trace: str = "trace-b1",
) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=actor,
        role=role,  # type: ignore[arg-type]
        trace_id=trace,
    )


def _qa_copilot(
    db: DatabaseManager,
    *,
    clock: FixedClock,
    checkpointer: Any = None,
    planner_model: ScriptedChatModel | None = None,
) -> tuple[CopilotService, ScriptedChatModel]:
    planner = planner_model or ScriptedChatModel(
        script=[Plan(intent="qa", steps=[PlanStep(goal="Overview")])]
    )
    investigator = ScriptedChatModel(
        script=[
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_b1_1"}]),
            "Overview done.",
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_b1_2"}]),
            "Overview 2 done.",
        ]
    )
    synthesizer = ScriptedChatModel(
        script=[
            Answer(
                summary="Acme has 10 devices.",
                result_device_ids=[],
                claims=[],
                caveats=["As of 2026-06-12."],
            ),
            Answer(
                summary="Confirmed 10 devices after restart.",
                result_device_ids=[],
                claims=[],
                caveats=["As of 2026-06-12."],
            ),
        ]
    )
    models = {"planner": planner, "investigator": investigator, "synthesizer": synthesizer}
    svc = CopilotService(
        db=db,
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        clock=clock,
        model_factory=lambda node: models[node],
        model_id="google_genai:gemini-3.8-flash",
    )
    return svc, planner


@pytest.mark.integration
def test_thread_title_from_first_question_rename_hide_and_response_json(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """First question sets title (<=60 chars), response_json stored, rename/hide work."""
    clock = FixedClock(FIXED_NOW)
    svc, _ = _qa_copilot(db_manager, clock=clock)
    ctx_a = _ctx(actor="rev_01:acme_admin")
    ctx_b = _ctx(actor="rev_02:acme_admin")

    long_q = (
        "   Which devices across Acme are currently low on disk space and need urgent "
        "attention before Monday morning?   "
    )
    resp = svc.run_turn(ctx_a, None, long_q)
    assert resp.status == "completed"

    detail = svc.get_thread(ctx_a, resp.thread_id)
    assert detail is not None
    title = detail["thread"]["title"]
    assert title is not None
    assert len(title) <= 60
    assert title.startswith("Which devices across Acme")

    # Full TurnResponse persisted in turns.response_json
    assert len(detail["turns"]) == 1
    stored_rj = detail["turns"][0]["response_json"]
    assert stored_rj is not None
    rehydrated = TurnResponse.model_validate(stored_rj)
    assert rehydrated.turn_id == resp.turn_id
    assert rehydrated.answer_markdown == resp.answer_markdown

    # Rename & hide enforce actor ownership
    with pytest.raises(ThreadNotOwnedError):
        svc.rename_thread(ctx_b, resp.thread_id, "Hijacked title")
    with pytest.raises(ThreadNotOwnedError):
        svc.hide_thread(ctx_b, resp.thread_id)

    svc.rename_thread(ctx_a, resp.thread_id, "Disk Triage Q2")
    listed = svc.list_threads(ctx_a, search="triage")
    assert [t["thread_id"] for t in listed] == [resp.thread_id]
    assert listed[0]["title"] == "Disk Triage Q2"

    # Soft hide removes thread from default listing, keeps turns for audit
    svc.hide_thread(ctx_a, resp.thread_id)
    assert svc.list_threads(ctx_a) == []
    assert [t["thread_id"] for t in svc.list_threads(ctx_a, include_hidden=True)] == [
        resp.thread_id
    ]
    still_there = svc.get_thread(ctx_a, resp.thread_id)
    assert still_there is not None and len(still_there["turns"]) == 1


@pytest.mark.integration
def test_continue_after_service_restart_and_missing_checkpoint_rebuilds_digest(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """Continuing after checkpoint loss rebuilds conversation_digest from copilot.turns."""
    clock = FixedClock(FIXED_NOW)
    ctx = _ctx(actor="rev_01:acme_admin")

    # Turn 1 runs on service instance #1 (ephemeral saver simulating later GC / restart)
    svc1, _ = _qa_copilot(db_manager, clock=clock, checkpointer=InMemorySaver())
    r1 = svc1.run_turn(ctx, None, "Give me a fleet overview for Acme")
    assert r1.status == "completed"

    # Turn 2 runs on a BRAND-NEW service instance with an empty checkpointer (checkpoint GC'd)
    planner2 = ScriptedChatModel(script=[Plan(intent="qa", steps=[PlanStep(goal="Follow-up")])])
    svc2, _ = _qa_copilot(
        db_manager, clock=clock, checkpointer=InMemorySaver(), planner_model=planner2
    )
    r2 = svc2.run_turn(ctx, r1.thread_id, "Can you confirm that count?")
    assert r2.status == "completed"

    # Planner on turn 2 must have received the rebuilt digest from copilot.turns!
    planner_text = " ".join(str(m.content) for m in planner2.recorded_inputs[-1])
    assert "Give me a fleet overview for Acme" in planner_text
    assert "Acme has 10 devices." in planner_text


@pytest.mark.integration
def test_max_30_turns_returns_start_new_chat_prompt(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """Thread with 30 persisted turns refuses turn 31 with friendly start-new-chat message."""
    clock = FixedClock(FIXED_NOW)
    planner = ScriptedChatModel(script=[Plan(intent="qa", steps=[])])
    svc, _ = _qa_copilot(db_manager, clock=clock, planner_model=planner)
    ctx = _ctx(actor="rev_01:acme_admin")
    store = PgCopilotStore(db_manager)

    tid = "acme-001:thirty-turns"
    store.ensure_thread("acme-001", tid, ctx.actor_id, title="Long chat")
    for i in range(30):
        store.ensure_turn(
            "acme-001",
            tid,
            f"turn_seed_{i}",
            f"trace_{i}",
            ctx.actor_id,
            f"Question {i}",
            status="completed",
        )

    resp = svc.run_turn(ctx, tid, "One turn too many")
    assert resp.status == "failed"
    assert "new chat" in resp.answer_markdown.lower()
    assert planner.recorded_inputs == []


@pytest.mark.integration
def test_redact_turns_clears_real_response_json_column(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """redact_expired_turns clears the persisted turns.response_json column."""
    clock = FixedClock(FIXED_NOW)
    svc, _ = _qa_copilot(db_manager, clock=clock)
    ctx = _ctx(actor="rev_01:acme_admin")
    resp = svc.run_turn(ctx, None, "Summarize fleet for alice@acme.test")
    assert resp.status == "completed"

    with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE copilot.turns SET created_at = %s WHERE turn_id = %s",
                (FIXED_NOW - timedelta(days=10), resp.turn_id),
            )
        conn.commit()

    count = redact_expired_turns(
        db_manager,
        cutoff=FIXED_NOW - timedelta(days=7),
        now=FIXED_NOW,
        company_id="acme-001",
    )
    assert count == 1
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "SELECT user_message, assistant_response, response_json, redacted_at "
            "FROM copilot.turns WHERE turn_id = %s",
            (resp.turn_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["redacted_at"] == FIXED_NOW
    assert row["user_message"] is None
    assert row["assistant_response"] is None
    assert row["response_json"] is None
    assert scan_redacted_turns(db_manager, company_id="acme-001") == []


@pytest.mark.integration
def test_cli_threads_and_chat_thread_id_option(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """CLI exposes `threads` command and `chat --thread-id` option."""
    runner = CliRunner()
    help_res = runner.invoke(cli_app, ["chat", "--help"])
    assert help_res.exit_code == 0
    assert "--thread-id" in help_res.output

    store = PgCopilotStore(db_manager)
    store.ensure_thread("acme-001", "acme-001:cli-t1", "acme-001_it_admin", title="CLI Thread One")
    threads_res = runner.invoke(cli_app, ["threads", "--company", "acme-001", "--role", "it_admin"])
    assert threads_res.exit_code == 0
    assert "CLI Thread One" in threads_res.output
