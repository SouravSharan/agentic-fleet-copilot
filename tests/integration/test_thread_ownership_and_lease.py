"""Integration tests for Phase A3: actor-owned threads, CAS run lease, and turn redaction."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from fleet_copilot.agent.schemas import Answer, Claim, Plan, PlanStep
from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.ops.redact_turns import redact_expired_turns, scan_redacted_turns
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
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
    actor: str = "acme_admin_a",
    role: str = "it_admin",
    trace: str = "trace-a3",
) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=actor,
        role=role,  # type: ignore[arg-type]
        trace_id=trace,
    )


def _stage_screen_lock_copilot(
    db: DatabaseManager,
    *,
    clock: FixedClock | None = None,
    adapter: MockAdapter | None = None,
) -> tuple[CopilotService, MockAdapter, AuditService]:
    planner = ScriptedChatModel(
        script=[
            Plan(intent="action", steps=[PlanStep(goal="Open ticket")]),
            Plan(intent="qa", steps=[PlanStep(goal="Follow-up overview")]),
        ]
    )

    def _pick_ticket_call(msgs: list[BaseMessage]) -> AIMessage:
        data = json.loads(str(msgs[-1].content))
        handles = [e["h"] for e in data["evidence"] if e["device"] == "1LYSSFD074BB"]
        return make_tool_call_ai(
            [
                {
                    "name": "open_remediation_ticket",
                    "args": {
                        "device_id": "1LYSSFD074BB",
                        "check_id": "screen_lock",
                        "note": "Remediate screen_lock drift",
                        "evidence": handles,
                    },
                    "id": "tc_ticket_a3",
                }
            ]
        )

    investigator = ScriptedChatModel(
        script=[
            make_tool_call_ai(
                [
                    {
                        "name": "query_compliance",
                        "args": {"check_id": "screen_lock", "status": "fail"},
                        "id": "tc_comp_a3",
                    }
                ]
            ),
            _pick_ticket_call,
            "Ticket staged and awaiting approval.",
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_ov_a3"}]),
            "Overview complete.",
        ]
    )
    synthesizer = ScriptedChatModel(
        script=[
            Answer(
                summary="Drafted a screen_lock remediation ticket for 1LYSSFD074BB.",
                result_device_ids=["1LYSSFD074BB"],
                claims=[
                    Claim(
                        text="1LYSSFD074BB is failing screen_lock [E1].",
                        citations=["E1"],
                        device_ids=["1LYSSFD074BB"],
                        numbers=[],
                    )
                ],
                caveats=["Requires human approval."],
            ),
            Answer(
                summary="Acme has 10 devices.",
                result_device_ids=[],
                claims=[],
                caveats=["As of 2026-06-12."],
            ),
        ]
    )
    mock_adapter = adapter or MockAdapter()
    audit = AuditService(db)
    models = {
        "planner": planner,
        "investigator": investigator,
        "synthesizer": synthesizer,
    }
    svc = CopilotService(
        db=db,
        audit_svc=audit,
        adapter=mock_adapter,
        checkpointer=InMemorySaver(),
        clock=clock or FixedClock(FIXED_NOW),
        model_factory=lambda node: models[node],
        model_id="google_genai:gemini-3.8-flash",
    )
    return svc, mock_adapter, audit


def _simple_qa_copilot(
    db: DatabaseManager,
    *,
    planner_script: list[Any],
    clock: FixedClock,
) -> CopilotService:
    planner = ScriptedChatModel(script=planner_script)
    investigator = ScriptedChatModel(
        script=[
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_ov_1"}]),
            "Overview done.",
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_ov_2"}]),
            "Overview done again.",
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
                summary="Acme still has 10 devices.",
                result_device_ids=[],
                claims=[],
                caveats=["As of 2026-06-12."],
            ),
        ]
    )
    models = {
        "planner": planner,
        "investigator": investigator,
        "synthesizer": synthesizer,
    }
    return CopilotService(
        db=db,
        checkpointer=InMemorySaver(),
        clock=clock,
        model_factory=lambda node: models[node],
        model_id="google_genai:gemini-3.8-flash",
    )


@pytest.mark.integration
def test_same_company_other_actor_rejected_on_continue_resume_get_and_list(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """A3: Same company, different actor -> run_turn/resume/get_thread raise ThreadNotOwnedError."""
    svc, adapter, audit = _stage_screen_lock_copilot(db_manager)
    ctx_a = _ctx(company_id="acme-001", actor="rev_01:acme_admin")
    ctx_b = _ctx(company_id="acme-001", actor="rev_02:acme_admin")

    # Actor A creates a thread and pauses on an approval interrupt
    r1 = svc.run_turn(ctx_a, None, "Open a screen_lock ticket for 1LYSSFD074BB")
    assert r1.status == "waiting_approval"
    assert r1.approval_packet is not None
    prop_id = r1.approval_packet["proposals"][0]["proposal_id"]

    # Actor B in the SAME company cannot continue, resume, inspect, or list Actor A's thread
    with pytest.raises(ThreadNotOwnedError):
        svc.run_turn(ctx_b, r1.thread_id, "Hijack same-tenant thread")

    with pytest.raises(ThreadNotOwnedError):
        svc.resume(ctx_b, r1.thread_id, {prop_id: "approve"}, note="Hijacked approval")

    with pytest.raises(ThreadNotOwnedError):
        svc.get_thread(ctx_b, r1.thread_id)

    assert svc.list_threads(ctx_b) == []
    assert adapter.call_count == 0

    # Actor A can list, inspect, and resume their own thread
    a_threads = svc.list_threads(ctx_a)
    assert [t["thread_id"] for t in a_threads] == [r1.thread_id]
    a_detail = svc.get_thread(ctx_a, r1.thread_id)
    assert a_detail is not None
    assert a_detail["thread"]["actor_id"] == ctx_a.actor_id
    assert len(a_detail["turns"]) == 1

    # Audit log captured all 3 access_denied events for Actor B and chain stays valid
    denied = [
        e
        for e in audit.list(ctx_a)
        if e["event_type"] == "access_denied" and e["actor_id"] == ctx_b.actor_id
    ]
    assert len(denied) == 3
    assert all(e["payload_json"].get("reason_code") == "thread_not_owned" for e in denied)

    r2 = svc.resume(ctx_a, r1.thread_id, {prop_id: "approve"}, note="Owner approved")
    assert r2.status == "completed"
    assert adapter.call_count == 1
    assert audit.verify(ctx_a)["ok"] is True


@pytest.mark.integration
def test_resume_nonexistent_thread_raises_thread_not_owned(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """A3: Resuming an unknown thread in the caller's company raises ThreadNotOwnedError (404)."""
    clock = FixedClock(FIXED_NOW)
    svc = _simple_qa_copilot(
        db_manager,
        planner_script=[Plan(intent="qa", steps=[PlanStep(goal="Overview")])],
        clock=clock,
    )
    ctx_a = _ctx(company_id="acme-001", actor="rev_01:acme_admin")
    with pytest.raises(ThreadNotOwnedError):
        svc.resume(ctx_a, "acme-001:nonexistent123", {})


@pytest.mark.integration
def test_run_lease_blocks_concurrent_run_with_thread_busy_and_releases_in_finally(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """A3: 90s CAS lease rejects concurrent turns with thread_busy and releases in finally."""
    clock = FixedClock(FIXED_NOW)
    entered_planner = threading.Event()
    release_planner = threading.Event()

    def _blocking_planner(_msgs: Any) -> Plan:
        entered_planner.set()
        release_planner.wait(timeout=5.0)
        return Plan(intent="qa", steps=[PlanStep(goal="Overview", tool_hint="get_fleet_overview")])

    svc = _simple_qa_copilot(
        db_manager,
        planner_script=[
            _blocking_planner,
            Plan(intent="qa", steps=[PlanStep(goal="Second overview")]),
        ],
        clock=clock,
    )
    ctx = _ctx(company_id="acme-001", actor="rev_01:acme_admin")
    thread_id = "acme-001:lease-test-01"
    store = PgCopilotStore(db_manager)

    first_result: list[Any] = []

    def _worker() -> None:
        first_result.append(svc.run_turn(ctx, thread_id, "First message"))

    t = threading.Thread(target=_worker)
    t.start()
    assert entered_planner.wait(timeout=5.0), "First turn never entered planner"

    # Lease must be actively held in DB while worker is running
    row_during = store.get_thread("acme-001", thread_id)
    assert row_during is not None
    assert row_during["run_lease_until"] is not None
    assert row_during["run_lease_until"] > clock.now()

    # Concurrent run_turn and resume on same thread return friendly failed(thread_busy)
    busy_run = svc.run_turn(ctx, thread_id, "Concurrent message")
    assert busy_run.status == "failed"
    assert "thread_busy" in busy_run.answer_markdown
    assert any("thread_busy" in w for w in busy_run.warnings)

    busy_resume = svc.resume(ctx, thread_id, {})
    assert busy_resume.status == "failed"
    assert "thread_busy" in busy_resume.answer_markdown

    # Release worker; first turn completes and releases run_lease_until in finally
    release_planner.set()
    t.join(timeout=5.0)
    assert len(first_result) == 1
    assert first_result[0].status == "completed"

    row_after = store.get_thread("acme-001", thread_id)
    assert row_after is not None
    assert row_after["run_lease_until"] is None

    # Lease must ALSO release in finally when graph raises an exception
    failing_svc = _simple_qa_copilot(
        db_manager,
        planner_script=[RuntimeError("Simulated LLM outage")],
        clock=clock,
    )
    failed_resp = failing_svc.run_turn(ctx, thread_id, "Trigger error")
    assert failed_resp.status == "failed"
    row_after_err = store.get_thread("acme-001", thread_id)
    assert row_after_err is not None
    assert row_after_err["run_lease_until"] is None

    # Stale lease (> 90s old) is reclaimable via CAS
    assert store.acquire_run_lease(
        "acme-001",
        thread_id,
        ctx.actor_id,
        now=clock.now(),
        lease_until=clock.now() + timedelta(seconds=90),
    )
    assert svc.run_turn(ctx, thread_id, "Still leased").status == "failed"

    clock.advance(timedelta(seconds=91))
    recovered = svc.run_turn(ctx, thread_id, "After 90s expiry")
    assert recovered.status == "completed"
    row_final = store.get_thread("acme-001", thread_id)
    assert row_final is not None
    assert row_final["run_lease_until"] is None


@pytest.mark.integration
def test_redact_turns_clears_text_and_json_columns_keeps_hashes(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """A3: redact_expired_turns clears text/JSON payloads, preserves hashes, sets redacted_at."""
    clock = FixedClock(FIXED_NOW)
    svc = _simple_qa_copilot(
        db_manager,
        planner_script=[
            Plan(intent="qa", steps=[PlanStep(goal="Old turn")]),
            Plan(intent="qa", steps=[PlanStep(goal="Fresh turn")]),
        ],
        clock=clock,
    )
    ctx = _ctx(company_id="acme-001", actor="rev_01:acme_admin")

    r_old = svc.run_turn(ctx, None, "Check alice@acme.test device at 10.0.0.42")
    assert r_old.status == "completed"
    r_fresh = svc.run_turn(ctx, None, "Check fresh fleet status")
    assert r_fresh.status == "completed"

    # Backdate the first turn beyond the 7-day retention window
    old_ts = FIXED_NOW - timedelta(days=10)
    with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE copilot.turns SET created_at = %s WHERE turn_id = %s",
                (old_ts, r_old.turn_id),
            )
            cur.execute(
                "UPDATE copilot.turns SET created_at = %s WHERE turn_id = %s",
                (FIXED_NOW, r_fresh.turn_id),
            )
        conn.commit()

    # Capture original hashes before redaction
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "SELECT input_hash, answer_hash FROM copilot.turns WHERE turn_id = %s",
            (r_old.turn_id,),
        )
        before = cur.fetchone()
    assert before is not None
    assert before["input_hash"] and before["answer_hash"]

    cutoff = FIXED_NOW - timedelta(days=7)
    redacted_count = redact_expired_turns(
        db_manager,
        cutoff=cutoff,
        now=FIXED_NOW,
        company_id="acme-001",
    )
    assert redacted_count == 1

    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute("SELECT * FROM copilot.turns WHERE turn_id = %s", (r_old.turn_id,))
        old_row = cur.fetchone()
        cur.execute("SELECT * FROM copilot.turns WHERE turn_id = %s", (r_fresh.turn_id,))
        fresh_row = cur.fetchone()

    assert old_row is not None
    assert old_row["redacted_at"] == FIXED_NOW
    assert old_row["user_message"] is None
    assert old_row["assistant_response"] is None
    assert old_row["plan_json"] is None
    assert old_row["tool_calls_json"] == []
    assert old_row["evidence_json"] == []
    assert old_row["input_hash"] == before["input_hash"]
    assert old_row["answer_hash"] == before["answer_hash"]
    assert old_row["status"] == "completed"

    assert fresh_row is not None
    assert fresh_row["redacted_at"] is None
    assert fresh_row["user_message"] == "Check fresh fleet status"

    assert scan_redacted_turns(db_manager, company_id="acme-001") == []


@pytest.mark.integration
def test_redact_turns_automatically_covers_future_json_columns(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """A3: Redaction discovers & clears future JSONB payload columns (e.g. response_json)."""
    clock = FixedClock(FIXED_NOW)
    svc = _simple_qa_copilot(
        db_manager,
        planner_script=[Plan(intent="qa", steps=[PlanStep(goal="Chart turn")])],
        clock=clock,
    )
    ctx = _ctx(company_id="acme-001", actor="rev_01:acme_admin")
    resp = svc.run_turn(ctx, None, "Plot disk free pct for acme-macbook-4")
    assert resp.status == "completed"

    admin_url = get_settings().admin_database_url
    try:
        with psycopg.connect(admin_url, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE copilot.turns ADD COLUMN IF NOT EXISTS future_payload_json JSONB"
                )
                cur.execute(
                    """
                    UPDATE copilot.turns
                    SET created_at = %s,
                        future_payload_json = %s::jsonb
                    WHERE turn_id = %s
                    """,
                    (
                        FIXED_NOW - timedelta(days=10),
                        json.dumps(
                            {
                                "answer_markdown": "Secret summary for alice@acme.test",
                                "charts": [{"data": [{"hostname": "acme-macbook-4", "pct": 2.0}]}],
                            }
                        ),
                        resp.turn_id,
                    ),
                )
            conn.commit()

        redacted_count = redact_expired_turns(
            db_manager,
            cutoff=FIXED_NOW - timedelta(days=7),
            now=FIXED_NOW,
            company_id="acme-001",
        )
        assert redacted_count == 1

        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                "SELECT user_message, assistant_response, future_payload_json, redacted_at "
                "FROM copilot.turns WHERE turn_id = %s",
                (resp.turn_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["redacted_at"] == FIXED_NOW
        assert row["user_message"] is None
        assert row["assistant_response"] is None
        assert row["future_payload_json"] is None
        assert scan_redacted_turns(db_manager, company_id="acme-001") == []
    finally:
        with psycopg.connect(admin_url, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE copilot.turns DROP COLUMN IF EXISTS future_payload_json")
            conn.commit()
