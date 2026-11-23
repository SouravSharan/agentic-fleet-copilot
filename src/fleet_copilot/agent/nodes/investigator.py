"""`investigator` node: role-filtered tool-calling LLM preserving Gemini 3 thought signatures."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from fleet_copilot.agent.deadline import check_turn_deadline
from fleet_copilot.agent.prompts import load_prompt
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext

logger = logging.getLogger(__name__)


def _company_display_name(company_id: str) -> str:
    return company_id.split("-", 1)[0].title()


def make_investigator_node(deps: Any) -> Any:
    """Build the `investigator` node closed over `AgentDeps`.

    Gemini 3 rule #2 (05 §3.11): NEVER edit, reorder, or split AIMessages that carry tool calls,
    so `__gemini_function_call_thought_signatures__` survive intact across tool loops and resumes.
    """

    def investigator(
        state: CopilotState,
        config: RunnableConfig,
        runtime: Runtime[TenantContext],
    ) -> dict[str, Any]:
        check_turn_deadline(config)
        ctx = runtime.context
        allowed_names = set(state.get("allowed_tool_names", []))
        bound_tools = [spec.tool for spec in deps.all_tool_specs if spec.name in allowed_names]

        template, _ = load_prompt("investigator")
        sys_text = template.format(
            company_id=ctx.company_id,
            company_name=_company_display_name(ctx.company_id),
            role=ctx.role,
            telemetry_as_of=state.get("telemetry_as_of", ""),
            plan_json=json.dumps(state.get("plan") or {}, sort_keys=True),
        )

        # Prepend SystemMessage to unmodified message history (never mutate existing messages).
        history = list(state.get("messages") or [])
        prompt_messages = [SystemMessage(content=sys_text), *history]

        llm = deps.model_factory("investigator")
        model_with_tools = llm.bind_tools(bound_tools) if bound_tools else llm
        response = model_with_tools.invoke(prompt_messages)

        meta = getattr(response, "usage_metadata", None) or {}
        usage_entry = {
            "node": "investigator",
            "model": deps.model_id,
            "input_tokens": int(meta.get("input_tokens", 0) or 0),
            "output_tokens": int(meta.get("output_tokens", 0) or 0),
        }

        return {
            "messages": [response],
            "usage": [usage_entry],
            "warnings": [],
        }

    return investigator
