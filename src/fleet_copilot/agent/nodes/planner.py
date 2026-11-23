"""`planner` node: structured LLM classification into `Plan` + audit `plan_created`."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from fleet_copilot.agent.prompts import load_prompt
from fleet_copilot.agent.schemas import Plan
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import PlanCreatedPayload

logger = logging.getLogger(__name__)


def _extract_usage(raw_msg: Any, node: str, model_id: str) -> dict[str, Any]:
    meta = getattr(raw_msg, "usage_metadata", None) or {}
    return {
        "node": node,
        "model": model_id,
        "input_tokens": int(meta.get("input_tokens", 0) or 0),
        "output_tokens": int(meta.get("output_tokens", 0) or 0),
    }


def _company_display_name(company_id: str) -> str:
    return company_id.split("-", 1)[0].title()


def make_planner_node(deps: Any) -> Any:
    """Build the `planner` node closed over `AgentDeps`."""

    def planner(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        template, _ = load_prompt("planner")
        sys_text = template.format(
            company_id=ctx.company_id,
            company_name=_company_display_name(ctx.company_id),
            role=ctx.role,
            telemetry_as_of=state.get("telemetry_as_of", ""),
            allowed_tools=", ".join(state.get("allowed_tool_names", [])),
        )

        llm = deps.model_factory("planner")
        structured = llm.with_structured_output(Plan, include_raw=True)
        out = structured.invoke(
            [
                SystemMessage(content=sys_text),
                HumanMessage(content=state.get("user_message", "")),
            ]
        )

        if isinstance(out, dict) and "parsed" in out:
            plan_obj = out["parsed"]
            raw_msg = out.get("raw")
        else:
            plan_obj = out
            raw_msg = None

        plan = plan_obj if isinstance(plan_obj, Plan) else Plan.model_validate(plan_obj)
        plan_dict = plan.model_dump(mode="json")

        turn_id = state.get("turn_id", "")
        thread_id = state.get("thread_id", "")
        deps.copilot_store.update_turn(ctx.company_id, turn_id, plan_json=plan_dict)
        deps.audit_svc.record_standalone(
            ctx,
            "plan_created",
            PlanCreatedPayload(
                turn_id=turn_id,
                intent=plan.intent,
                steps_count=len(plan.steps),
                refers_to_other_org=plan.refers_to_other_org,
            ),
            thread_id=thread_id,
        )

        logger.info(
            "plan_created",
            extra={
                "company_id": ctx.company_id,
                "turn_id": turn_id,
                "intent": plan.intent,
                "steps_count": len(plan.steps),
                "refers_to_other_org": plan.refers_to_other_org,
            },
        )

        refusal_reason: str | None = None
        if plan.refers_to_other_org:
            refusal_reason = "cross_tenant_request"
        elif plan.role_violation or (ctx.role == "viewer" and plan.intent == "action"):
            refusal_reason = "role_forbidden"
        elif plan.intent == "out_of_scope":
            refusal_reason = "out_of_scope"

        return {
            "plan": plan_dict,
            "refusal_reason": refusal_reason,
            "usage": [_extract_usage(raw_msg, "planner", deps.model_id)],
        }

    return planner
