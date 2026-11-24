"""Integration tests for GuardedCopilot limits, DB spend ledger hardening, and eval isolation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from fleet_copilot.agent.llm import NodeName
from fleet_copilot.agent.schemas import Answer, Claim, Plan, PlanStep
from fleet_copilot.clock import FixedClock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.db.postgres import make_checkpointer
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.guarded import GuardedCopilot
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset
from tests.graph.fakes import ScriptedChatModel, make_tool_call_ai

FIXED_NOW = datetime(2026, 11, 24, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)
    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        saver = make_checkpointer(manager, settings=settings)
        saver.setup()
        yield manager


@pytest.fixture()
def clean_spend_ledger() -> Iterator[None]:
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute("TRUNCATE TABLE copilot.spend_ledger RESTART IDENTITY")
    yield


def _ctx(
    company_id: str = "acme-001",
    actor_id: str = "rev_01:acme_admin",
    role: str = "it_admin",
) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role=role,  # type: ignore[arg-type]
        trace_id="trace-guarded-1",
    )


def _make_scripted_copilot(
    db: DatabaseManager,
    clock: FixedClock,
    *,
    planner_script: list[Any] | None = None,
    settings: Settings | None = None,
) -> CopilotService:
    planner = ScriptedChatModel(
        script=planner_script
        or [Plan(intent="qa", steps=[PlanStep(goal="Overview", tool_hint="get_fleet_overview")])]
    )
    investigator = ScriptedChatModel(
        script=[
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_g1"}]),
            "Done.",
        ]
    )
    synthesizer = ScriptedChatModel(
        script=[
            Answer(
                summary="Acme has 10 devices.",
                claims=[
                    Claim(
                        text="Device 1LYSSFD074BB is in the fleet [E1].",
                        citations=["E1"],
                        device_ids=["1LYSSFD074BB"],
                        numbers=[],
                    )
                ],
                caveats=["As of 2026-06-12."],
            )
        ]
    )

    def _factory(node: NodeName) -> Any:
        return {"planner": planner, "investigator": investigator, "synthesizer": synthesizer}[node]

    cfg = settings or get_settings()
    return CopilotService(
        db=db,
        audit_svc=AuditService(db),
        checkpointer=make_checkpointer(db, settings=cfg),
        clock=clock,
        settings=cfg,
        model_factory=_factory,
        model_id="google_genai:gemini-3.8-flash",
    )


def test_spend_ledger_cannot_be_selected_directly_by_app_roles(
    db_manager: DatabaseManager,
    clean_spend_ledger: None,
) -> None:
    settings = get_settings()
    for dsn in (
        settings.copilot_database_url,
        settings.telemetry_ro_database_url,
        settings.telemetry_writer_database_url,
    ):
        with psycopg.connect(dsn, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT * FROM copilot.spend_ledger")
            conn.rollback()


def test_live_unavailable_when_gemini_key_missing(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    clock = FixedClock(FIXED_NOW)
    cfg = get_settings().model_copy(update={"gemini_api_key": None, "llm_replay_mode": "off"})
    copilot = _make_scripted_copilot(db_manager, clock, settings=cfg)
    guarded = GuardedCopilot(copilot, settings=cfg, clock=clock)

    assert guarded.mode() == "examples_only"
    resp = guarded.run_turn(_ctx(), None, "Overview")
    assert resp.status == "limited"
    assert "live_unavailable" in resp.warnings


def test_per_code_turn_cap_and_failed_turns_counted_and_persists_across_instances(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    clock = FixedClock(FIXED_NOW)
    cfg = get_settings().model_copy(
        update={
            "gemini_api_key": SecretStr("test-key"),
            "llm_replay_mode": "off",
            "demo_turns_per_code": 2,
            "demo_turns_per_hour": 10,
            "demo_spend_cap_usd": 5.0,
        }
    )
    # First turn fails before LLM returns (after graph invoked) -> charges SPEND_RESERVE_USD ($0.05)
    failing_copilot = _make_scripted_copilot(
        db_manager,
        clock,
        planner_script=[RuntimeError("Simulated outage")],
        settings=cfg,
    )
    g1 = GuardedCopilot(failing_copilot, settings=cfg, clock=clock)
    r1 = g1.run_turn(_ctx(actor_id="rev_01:acme_admin"), None, "First attempt")
    assert r1.status == "failed"
    assert failing_copilot._store.spend_total_usd("acme-001") == pytest.approx(0.05, abs=1e-6)

    # Second turn on a BRAND NEW GuardedCopilot instance (same rev_01, different persona!)
    ok_copilot = _make_scripted_copilot(db_manager, clock, settings=cfg)
    g2 = GuardedCopilot(ok_copilot, settings=cfg, clock=clock)
    r2 = g2.run_turn(_ctx(company_id="globex-002", actor_id="rev_01:globex_admin"), None, "Second")
    assert r2.status == "completed"

    # Third turn hits DEMO_TURNS_PER_CODE=2 across instances & companies
    r3 = g2.run_turn(_ctx(actor_id="rev_01:acme_admin"), None, "Third attempt")
    assert r3.status == "limited"
    assert "turn_cap_reached" in r3.warnings

    # Another reviewer (rev_02) is unaffected
    r_other = g2.run_turn(_ctx(actor_id="rev_02:acme_admin"), None, "Other reviewer")
    assert r_other.status == "completed"


def test_rolling_hour_rate_limit_uses_injected_clock(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    clock = FixedClock(FIXED_NOW)
    cfg = get_settings().model_copy(
        update={
            "gemini_api_key": SecretStr("test-key"),
            "llm_replay_mode": "off",
            "demo_turns_per_code": 25,
            "demo_turns_per_hour": 2,
            "demo_spend_cap_usd": 5.0,
        }
    )
    guarded = GuardedCopilot(
        _make_scripted_copilot(db_manager, clock, settings=cfg),
        settings=cfg,
        clock=clock,
    )
    ctx = _ctx(actor_id="rev_hr:acme_admin")
    assert guarded.run_turn(ctx, None, "Turn 1").status == "completed"
    assert guarded.run_turn(ctx, None, "Turn 2").status == "completed"

    blocked = guarded.run_turn(ctx, None, "Turn 3")
    assert blocked.status == "limited"
    assert "rate_limited" in blocked.warnings

    # Advance past 1 hour -> rolling window expires and turns are allowed again
    clock.advance(timedelta(minutes=61))
    assert guarded.run_turn(ctx, None, "Turn 4 after 1h").status == "completed"


def test_global_spend_cap_with_reserve_and_resume_uncounted(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    clock = FixedClock(FIXED_NOW)
    cfg = get_settings().model_copy(
        update={
            "gemini_api_key": SecretStr("test-key"),
            "llm_replay_mode": "off",
            "demo_turns_per_code": 25,
            "demo_turns_per_hour": 20,
            "demo_spend_cap_usd": 0.10,
        }
    )
    copilot = _make_scripted_copilot(db_manager, clock, settings=cfg)
    guarded = GuardedCopilot(copilot, settings=cfg, clock=clock)
    ctx = _ctx(actor_id="rev_cap:acme_admin")

    # Seed $0.050001 into spend_ledger: 0.050001 + 0.05 reserve = 0.100001 > 0.10 cap!
    copilot._store.record_spend(
        "acme-001",
        actor_id="rev_other:acme_admin",
        rev_id="rev_other",
        turn_id="seed-turn-1",
        cost_usd=0.050001,
        created_at=clock.now(),
    )
    assert guarded.mode() == "examples_only"

    blocked = guarded.run_turn(ctx, None, "Should be blocked by budget reserve")
    assert blocked.status == "limited"
    assert "demo_budget_exhausted" in blocked.warnings

    # Resume is NEVER blocked or counted by GuardedCopilot
    copilot._store.ensure_thread("acme-001", "acme-001:resume-free", ctx.actor_id)
    resumed = guarded.resume(ctx, "acme-001:resume-free", {})
    assert resumed.status == "completed"
    assert copilot._store.turns_by_rev_since("acme-001", "rev_cap", None) == 0


def test_eval_runner_never_writes_to_spend_ledger(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
    tmp_path: Any,
) -> None:
    """Evals call CopilotService directly (never GuardedCopilot), ignore demo caps, write 0 rows."""
    from evals.run import run_eval_suite

    # Even when spend_ledger already exceeds DEMO_SPEND_CAP_USD, evals are NOT blocked
    # and write ZERO additional rows to copilot.spend_ledger.
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO copilot.spend_ledger (
                    company_id, actor_id, rev_id, turn_id, cost_usd, created_at
                )
                VALUES ('acme-001', 'rev_01:acme_admin', 'rev_01', 'pre_eval', 99.0, now())
                """
            )

    report = run_eval_suite(
        mode="replay",
        case_ids=["qa-acme-low-disk"],
        repeats=1,
        out_prefix=tmp_path / "eval_out",
    )
    assert report["summary"]["overall_pass_rate"] == 1.0

    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        count = conn.execute("SELECT COUNT(*) FROM copilot.spend_ledger").fetchone()
        assert count is not None and int(count[0]) == 1


def test_failed_turn_after_two_llm_calls_records_actual_accumulated_cost(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    """Failure after 2 LLM calls (planner + investigator) records real accumulated token cost."""
    clock = FixedClock(FIXED_NOW)
    cfg = get_settings().model_copy(
        update={
            "gemini_api_key": SecretStr("test-key"),
            "llm_replay_mode": "off",
            "demo_spend_cap_usd": 5.0,
        }
    )
    planner = ScriptedChatModel(
        script=[
            Plan(intent="qa", steps=[PlanStep(goal="Overview", tool_hint="get_fleet_overview")])
        ]
    )
    investigator = ScriptedChatModel(
        script=[
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_fail2"}]),
            RuntimeError("Simulated timeout on 3rd LLM call"),
        ]
    )
    synthesizer = ScriptedChatModel(script=[Answer(summary="never reached")])

    def _factory(node: NodeName) -> Any:
        return {"planner": planner, "investigator": investigator, "synthesizer": synthesizer}[node]

    copilot = CopilotService(
        db=db_manager,
        audit_svc=AuditService(db_manager),
        checkpointer=make_checkpointer(db_manager, settings=cfg),
        clock=clock,
        settings=cfg,
        model_factory=_factory,
        model_id="google_genai:gemini-3.8-flash",
    )
    guarded = GuardedCopilot(copilot, settings=cfg, clock=clock)
    resp = guarded.run_turn(_ctx(actor_id="rev_fail2:acme_admin"), None, "Overview")
    assert resp.status == "failed"
    assert resp.usage.input_tokens > 0
    assert resp.usage.output_tokens > 0
    assert resp.usage.cost_usd_est > 0.0
    # Ledger recorded the exact real accumulated cost (not $0 and not flat $0.05 reserve)
    recorded = copilot._store.spend_total_usd("acme-001")
    assert recorded == pytest.approx(resp.usage.cost_usd_est, abs=1e-6)
    assert recorded < 0.05


def test_pre_graph_thread_turn_limit_records_zero_cost_no_reserve(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    """thread_turn_limit fails before graph invocation and MUST NOT charge the $0.05 reserve."""
    clock = FixedClock(FIXED_NOW)
    cfg = get_settings().model_copy(
        update={
            "gemini_api_key": SecretStr("test-key"),
            "llm_replay_mode": "off",
            "demo_spend_cap_usd": 5.0,
        }
    )
    copilot = _make_scripted_copilot(db_manager, clock, settings=cfg)
    guarded = GuardedCopilot(copilot, settings=cfg, clock=clock)
    ctx = _ctx(actor_id="rev_lim:acme_admin")
    tid = "acme-001:maxed-out-thread"
    copilot._store.ensure_thread("acme-001", tid, ctx.actor_id, title="Maxed thread")
    for idx in range(30):
        copilot._store.ensure_turn(
            "acme-001", tid, f"pre_turn_{idx}", f"tr_{idx}", ctx.actor_id, f"msg {idx}"
        )

    resp = guarded.run_turn(ctx, tid, "31st message should hit thread_turn_limit")
    assert resp.status == "failed"
    assert "thread_turn_limit" in resp.answer_markdown
    assert copilot._store.spend_total_usd("acme-001") == 0.0
