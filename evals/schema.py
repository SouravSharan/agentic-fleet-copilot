"""Pydantic v2 schema and YAML loader for oracle-backed evaluation cases."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CASES_DIR = Path(__file__).resolve().parent / "cases"


class _EvalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OracleRef(_EvalModel):
    """Pointer to an independent function in `evals/oracle.py` that backs a literal expectation."""

    fn: str
    args: dict[str, Any] = Field(default_factory=dict)


class ProposalTarget(_EvalModel):
    """Expected action tool + target pair (allow or refuse)."""

    tool: str
    target: str


class ProposalExpect(_EvalModel):
    """Expected action gate decisions, case-specific refusal reasons, and offered alternatives."""

    allow: list[ProposalTarget] = Field(default_factory=list)
    refuse: list[ProposalTarget] = Field(default_factory=list)
    refusal_reason_any: list[str] = Field(default_factory=list)
    alternative: list[str] = Field(default_factory=list)
    alternative_keywords: list[str] = Field(default_factory=list)


class TurnSpec(_EvalModel):
    """One conversational turn in an eval case, with optional HITL resume decisions."""

    user: str
    decisions: dict[str, Literal["approve", "reject", "defer"]] | None = None
    between_pause_and_resume_scenario: str | None = None
    between_scenario_device_id: str | None = None


class ExpectSpec(_EvalModel):
    """Deterministic grading expectations evaluated against `TurnResponse` and DB/audit checks."""

    device_set: list[str] | None = None
    oracle_ref: OracleRef | None = None
    count: int | None = None
    refusal: bool | None = None
    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    proposals: ProposalExpect | None = None
    must_not_mention: list[str] = Field(default_factory=list)
    must_mention: list[str] = Field(default_factory=list)
    citations: Literal["required", "optional"] = "required"
    mentions_threshold: bool | None = None
    expected_execution_status: (
        Literal["executed", "rejected", "deferred", "stale_blocked"] | None
    ) = None
    no_unauthorized_execution: bool = False
    no_staged_proposals: bool = False
    must_not_stage_devices: list[str] = Field(default_factory=list)
    must_not_match_pii: bool = False
    expect_caveat_or_refusal: bool = False
    expect_chart: bool = False
    expect_sql: bool = False
    expected_group_values: dict[str, float] | None = None

    @model_validator(mode="after")
    def _literal_sets_require_oracle_ref(self) -> ExpectSpec:
        if self.device_set is not None and self.oracle_ref is None:
            raise ValueError("Cases declaring a literal `device_set` MUST include `oracle_ref`.")
        if self.count is not None and self.refusal is not True and self.oracle_ref is None:
            raise ValueError("Cases declaring a literal `count` MUST include `oracle_ref`.")
        if self.expected_group_values is not None and self.oracle_ref is None:
            raise ValueError(
                "Cases declaring literal `expected_group_values` MUST include `oracle_ref`."
            )
        return self


class EvalCase(_EvalModel):
    """Single reproducible multi-turn or single-turn evaluation case."""

    id: str
    tenant: str
    role: Literal["viewer", "it_admin"]
    turns: list[TurnSpec] = Field(min_length=1)
    expect: ExpectSpec
    tags: list[str] = Field(default_factory=list)
    setup_scenario: str | None = None
    setup_device_id: str | None = None


def load_cases(
    cases_dir: Path | None = None,
    *,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
) -> list[EvalCase]:
    """Load and validate all YAML cases under `cases_dir`, filtering optionally by tags or ids."""
    root = cases_dir or CASES_DIR
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()

    for yaml_path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(f"{yaml_path} must contain a top-level YAML list of cases")
        for item in raw:
            case = EvalCase.model_validate(item)
            if case.id in seen_ids:
                raise ValueError(f"Duplicate eval case id: {case.id!r}")
            seen_ids.add(case.id)
            cases.append(case)

    if tags:
        wanted_tags = {t.strip() for t in tags if t.strip()}
        cases = [c for c in cases if wanted_tags.intersection(c.tags)]
    if case_ids:
        wanted_ids = {i.strip() for i in case_ids if i.strip()}
        cases = [c for c in cases if c.id in wanted_ids]
    return cases
