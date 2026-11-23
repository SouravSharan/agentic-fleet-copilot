"""Unit tests for canonical approval_hash computation."""

from __future__ import annotations

from fleet_copilot.domain.policy_rules import approval_hash, proposal_approval_hash


def test_approval_hash_invariant_to_key_order_and_sensitive_to_changes() -> None:
    f1 = {
        "tool_name": "create_upgrade_order",
        "target_type": "device",
        "target_id": "M4XVHUV1MEPZ",
        "arguments": {"spec": "32 GB DDR5 SODIMM", "component": "ram", "device_id": "M4XVHUV1MEPZ"},
        "evidence_snapshot_ids": ["snap_a", "snap_b"],
        "dedupe_key": "upgrade:M4XVHUV1MEPZ:ram",
    }
    f2 = {
        "dedupe_key": "upgrade:M4XVHUV1MEPZ:ram",
        "evidence_snapshot_ids": ["snap_a", "snap_b"],
        "arguments": {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB DDR5 SODIMM"},
        "target_id": "M4XVHUV1MEPZ",
        "target_type": "device",
        "tool_name": "create_upgrade_order",
    }
    h1 = approval_hash(f1)
    h2 = approval_hash(f2)
    assert len(h1) == 64
    assert h1 == h2

    # Unsorted evidence snapshot IDs normalize identically via proposal_approval_hash
    p1 = proposal_approval_hash(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="M4XVHUV1MEPZ",
        arguments={"component": "ram", "spec": "32 GB DDR5 SODIMM"},
        evidence_snapshot_ids=["snap_b", "snap_a"],
        dedupe_key="upgrade:M4XVHUV1MEPZ:ram",
    )
    p2 = proposal_approval_hash(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="M4XVHUV1MEPZ",
        arguments={"spec": "32 GB DDR5 SODIMM", "component": "ram"},
        evidence_snapshot_ids=["snap_a", "snap_b"],
        dedupe_key="upgrade:M4XVHUV1MEPZ:ram",
    )
    assert p1 == p2

    # Any argument mutation changes the hash
    p3 = proposal_approval_hash(
        tool_name="create_upgrade_order",
        target_type="device",
        target_id="M4XVHUV1MEPZ",
        arguments={"spec": "64 GB DDR5 SODIMM", "component": "ram"},
        evidence_snapshot_ids=["snap_a", "snap_b"],
        dedupe_key="upgrade:M4XVHUV1MEPZ:ram",
    )
    assert p3 != p1
