"""`synthesizer` node: fresh-prompt structured `Answer` generation (no tool-call history)."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from fleet_copilot.agent.prompts import load_prompt
from fleet_copilot.agent.schemas import Answer
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext

logger = logging.getLogger(__name__)


def _company_display_name(company_id: str) -> str:
    return company_id.split("-", 1)[0].title()


def _extract_usage(raw_msg: Any, model_id: str) -> dict[str, Any]:
    meta = getattr(raw_msg, "usage_metadata", None) or {}
    return {
        "node": "synthesizer",
        "model": model_id,
        "input_tokens": int(meta.get("input_tokens", 0) or 0),
        "output_tokens": int(meta.get("output_tokens", 0) or 0),
    }


def make_synthesizer_node(deps: Any) -> Any:
    """Build the `synthesizer` node closed over `AgentDeps`.

    Gemini 3 rule #3 (05 §3.11): receives a fresh prompt containing question, plan, ledger digest,
    and compact tool summaries — NEVER tool-call AIMessage history.
    """

    def synthesizer(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        template, _ = load_prompt("synthesizer")

        # Build compact ledger digest (only handles, devices, metrics, values, timestamps)
        raw_ev = state.get("evidence") or {}
        ledger_digest = [
            {
                "h": h,
                "device_id": ev.get("device_id"),
                "hostname": ev.get("hostname"),
                "metric": ev.get("metric"),
                "value": ev.get("value"),
                "unit": ev.get("unit"),
                "at": ev.get("collected_at"),
                "label": ev.get("label"),
            }
            for h, ev in sorted(
                raw_ev.items(),
                key=lambda kv: (
                    int(kv[0][1:]) if kv[0].startswith("E") and kv[0][1:].isdigit() else 10**9,
                    kv[0],
                ),
            )
        ]

        # Read investigator's final wrap-up via `.text` (Gemini 3 rule #1: list-shaped content)
        investigator_note = ""
        msgs = state.get("messages") or []
        if msgs and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls:
            investigator_note = msgs[-1].text

        tool_summaries = {
            "tool_outputs": state.get("tool_outputs") or [],
            "verdicts": state.get("verdicts") or [],
            "drafts": state.get("drafts") or [],
            "investigator_note": investigator_note,
            "warnings": state.get("warnings") or [],
        }

        violations = state.get("verifier_violations") or []
        if violations:
            violations_section = (
                "IMPORTANT — Previous grounding violations to fix:\n"
                + json.dumps(violations, indent=2, sort_keys=True)
                + "\n"
            )
        else:
            violations_section = ""

        prompt_text = template.format(
            company_id=ctx.company_id,
            company_name=_company_display_name(ctx.company_id),
            telemetry_as_of=state.get("telemetry_as_of", ""),
            violations_section=violations_section,
            user_message=state.get("user_message", ""),
            plan_json=json.dumps(state.get("plan") or {}, sort_keys=True),
            tool_summaries_json=json.dumps(tool_summaries, sort_keys=True),
            ledger_digest_json=json.dumps(ledger_digest, sort_keys=True),
        )

        llm = deps.model_factory("synthesizer")
        structured = llm.with_structured_output(Answer, include_raw=True)
        out = structured.invoke(
            [
                SystemMessage(
                    content="Return strictly valid JSON conforming to the Answer schema."
                ),
                HumanMessage(content=prompt_text),
            ]
        )

        if isinstance(out, dict) and "parsed" in out:
            ans_obj = out["parsed"]
            raw_msg = out.get("raw")
        else:
            ans_obj = out
            raw_msg = None

        answer = ans_obj if isinstance(ans_obj, Answer) else Answer.model_validate(ans_obj)
        return {
            "answer": answer.model_dump(mode="json"),
            "usage": [_extract_usage(raw_msg, deps.model_id)],
        }

    return synthesizer
