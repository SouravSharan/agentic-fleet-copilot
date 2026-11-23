"""`execute_decisions` node: applies human decisions via `ActionService`, skipping queue-decided."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext


def make_execute_decisions_node(deps: Any) -> Any:
    """Build the `execute_decisions` node closed over `AgentDeps`."""

    def execute_decisions(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        staged = list(state.get("staged_proposals") or [])
        decisions = dict(state.get("decisions") or {})
        note = state.get("decision_note")
        results: list[dict[str, Any]] = []
        updated_proposals: list[dict[str, Any]] = []

        for prop in staged:
            pid = str(prop["proposal_id"])
            current = deps.copilot_store.get_proposal(ctx.company_id, pid)
            if current is None:
                results.append(
                    {"proposal_id": pid, "status": "not_found", "message": "Proposal not found."}
                )
                continue

            # If already decided outside the graph (e.g. approvals queue / API), report & skip!
            if current["status"] != "pending_approval":
                results.append(
                    {
                        "proposal_id": pid,
                        "status": str(current["status"]),
                        "skipped": True,
                        "receipt": current.get("external_receipt_json"),
                        "message": (
                            f"Already decided outside chat: {current['status']} "
                            f"by {current.get('reviewer_id')}"
                        ),
                    }
                )
                updated_proposals.append({**prop, "status": str(current["status"])})
                continue

            raw_decision = str(decisions.get(pid, "defer")).strip().lower()
            if raw_decision not in ("approve", "reject", "defer"):
                raw_decision = "defer"

            dec = deps.action_svc.decide(
                ctx,
                pid,
                raw_decision,  # type: ignore[arg-type]
                "chat",
                deps.clock,
                note=note,
            )
            if not dec.applied:
                final_st = str(dec.proposal["status"]) if dec.proposal else "unknown"
                results.append(
                    {
                        "proposal_id": pid,
                        "status": final_st,
                        "skipped": True,
                        "message": dec.message,
                    }
                )
                updated_proposals.append({**prop, "status": final_st})
                continue

            if raw_decision == "approve":
                exe = deps.action_svc.execute(ctx, pid, deps.clock)
                results.append(
                    {
                        "proposal_id": pid,
                        "status": exe.status,
                        "receipt": exe.receipt.model_dump(mode="json") if exe.receipt else None,
                        "message": exe.message,
                    }
                )
                updated_proposals.append({**prop, "status": exe.status})
            elif raw_decision == "reject":
                results.append(
                    {"proposal_id": pid, "status": "rejected", "message": "Rejected by reviewer."}
                )
                updated_proposals.append({**prop, "status": "rejected"})
            else:
                results.append(
                    {
                        "proposal_id": pid,
                        "status": "pending_approval",
                        "deferred": True,
                        "message": "Deferred; remains pending in approvals queue.",
                    }
                )
                updated_proposals.append({**prop, "status": "pending_approval"})

        return {
            "staged_proposals": updated_proposals,
            "execution_results": results,
            "approval_packet": None,
        }

    return execute_decisions
