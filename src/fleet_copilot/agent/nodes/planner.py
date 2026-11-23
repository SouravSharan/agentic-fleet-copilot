"""`planner` node: structured LLM classification into `Plan` with parse-retry + audit."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError

from fleet_copilot.agent.prompts import load_prompt
from fleet_copilot.agent.schemas import Plan
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import PlanCreatedPayload

logger = logging.getLogger(__name__)


class StructuredOutputParseError(RuntimeError):
    """Raised when structured LLM output cannot be parsed after retry."""


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


def _invoke_plan_with_retry(
    structured: Any,
    messages: list[Any],
    *,
    max_retries: int = 1,
) -> tuple[Plan, Any]:
    last_err: Exception | None = None
    prompt_msgs = list(messages)
    for attempt in range(max_retries + 1):
        out = structured.invoke(prompt_msgs)
        raw_msg = out.get("raw") if isinstance(out, dict) else None
        parsed = out.get("parsed") if isinstance(out, dict) and "parsed" in out else out
        parsing_err = out.get("parsing_error") if isinstance(out, dict) else None
        if parsed is not None and parsing_err is None:
            try:
                plan = parsed if isinstance(parsed, Plan) else Plan.model_validate(parsed)
                return plan, raw_msg
            except ValidationError as exc:
                last_err = exc
        else:
            last_err = (
                parsing_err
                if isinstance(parsing_err, Exception)
                else ValueError("Structured output returned parsed=None")
            )
        if attempt < max_retries:
            prompt_msgs = [
                *messages,
                HumanMessage(
                    content=(
                        "Your previous output could not be parsed as valid Plan JSON. "
                        "Return ONLY valid JSON matching the Plan schema."
                    )
                ),
            ]
    raise StructuredOutputParseError(f"Planner failed to produce valid Plan JSON: {last_err}")


def make_planner_node(deps: Any) -> Any:
    """Build the `planner` node closed over `AgentDeps`."""

    def planner(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        template, _ = load_prompt("planner")
        digest = list(state.get("conversation_digest") or [])[-3:]
        sys_text = template.format(
            company_id=ctx.company_id,
            company_name=_company_display_name(ctx.company_id),
            role=ctx.role,
            telemetry_as_of=state.get("telemetry_as_of", ""),
            allowed_tools=", ".join(state.get("allowed_tool_names", [])),
            conversation_history_json=json.dumps(digest, sort_keys=True),
        )

        llm = deps.model_factory("planner")
        structured = llm.with_structured_output(Plan, include_raw=True)
        plan, raw_msg = _invoke_plan_with_retry(
            structured,
            [
                SystemMessage(content=sys_text),
                HumanMessage(content=state.get("user_message", "")),
            ],
            max_retries=1,
        )
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
