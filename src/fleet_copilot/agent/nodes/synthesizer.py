"""`synthesizer` node: fresh-prompt structured `Answer` generation with parse-retry."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from pydantic import ValidationError

from fleet_copilot.agent.deadline import check_turn_deadline
from fleet_copilot.agent.nodes.planner import StructuredOutputParseError
from fleet_copilot.agent.prompts import load_prompt
from fleet_copilot.agent.schemas import Answer
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.tools.sanitize import neutralize_untrusted_text

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


def _invoke_answer_with_retry(
    structured: Any,
    messages: list[Any],
    *,
    max_retries: int = 1,
) -> tuple[Answer, Any]:
    last_err: Exception | None = None
    prompt_msgs = list(messages)
    for attempt in range(max_retries + 1):
        out = structured.invoke(prompt_msgs)
        raw_msg = out.get("raw") if isinstance(out, dict) else None
        parsed = out.get("parsed") if isinstance(out, dict) and "parsed" in out else out
        parsing_err = out.get("parsing_error") if isinstance(out, dict) else None
        if parsed is not None and parsing_err is None:
            try:
                ans = parsed if isinstance(parsed, Answer) else Answer.model_validate(parsed)
                return ans, raw_msg
            except ValidationError as exc:
                last_err = exc
        else:
            last_err = (
                parsing_err
                if isinstance(parsing_err, Exception)
                else ValueError("Structured output returned parsed=None")
            )
        if attempt < max_retries:
            prompt_msgs = [
                *messages,
                HumanMessage(
                    content=(
                        "Your previous output could not be parsed as valid Answer JSON. "
                        "Return ONLY valid JSON matching the Answer schema."
                    )
                ),
            ]
    raise StructuredOutputParseError(f"Synthesizer failed to produce valid Answer JSON: {last_err}")


def make_synthesizer_node(deps: Any) -> Any:
    """Build the `synthesizer` node closed over `AgentDeps`.

    Receives a fresh prompt containing question, plan, ledger digest,
    conversation digest, and compact tool summaries — NEVER tool-call AIMessage history.
    """

    def synthesizer(
        state: CopilotState,
        config: RunnableConfig,
        runtime: Runtime[TenantContext],
    ) -> dict[str, Any]:
        check_turn_deadline(config)
        ctx = runtime.context
        template, _ = load_prompt("synthesizer")

        # Build compact ledger digest (neutralizing untrusted telemetry text for the LLM while
        # preserving raw values in state["evidence"] for the UI evidence panel).
        raw_ev = state.get("evidence") or {}
        ledger_digest: list[dict[str, Any]] = []
        for h, ev in sorted(
            raw_ev.items(),
            key=lambda kv: (
                {"E": 0, "C": 1, "Q": 2}.get(kv[0][:1], 9),
                int(kv[0][1:])
                if len(kv[0]) >= 2 and kv[0][:1] in ("E", "C", "Q") and kv[0][1:].isdigit()
                else 10**9,
                kv[0],
            ),
        ):
            ev_suspicious = False
            raw_host = ev.get("hostname")
            if isinstance(raw_host, str):
                safe_host, f_h = neutralize_untrusted_text(raw_host, evidence_handle=h)
                ev_suspicious = ev_suspicious or f_h
            else:
                safe_host = raw_host

            raw_metric = ev.get("metric")
            if isinstance(raw_metric, str):
                safe_metric, f_m = neutralize_untrusted_text(raw_metric, evidence_handle=h)
                ev_suspicious = ev_suspicious or f_m
            else:
                safe_metric = raw_metric

            raw_val = ev.get("value")
            if isinstance(raw_val, str):
                safe_val: Any
                safe_val, f_v = neutralize_untrusted_text(raw_val, evidence_handle=h)
                ev_suspicious = ev_suspicious or f_v
            else:
                safe_val = raw_val

            raw_label = ev.get("label")
            if isinstance(raw_label, str):
                safe_label, f_l = neutralize_untrusted_text(raw_label, evidence_handle=h)
                ev_suspicious = ev_suspicious or f_l
            else:
                safe_label = raw_label

            entry: dict[str, Any] = {
                "h": h,
                "device_id": ev.get("device_id"),
                "hostname": safe_host,
                "metric": safe_metric,
                "value": safe_val,
                "unit": ev.get("unit"),
                "at": ev.get("collected_at"),
                "label": safe_label,
            }
            if ev_suspicious:
                entry["suspicious_text"] = True
            ledger_digest.append(entry)

        # Read investigator's concise wrap-up via `.text` (Gemini 3 rule #1: list-shaped content)
        investigator_note = ""
        msgs = state.get("messages") or []
        if msgs and isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls:
            investigator_note, _ = neutralize_untrusted_text(msgs[-1].text)

        tool_summaries = {
            "tool_outputs": state.get("tool_outputs") or [],
            "charts": [
                {
                    "handle": c.get("handle"),
                    "title": c.get("title"),
                    "row_count": c.get("row_count"),
                }
                for c in (state.get("charts") or [])
            ],
            "queries": [
                {
                    "handle": q.get("handle"),
                    "purpose": q.get("purpose"),
                    "row_count": q.get("row_count"),
                }
                for q in (state.get("queries") or [])
            ],
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

        digest = list(state.get("conversation_digest") or [])[-3:]
        prompt_text = template.format(
            company_id=ctx.company_id,
            company_name=_company_display_name(ctx.company_id),
            telemetry_as_of=state.get("telemetry_as_of", ""),
            violations_section=violations_section,
            conversation_history_json=json.dumps(digest, sort_keys=True),
            user_message=state.get("user_message", ""),
            plan_json=json.dumps(state.get("plan") or {}, sort_keys=True),
            tool_summaries_json=json.dumps(tool_summaries, sort_keys=True),
            ledger_digest_json=json.dumps(ledger_digest, sort_keys=True),
        )

        llm = deps.model_factory("synthesizer")
        structured = llm.with_structured_output(Answer, include_raw=True)
        answer, raw_msg = _invoke_answer_with_retry(
            structured,
            [
                SystemMessage(
                    content="Return strictly valid JSON conforming to the Answer schema."
                ),
                HumanMessage(content=prompt_text),
            ],
            max_retries=1,
        )
        return {
            "answer": answer.model_dump(mode="json"),
            "usage": [_extract_usage(raw_msg, deps.model_id)],
        }

    return synthesizer
