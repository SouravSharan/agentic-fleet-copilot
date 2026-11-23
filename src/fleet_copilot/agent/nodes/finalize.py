"""`finalize` node: render markdown + footnotes, persist turn trace, audit `turn_completed`."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from fleet_copilot.agent.prompts import prompt_bundle_version
from fleet_copilot.agent.schemas import Answer, Plan
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import TurnCompletedPayload

logger = logging.getLogger(__name__)


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
            snap = str(ev.get("snapshot_id", ""))
            at = str(ev.get("collected_at", ""))[:19]
            lines.append(
                f"- `[{h}]` `{snap}` · `{ev.get('device_id')}` ({host}) · {ev.get('label')} · {at}"
            )

    return "\n".join(lines), citations_out


def make_finalize_node(deps: Any) -> Any:
    """Build the `finalize` node closed over `AgentDeps`."""

    def finalize(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
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
        telemetry_as_of = state.get("telemetry_as_of", "")

        markdown, citations_out = render_answer_markdown(
            answer=answer_obj,
            plan=plan_obj,
            evidence_map=evidence_map,
            execution_results=exec_results,
            warnings=warnings,
            telemetry_as_of=telemetry_as_of,
        )

        final_status = state.get("status") or "completed"
        if final_status not in ("completed", "waiting_approval", "refused", "failed"):
            final_status = "completed"
        if final_status == "waiting_approval" and not state.get("approval_packet"):
            final_status = "completed"

        usage_list = list(state.get("usage") or [])
        tokens_in = sum(int(u.get("input_tokens", 0) or 0) for u in usage_list)
        tokens_out = sum(int(u.get("output_tokens", 0) or 0) for u in usage_list)
        cost_est = deps.runtime_cfg.estimate_cost_usd(deps.model_id, tokens_in, tokens_out)

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
            latency_ms=0,
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
                latency_ms=0,
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
            },
        )

        return {
            "answer_markdown": markdown,
            "status": final_status,
            "messages": [AIMessage(content=markdown)],
        }

    return finalize
