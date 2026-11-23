"""Unit tests for strict audit event payload models (extra=forbid, length cap, forbidden keys)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fleet_copilot.services.audit import (
    AUDIT_PAYLOAD_MODELS,
    FORBIDDEN_AUDIT_FIELD_NAMES,
    ActionExecutedPayload,
    ProposalRefusedPayload,
    ProposalStagedPayload,
    validate_audit_payload,
)


def test_all_05_section_392_events_registered_and_strict() -> None:
    expected_events = {
        "turn_started",
        "plan_created",
        "tool_called",
        "access_denied",
        "refusal",
        "grounding_violation",
        "proposal_refused",
        "proposal_staged",
        "approval_requested",
        "proposal_decided",
        "freshness_blocked",
        "action_executed",
        "action_failed",
        "proposal_expired",
        "turn_completed",
        "demo_reset",
    }
    assert expected_events.issubset(AUDIT_PAYLOAD_MODELS.keys())

    # No model may declare any forbidden free-text / PII field name
    for event_type, model_cls in AUDIT_PAYLOAD_MODELS.items():
        overlap = FORBIDDEN_AUDIT_FIELD_NAMES & set(model_cls.model_fields.keys())
        assert not overlap, f"{event_type} declares forbidden fields: {overlap}"


def test_audit_payload_rejects_unknown_fields_and_long_strings() -> None:
    # Unknown field rejected (extra=forbid)
    with pytest.raises(ValidationError):
        ProposalRefusedPayload(
            tool_name="create_upgrade_order",
            target_type="device",
            target_ref="M4XVHUV1MEPZ",
            failed_checks=["ram_upgradable"],
            policy_version="pol_1234",
            message="free text not allowed",  # type: ignore[call-arg]
        )

    # Strings longer than 128 chars rejected
    with pytest.raises(ValidationError):
        ActionExecutedPayload(
            proposal_id="p1",
            adapter="mock",
            receipt_id="x" * 129,
            receipt_hash="a" * 64,
        )

    # List elements longer than 128 chars rejected
    with pytest.raises(ValidationError):
        ProposalStagedPayload(
            proposal_id="p1",
            tool_name="create_upgrade_order",
            target_type="device",
            target_ref="DEV1",
            dedupe_key_hash="h" * 64,
            approval_hash="a" * 64,
            evidence_snapshot_ids=["s" * 129],
            policy_version="pol_1234",
            expires_at="2026-11-26T12:00:00+00:00",
        )

    # Unknown event type rejected
    with pytest.raises(ValueError, match="Unknown audit event_type"):
        validate_audit_payload("unknown_event", {})
