"""FastAPI application factory with `X-Trace-Id` middleware and service dependency container."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response

from fleet_copilot.clock import Clock, SystemClock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db.postgres import get_db_manager, make_checkpointer
from fleet_copilot.interfaces.api.routes import build_router
from fleet_copilot.observability.logging import bind_trace_id, configure_logging
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.demo_auth import validate_auth_config
from fleet_copilot.services.guarded import GuardedCopilot
from fleet_copilot.services.policy import PolicyService


@dataclass
class ApiContainer:
    """Service dependencies injected into HTTP routes (routes never talk to the DB directly)."""

    guarded: GuardedCopilot
    telemetry_repo: Any
    store: Any
    policy_svc: PolicyService
    audit_svc: Any
    clock: Clock
    settings: Settings


def _default_container(settings: Settings) -> ApiContainer:
    db = get_db_manager()
    repo = PgTelemetryRepository(db, settings=settings)
    store = PgCopilotStore(db)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db)
    clock = SystemClock()
    copilot = CopilotService(
        db=db,
        telemetry_repo=repo,
        copilot_store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        adapter=MockAdapter(),
        checkpointer=make_checkpointer(db, settings=settings),
        clock=clock,
        settings=settings,
    )
    guarded = GuardedCopilot(copilot, settings=settings, clock=clock)
    return ApiContainer(
        guarded=guarded,
        telemetry_repo=repo,
        store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        clock=clock,
        settings=settings,
    )


def create_app(
    *,
    container: ApiContainer | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Construct the minimal FastAPI app over `GuardedCopilot`."""
    cfg = settings if settings is not None else get_settings()
    configure_logging(log_format=cfg.log_format, log_level=cfg.log_level)
    validate_auth_config(cfg)

    resolved = container if container is not None else _default_container(cfg)
    app = FastAPI(title="Fleet Copilot API", version="0.1.0")

    @app.middleware("http")
    async def trace_id_middleware(request: Request, call_next: Any) -> Response:
        incoming = (request.headers.get("X-Trace-Id") or "").strip()
        trace_id = (
            incoming
            if incoming
            and len(incoming) <= 64
            and incoming.replace("-", "").replace("_", "").isalnum()
            else f"api-{uuid.uuid4().hex[:12]}"
        )
        request.state.trace_id = trace_id
        with bind_trace_id(trace_id):
            response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    app.include_router(build_router(resolved))
    return app
