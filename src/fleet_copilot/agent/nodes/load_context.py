"""`load_context` node: resolve data clock, thresholds, role-filtered tools, turn row, audit."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from langgraph.runtime import Runtime

from fleet_copilot.agent.prompts import prompt_bundle_version
from fleet_copilot.agent.state import CopilotState, reset_list
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import TurnStartedPayload
from fleet_copilot.tools.registry import tools_for

logger = logging.getLogger(__name__)


def _thread_title_from_message(message: str, *, max_len: int = 60) -> str:
    collapsed = " ".join(message.split())
    if not collapsed:
        return "New conversation"
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


def _rebuild_digest_from_turns(
    turns: list[dict[str, Any]],
    *,
    exclude_turn_id: str,
) -> list[dict[str, Any]]:
    """Reconstruct last 3 conversation digest entries from persisted `copilot.turns` rows."""
    rebuilt: list[dict[str, Any]] = []
    for row in turns:
        if str(row.get("turn_id", "")) == exclude_turn_id:
            continue
        user_msg = row.get("user_message")
        if not user_msg:
            continue
        rj = row.get("response_json") if isinstance(row.get("response_json"), dict) else None
        ans = (
            rj.get("answer")
            if isinstance(rj, dict) and isinstance(rj.get("answer"), dict)
            else None
        )
        summary = (
            str(ans.get("summary"))
            if ans and ans.get("summary")
            else str(row.get("assistant_response") or "")[:240]
        )
        result_devs = list(ans.get("result_device_ids") or []) if ans else []
        refused_alts = []
        if isinstance(rj, dict):
            for v in rj.get("verdicts") or []:
                if isinstance(v, dict) and v.get("decision") == "refuse":
                    refused_alts.append(
                        {
                            "refused_tool": v.get("tool_name"),
                            "target_id": v.get("target_id"),
                            "alternatives": v.get("alternatives") or [],
                        }
                    )
        rebuilt.append(
            {
                "user": str(user_msg),
                "summary": summary,
                "result_device_ids": result_devs,
                "refused_alternatives": refused_alts,
            }
        )
    return rebuilt[-3:]


def make_load_context_node(deps: Any) -> Any:
    """Build the `load_context` node function closed over `AgentDeps`."""

    def load_context(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        thread_id = state.get("thread_id") or f"{ctx.company_id}:{uuid.uuid4().hex[:12]}"
        turn_id = state.get("turn_id") or f"turn_{uuid.uuid4().hex[:12]}"
        user_message = state.get("user_message") or ""
        msgs = state.get("messages") or []
        if not user_message and msgs:
            last = msgs[-1]
            user_message = getattr(last, "text", str(last.content))

        as_of = deps.telemetry_repo.tenant_as_of(ctx.company_id)
        policies = deps.policy_svc.resolve(ctx)
        tool_cfgs = deps.copilot_store.tool_configs(ctx.company_id)
        tenant_hostnames: dict[str, str] = {}
        try:
            for dev in deps.telemetry_repo.latest_states(ctx.company_id):
                if getattr(dev, "os_hostname", None):
                    tenant_hostnames[str(dev.os_hostname).lower()] = str(dev.device_id)
        except Exception:  # noqa: BLE001
            # The verifier can still match device ids; hostname matching is degraded, so say so.
            logger.warning(
                "hostname map unavailable; verifier falls back to device ids",
                extra={"company_id": ctx.company_id},
                exc_info=True,
            )
            tenant_hostnames = {}
        allowed_specs = tools_for(
            ctx,
            tool_cfgs,
            deps.all_tool_specs,
            actions_enabled=deps.actions_enabled,
        )
        allowed_names = [s.name for s in allowed_specs]

        input_hash = hashlib.sha256(user_message.encode("utf-8")).hexdigest()
        p_ver = prompt_bundle_version()

        deps.copilot_store.ensure_thread(
            ctx.company_id,
            thread_id,
            ctx.actor_id,
            title=_thread_title_from_message(user_message),
        )
        existing_digest = list(state.get("conversation_digest") or [])
        if not existing_digest:
            try:
                prior_turns = deps.copilot_store.list_turns(ctx.company_id, thread_id)
                existing_digest = _rebuild_digest_from_turns(prior_turns, exclude_turn_id=turn_id)
            except Exception:  # noqa: BLE001
                existing_digest = []

        deps.copilot_store.ensure_turn(
            ctx.company_id,
            thread_id,
            turn_id,
            ctx.trace_id,
            ctx.actor_id,
            user_message,
            status="running",
            input_hash=input_hash,
        )
        deps.audit_svc.record_standalone(
            ctx,
            "turn_started",
            TurnStartedPayload(
                turn_id=turn_id,
                thread_id=thread_id,
                role=ctx.role,
                model_id=deps.model_id,
                prompt_version=p_ver,
                input_hash=input_hash,
            ),
            thread_id=thread_id,
        )

        logger.info(
            "turn_started",
            extra={
                "company_id": ctx.company_id,
                "role": ctx.role,
                "turn_id": turn_id,
                "allowed_tools_count": len(allowed_names),
            },
        )

        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "user_message": user_message,
            "telemetry_as_of": as_of.isoformat(),
            "resolved_policies": {
                k: {"value": v.value, "source": v.source} for k, v in policies.items()
            },
            "allowed_tool_names": allowed_names,
            "tenant_hostnames": tenant_hostnames,
            "conversation_digest": existing_digest,
            "plan": None,
            "tool_trace": reset_list(),
            "tool_outputs": reset_list(),
            "insights": reset_list(),
            "charts": reset_list(),
            "queries": reset_list(),
            "drafts": reset_list(),
            "verdicts": reset_list(),
            "staged_proposals": [],
            "approval_packet": None,
            "decisions": {},
            "decision_note": None,
            "execution_results": [],
            "answer": None,
            "answer_markdown": "",
            "status": "running",
            "refusal_reason": None,
            "verifier_violations": [],
            "verifier_retries": 0,
            "warnings": reset_list(),
            "step_count": 0,
            "budget_exhausted": False,
            "usage": reset_list(),
        }

    return load_context
