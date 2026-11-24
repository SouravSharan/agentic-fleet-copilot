"""LangGraph workflow assembly for the Fleet Copilot orchestration graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from fleet_copilot.agent.llm import NodeName
from fleet_copilot.agent.nodes.execute_decisions import make_execute_decisions_node
from fleet_copilot.agent.nodes.finalize import make_finalize_node
from fleet_copilot.agent.nodes.human_approval import human_approval
from fleet_copilot.agent.nodes.investigator import make_investigator_node
from fleet_copilot.agent.nodes.load_context import make_load_context_node
from fleet_copilot.agent.nodes.planner import make_planner_node
from fleet_copilot.agent.nodes.refuse import make_refuse_node
from fleet_copilot.agent.nodes.stage_proposals import make_stage_proposals_node
from fleet_copilot.agent.nodes.synthesizer import make_synthesizer_node
from fleet_copilot.agent.nodes.tools import make_tools_node
from fleet_copilot.agent.nodes.verify_grounding import make_verify_grounding_node
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.clock import Clock
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.runtime_config import RuntimeConfig
from fleet_copilot.tools.registry import ToolSpec


@dataclass(frozen=True)
class AgentDeps:
    """All runtime dependencies injected into graph nodes (never serialized into state)."""

    telemetry_repo: Any
    copilot_store: Any
    policy_svc: Any
    action_svc: Any
    audit_svc: Any
    clock: Clock
    runtime_cfg: RuntimeConfig
    all_tool_specs: list[ToolSpec]
    model_factory: Callable[[NodeName], BaseChatModel]
    model_id: str
    actions_enabled: bool = True
    skip_planner: bool = False
    skip_verifier: bool = False


def _route_after_planner(state: CopilotState) -> str:
    if state.get("refusal_reason"):
        return "refuse"
    plan = state.get("plan") or {}
    if plan.get("needs_clarification"):
        return "finalize"
    return "investigator"


def _route_after_investigator(state: CopilotState) -> str:
    """Always route pending tool calls through `tools` so no dangling tool_calls remain."""
    msgs = state.get("messages") or []
    if msgs and isinstance(msgs[-1], AIMessage) and msgs[-1].tool_calls:
        return "tools"
    return "synthesizer"


def _make_route_after_tools() -> Callable[[CopilotState], str]:
    """After responding to all tool calls, exit to `synthesizer` on `budget_exhausted` flag."""

    def _route(state: CopilotState) -> str:
        if bool(state.get("budget_exhausted", False)):
            return "synthesizer"
        return "investigator"

    return _route


def _route_after_verifier(state: CopilotState) -> str:
    if state.get("verifier_violations"):
        return "synthesizer"
    if state.get("drafts"):
        return "stage_proposals"
    return "finalize"


def _route_after_stage(state: CopilotState) -> str:
    if state.get("staged_proposals"):
        return "human_approval"
    return "finalize"


def build_agent_graph(
    deps: AgentDeps,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Any:
    """Compile the Fleet Copilot `StateGraph` with explicit `context_schema=TenantContext`."""
    graph = StateGraph(CopilotState, context_schema=TenantContext)

    graph.add_node("load_context", make_load_context_node(deps))
    graph.add_node("planner", make_planner_node(deps))
    graph.add_node("refuse", make_refuse_node(deps))
    graph.add_node("investigator", make_investigator_node(deps))
    graph.add_node("tools", make_tools_node(deps))
    graph.add_node("synthesizer", make_synthesizer_node(deps))
    graph.add_node("verify_grounding", make_verify_grounding_node(deps))
    graph.add_node("stage_proposals", make_stage_proposals_node(deps))
    graph.add_node("human_approval", human_approval)
    graph.add_node("execute_decisions", make_execute_decisions_node(deps))
    graph.add_node("finalize", make_finalize_node(deps))

    graph.add_edge(START, "load_context")
    if deps.skip_planner:
        graph.add_edge("load_context", "investigator")
    else:
        graph.add_edge("load_context", "planner")
        graph.add_conditional_edges(
            "planner",
            _route_after_planner,
            {"refuse": "refuse", "finalize": "finalize", "investigator": "investigator"},
        )
    graph.add_conditional_edges(
        "investigator",
        _route_after_investigator,
        {"tools": "tools", "synthesizer": "synthesizer"},
    )
    graph.add_conditional_edges(
        "tools",
        _make_route_after_tools(),
        {"investigator": "investigator", "synthesizer": "synthesizer"},
    )
    if deps.skip_verifier:
        graph.add_conditional_edges(
            "synthesizer",
            _route_after_verifier,
            {
                "synthesizer": "synthesizer",
                "stage_proposals": "stage_proposals",
                "finalize": "finalize",
            },
        )
    else:
        graph.add_edge("synthesizer", "verify_grounding")
        graph.add_conditional_edges(
            "verify_grounding",
            _route_after_verifier,
            {
                "synthesizer": "synthesizer",
                "stage_proposals": "stage_proposals",
                "finalize": "finalize",
            },
        )
    graph.add_conditional_edges(
        "stage_proposals",
        _route_after_stage,
        {"human_approval": "human_approval", "finalize": "finalize"},
    )
    graph.add_edge("human_approval", "execute_decisions")
    graph.add_edge("execute_decisions", "finalize")
    graph.add_edge("refuse", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
