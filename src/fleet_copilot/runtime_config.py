"""Typed loader for `config/runtime.yaml` (per-node LLM configs, budgets, and cost table)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_RUNTIME_YAML = Path(__file__).resolve().parents[2] / "config" / "runtime.yaml"


class _CfgModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeLlmConfig(_CfgModel):
    """Per-node generation and thinking settings."""

    thinking_level: Literal["minimal", "low", "medium", "high"] = "low"
    max_output_tokens: int = Field(default=2048, ge=128, le=16384)
    max_retries: int = Field(default=2, ge=0, le=6)
    timeout_s: int = Field(default=45, ge=5, le=300)


class BudgetConfig(_CfgModel):
    """Loop, tool-call, retry, and timeout budgets."""

    max_tool_calls: int = Field(default=8, ge=1, le=30)
    max_verifier_retries: int = Field(default=1, ge=0, le=3)
    recursion_limit: int = Field(default=30, ge=5, le=100)
    turn_timeout_s: int = Field(default=60, ge=5, le=300)
    per_tool_caps: dict[str, int] = Field(default_factory=lambda: {"default": 4})

    def cap_for(self, tool_name: str) -> int:
        return self.per_tool_caps.get(tool_name, self.per_tool_caps.get("default", 4))


class ModelPrice(_CfgModel):
    """USD per 1M input/output tokens."""

    input: float
    output: float


class RuntimeConfig(_CfgModel):
    """Top-level runtime configuration."""

    nodes: dict[str, NodeLlmConfig]
    budgets: BudgetConfig
    pricing_usd_per_1m: dict[str, ModelPrice]

    def node_config(self, node_name: str) -> NodeLlmConfig:
        return self.nodes.get(node_name, NodeLlmConfig())

    def estimate_cost_usd(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Estimate USD cost for a turn given model ID and token counts."""
        bare = model_id.split(":", 1)[-1]
        price = self.pricing_usd_per_1m.get(bare) or self.pricing_usd_per_1m["default"]
        cost = (input_tokens * price.input + output_tokens * price.output) / 1_000_000.0
        return round(cost, 6)


@lru_cache(maxsize=1)
def get_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Load and cache `config/runtime.yaml`."""
    target = path or _DEFAULT_RUNTIME_YAML
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return RuntimeConfig.model_validate(raw)
