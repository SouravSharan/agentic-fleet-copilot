"""Focused API integration tests: one test per endpoint and security behaviour."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fleet_copilot.agent.llm import NodeName
from fleet_copilot.agent.schemas import Answer, Claim, Plan, PlanStep
from fleet_copilot.clock import FixedClock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.db.postgres import make_checkpointer
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.interfaces.api.app import ApiContainer, create_app
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import DraftInput
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.fleet import get_device_details
from fleet_copilot.services.guarded import GuardedCopilot
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset
from tests.graph.fakes import ScriptedChatModel, make_tool_call_ai

FIXED_NOW = datetime(2026, 11, 24, 14, 0, 0, tzinfo=UTC)


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


def _build_test_client(
    db: DatabaseManager,
    *,
    settings: Settings | None = None,
    clock: FixedClock | None = None,
) -> tuple[TestClient, ApiContainer]:
    cfg = settings or get_settings().model_copy(
        update={
            "app_env": "prod",
            "demo_access_codes": "code-r1:rev_01,code-r2:rev_02",
            "session_signing_key": SecretStr("a1" * 32),
            "gemini_api_key": SecretStr("fake-live-key"),
            "llm_replay_mode": "off",
            "demo_turns_per_code": 25,
            "demo_turns_per_hour": 20,
            "demo_spend_cap_usd": 5.0,
        }
    )
    used_clock = clock or FixedClock(FIXED_NOW)
    repo = PgTelemetryRepository(db, cfg)
    store = PgCopilotStore(db)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db)
    adapter = MockAdapter()

    planner = ScriptedChatModel(
        script=[
            Plan(intent="qa", steps=[PlanStep(goal="Overview", tool_hint="get_fleet_overview")])
        ]
    )
    investigator = ScriptedChatModel(
        script=[
            make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": "tc_api_1"}]),
            "Done.",
        ]
    )
    synthesizer = ScriptedChatModel(
        script=[
            Answer(
                summary="Fleet overview complete.",
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

    copilot = CopilotService(
        db=db,
        telemetry_repo=repo,
        copilot_store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        adapter=adapter,
        checkpointer=make_checkpointer(db, settings=cfg),
        clock=used_clock,
        settings=cfg,
        model_factory=_factory,
        model_id="google_genai:gemini-3.8-flash",
    )
    guarded = GuardedCopilot(copilot, settings=cfg, clock=used_clock)
    container = ApiContainer(
        guarded=guarded,
        telemetry_repo=repo,
        store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        clock=used_clock,
        settings=cfg,
    )
    app = create_app(container=container, settings=cfg)
    return TestClient(app), container


def _login(client: TestClient, code: str, persona: str) -> dict[str, str]:
    r = client.post("/v1/auth/demo-login", json={"code": code, "persona": persona})
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Trace-Id")
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_healthz_returns_ok_and_trace_id(
    db_manager: DatabaseManager,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.headers.get("X-Trace-Id")


def test_demo_login_valid_and_invalid(
    db_manager: DatabaseManager,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    bad = client.post("/v1/auth/demo-login", json={"code": "wrong", "persona": "acme_admin"})
    assert bad.status_code == 401
    assert bad.headers.get("X-Trace-Id")

    good = client.post("/v1/auth/demo-login", json={"code": "code-r1", "persona": "acme_admin"})
    assert good.status_code == 200
    payload = good.json()
    assert payload["company_id"] == "acme-001"
    assert payload["actor_id"] == "rev_01:acme_admin"
    assert payload["role"] == "it_admin"


def test_get_insights_returns_tenant_briefing(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    headers = _login(client, "code-r1", "acme_admin")
    resp = client.get("/v1/insights", headers=headers)
    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-Id")
    assert resp.json()["company_id"] == "acme-001"
    assert resp.json()["kpis"]["devices"] == 10


def test_post_new_thread_and_list_and_get_threads(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    headers = _login(client, "code-r1", "acme_admin")

    msg = client.post(
        "/v1/threads/new/messages",
        json={"message": "Give me an Acme fleet overview"},
        headers=headers,
    )
    assert msg.status_code == 200, msg.text
    assert msg.headers.get("X-Trace-Id")
    tid = msg.json()["thread_id"]
    assert tid.startswith("acme-001:")
    assert msg.json()["status"] == "completed"

    tlist = client.get("/v1/threads", headers=headers)
    assert tlist.status_code == 200
    assert any(t["thread_id"] == tid for t in tlist.json()["threads"])

    tdet = client.get(f"/v1/threads/{tid}", headers=headers)
    assert tdet.status_code == 200
    assert tdet.json()["thread"]["thread_id"] == tid


def test_proposals_list_approve_executes_and_populates_outbox(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, container = _build_test_client(db_manager)
    headers = _login(client, "code-r1", "acme_admin")
    msg = client.post(
        "/v1/threads/new/messages",
        json={"message": "Overview"},
        headers=headers,
    )
    assert msg.status_code == 200
    tid = msg.json()["thread_id"]
    turn_id = msg.json()["turn_id"]

    ctx = TenantContext(
        company_id="acme-001", actor_id="rev_01:acme_admin", role="it_admin", trace_id="tr-api-prop"
    )
    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(
        ctx, ["compliance_drift"], container.telemetry_repo, container.policy_svc
    ).evidence:
        ledger.add(ev)
    for ev in get_device_details(
        ctx, "1LYSSFD074BB", container.telemetry_repo, container.policy_svc
    ).evidence:
        ledger.add(ev)
    handles = [
        ev.handle
        for ev in ledger.all()
        if ev.handle is not None
        and ev.device_id == "1LYSSFD074BB"
        and ev.metric.startswith("compliance:screen_lock")
    ]
    st = container.guarded.action_service.stage(
        ctx,
        [
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="1LYSSFD074BB",
                arguments={"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "note": "Fix"},
                rationale="Fix screen_lock",
                evidence_handles=handles,
            )
        ],
        tid,
        turn_id,
        ledger,
        container.clock,
    )
    pid = st.staged[0]["proposal_id"]

    plist = client.get("/v1/proposals?status=pending_approval", headers=headers)
    assert plist.status_code == 200
    assert any(p["proposal_id"] == pid for p in plist.json()["proposals"])

    dec = client.post(
        f"/v1/proposals/{pid}/decision",
        json={"decision": "approve", "note": "Approved via API"},
        headers=headers,
    )
    assert dec.status_code == 200
    assert dec.json()["decision"]["applied"] is True
    assert dec.json()["execution"]["status"] == "executed"

    outbox = client.get("/v1/outbox", headers=headers)
    assert outbox.status_code == 200
    assert len(outbox.json()["items"]) == 1
    assert outbox.json()["items"][0]["proposal_id"] == pid


def test_audit_verify_endpoint(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    headers = _login(client, "code-r1", "acme_admin")
    client.post("/v1/threads/new/messages", json={"message": "Overview"}, headers=headers)

    verify = client.get("/v1/audit/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


def test_cross_tenant_thread_returns_identical_404(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    acme_headers = _login(client, "code-r1", "acme_admin")
    globex_headers = _login(client, "code-r1", "globex_admin")

    tid = client.post(
        "/v1/threads/new/messages", json={"message": "Acme"}, headers=acme_headers
    ).json()["thread_id"]

    missing = client.get("/v1/threads/globex-002:does-not-exist", headers=globex_headers)
    cross = client.get(f"/v1/threads/{tid}", headers=globex_headers)
    assert missing.status_code == 404
    assert cross.status_code == 404
    assert cross.json() == missing.json()


def test_other_actor_same_company_thread_returns_identical_404(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    r1_headers = _login(client, "code-r1", "acme_admin")
    r2_headers = _login(client, "code-r2", "acme_admin")

    tid = client.post(
        "/v1/threads/new/messages", json={"message": "Acme"}, headers=r1_headers
    ).json()["thread_id"]

    missing = client.get("/v1/threads/acme-001:missing", headers=r2_headers)
    other_get = client.get(f"/v1/threads/{tid}", headers=r2_headers)
    other_post = client.post(
        f"/v1/threads/{tid}/messages", json={"message": "Hijack"}, headers=r2_headers
    )
    assert other_get.status_code == 404
    assert other_get.json() == missing.json()
    assert other_post.status_code == 404
    assert other_post.json() == missing.json()


def test_viewer_proposal_decision_returns_403(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    viewer_headers = _login(client, "code-r2", "acme_viewer")
    resp = client.post(
        "/v1/proposals/any-proposal-id/decision",
        json={"decision": "approve"},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


def test_limit_exceeded_returns_429_with_limit_code(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    cfg = get_settings().model_copy(
        update={
            "app_env": "prod",
            "demo_access_codes": "code-r1:rev_01",
            "session_signing_key": SecretStr("b2" * 32),
            "gemini_api_key": SecretStr("fake-live-key"),
            "llm_replay_mode": "off",
            "demo_turns_per_code": 1,
            "demo_turns_per_hour": 20,
            "demo_spend_cap_usd": 5.0,
        }
    )
    client, _ = _build_test_client(db_manager, settings=cfg)
    headers = _login(client, "code-r1", "acme_admin")

    first = client.post("/v1/threads/new/messages", json={"message": "Turn 1"}, headers=headers)
    assert first.status_code == 200
    tid = first.json()["thread_id"]

    second = client.post(f"/v1/threads/{tid}/messages", json={"message": "Turn 2"}, headers=headers)
    assert second.status_code == 429
    assert second.json()["code"] == "turn_cap_reached"
    assert second.headers.get("X-Trace-Id")


def test_tampered_jwt_company_id_rejected_with_401(
    db_manager: DatabaseManager,
    clean_spend_ledger: None,
) -> None:
    client, _ = _build_test_client(db_manager)
    headers = _login(client, "code-r1", "acme_admin")
    raw_token = headers["Authorization"].split(" ", 1)[1]
    hdr_b64, pay_b64, sig_b64 = raw_token.split(".")
    padded = pay_b64 + "=" * (-len(pay_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims["company_id"] = "globex-002"
    tampered_pay = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode("ascii")
    )
    tampered_token = f"{hdr_b64}.{tampered_pay}.{sig_b64}"
    resp = client.get("/v1/insights", headers={"Authorization": f"Bearer {tampered_token}"})
    assert resp.status_code == 401
    assert resp.headers.get("X-Trace-Id")
