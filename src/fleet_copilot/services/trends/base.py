"""Analyzer protocol, output contract, and shared policy-reading helpers for trend analysis."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.domain.models import Coverage, Insight, PolicyValue, TenantContext
from fleet_copilot.repositories.telemetry import TelemetryRepository


class AnalyzerOutput(BaseModel):
    """Structured output returned by a single trend analyzer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    insights: list[Insight] = Field(default_factory=list)
    coverage: Coverage
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    policy_applied: dict[str, PolicyValue] = Field(default_factory=dict)


class Analyzer(Protocol):
    """Protocol implemented by every fleet trend analyzer."""

    category: str

    def analyze(
        self,
        ctx: TenantContext,
        repo: TelemetryRepository,
        policies: dict[str, PolicyValue],
        as_of: datetime,
        window_days: int = 30,
        device_ids: Sequence[str] | None = None,
    ) -> AnalyzerOutput: ...


def resolve_policy_float(
    policies: dict[str, PolicyValue],
    key: str,
    default: float,
) -> tuple[float, PolicyValue]:
    """Read a numeric policy key, falling back to a global default when not yet seeded."""
    pv = policies.get(key)
    if pv is None:
        fallback = PolicyValue(value=default, source="global")
        return default, fallback
    return float(pv.value), pv


def resolve_policy_int(
    policies: dict[str, PolicyValue],
    key: str,
    default: int,
) -> tuple[int, PolicyValue]:
    """Read an integer policy key, falling back to a global default when not yet seeded."""
    pv = policies.get(key)
    if pv is None:
        fallback = PolicyValue(value=default, source="global")
        return default, fallback
    return int(pv.value), pv


def resolve_policy_str_list(
    policies: dict[str, PolicyValue],
    key: str,
    default: Sequence[str] = (),
) -> tuple[list[str], PolicyValue]:
    """Read a string-list policy key, falling back to `default` when absent."""
    pv = policies.get(key)
    if pv is None:
        fallback = PolicyValue(value=list(default), source="global")
        return list(default), fallback
    raw = pv.value if isinstance(pv.value, list) else []
    return [str(item) for item in raw], pv
