"""Structured agent contracts: Plan, Answer, Claim, TurnUsage, and TurnResponse (05 §3.2, §7)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _AgentModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlanStep(_AgentModel):
    """Single step in the planner's investigation plan."""

    goal: str
    tool_hint: str | None = None


class Plan(_AgentModel):
    """Structured plan produced by the `planner` node and persisted to `turns.plan_json`."""

    intent: Literal["qa", "insight", "action", "mixed", "out_of_scope"]
    refers_to_other_org: bool = False
    role_violation: bool = False
    needs_clarification: str | None = None
    steps: list[PlanStep] = Field(default_factory=list)


class Claim(_AgentModel):
    """Single grounded factual claim in the synthesized answer."""

    text: str
    citations: list[str] = Field(default_factory=list)
    device_ids: list[str] = Field(default_factory=list)
    numbers: list[float] = Field(default_factory=list)


class Answer(_AgentModel):
    """Structured answer produced by the `synthesizer` node (04 §4.1, 05 §3.2)."""

    summary: str
    claims: list[Claim] = Field(default_factory=list)
    insight_refs: list[str] = Field(default_factory=list)
    proposal_refs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class TurnUsage(_AgentModel):
    """Aggregated token usage, estimated USD cost, and wall latency for one turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd_est: float = 0.0
    latency_ms: int = 0


class TurnResponse(_AgentModel):
    """Public response returned by `CopilotService.run_turn` / `resume`, CLI, and API (05 §7)."""

    thread_id: str
    turn_id: str
    trace_id: str
    status: Literal["completed", "waiting_approval", "refused", "failed"]
    answer_markdown: str
    answer: Answer | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    insights: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    approval_packet: dict[str, Any] | None = None
    plan: Plan | None = None
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    telemetry_as_of: str | None = None
    usage: TurnUsage = Field(default_factory=TurnUsage)
    warnings: list[str] = Field(default_factory=list)
