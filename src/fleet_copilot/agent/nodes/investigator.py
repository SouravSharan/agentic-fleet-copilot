"""`investigator` node: role-filtered tool-calling LLM preserving Gemini 3 thought signatures."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from fleet_copilot.agent.deadline import check_turn_deadline
from fleet_copilot.agent.prompts import load_prompt
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext

logger = logging.getLogger(__name__)

MAX_INVESTIGATOR_TURNS = 4


def trim_messages_to_last_turns(
    messages: Sequence[BaseMessage],
    *,
    max_turns: int = MAX_INVESTIGATOR_TURNS,
) -> list[BaseMessage]:
    """Keep only the last `max_turns` complete turns, cutting strictly at `HumanMessage` boundaries.

    Never mutates or splits an `AIMessage(tool_calls=...)` / `ToolMessage` sequence, preserving
    Gemini 3 thought signatures across multi-turn conversations (10 §5, 11 §2 B1).
    """
    msg_list = list(messages)
    if max_turns <= 0 or not msg_list:
        return msg_list
    human_indices = [i for i, m in enumerate(msg_list) if isinstance(m, HumanMessage)]
    if len(human_indices) <= max_turns:
        return msg_list
    start_idx = human_indices[-max_turns]
    return msg_list[start_idx:]


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
        digest = list(state.get("conversation_digest") or [])[-3:]
        if digest:
            sys_text = (
                f"{sys_text}\n\nRecent conversation summary:\n{json.dumps(digest, sort_keys=True)}"
            )

        # Keep last 4 complete turns at HumanMessage boundaries without mutating any message.
        history = trim_messages_to_last_turns(
            list(state.get("messages") or []), max_turns=MAX_INVESTIGATOR_TURNS
        )
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
