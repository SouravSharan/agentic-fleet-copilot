"""Structured agent contracts: Plan, Answer, Claim, TurnUsage, and TurnResponse."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _AgentModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlanStep(_AgentModel):
    """Single step in the planner's investigation plan."""

    goal: str = Field(
        description="Concise objective for this investigation or action step.",
    )
    tool_hint: str | None = Field(
        default=None,
        description="Recommended tool name from the caller's allowed tool list, or null.",
    )


class Plan(_AgentModel):
    """Structured plan produced by the `planner` node and persisted to `turns.plan_json`."""

    intent: Literal["qa", "insight", "action", "mixed", "out_of_scope"] = Field(
        description=(
            "High-level request classification: qa, insight, action, mixed, or out_of_scope."
        ),
    )
    refers_to_other_org: bool = Field(
        default=False,
        description=(
            "True only when the user explicitly asks about a different company/tenant "
            "than the signed-in organization."
        ),
    )
    role_violation: bool = Field(
        default=False,
        description=(
            "True only when a read-only viewer explicitly requests a write/action operation."
        ),
    )
    needs_clarification: str | None = Field(
        default=None,
        description=(
            "Brief clarifying question if the request is ambiguous even with thread history; "
            "otherwise null."
        ),
    )
    steps: list[PlanStep] = Field(
        default_factory=list,
        description=(
            "Ordered investigation/action steps (1-5). Must be empty when refused or out_of_scope."
        ),
    )


class Claim(_AgentModel):
    """Single grounded factual claim in the synthesized answer."""

    text: str = Field(
        description=(
            "Concise factual statement referring to devices by hostname (not raw device_id "
            "unless requested or needed) with inline handles such as [E1], [C1], or [Q1]."
        ),
    )
    citations: list[str] = Field(
        default_factory=list,
        description=(
            "Exact evidence handles from the Evidence Ledger (e.g. ['E1'], ['C1'], ['Q1']) "
            "supporting this claim."
        ),
    )
    device_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Exact 12-character device_id for every device mentioned in `text` (including "
            "devices named by hostname)."
        ),
    )
    numbers: list[float] = Field(
        default_factory=list,
        description="Every numeric metric or count stated in `text`, taken directly from evidence.",
    )


class Answer(_AgentModel):
    """Structured grounded answer produced by the `synthesizer` node."""

    summary: str = Field(
        description=(
            "Concise 1-3 sentence executive answer directly addressing the user's question, "
            "naming devices by hostname unless the ID was explicitly requested."
        ),
    )
    result_device_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Exact 12-character device_ids directly answering the user's question. Include the "
            "target device_id for a specific named-device lookup even if the asked attribute is "
            "absent or N/A (e.g. desktop with no battery). Empty [] when a filtered fleet query "
            "matched 0 devices, a device was not found, or the response is a pure refusal or "
            "fleet-level aggregate."
        ),
    )
    claims: list[Claim] = Field(
        default_factory=list,
        description="Grounded factual claims supporting `summary`, each backed by valid citations.",
    )
    insight_refs: list[str] = Field(
        default_factory=list,
        description="Insight identifiers (`insight_id`) returned by trend analyzers, if any.",
    )
    proposal_refs: list[str] = Field(
        default_factory=list,
        description="Action proposal identifiers (`proposal_id`) staged for approval, if any.",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Required caveats: telemetry_as_of timestamp, applied policy thresholds and scopes, "
            "excluded platforms or missing telemetry notes, zero-match coverage counts, and "
            "refusal reasons with verified alternatives."
        ),
    )


class TurnUsage(_AgentModel):
    """Aggregated token usage, estimated USD cost, and wall latency for one turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd_est: float = 0.0
    latency_ms: int = 0


class TurnResponse(_AgentModel):
    """Public response returned by `CopilotService.run_turn` / `resume`, CLI, and API."""

    thread_id: str
    turn_id: str
    trace_id: str
    status: Literal["completed", "waiting_approval", "refused", "failed", "limited"]
    answer_markdown: str
    answer: Answer | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    insights: list[dict[str, Any]] = Field(default_factory=list)
    charts: list[dict[str, Any]] = Field(default_factory=list)
    queries: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    approval_packet: dict[str, Any] | None = None
    plan: Plan | None = None
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    telemetry_as_of: str | None = None
    usage: TurnUsage = Field(default_factory=TurnUsage)
    warnings: list[str] = Field(default_factory=list)


class ProgressEvent(_AgentModel):
    """Streaming progress update emitted by `CopilotService.run_turn_stream`."""

    step: str
    label: str
    detail: str = ""
    response: TurnResponse | None = None
