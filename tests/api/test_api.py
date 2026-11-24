"""API integration tests covering JWT auth, tenant/actor isolation, RBAC, limits, and endpoints."""

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


def test_api_happy_path_all_endpoints_and_trace_id(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    client, container = _build_test_client(db_manager)

    # 1. Health check
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.headers.get("X-Trace-Id")

    # 2. Auth login (bad login rejected with 401; valid succeeds)
    bad_login = client.post("/v1/auth/demo-login", json={"code": "bad", "persona": "acme_admin"})
    assert bad_login.status_code == 401
    assert bad_login.headers.get("X-Trace-Id")

    headers = _login(client, "code-r1", "acme_admin")

    # 3. GET /v1/insights
    ins = client.get("/v1/insights", headers=headers)
    assert ins.status_code == 200
    assert ins.headers.get("X-Trace-Id")
    body = ins.json()
    assert body["company_id"] == "acme-001"
    assert body["kpis"]["devices"] == 10

    # 4. POST /v1/threads/new/messages creates "<company_id>:<uuid>" thread
    msg = client.post(
        "/v1/threads/new/messages",
        json={"message": "Give me an Acme fleet overview"},
        headers=headers,
    )
    assert msg.status_code == 200, msg.text
    assert msg.headers.get("X-Trace-Id")
    turn_data = msg.json()
    tid = turn_data["thread_id"]
    assert tid.startswith("acme-001:")
    assert turn_data["status"] == "completed"

    # 5. GET /v1/threads and GET /v1/threads/{id}
    tlist = client.get("/v1/threads", headers=headers)
    assert tlist.status_code == 200
    assert any(t["thread_id"] == tid for t in tlist.json()["threads"])

    tdet = client.get(f"/v1/threads/{tid}", headers=headers)
    assert tdet.status_code == 200
    assert tdet.json()["thread"]["thread_id"] == tid

    # 6. Proposals + approve via API -> executed -> outbox populated
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
        turn_data["turn_id"],
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
    assert dec.status_code == 200, dec.text
    assert dec.json()["decision"]["applied"] is True
    assert dec.json()["execution"]["status"] == "executed"

    # 7. GET /v1/outbox
    outbox = client.get("/v1/outbox", headers=headers)
    assert outbox.status_code == 200
    assert len(outbox.json()["items"]) == 1
    assert outbox.json()["items"][0]["proposal_id"] == pid

    # 8. GET /v1/audit/verify
    verify = client.get("/v1/audit/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


def test_api_isolation_404_viewer_403_tampered_jwt_401_and_limits_429(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    clean_spend_ledger: None,
) -> None:
    cfg = get_settings().model_copy(
        update={
            "app_env": "prod",
            "demo_access_codes": "code-r1:rev_01,code-r2:rev_02",
            "session_signing_key": SecretStr("b2" * 32),
            "gemini_api_key": SecretStr("fake-live-key"),
            "llm_replay_mode": "off",
            "demo_turns_per_code": 1,
            "demo_turns_per_hour": 20,
            "demo_spend_cap_usd": 5.0,
        }
    )
    client, _ = _build_test_client(db_manager, settings=cfg)

    r1_acme_headers = _login(client, "code-r1", "acme_admin")
    r2_acme_headers = _login(client, "code-r2", "acme_admin")
    r1_globex_headers = _login(client, "code-r1", "globex_admin")
    r2_viewer_headers = _login(client, "code-r2", "acme_viewer")

    # Create a thread as rev_01:acme_admin (uses rev_01's 1 allowed turn!)
    created = client.post(
        "/v1/threads/new/messages",
        json={"message": "Acme overview"},
        headers=r1_acme_headers,
    )
    assert created.status_code == 200
    acme_tid = created.json()["thread_id"]

    # 1. Cross-tenant 404 has exact same body as genuinely missing thread
    missing_resp = client.get("/v1/threads/globex-002:does-not-exist", headers=r1_globex_headers)
    cross_tenant_resp = client.get(f"/v1/threads/{acme_tid}", headers=r1_globex_headers)
    assert missing_resp.status_code == 404
    assert cross_tenant_resp.status_code == 404
    assert cross_tenant_resp.json() == missing_resp.json()

    # 2. Same company, OTHER actor -> 404 with identical body (both GET and POST)
    other_actor_get = client.get(f"/v1/threads/{acme_tid}", headers=r2_acme_headers)
    other_actor_post = client.post(
        f"/v1/threads/{acme_tid}/messages",
        json={"message": "Hijack attempt"},
        headers=r2_acme_headers,
    )
    assert other_actor_get.status_code == 404
    assert other_actor_get.json() == missing_resp.json()
    assert other_actor_post.status_code == 404
    assert other_actor_post.json() == missing_resp.json()

    # 3. Viewer deciding a proposal -> 403
    viewer_dec = client.post(
        "/v1/proposals/any-proposal-id/decision",
        json={"decision": "approve"},
        headers=r2_viewer_headers,
    )
    assert viewer_dec.status_code == 403

    # 4. Limit exceeded -> 429 with limit code (rev_01 already used 1/1 turns!)
    limited = client.post(
        f"/v1/threads/{acme_tid}/messages",
        json={"message": "Second turn should hit turn_cap_reached"},
        headers=r1_acme_headers,
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "turn_cap_reached"
    assert limited.headers.get("X-Trace-Id")

    # 5. Tampered JWT (modified company_id payload) -> rejected with 401
    raw_token = r1_acme_headers["Authorization"].split(" ", 1)[1]
    hdr_b64, pay_b64, sig_b64 = raw_token.split(".")
    padded = pay_b64 + "=" * (-len(pay_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims["company_id"] = "globex-002"
    tampered_pay = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode("ascii")
    )
    tampered_token = f"{hdr_b64}.{tampered_pay}.{sig_b64}"
    tampered = client.get("/v1/insights", headers={"Authorization": f"Bearer {tampered_token}"})
    assert tampered.status_code == 401
    assert tampered.headers.get("X-Trace-Id")
