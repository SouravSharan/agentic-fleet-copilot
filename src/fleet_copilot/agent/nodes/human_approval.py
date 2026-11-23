"""`human_approval` node: strictly side-effect-free `interrupt()` emitting the approval packet."""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from fleet_copilot.agent.state import CopilotState


def human_approval(state: CopilotState) -> dict[str, Any]:
    """Pause the graph with `interrupt()` and resume with `{decisions: {id: ...}, note}`."""
    packet = state.get("approval_packet")
    if not packet or not packet.get("proposals"):
        return {"decisions": {}, "decision_note": None}

    resume_payload = interrupt(packet)
    if isinstance(resume_payload, dict):
        decisions = dict(resume_payload.get("decisions") or {})
        note = resume_payload.get("note")
    else:
        decisions = {}
        note = None
    return {
        "decisions": decisions,
        "decision_note": str(note) if note is not None else None,
    }
