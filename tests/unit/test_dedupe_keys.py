"""Unit tests for deterministic proposal deduplication keys."""

from __future__ import annotations

from datetime import UTC, datetime

from fleet_copilot.domain.models import Evidence
from fleet_copilot.domain.policy_rules import ProposalDraft, dedupe_key

TS = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)


def _ev(device_id: str, metric: str, snap: str = "snap_1") -> Evidence:
    return Evidence(
        handle="E1",
        snapshot_id=snap,
        device_id=device_id,
        hostname=f"host-{device_id}",
        collected_at=TS,
        metric=metric,
        value=2.0,
        label=metric,
    )


def test_dedupe_keys_ignore_free_text_changes() -> None:
    u1 = ProposalDraft(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="DEV1",
        arguments={"device_id": "DEV1", "component": "ram", "spec": "32 GB DDR5"},
        rationale="Reason A",
        evidence=[_ev("DEV1", "ram_used_pct")],
    )
    u2 = u1.model_copy(
        update={
            "arguments": {"device_id": "DEV1", "component": "ram", "spec": "64 GB DDR5"},
            "rationale": "Completely different phrasing",
        }
    )
    assert dedupe_key(u1) == "upgrade:DEV1:ram"
    assert dedupe_key(u1) == dedupe_key(u2)

    t1 = ProposalDraft(
        tool_name="open_remediation_ticket",
        target_type="device",
        target_id="DEV1",
        arguments={"device_id": "DEV1", "check_id": "screen_lock", "note": "Note A"},
        rationale="A",
        evidence=[_ev("DEV1", "compliance:screen_lock")],
    )
    t2 = t1.model_copy(
        update={"arguments": {"device_id": "DEV1", "check_id": "screen_lock", "note": "Note B"}}
    )
    assert dedupe_key(t1) == "ticket:DEV1:screen_lock"
    assert dedupe_key(t1) == dedupe_key(t2)

    r1 = ProposalDraft(
        tool_name="flag_device_for_replacement",
        target_type="device",
        target_id="DEV1",
        arguments={"device_id": "DEV1", "reason": "Reason 1"},
        rationale="A",
        evidence=[_ev("DEV1", "battery_latest")],
    )
    r2 = r1.model_copy(update={"arguments": {"device_id": "DEV1", "reason": "Reason 2"}})
    assert dedupe_key(r1) == "replace:DEV1"
    assert dedupe_key(r1) == dedupe_key(r2)


def test_notify_dedupe_key_fingerprints_evidence_not_message() -> None:
    ev_a = _ev("DEV1", "disk_free_pct", "snap_1")
    ev_b = _ev("DEV2", "compliance:screen_lock", "snap_2")

    n1 = ProposalDraft(
        tool_name="notify_employee",
        target_type="employee",
        target_id="emp-acme-1003",
        arguments={"employee_id": "emp-acme-1003", "message": "Please free up disk space on DEV1."},
        rationale="Disk low",
        evidence=[ev_a, ev_b],
    )
    # Different wording + reversed evidence order -> identical dedupe_key
    n2 = ProposalDraft(
        tool_name="notify_employee",
        target_type="employee",
        target_id="emp-acme-1003",
        arguments={"employee_id": "emp-acme-1003", "message": "Urgent: clean up DEV1 now!"},
        rationale="Reworded rationale",
        evidence=[ev_b, ev_a],
    )
    # Different evidence topic -> different dedupe_key
    n3 = n1.model_copy(update={"evidence": [_ev("DEV1", "ram_used_pct", "snap_3")]})

    k1 = dedupe_key(n1)
    assert k1.startswith("notify:emp-acme-1003:")
    assert len(k1.split(":")[-1]) == 12
    assert k1 == dedupe_key(n2)
    assert k1 != dedupe_key(n3)
