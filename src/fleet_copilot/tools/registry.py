"""Tool specification, role/config filtering, telemetry string hygiene, and JSON formatting."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.tools import BaseTool

from fleet_copilot.clock import Clock
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext, ToolConfigRow, ToolResult
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.actions import ActionService
from fleet_copilot.services.policy import PolicyService

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_ROLE_RANK: dict[str, int] = {"employee": 0, "viewer": 1, "it_admin": 2}
_MAX_TELEMETRY_STR_LEN = 80
_MAX_TOOL_ROWS = 20


@dataclass(frozen=True)
class ToolSpec:
    """Metadata descriptor binding an LLM tool to its RBAC floor and kind."""

    name: str
    min_role: Literal["viewer", "it_admin"]
    kind: Literal["read", "action"]
    tool: BaseTool


@dataclass
class TurnToolSink:
    """In-memory per-step accumulator passed via RunnableConfig (never stored in checkpoints)."""

    ledger: EvidenceLedger
    insights: list[dict[str, Any]] = field(default_factory=list)
    charts: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    call_meta: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDeps:
    """Service dependencies injected into tool factories (never exposed to the LLM)."""

    telemetry_repo: TelemetryRepository
    copilot_store: PgCopilotStore
    policy_svc: PolicyService
    action_svc: ActionService
    clock: Clock
    db: Any | None = None
    audit_svc: Any | None = None


def sanitize_telemetry_str(value: str, *, max_len: int = _MAX_TELEMETRY_STR_LEN) -> str:
    """Strip control chars and clip telemetry strings to <= 80 chars (injection guard)."""
    cleaned = _CTRL_RE.sub(" ", value).strip()
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _sanitize_value(obj: Any, _truncated_flag: list[bool] | None = None) -> Any:
    if isinstance(obj, str):
        return sanitize_telemetry_str(obj)
    if isinstance(obj, list):
        if len(obj) > 50 and _truncated_flag is not None:
            _truncated_flag[0] = True
        return [_sanitize_value(item, _truncated_flag) for item in obj[:50]]
    if isinstance(obj, dict):
        return {str(k): _sanitize_value(v, _truncated_flag) for k, v in obj.items()}
    return obj


def format_tool_output(
    result: ToolResult,
    ledger: EvidenceLedger,
    *,
    max_rows: int = _MAX_TOOL_ROWS,
) -> tuple[str, list[dict[str, Any]]]:
    """Register evidence in `ledger` and return compact JSON + evidence entries for the LLM."""
    list_truncated = [False]
    ev_entries: list[dict[str, Any]] = []
    for ev in result.evidence:
        handle = ledger.add(ev)
        ev_entries.append(
            {
                "h": handle,
                "device": ev.device_id,
                "metric": ev.metric,
                "value": _sanitize_value(ev.value, list_truncated),
                "at": ev.collected_at.isoformat(),
            }
        )

    rows = result.data[:max_rows]
    sanitized_rows = [_sanitize_value(r, list_truncated) for r in rows]
    truncated = result.truncated or (len(result.data) > max_rows) or list_truncated[0]

    payload: dict[str, Any] = {
        "ok": result.ok,
        "summary": sanitize_telemetry_str(result.summary, max_len=240),
        "rows": sanitized_rows,
        "evidence": ev_entries[:40],
        "notes": [
            sanitize_telemetry_str(n, max_len=160)
            for n in (result.coverage.notes if result.coverage else [])
        ],
        "policy": {
            k: {"value": v.value, "source": v.source} for k, v in result.policy_applied.items()
        },
        "truncated": truncated,
    }
    if result.coverage is not None:
        payload["coverage"] = {
            "telemetry_as_of": result.coverage.telemetry_as_of.isoformat(),
            "evaluated": result.coverage.evaluated,
            "matched": result.coverage.matched,
        }
    if result.error is not None:
        payload["error"] = sanitize_telemetry_str(result.error, max_len=160)

    return json.dumps(payload, sort_keys=True, ensure_ascii=False), ev_entries


def tools_for(
    ctx: TenantContext,
    configs: Sequence[ToolConfigRow],
    all_specs: Sequence[ToolSpec],
    *,
    actions_enabled: bool = True,
) -> list[ToolSpec]:
    """Filter `all_specs` by global kill-switch, tenant toggle, role, and `ACTIONS_ENABLED`."""
    global_by_name = {c.tool_name: c for c in configs if c.company_id == "*"}
    tenant_by_name = {c.tool_name: c for c in configs if c.company_id == ctx.company_id}
    actor_rank = _ROLE_RANK.get(ctx.role, 0)

    allowed: list[ToolSpec] = []
    for spec in all_specs:
        if spec.kind == "action" and not actions_enabled:
            continue

        g_cfg = global_by_name.get(spec.name)
        t_cfg = tenant_by_name.get(spec.name)

        # Global disabled row always wins (platform kill-switch)
        if g_cfg is not None and not g_cfg.enabled:
            continue
        if t_cfg is not None:
            if not t_cfg.enabled:
                continue
        elif g_cfg is None or not g_cfg.enabled:
            continue

        # Effective minimum role is the stricter of spec floor and DB config floor
        effective_min = spec.min_role
        for cfg in (g_cfg, t_cfg):
            if cfg is not None and _ROLE_RANK.get(cfg.min_role, 0) > _ROLE_RANK.get(
                effective_min, 0
            ):
                effective_min = cfg.min_role  # type: ignore[assignment]
        if actor_rank < _ROLE_RANK.get(effective_min, 2):
            continue

        allowed.append(spec)
    return allowed


def build_all_tool_specs(deps: ToolDeps) -> list[ToolSpec]:
    """Construct all 13 LLM-facing tool specifications (7 read + chart + SQL + 4 actions)."""
    from fleet_copilot.tools.action_tools import build_action_tool_specs
    from fleet_copilot.tools.chart_tool import build_chart_tool_spec
    from fleet_copilot.tools.read_tools import build_read_tool_specs
    from fleet_copilot.tools.sql_tool import build_sql_tool_spec

    return [
        *build_read_tool_specs(deps),
        build_chart_tool_spec(deps),
        build_sql_tool_spec(deps),
        *build_action_tool_specs(deps),
    ]
