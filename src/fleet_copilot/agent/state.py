"""JSON-native LangGraph state definition and reducers for Fleet Copilot (05 §3.2)."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


def merge_evidence_dicts(
    existing: dict[str, dict[str, Any]] | None,
    incoming: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Merge serialized `EvidenceLedger` handle maps so handles remain unique per thread."""
    merged = dict(existing or {})
    if incoming:
        merged.update(incoming)
    return merged


def turn_list_reducer(
    existing: list[Any] | None,
    incoming: Any,
) -> list[Any]:
    """Append list items within a turn, or reset cleanly when `{"__reset__": True}` is emitted."""
    if isinstance(incoming, dict) and incoming.get("__reset__"):
        return list(incoming.get("items", []))
    base = list(existing or [])
    if isinstance(incoming, list):
        base.extend(incoming)
    elif incoming is not None:
        base.append(incoming)
    return base


def reset_list(items: list[Any] | None = None) -> Any:
    """Return a JSON-native sentinel instructing `turn_list_reducer` to start a fresh turn list."""
    return {"__reset__": True, "items": list(items or [])}


class CopilotState(TypedDict, total=False):
    """JSON-native LangGraph state (plus LangChain messages for thought-signature fidelity)."""

    messages: Annotated[list[AnyMessage], add_messages]
    turn_id: str
    thread_id: str
    user_message: str
    telemetry_as_of: str
    resolved_policies: dict[str, dict[str, Any]]
    allowed_tool_names: list[str]
    plan: dict[str, Any] | None
    evidence: Annotated[dict[str, dict[str, Any]], merge_evidence_dicts]
    tool_trace: Annotated[list[dict[str, Any]], turn_list_reducer]
    tool_outputs: Annotated[list[dict[str, Any]], turn_list_reducer]
    insights: Annotated[list[dict[str, Any]], turn_list_reducer]
    drafts: Annotated[list[dict[str, Any]], turn_list_reducer]
    verdicts: Annotated[list[dict[str, Any]], turn_list_reducer]
    staged_proposals: list[dict[str, Any]]
    approval_packet: dict[str, Any] | None
    decisions: dict[str, Any]
    decision_note: str | None
    execution_results: list[dict[str, Any]]
    answer: dict[str, Any] | None
    answer_markdown: str
    status: str
    refusal_reason: str | None
    verifier_violations: list[dict[str, Any]]
    verifier_retries: int
    warnings: Annotated[list[str], turn_list_reducer]
    step_count: int
    replans: int
    usage: Annotated[list[dict[str, Any]], turn_list_reducer]
