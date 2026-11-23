"""`verify_grounding` node: two-tier deterministic citation and numeric verifier (05 §3.2)."""

from __future__ import annotations

import logging
import re
from typing import Any

from langgraph.runtime import Runtime

from fleet_copilot.agent.schemas import Answer, Claim
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import GroundingViolationPayload

logger = logging.getLogger(__name__)

# Patterns stripped before extracting standalone numeric values from claim text
_ISO_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_SHORT_DATE_RE = re.compile(r"\b\d{2}-\d{2}\b")
_VERSION_CONTEXT_RE = re.compile(
    r"(?:\b(?:macos|windows|os|version|ver|gen|xps|m|v)\s*\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+\b|\b\d{2}H[12]\b)",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"\bE\d+\b")
_STANDALONE_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.\-:])\d+(?:\.\d+)?(?![A-Za-z0-9_.\-:])")


def _extract_prompt_numbers(text: str) -> set[float]:
    nums: set[float] = set()
    for match in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            nums.add(float(match))
        except ValueError:
            pass
    return nums


def _extract_claim_text_numbers(text: str) -> list[float]:
    cleaned = _ISO_DATE_RE.sub(" ", text)
    cleaned = _SHORT_DATE_RE.sub(" ", cleaned)
    cleaned = _VERSION_CONTEXT_RE.sub(" ", cleaned)
    cleaned = _HANDLE_RE.sub(" ", cleaned)
    nums: list[float] = []
    for m in _STANDALONE_NUM_RE.findall(cleaned):
        try:
            nums.append(float(m))
        except ValueError:
            pass
    return nums


def _collect_verifiable_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        fval = float(obj)
        out.add(fval)
        # Share values like 0.93 are often written as 93 or 93.0%
        if 0.0 < fval <= 1.0:
            out.add(round(fval * 100.0, 2))
            out.add(round(fval * 100.0, 1))
            out.add(float(round(fval * 100.0)))
    elif isinstance(obj, str):
        # Also collect date components (year/month/day) from ISO timestamps
        for part in re.findall(r"\d+(?:\.\d+)?", obj):
            if len(part) <= 6:
                try:
                    out.add(float(part))
                except ValueError:
                    pass
    elif isinstance(obj, list):
        for item in obj:
            _collect_verifiable_numbers(item, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_verifiable_numbers(v, out)


def _is_number_verifiable(num: float, pool: set[float], exempt: set[float]) -> bool:
    if any(abs(num - ex) <= 1e-6 for ex in exempt):
        return True
    is_int = abs(num - round(num)) < 1e-9
    for candidate in pool:
        if is_int and abs(candidate - round(candidate)) < 1e-9:
            if int(round(num)) == int(round(candidate)):
                return True
        if abs(num - candidate) <= 0.2 + 1e-9:
            return True
    return False


def verify_answer_grounding(
    answer: Answer,
    *,
    evidence_map: dict[str, dict[str, Any]],
    tool_outputs: list[dict[str, Any]],
    resolved_policies: dict[str, dict[str, Any]],
    insights: list[dict[str, Any]],
    user_message: str,
    telemetry_as_of: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run hard citation/device checks and soft numeric faithfulness checks on `answer`."""
    hard_violations: list[dict[str, Any]] = []
    soft_violations: list[dict[str, Any]] = []

    # Build verifiable numeric pool and exempt numbers (prompt, as_of date parts, version tokens)
    pool: set[float] = {0.0, float(len(evidence_map)), float(len(insights))}
    _collect_verifiable_numbers(evidence_map, pool)
    _collect_verifiable_numbers(tool_outputs, pool)
    _collect_verifiable_numbers(resolved_policies, pool)
    _collect_verifiable_numbers(insights, pool)

    exempt: set[float] = _extract_prompt_numbers(user_message)
    _collect_verifiable_numbers(telemetry_as_of, exempt)

    # Also exempt OS version numbers present in evidence/tool outputs
    for ev in evidence_map.values():
        val = ev.get("value")
        if isinstance(val, str):
            for tok in re.findall(r"\d+(?:\.\d+)?", val):
                try:
                    exempt.add(float(tok))
                except ValueError:
                    pass

    for idx, claim in enumerate(answer.claims):
        if not claim.citations:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "uncited_claim",
                    "claim_index": idx,
                    "detail": "Factual claim has no citations.",
                }
            )
            continue

        cited_devices: set[str] = set()
        missing_handles: list[str] = []
        for h in claim.citations:
            ev_rec = evidence_map.get(h)
            if ev_rec is None:
                missing_handles.append(h)
            else:
                dev_id = ev_rec.get("device_id")
                if dev_id:
                    cited_devices.add(str(dev_id))

        if missing_handles:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "unknown_citation",
                    "claim_index": idx,
                    "detail": f"Unknown citation handle(s): {', '.join(missing_handles)}",
                }
            )

        for dev_id in claim.device_ids:
            if dev_id not in cited_devices:
                hard_violations.append(
                    {
                        "tier": "hard",
                        "code": "device_not_in_cited_evidence",
                        "claim_index": idx,
                        "detail": (
                            f"Device {dev_id} is not present in cited handles {claim.citations}."
                        ),
                    }
                )

        # Soft check: numbers in claim.numbers and standalone numbers in claim.text
        claim_pool = pool | {float(len(claim.device_ids)), float(len(claim.citations))}
        numbers_to_check = list(claim.numbers) + _extract_claim_text_numbers(claim.text)
        bad_nums: list[float] = []
        for num in numbers_to_check:
            if not _is_number_verifiable(num, claim_pool, exempt):
                if num not in bad_nums:
                    bad_nums.append(num)
        if bad_nums:
            soft_violations.append(
                {
                    "tier": "soft",
                    "code": "unverified_number",
                    "claim_index": idx,
                    "detail": f"Numbers not matched in tool outputs: {bad_nums}",
                }
            )

    return hard_violations, soft_violations


def make_verify_grounding_node(deps: Any) -> Any:
    """Build the `verify_grounding` node enforcing retry-once + strip(hard)/warn(soft) + audit."""

    def verify_grounding(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        raw_answer = state.get("answer")
        if not raw_answer:
            return {"verifier_violations": []}

        answer = Answer.model_validate(raw_answer)
        evidence_map = state.get("evidence") or {}
        tool_outputs = state.get("tool_outputs") or []
        resolved_policies = state.get("resolved_policies") or {}
        insights = state.get("insights") or []
        user_message = state.get("user_message") or ""
        telemetry_as_of = state.get("telemetry_as_of") or ""

        hard_v, soft_v = verify_answer_grounding(
            answer,
            evidence_map=evidence_map,
            tool_outputs=tool_outputs,
            resolved_policies=resolved_policies,
            insights=insights,
            user_message=user_message,
            telemetry_as_of=telemetry_as_of,
        )
        all_v = hard_v + soft_v
        if not all_v:
            return {"verifier_violations": []}

        retries = int(state.get("verifier_retries", 0))
        max_retries = deps.runtime_cfg.budgets.max_verifier_retries
        codes = sorted({v["code"] for v in all_v})
        counts = {"hard": len(hard_v), "soft": len(soft_v)}
        turn_id = state.get("turn_id", "")
        thread_id = state.get("thread_id", "")

        if retries < max_retries:
            deps.audit_svc.record_standalone(
                ctx,
                "grounding_violation",
                GroundingViolationPayload(
                    turn_id=turn_id,
                    codes=codes,
                    counts=counts,
                    outcome="retried",
                ),
                thread_id=thread_id,
            )
            return {
                "verifier_violations": all_v,
                "verifier_retries": retries + 1,
            }

        # Retries exhausted: strip hard-violating claims; keep soft-violating claims with a warning.
        warnings: list[str] = []
        updated_answer = answer
        if hard_v:
            bad_indices = {int(v["claim_index"]) for v in hard_v}
            kept_claims: list[Claim] = [
                c for i, c in enumerate(answer.claims) if i not in bad_indices
            ]
            caveats = list(answer.caveats)
            caveats.append(
                f"Removed {len(bad_indices)} unverified claim(s) after grounding check failed."
            )
            updated_answer = answer.model_copy(update={"claims": kept_claims, "caveats": caveats})
            warnings.append(
                f"Grounding verifier stripped {len(bad_indices)} claim(s) with invalid citations."
            )
            deps.audit_svc.record_standalone(
                ctx,
                "grounding_violation",
                GroundingViolationPayload(
                    turn_id=turn_id,
                    codes=codes,
                    counts=counts,
                    outcome="stripped",
                ),
                thread_id=thread_id,
            )
        else:
            warnings.append(
                "Numeric grounding check flagged one or more numbers not matched in tool outputs."
            )
            deps.audit_svc.record_standalone(
                ctx,
                "grounding_violation",
                GroundingViolationPayload(
                    turn_id=turn_id,
                    codes=codes,
                    counts=counts,
                    outcome="warned",
                ),
                thread_id=thread_id,
            )

        return {
            "answer": updated_answer.model_dump(mode="json"),
            "verifier_violations": [],
            "warnings": warnings,
        }

    return verify_grounding
