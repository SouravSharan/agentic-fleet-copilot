"""Unit tests for strict replay/record cassette behavior on ReplayableChatGoogleGenerativeAI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr

from fleet_copilot.agent.replay import CassetteMiss, ReplayableChatGoogleGenerativeAI


def test_replay_strict_miss_raises(tmp_path: Path) -> None:
    cassette = tmp_path / "empty.jsonl"
    model = ReplayableChatGoogleGenerativeAI(
        model="gemini-3.8-flash",
        google_api_key=SecretStr("replay-dummy"),
        temperature=None,
        thinking_config={"thinking_level": "low"},
        replay_mode="replay",
        cassette_path=cassette,
    )
    with pytest.raises(CassetteMiss):
        model.invoke([HumanMessage(content="Never recorded prompt")])


def test_replay_record_then_replay_identical(tmp_path: Path) -> None:
    cassette = tmp_path / "case_01.jsonl"
    calls = 0

    def _fake_inner(_messages: Any, **_kwargs: Any) -> ChatResult:
        nonlocal calls
        calls += 1
        ai = AIMessage(
            content=[{"type": "text", "text": ""}],
            tool_calls=[
                {
                    "name": "find_devices",
                    "args": {"low_disk_only": True},
                    "id": "call_999",
                    "type": "tool_call",
                }
            ],
            additional_kwargs={
                "__gemini_function_call_thought_signatures__": {"call_999": "Ep8BCpwBAWk="},
                "raw_bytes_sig": b"\x01\x02\x03\xff",
            },
            usage_metadata={"input_tokens": 111, "output_tokens": 22, "total_tokens": 133},
        )
        return ChatResult(generations=[ChatGeneration(message=ai)])

    recorder = ReplayableChatGoogleGenerativeAI(
        model="gemini-3.8-flash",
        google_api_key=SecretStr("replay-dummy"),
        temperature=None,
        thinking_config={"thinking_level": "low"},
        replay_mode="record",
        cassette_path=cassette,
    )
    recorder.set_inner_generate_for_test(_fake_inner)

    recorded_msg = recorder.invoke([HumanMessage(content="Which devices are low on disk?")])
    assert calls == 1
    assert cassette.exists()

    replayer = ReplayableChatGoogleGenerativeAI(
        model="gemini-3.8-flash",
        google_api_key=SecretStr("replay-dummy"),
        temperature=None,
        thinking_config={"thinking_level": "low"},
        replay_mode="replay",
        cassette_path=cassette,
    )

    replayed_msg = replayer.invoke([HumanMessage(content="Which devices are low on disk?")])
    assert isinstance(replayed_msg, AIMessage)
    assert replayed_msg.tool_calls == recorded_msg.tool_calls
    assert replayed_msg.additional_kwargs == recorded_msg.additional_kwargs
    assert replayed_msg.additional_kwargs["raw_bytes_sig"] == b"\x01\x02\x03\xff"
    assert replayed_msg.usage_metadata == recorded_msg.usage_metadata

    # Changing the prompt or thinking config produces a strict CassetteMiss
    with pytest.raises(CassetteMiss):
        replayer.invoke([HumanMessage(content="Different prompt")])
