"""Unit tests for hardened full-answer grounding verifier."""

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


def test_finalize_neutralizes_unknown_handles_and_preserves_valid_handles() -> None:
    """Unknown bracketed handles [E99]/[C9]/[Q9] become plain text; valid ones stay intact."""
    from fleet_copilot.agent.nodes.finalize import (
        neutralize_answer_unknown_handles,
        neutralize_unknown_handles,
    )

    ev_map = {
        **_sample_evidence(),
        "C1": {"handle": "C1", "device_id": "fleet", "snapshot_id": "snap_8nm2"},
        "Q1": {"handle": "Q1", "device_id": "fleet", "snapshot_id": "query_1"},
    }

    # 1. Unknown handles neutralized
    text_unk, changed_unk = neutralize_unknown_handles(
        "The requested evidence handle [E99] does not exist, nor do [C9] or [Q42].",
        ev_map,
    )
    assert changed_unk is True
    assert text_unk == "The requested evidence handle E99 does not exist, nor do C9 or Q42."
    assert "[E99]" not in text_unk

    # 2. Valid handles untouched (byte-identical)
    valid_str = "Disk is 2.6% [E1], chart [C1], query [Q1], combined [E1, E2]."
    text_val, changed_val = neutralize_unknown_handles(valid_str, ev_map)
    assert changed_val is False
    assert text_val == valid_str

    # 3. Mixed valid and unknown handles in text & Answer fields
    ans = Answer(
        summary="Device 8NM23J95R5I6 has 2.6% free disk [E1]; handle [E99] does not exist.",
        result_device_ids=["8NM23J95R5I6"],
        claims=[
            Claim(
                text="Free disk is 2.6% [E1], not [E99] or [E1, E88].",
                citations=["E1"],
                device_ids=["8NM23J95R5I6"],
                numbers=[2.6],
            )
        ],
        caveats=["User-requested [E99] is absent from the ledger."],
    )
    clean_ans, changed = neutralize_answer_unknown_handles(ans, ev_map)
    assert changed is True
    assert "[E99]" not in clean_ans.summary and "E99" in clean_ans.summary
    assert "[E1]" in clean_ans.summary
    assert "[E99]" not in clean_ans.claims[0].text and "[E88]" not in clean_ans.claims[0].text
    assert "[E1]" in clean_ans.claims[0].text
    assert "[E99]" not in clean_ans.caveats[0] and "E99" in clean_ans.caveats[0]


def test_strip_keeps_devices_grounded_by_sql_and_chart_handles() -> None:
    """After retries run out, devices cited only via `[Q#]`/`[C#]` rows survive the strip step."""
    from types import SimpleNamespace

    from fleet_copilot.agent.nodes.verify_grounding import make_verify_grounding_node
    from fleet_copilot.domain.models import TenantContext

    recorded: list[str] = []
    deps = SimpleNamespace(
        runtime_cfg=SimpleNamespace(budgets=SimpleNamespace(max_verifier_retries=1)),
        audit_svc=SimpleNamespace(
            record_standalone=lambda _ctx, event, _payload, **_kw: recorded.append(event)
        ),
    )
    runtime = SimpleNamespace(
        context=TenantContext(
            company_id="acme-001", actor_id="t:admin", role="it_admin", trace_id="t-1"
        )
    )
    answer = Answer(
        summary="Two devices are relevant.",
        result_device_ids=["8NM23J95R5I6", "MT7PJB7N5LRE"],
        claims=[
            Claim(
                text="One device matched the query [Q1].",
                citations=["Q1"],
                device_ids=["8NM23J95R5I6"],
            ),
            Claim(
                text="One device is shown in the chart [C1].",
                citations=["C1"],
                device_ids=["MT7PJB7N5LRE"],
            ),
            Claim(text="An unsupported statement [E9].", citations=["E9"], device_ids=[]),
        ],
    )
    state = {
        "answer": answer.model_dump(mode="json"),
        # Multi-device chart/SQL artifacts are anchored to device "fleet" in the ledger;
        # the devices they ground live in their rows.
        "evidence": {
            "Q1": {
                "handle": "Q1",
                "snapshot_id": "query_ab12",
                "device_id": "fleet",
                "metric": "sql_query",
                "value": 2,
            },
            "C1": {
                "handle": "C1",
                "snapshot_id": "snap_c1",
                "device_id": "fleet",
                "metric": "chart:disk_free_pct",
                "value": 60,
            },
        },
        "queries": [
            {"handle": "Q1", "rows": [{"device_id": "8NM23J95R5I6"}, {"device_id": "AAAA11112222"}]}
        ],
        "charts": [
            {
                "handle": "C1",
                "data_rows": [{"device_id": "MT7PJB7N5LRE"}, {"device_id": "BBBB33334444"}],
            }
        ],
        "verifier_retries": 1,
        "user_message": "which devices",
    }

    out = make_verify_grounding_node(deps)(state, runtime)  # type: ignore[arg-type]

    stripped = Answer.model_validate(out["answer"])
    assert [c.citations for c in stripped.claims] == [["Q1"], ["C1"]]
    assert sorted(stripped.result_device_ids) == ["8NM23J95R5I6", "MT7PJB7N5LRE"]
    assert recorded == ["grounding_violation"]
