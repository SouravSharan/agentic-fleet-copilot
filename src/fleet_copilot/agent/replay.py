"""Record/replay wrapper over `ChatGoogleGenerativeAI` preserving thought signatures."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, ConfigDict, PrivateAttr


class CassetteMiss(RuntimeError):
    """Raised in strict `replay` mode when a request key is absent from the cassette."""


def _encode_value(obj: Any) -> Any:
    """Recursively convert arbitrary Python/Pydantic/bytes objects to canonical JSON structures."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return {"__b64__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, BaseModel):
        return _encode_value(obj.model_dump(mode="python"))
    if isinstance(obj, dict):
        return {str(k): _encode_value(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_encode_value(item) for item in obj]
    return str(obj)


def _decode_value(obj: Any) -> Any:
    """Restore base64-tagged bytes markers recursively."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"__b64__"} and isinstance(obj["__b64__"], str):
            return base64.b64decode(obj["__b64__"].encode("ascii"))
        return {k: _decode_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_value(item) for item in obj]
    return obj


def _canonical_json(obj: Any) -> str:
    return json.dumps(_encode_value(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_kwargs_for_key(
    *,
    kwargs: dict[str, Any],
    thinking_config: Any,
    max_output_tokens: int | None,
    seed: int | None,
    temperature: float | None,
) -> dict[str, Any]:
    """Extract canonical request-shaping kwargs (tools, schemas, generation/thinking config)."""
    kept_keys = (
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "response_mime_type",
        "response_json_schema",
        "response_schema",
        "stop",
    )
    normalized: dict[str, Any] = {}
    for k in kept_keys:
        if k in kwargs and kwargs[k] is not None:
            normalized[k] = _encode_value(kwargs[k])
    if thinking_config is not None:
        normalized["thinking_config"] = _encode_value(thinking_config)
    if max_output_tokens is not None:
        normalized["max_output_tokens"] = max_output_tokens
    if seed is not None:
        normalized["seed"] = seed
    if temperature is not None:
        normalized["temperature"] = temperature
    return normalized


def _strip_volatile_message_ids(serialized_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop volatile LangGraph `add_messages` UUIDs (`data.id`) from replay keys."""
    cleaned: list[dict[str, Any]] = []
    for item in serialized_messages:
        entry = dict(item)
        data = entry.get("data")
        if isinstance(data, dict) and "id" in data:
            new_data = dict(data)
            new_data.pop("id", None)
            entry["data"] = new_data
        cleaned.append(entry)
    return cleaned


def canonical_replay_key(
    *,
    model: str,
    messages: list[BaseMessage],
    kwargs: dict[str, Any],
    thinking_config: Any = None,
    max_output_tokens: int | None = None,
    seed: int | None = None,
    temperature: float | None = None,
) -> str:
    """Compute deterministic SHA-256 cassette key for an LLM request."""
    payload = {
        "model": model,
        "messages": _encode_value(_strip_volatile_message_ids(messages_to_dict(messages))),
        "kwargs": _normalize_kwargs_for_key(
            kwargs=kwargs,
            thinking_config=thinking_config,
            max_output_tokens=max_output_tokens,
            seed=seed,
            temperature=temperature,
        ),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def serialize_chat_result(result: ChatResult) -> dict[str, Any]:
    """Serialize `ChatResult` preserving `additional_kwargs`, `tool_calls`, and usage metadata."""
    gens: list[dict[str, Any]] = []
    for gen in result.generations:
        msg = gen.message
        msg_dict = messages_to_dict([msg])[0]
        entry: dict[str, Any] = {
            "message": _encode_value(msg_dict),
            "generation_info": _encode_value(gen.generation_info),
        }
        if isinstance(msg, AIMessage):
            entry["usage_metadata"] = _encode_value(msg.usage_metadata)
            entry["tool_calls"] = _encode_value(msg.tool_calls)
            entry["additional_kwargs"] = _encode_value(msg.additional_kwargs)
            entry["response_metadata"] = _encode_value(msg.response_metadata)
        gens.append(entry)
    return {
        "generations": gens,
        "llm_output": _encode_value(result.llm_output),
    }


def deserialize_chat_result(data: dict[str, Any]) -> ChatResult:
    """Reconstruct a `ChatResult` from `serialize_chat_result()`."""
    gens: list[ChatGeneration] = []
    for entry in data.get("generations", []):
        msg_raw = _decode_value(entry["message"])
        msg = messages_from_dict([msg_raw])[0]
        if isinstance(msg, AIMessage):
            extra_kwargs = _decode_value(entry.get("additional_kwargs"))
            if extra_kwargs is not None:
                msg.additional_kwargs = extra_kwargs
            tc = _decode_value(entry.get("tool_calls"))
            if tc is not None:
                msg.tool_calls = tc
            usage = _decode_value(entry.get("usage_metadata"))
            if usage is not None:
                msg.usage_metadata = usage
            resp_meta = _decode_value(entry.get("response_metadata"))
            if resp_meta is not None:
                msg.response_metadata = resp_meta
        gen_info = _decode_value(entry.get("generation_info"))
        gens.append(ChatGeneration(message=msg, generation_info=gen_info))
    llm_output = _decode_value(data.get("llm_output"))
    return ChatResult(generations=gens, llm_output=llm_output)


class CassetteStore:
    """Append-only JSONL store for recorded LLM responses keyed by canonical request SHA-256."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._cursors: dict[str, int] = defaultdict(int)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                rec = json.loads(stripped)
                self._entries[str(rec["key"])].append(rec["result"])

    def get(self, key: str) -> ChatResult:
        """Return the next recorded `ChatResult` for `key`, raising `CassetteMiss` if absent."""
        bucket = self._entries.get(key)
        if not bucket:
            raise CassetteMiss(f"Cassette miss for key {key[:16]}... in {self.path}")
        idx = self._cursors[key]
        if idx >= len(bucket):
            # Fall back to last recorded response if an identical prompt repeats beyond count
            idx = len(bucket) - 1
        else:
            self._cursors[key] = idx + 1
        return deserialize_chat_result(bucket[idx])

    def append(self, key: str, result: ChatResult) -> None:
        """Persist a new `(key, result)` entry to the JSONL cassette file."""
        serialized = serialize_chat_result(result)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"key": key, "result": serialized}, sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._entries[key].append(serialized)


class ReplayableChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """`ChatGoogleGenerativeAI` subclass adding strict record/replay cassette support."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    replay_mode: Literal["off", "record", "replay"] = "off"
    cassette_path: Path | None = None
    _cassette_store: CassetteStore | None = PrivateAttr(default=None)
    _inner_generate_fn: Callable[..., ChatResult] | None = PrivateAttr(default=None)

    def set_inner_generate_for_test(self, fn: Callable[..., ChatResult]) -> None:
        """Inject a fake inner `_generate` callable for offline unit tests."""
        self._inner_generate_fn = fn

    def set_cassette_store(self, store: CassetteStore) -> None:
        """Bind a shared `CassetteStore` so cursors persist across nodes and eval repeats."""
        self._cassette_store = store

    def _get_store(self) -> CassetteStore:
        if self._cassette_store is None:
            if self.cassette_path is None:
                raise ValueError("cassette_path must be set when replay_mode is record or replay")
            self._cassette_store = CassetteStore(self.cassette_path)
        return self._cassette_store

    def _call_inner(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._inner_generate_fn is not None:
            return self._inner_generate_fn(messages, stop=stop, run_manager=run_manager, **kwargs)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.replay_mode == "off":
            return self._call_inner(messages, stop=stop, run_manager=run_manager, **kwargs)

        key_kwargs = dict(kwargs)
        if stop is not None:
            key_kwargs["stop"] = stop
        key = canonical_replay_key(
            model=self.model,
            messages=messages,
            kwargs=key_kwargs,
            thinking_config=self.thinking_config,
            max_output_tokens=self.max_output_tokens,
            seed=self.seed,
            temperature=self.temperature,
        )
        store = self._get_store()
        if self.replay_mode == "replay":
            return store.get(key)

        result = self._call_inner(messages, stop=stop, run_manager=run_manager, **kwargs)
        store.append(key, result)
        return result
