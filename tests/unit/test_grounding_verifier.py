"""Unit tests for hardened full-answer grounding verifier (audit/09 H1 and H2)."""

from __future__ import annotations

from fleet_copilot.agent.nodes.verify_grounding import verify_answer_grounding
from fleet_copilot.agent.schemas import Answer, Claim


def _sample_evidence() -> dict[str, dict]:
    return {
        "E1": {
            "handle": "E1",
            "snapshot_id": "snap_8nm2",
            "device_id": "8NM23J95R5I6",
            "hostname": "acme-thinkpad-8",
            "collected_at": "2026-06-12T11:00:00+00:00",
            "metric": "disk_free_pct",
            "value": 2.6,
            "unit": "%",
            "label": "Disk free 2.6%",
        },
        "E2": {
            "handle": "E2",
            "snapshot_id": "snap_mt7p",
            "device_id": "MT7PJB7N5LRE",
            "hostname": "acme-macbook-4",
            "collected_at": "2026-06-12T11:00:00+00:00",
            "metric": "disk_free_pct",
            "value": 2.0,
            "unit": "%",
            "label": "Disk free 2.0%",
        },
    }


def test_verifier_flags_fabricated_device_id_in_summary_and_caveats() -> None:
    ans = Answer(
        summary="Low disk on 8NM23J95R5I6 and fabricated device FAKE12345678.",
        result_device_ids=["8NM23J95R5I6"],
        claims=[
            Claim(
                text="8NM23J95R5I6 has 2.6% free disk [E1].",
                citations=["E1"],
                device_ids=["8NM23J95R5I6"],
                numbers=[2.6],
            )
        ],
        caveats=["Also checked cross-tenant device E4V0SB5I0MYU and globex-dell-7."],
    )
    hard, _ = verify_answer_grounding(
        ans,
        evidence_map=_sample_evidence(),
        tool_outputs=[{"tool_name": "find_devices", "output": {"coverage": {"matched": 2}}}],
        resolved_policies={"low_disk_free_pct": {"value": 15.0, "source": "global"}},
        insights=[],
        user_message="Which devices are low on disk?",
        telemetry_as_of="2026-06-12T11:00:00+00:00",
        tenant_hostnames={"acme-thinkpad-8": "8NM23J95R5I6", "acme-macbook-4": "MT7PJB7N5LRE"},
    )
    codes = {v["code"] for v in hard}
    assert "unknown_device_in_summary" in codes
    assert "unknown_device_in_caveat" in codes


def test_verifier_flags_unreported_device_id_or_hostname_in_claim_text() -> None:
    # Claim omits device_ids but mentions MT7PJB7N5LRE & acme-macbook-4 while citing only E1
    ans = Answer(
        summary="Disk check complete.",
        result_device_ids=["8NM23J95R5I6"],
        claims=[
            Claim(
                text="MT7PJB7N5LRE (acme-macbook-4) has 2.6% free disk [E1].",
                citations=["E1"],
                device_ids=[],
                numbers=[2.6],
            )
        ],
        caveats=[],
    )
    hard, _ = verify_answer_grounding(
        ans,
        evidence_map=_sample_evidence(),
        tool_outputs=[],
        resolved_policies={},
        insights=[],
        user_message="Check disk",
        telemetry_as_of="2026-06-12T11:00:00+00:00",
        tenant_hostnames={"acme-thinkpad-8": "8NM23J95R5I6", "acme-macbook-4": "MT7PJB7N5LRE"},
    )
    assert any(v["code"] == "device_not_in_cited_evidence" for v in hard)


def test_verifier_flags_unverified_summary_number() -> None:
    ans = Answer(
        summary="5 laptops failed disk checks with 42.5% capacity loss.",
        result_device_ids=["8NM23J95R5I6"],
        claims=[
            Claim(
                text="8NM23J95R5I6 has 2.6% free disk [E1].",
                citations=["E1"],
                device_ids=["8NM23J95R5I6"],
                numbers=[2.6],
            )
        ],
        caveats=[],
    )
    hard, soft = verify_answer_grounding(
        ans,
        evidence_map=_sample_evidence(),
        tool_outputs=[{"tool_name": "find_devices", "output": {"coverage": {"matched": 1}}}],
        resolved_policies={},
        insights=[],
        user_message="Check disk",
        telemetry_as_of="2026-06-12T11:00:00+00:00",
        tenant_hostnames={"acme-thinkpad-8": "8NM23J95R5I6"},
    )
    assert hard == []
    assert any(v["code"] == "unverified_number_in_summary" for v in soft)


def test_verifier_allows_prompt_echo_on_not_found() -> None:
    ans = Answer(
        summary="Device E4V0SB5I0MYU was not found in your tenant.",
        result_device_ids=[],
        claims=[],
        caveats=["Device E4V0SB5I0MYU is not present in acme-001."],
    )
    hard, soft = verify_answer_grounding(
        ans,
        evidence_map={},
        tool_outputs=[
            {"tool_name": "get_device_details", "output": {"ok": False, "error": "not_found"}}
        ],
        resolved_policies={},
        insights=[],
        user_message="Show details for device E4V0SB5I0MYU.",
        telemetry_as_of="2026-06-12T11:00:00+00:00",
        tenant_hostnames={"acme-thinkpad-8": "8NM23J95R5I6"},
    )
    assert hard == []
    assert soft == []


def test_verifier_checks_result_device_ids_in_cited_evidence() -> None:
    # MT7PJB7N5LRE is in ledger E2, but only E1 (8NM2) was cited in claims!
    ans = Answer(
        summary="Low disk on 8NM23J95R5I6.",
        result_device_ids=["8NM23J95R5I6", "MT7PJB7N5LRE"],
        claims=[
            Claim(
                text="8NM23J95R5I6 has 2.6% free disk [E1].",
                citations=["E1"],
                device_ids=["8NM23J95R5I6"],
                numbers=[2.6],
            )
        ],
        caveats=[],
    )
    hard, _ = verify_answer_grounding(
        ans,
        evidence_map=_sample_evidence(),
        tool_outputs=[],
        resolved_policies={},
        insights=[],
        user_message="Which devices are low on disk?",
        telemetry_as_of="2026-06-12T11:00:00+00:00",
        tenant_hostnames={"acme-thinkpad-8": "8NM23J95R5I6", "acme-macbook-4": "MT7PJB7N5LRE"},
    )
    assert any(v["code"] == "result_device_not_cited" for v in hard)
