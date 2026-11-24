"""`finalize` node: render markdown + footnotes, persist turn trace, audit `turn_completed`."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from fleet_copilot.agent.prompts import prompt_bundle_version
from fleet_copilot.agent.schemas import Answer, Plan
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import TurnCompletedPayload
from fleet_copilot.tools.sanitize import neutralize_untrusted_text

logger = logging.getLogger(__name__)

_BRACKETED_HANDLES_RE = re.compile(r"\[([ECQ]\d+(?:\s*,\s*[ECQ]\d+)*)\]")


def neutralize_unknown_handles(
    text: str,
    evidence_map: dict[str, Any],
) -> tuple[str, bool]:
    """Rewrite any bracketed `[E#]`/`[C#]`/`[Q#]` absent from `evidence_map` to plain text."""
    neutralized = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal neutralized
        tokens = [tok.strip() for tok in match.group(1).split(",") if tok.strip()]
        if all(tok in evidence_map for tok in tokens):
            return match.group(0)
        neutralized = True
        if len(tokens) == 1:
            return tokens[0]
        valid = [tok for tok in tokens if tok in evidence_map]
        unknown = [tok for tok in tokens if tok not in evidence_map]
        parts: list[str] = []
        if valid:
            parts.append("[" + ", ".join(valid) + "]")
        parts.extend(unknown)
        return ", ".join(parts)

    return _BRACKETED_HANDLES_RE.sub(_replace, text), neutralized


def neutralize_answer_unknown_handles(
    answer: Answer,
    evidence_map: dict[str, Any],
) -> tuple[Answer, bool]:
    """Neutralize unknown bracketed handles in `Answer.summary`, `claims`, and `caveats`."""
    any_changed = False
    clean_summary, ch_sum = neutralize_unknown_handles(answer.summary, evidence_map)
    any_changed = any_changed or ch_sum

    clean_claims = []
    for claim in answer.claims:
        c_text, ch_c = neutralize_unknown_handles(claim.text, evidence_map)
        any_changed = any_changed or ch_c
        clean_claims.append(claim.model_copy(update={"text": c_text}) if ch_c else claim)

    clean_caveats = []
    for cav in answer.caveats:
        c_cav, ch_cav = neutralize_unknown_handles(cav, evidence_map)
        any_changed = any_changed or ch_cav
        clean_caveats.append(c_cav)

    if not any_changed:
        return answer, False
    return (
        answer.model_copy(
            update={
                "summary": clean_summary,
                "claims": clean_claims,
                "caveats": clean_caveats,
            }
        ),
        True,
    )


def render_answer_markdown(
    *,
    answer: Answer | None,
    plan: Plan | None,
    evidence_map: dict[str, dict[str, Any]],
    execution_results: list[dict[str, Any]],
    warnings: list[str],
    telemetry_as_of: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Render structured `Answer` into Markdown with `[E1]` footnotes and return cited evidence."""
    if answer is None and plan is not None and plan.needs_clarification:
        return plan.needs_clarification, []
    if answer is None:
        return "No response generated.", []

    lines: list[str] = [answer.summary]
    cited_handles: list[str] = []
    seen_handles: set[str] = set()

    if answer.claims:
        lines.append("")
        for claim in answer.claims:
            tag = ""
            if claim.citations:
                for h in claim.citations:
                    if h not in seen_handles and h in evidence_map:
                        seen_handles.add(h)
                        cited_handles.append(h)
                if not any(f"[{h}]" in claim.text for h in claim.citations):
                    tag = " " + "".join(f"[{h}]" for h in claim.citations)
            lines.append(f"- {claim.text}{tag}")

    if execution_results:
        lines.append("")
        lines.append("**Action outcomes:**")
        for res in execution_results:
            receipt = res.get("receipt")
            rcpt_str = (
                f" (receipt `{receipt['receipt_id']}`)"
                if isinstance(receipt, dict) and receipt.get("receipt_id")
                else ""
            )
            lines.append(
                f"- `{res['proposal_id']}` -> **{res['status']}**{rcpt_str}: "
                f"{res.get('message', '')}"
            )

    if warnings:
        lines.append("")
        lines.append("**Warnings:**")
        for w in warnings:
            lines.append(f"- {w}")

    if answer.caveats:
        lines.append("")
        lines.append("**Caveats & scope:**")
        for c in answer.caveats:
            lines.append(f"- {c}")
    elif telemetry_as_of:
        lines.append("")
        lines.append(f"*Telemetry data as of {telemetry_as_of}.*")

    citations_out: list[dict[str, Any]] = []
    if cited_handles:
        lines.append("")
        lines.append("**Evidence:**")
        for h in cited_handles:
            ev = evidence_map[h]
            citations_out.append(ev)
            host = ev.get("hostname") or ev.get("device_id")
            safe_host, _ = neutralize_untrusted_text(str(host), evidence_handle=h)
            safe_label, _ = neutralize_untrusted_text(str(ev.get("label") or ""), evidence_handle=h)
            snap = str(ev.get("snapshot_id", ""))
            at = str(ev.get("collected_at", ""))[:19]
            lines.append(
                f"- `[{h}]` `{snap}` · `{ev.get('device_id')}` ({safe_host}) · {safe_label} · {at}"
            )

    return "\n".join(lines), citations_out


def make_finalize_node(deps: Any) -> Any:
    """Build the `finalize` node closed over `AgentDeps`."""

    def finalize(
        state: CopilotState,
        config: RunnableConfig,
        runtime: Runtime[TenantContext],
    ) -> dict[str, Any]:
        ctx = runtime.context
        turn_id = state.get("turn_id", "")
        thread_id = state.get("thread_id", "")
        user_message = state.get("user_message", "")
        evidence_map = state.get("evidence") or {}
        plan_dict = state.get("plan")
        plan_obj = Plan.model_validate(plan_dict) if plan_dict else None
        raw_answer = state.get("answer")
        answer_obj = Answer.model_validate(raw_answer) if raw_answer else None
        exec_results = list(state.get("execution_results") or [])
        warnings = list(state.get("warnings") or [])
        extra_warnings: list[str] = []
        telemetry_as_of = state.get("telemetry_as_of", "")

        handles_neutralized = False
        if answer_obj is not None:
            answer_obj, handles_neutralized = neutralize_answer_unknown_handles(
                answer_obj, evidence_map
            )
        if handles_neutralized and "unknown_handle_neutralized" not in warnings:
            warnings.append("unknown_handle_neutralized")
            extra_warnings.append("unknown_handle_neutralized")

        markdown, citations_out = render_answer_markdown(
            answer=answer_obj,
            plan=plan_obj,
            evidence_map=evidence_map,
            execution_results=exec_results,
            warnings=warnings,
            telemetry_as_of=telemetry_as_of,
        )
        markdown, md_neutralized = neutralize_unknown_handles(markdown, evidence_map)
        if md_neutralized and "unknown_handle_neutralized" not in warnings:
            warnings.append("unknown_handle_neutralized")
            extra_warnings.append("unknown_handle_neutralized")
            markdown, citations_out = render_answer_markdown(
                answer=answer_obj,
                plan=plan_obj,
                evidence_map=evidence_map,
                execution_results=exec_results,
                warnings=warnings,
                telemetry_as_of=telemetry_as_of,
            )
            markdown, _ = neutralize_unknown_handles(markdown, evidence_map)

        final_status = state.get("status") or "completed"
        if final_status not in ("completed", "waiting_approval", "refused", "failed"):
            final_status = "completed"
        if final_status == "waiting_approval" and not state.get("approval_packet"):
            final_status = "completed"

        usage_list = list(state.get("usage") or [])
        tokens_in = sum(int(u.get("input_tokens", 0) or 0) for u in usage_list)
        tokens_out = sum(int(u.get("output_tokens", 0) or 0) for u in usage_list)
        cost_est = deps.runtime_cfg.estimate_cost_usd(deps.model_id, tokens_in, tokens_out)

        cfg_dict = config if isinstance(config, dict) else {}
        configurable = cfg_dict.get("configurable") if isinstance(cfg_dict, dict) else None
        turn_t0 = configurable.get("_turn_t0") if isinstance(configurable, dict) else None
        latency_ms = (
            max(0, int((time.monotonic() - float(turn_t0)) * 1000))
            if isinstance(turn_t0, (int, float))
            else 0
        )

        input_hash = hashlib.sha256(user_message.encode("utf-8")).hexdigest()
        answer_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        p_ver = prompt_bundle_version()

        deps.copilot_store.update_turn(
            ctx.company_id,
            turn_id,
            status=final_status,
            plan_json=plan_dict,
            tool_calls_json=list(state.get("tool_trace") or []),
            evidence_json=evidence_map,
            assistant_response=markdown,
            model_id=deps.model_id,
            prompt_version=p_ver,
            input_hash=input_hash,
            answer_hash=answer_hash,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd_est=cost_est,
            latency_ms=latency_ms,
            completed_at=deps.clock.now(),
        )
        deps.audit_svc.record_standalone(
            ctx,
            "turn_completed",
            TurnCompletedPayload(
                turn_id=turn_id,
                status=final_status,  # type: ignore[arg-type]
                answer_hash=answer_hash,
                citations=len(citations_out),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd_est=cost_est,
                latency_ms=latency_ms,
            ),
            thread_id=thread_id,
        )

        logger.info(
            "turn_completed",
            extra={
                "company_id": ctx.company_id,
                "turn_id": turn_id,
                "status": final_status,
                "citations": len(citations_out),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
            },
        )

        # Update compact conversation digest (last 3 turns) for referential follow-ups
        refused_alts = [
            {
                "refused_tool": v.get("tool_name"),
                "target_id": v.get("target_id"),
                "alternatives": v.get("alternatives") or [],
            }
            for v in (state.get("verdicts") or [])
            if v.get("decision") == "refuse"
        ]
        prior_digest = list(state.get("conversation_digest") or [])
        new_digest = (
            prior_digest
            + [
                {
                    "user": user_message,
                    "summary": answer_obj.summary if answer_obj is not None else markdown[:240],
                    "result_device_ids": (
                        list(answer_obj.result_device_ids) if answer_obj is not None else []
                    ),
                    "refused_alternatives": refused_alts,
                }
            ]
        )[-3:]

        out_state: dict[str, Any] = {
            "answer_markdown": markdown,
            "status": final_status,
            "conversation_digest": new_digest,
            "messages": [AIMessage(content=markdown)],
        }
        if handles_neutralized and answer_obj is not None:
            out_state["answer"] = answer_obj.model_dump(mode="json")
        if extra_warnings:
            out_state["warnings"] = extra_warnings
        return out_state

    return finalize
