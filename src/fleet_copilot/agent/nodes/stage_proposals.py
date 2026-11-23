"""`stage_proposals` node: idempotent DB staging using uuid5(thread_id, tool_call_id)."""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.runtime import Runtime

from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.actions import DraftInput
from fleet_copilot.services.audit import ApprovalRequestedPayload


def make_stage_proposals_node(deps: Any) -> Any:
    """Build the `stage_proposals` node closed over `AgentDeps`."""

    def stage_proposals(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        raw_drafts = list(state.get("drafts") or [])
        if not raw_drafts:
            return {"staged_proposals": [], "approval_packet": None}

        thread_id = state.get("thread_id", "")
        turn_id = state.get("turn_id", "")
        ledger = EvidenceLedger.from_json(state.get("evidence") or {})

        draft_inputs: list[DraftInput] = []
        for idx, d in enumerate(raw_drafts):
            tc_id = str(d.get("tool_call_id") or f"draft_{idx}")
            prop_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{thread_id}:{tc_id}"))
            draft_inputs.append(
                DraftInput(
                    tool_name=d["tool_name"],
                    target_type=d["target_type"],
                    target_id=d["target_id"],
                    arguments=dict(d.get("arguments") or {}),
                    rationale=str(d.get("rationale") or ""),
                    evidence_handles=list(d.get("evidence_handles") or []),
                    proposal_id=prop_id,
                )
            )

        batch_id = f"batch_{uuid.uuid5(uuid.NAMESPACE_URL, f'{thread_id}:{turn_id}').hex[:12]}"
        res = deps.action_svc.stage(
            ctx,
            draft_inputs,
            thread_id,
            turn_id,
            ledger,
            deps.clock,
            batch_id=batch_id,
        )

        # Serialize staged proposals to JSON-native dicts for checkpointing
        staged_json: list[dict[str, Any]] = []
        for row in res.staged:
            staged_json.append(
                {
                    "proposal_id": str(row["proposal_id"]),
                    "batch_id": str(row["batch_id"]),
                    "tool_name": str(row["tool_name"]),
                    "target_type": str(row["target_type"]),
                    "target_id": str(row["target_id"]),
                    "arguments": dict(row["arguments_json"] or {}),
                    "rationale": str(row["rationale"]),
                    "evidence_snapshot_ids": list(row["evidence_snapshot_ids"] or []),
                    "status": str(row["status"]),
                    "expires_at": (
                        row["expires_at"].isoformat()
                        if hasattr(row.get("expires_at"), "isoformat")
                        else row.get("expires_at")
                    ),
                }
            )

        if not staged_json:
            return {"staged_proposals": [], "approval_packet": None}

        proposal_ids = [p["proposal_id"] for p in staged_json]
        deps.audit_svc.record_standalone(
            ctx,
            "approval_requested",
            ApprovalRequestedPayload(batch_id=batch_id, proposal_ids=proposal_ids),
            thread_id=thread_id,
        )
        deps.copilot_store.update_turn(ctx.company_id, turn_id, status="waiting_approval")

        packet = {
            "batch_id": batch_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "proposals": staged_json,
        }
        return {
            "staged_proposals": staged_json,
            "approval_packet": packet,
            "status": "waiting_approval",
        }

    return stage_proposals
