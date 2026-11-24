"""HTTP routes delegating exclusively to `GuardedCopilot` and domain services (zero direct DB)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from fleet_copilot.domain.models import TenantContext
from fleet_copilot.interfaces.api.auth import (
    SESSION_TTL_SECONDS,
    issue_session_token,
    require_tenant_context,
)
from fleet_copilot.interfaces.api.schemas import (
    DemoLoginRequest,
    DemoLoginResponse,
    PostMessageRequest,
    ProposalDecisionRequest,
)
from fleet_copilot.services.briefing import build_briefing
from fleet_copilot.services.copilot import ThreadNotOwnedError
from fleet_copilot.services.demo_auth import DemoAuthError, sign_in

_NOT_FOUND_DETAIL = "Not found"


def build_router(container: Any) -> APIRouter:
    """Build the `/healthz` and `/v1/*` router wired to `container` services."""
    router = APIRouter()

    def _ctx(request: Request) -> TenantContext:
        return require_tenant_context(request, container.settings)

    @router.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "mode": container.guarded.mode()}

    @router.post("/v1/auth/demo-login", response_model=DemoLoginResponse)
    def demo_login(body: DemoLoginRequest) -> DemoLoginResponse:
        try:
            session = sign_in(body.code, body.persona, settings=container.settings)
        except DemoAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc

        token = issue_session_token(session, settings=container.settings)
        return DemoLoginResponse(
            access_token=token,
            expires_in_seconds=SESSION_TTL_SECONDS,
            actor_id=session.actor_id,
            company_id=session.company_id,
            role=session.role,
            rev_id=session.rev_id,
            persona=session.persona,
        )

    @router.get("/v1/insights")
    def get_insights(ctx: TenantContext = Depends(_ctx)) -> dict[str, Any]:  # noqa: B008
        briefing = build_briefing(
            ctx,
            container.telemetry_repo,
            container.store,
            container.policy_svc,
        )
        return briefing.model_dump(mode="json")

    @router.get("/v1/threads")
    def list_threads(
        ctx: TenantContext = Depends(_ctx),  # noqa: B008
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        rows = container.guarded.list_threads(ctx, limit=limit)
        return {"threads": rows}

    @router.get("/v1/threads/{thread_id}")
    def get_thread(
        thread_id: str,
        ctx: TenantContext = Depends(_ctx),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            data = container.guarded.get_thread(ctx, thread_id)
        except ThreadNotOwnedError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_NOT_FOUND_DETAIL,
            ) from exc
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
        return data

    @router.post("/v1/threads/{thread_id}/messages")
    def post_thread_message(
        thread_id: str,
        body: PostMessageRequest,
        ctx: TenantContext = Depends(_ctx),  # noqa: B008
    ) -> Any:
        if thread_id == "new":
            # Always create threads in the exact "<company_id>:<uuid>" prefix format so
            # ownership checks and ops.demo_reset_company checkpoint cleanup always match.
            target_tid = f"{ctx.company_id}:{uuid.uuid4().hex[:12]}"
        else:
            try:
                existing = container.guarded.get_thread(ctx, thread_id)
            except ThreadNotOwnedError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_NOT_FOUND_DETAIL,
                ) from exc
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_NOT_FOUND_DETAIL,
                )
            target_tid = thread_id

        try:
            resp = container.guarded.run_turn(ctx, target_tid, body.message)
        except ThreadNotOwnedError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_NOT_FOUND_DETAIL,
            ) from exc

        if resp.status == "limited":
            limit_code = resp.warnings[0] if resp.warnings else "limited"
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": resp.answer_markdown, "code": limit_code},
            )
        return resp.model_dump(mode="json")

    @router.get("/v1/proposals")
    def list_proposals(
        ctx: TenantContext = Depends(_ctx),  # noqa: B008
        status_filter: str | None = Query(default=None, alias="status"),
        device_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        rows = container.guarded.action_service.list(ctx, status=status_filter, device_id=device_id)
        return {"proposals": rows}

    @router.post("/v1/proposals/{proposal_id}/decision")
    def decide_proposal(
        proposal_id: str,
        body: ProposalDecisionRequest,
        ctx: TenantContext = Depends(_ctx),  # noqa: B008
    ) -> dict[str, Any]:
        dec = container.guarded.action_service.decide(
            ctx,
            proposal_id,
            body.decision,
            "api",
            container.clock,
            note=body.note,
        )
        if ctx.role != "it_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only it_admin may decide action proposals.",
            )
        if dec.proposal is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

        execution = None
        if dec.applied and body.decision == "approve":
            exe = container.guarded.action_service.execute(ctx, proposal_id, container.clock)
            execution = exe.model_dump(mode="json")

        return {
            "decision": dec.model_dump(mode="json"),
            "execution": execution,
        }

    @router.get("/v1/outbox")
    def list_outbox(
        ctx: TenantContext = Depends(_ctx),  # noqa: B008
        channel: str | None = Query(default=None),
        proposal_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = container.store.list_outbox(
            ctx.company_id,
            channel=channel,
            proposal_id=proposal_id,
            limit=limit,
        )
        return {"items": rows}

    @router.get("/v1/audit/verify")
    def verify_audit(ctx: TenantContext = Depends(_ctx)) -> dict[str, Any]:  # noqa: B008
        return container.audit_svc.verify(ctx)

    return router
