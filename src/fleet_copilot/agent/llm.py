"""Factory building per-node Gemini chat models from Settings and `config/runtime.yaml`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from fleet_copilot.agent.replay import CassetteStore, ReplayableChatGoogleGenerativeAI
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.runtime_config import RuntimeConfig, get_runtime_config

NodeName = Literal["planner", "investigator", "synthesizer"]


def parse_model_name(model_spec: str) -> str:
    """Strip optional `google_genai:` provider prefix and return the bare Gemini model name."""
    if ":" in model_spec:
        provider, bare = model_spec.split(":", 1)
        if provider != "google_genai":
            raise ValueError(
                f"Unsupported LLM provider {provider!r}; expected 'google_genai:<model>'"
            )
        return bare
    return model_spec


def cassette_path_for(
    *,
    cassette_dir: str | Path,
    model_spec: str,
    case_or_session: str,
) -> Path:
    """Return `$LLM_CASSETTE_DIR/<model>/<case_or_session>.jsonl`."""
    bare = parse_model_name(model_spec).replace("/", "_")
    return Path(cassette_dir) / bare / f"{case_or_session}.jsonl"


def make_chat_model(
    node: NodeName,
    *,
    settings: Settings | None = None,
    runtime_cfg: RuntimeConfig | None = None,
    cassette_name: str | None = None,
    cassette_store: CassetteStore | None = None,
    model_override: str | None = None,
    thinking_override: Literal["minimal", "low", "medium", "high"] | None = None,
) -> BaseChatModel:
    """Construct a per-node `ReplayableChatGoogleGenerativeAI` configured from `runtime.yaml`.

    Gemini 3 configuration rules:
    - `temperature` is left unset (`None`) so Gemini 3 uses its default sampling.
    - `thinking_config={"thinking_level": ...}` is set per node from `config/runtime.yaml`.
    - `disable_streaming=True` in `record` / `replay` modes.
    """
    cfg = settings if settings is not None else get_settings()
    rt = runtime_cfg if runtime_cfg is not None else get_runtime_config()
    node_cfg = rt.node_config(node)

    model_spec = model_override or cfg.llm_model
    bare_model = parse_model_name(model_spec)
    replay_mode_raw = cfg.llm_replay_mode.strip().lower()
    if replay_mode_raw not in ("off", "record", "replay"):
        raise ValueError(f"Invalid LLM_REPLAY_MODE: {cfg.llm_replay_mode!r}")
    replay_mode: Literal["off", "record", "replay"] = replay_mode_raw  # type: ignore[assignment]

    api_key: SecretStr | None = cfg.gemini_api_key
    if replay_mode == "replay" and api_key is None:
        api_key = SecretStr("replay-offline-placeholder-key")
    if api_key is None:
        raise ValueError(
            "GEMINI_API_KEY must be set in .env (or use LLM_REPLAY_MODE=replay for offline runs)"
        )

    c_path: Path | None = None
    if replay_mode in ("record", "replay"):
        c_path = (
            cassette_store.path
            if cassette_store is not None
            else cassette_path_for(
                cassette_dir=cfg.llm_cassette_dir,
                model_spec=model_spec,
                case_or_session=cassette_name or "default",
            )
        )

    level = thinking_override or node_cfg.thinking_level
    model_cls = cast(Any, ReplayableChatGoogleGenerativeAI)
    instance = model_cls(
        model=bare_model,
        google_api_key=api_key,
        temperature=None,
        thinking_config={"thinking_level": level},
        max_output_tokens=node_cfg.max_output_tokens,
        max_retries=node_cfg.max_retries,
        timeout=node_cfg.timeout_s,
        disable_streaming=replay_mode in ("record", "replay"),
        replay_mode=replay_mode,
        cassette_path=c_path,
    )
    if cassette_store is not None:
        instance.set_cassette_store(cassette_store)
    return cast(BaseChatModel, instance)
