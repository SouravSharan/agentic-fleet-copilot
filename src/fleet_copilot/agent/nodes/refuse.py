"""`refuse` node: renders scoped refusals (cross-tenant, viewer role, out-of-scope) + audits."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from fleet_copilot.agent.schemas import Answer
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import AccessDeniedPayload, RefusalPayload


def make_refuse_node(deps: Any) -> Any:
    """Build the `refuse` node closed over `AgentDeps`."""

    def refuse(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        reason = state.get("refusal_reason") or "out_of_scope"
        thread_id = state.get("thread_id", "")

        if reason == "cross_tenant_request":
            summary = (
                f"I cannot access data for other organizations. This session is strictly scoped "
                f"to your tenant (`{ctx.company_id}`), and I cannot confirm whether another "
                f"organization exists."
            )
            deps.audit_svc.record_standalone(
                ctx,
                "access_denied",
                AccessDeniedPayload(reason_code="cross_tenant_not_found", tool_name="planner"),
                thread_id=thread_id,
            )
        elif reason == "role_forbidden":
            summary = (
                f"Your role (`{ctx.role}`) has read-only access and cannot stage or execute "
                f"fleet actions. Ask an `it_admin` to propose and approve this action."
            )
            deps.audit_svc.record_standalone(
                ctx,
                "access_denied",
                AccessDeniedPayload(reason_code="role_forbidden", tool_name="planner"),
                thread_id=thread_id,
            )
        else:
            summary = (
                "I cannot fulfill that request. Fleet Copilot only answers grounded questions, "
                "analyzes 30-day fleet telemetry trends, and stages policy-checked actions for "
                "human approval within your tenant."
            )

        deps.audit_svc.record_standalone(
            ctx,
            "refusal",
            RefusalPayload(reason_code=reason),
            thread_id=thread_id,
        )

        ans = Answer(
            summary=summary,
            claims=[],
            caveats=[f"Refusal reason: {reason}"],
        )
        return {
            "answer": ans.model_dump(mode="json"),
            "status": "refused",
        }

    return refuse
