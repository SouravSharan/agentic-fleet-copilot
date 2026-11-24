"""Unit tests for investigator history trimming at HumanMessage boundaries."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from fleet_copilot.agent.nodes.investigator import trim_messages_to_last_turns
from tests.graph.fakes import make_tool_call_ai


def _make_turn(idx: int) -> list[BaseMessage]:
    tc_id = f"tc_{idx}"
    return [
        HumanMessage(content=f"Turn {idx} question"),
        make_tool_call_ai([{"name": "get_fleet_overview", "args": {}, "id": tc_id}]),
        ToolMessage(content='{"ok": true}', tool_call_id=tc_id, name="get_fleet_overview"),
        AIMessage(content=[{"type": "text", "text": f"Turn {idx} summary"}]),
    ]


def test_trim_keeps_last_4_complete_turns_at_human_boundary_and_preserves_signatures() -> None:
    """Trimming keeps <= 4 complete turns, never splits tool sequences or drops signatures."""
    all_msgs: list[BaseMessage] = []
    for i in range(1, 7):  # 6 complete turns
        all_msgs.extend(_make_turn(i))

    trimmed = trim_messages_to_last_turns(all_msgs, max_turns=4)
    assert isinstance(trimmed[0], HumanMessage)
    assert trimmed[0].content == "Turn 3 question"

    human_contents = [m.content for m in trimmed if isinstance(m, HumanMessage)]
    assert human_contents == [
        "Turn 3 question",
        "Turn 4 question",
        "Turn 5 question",
        "Turn 6 question",
    ]

    # Every AIMessage tool_call_id has a paired ToolMessage and retains its thought signature
    emitted_ids: list[str] = []
    for msg in trimmed:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            sigs = msg.additional_kwargs.get("__gemini_function_call_thought_signatures__", {})
            for tc in msg.tool_calls:
                emitted_ids.append(str(tc["id"]))
                assert str(tc["id"]) in sigs

    responded_ids = [m.tool_call_id for m in trimmed if isinstance(m, ToolMessage)]
    assert emitted_ids == ["tc_3", "tc_4", "tc_5", "tc_6"]
    assert responded_ids == ["tc_3", "tc_4", "tc_5", "tc_6"]


def test_trim_noop_when_four_or_fewer_turns() -> None:
    """4 or fewer turns pass through unmodified."""
    msgs: list[BaseMessage] = []
    for i in range(1, 5):
        msgs.extend(_make_turn(i))
    trimmed = trim_messages_to_last_turns(msgs, max_turns=4)
    assert trimmed == msgs
