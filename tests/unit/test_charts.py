"""Unit tests for B2 chart spec validator, ledger C/Q handles, and chart grounding verifier."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fleet_copilot.agent.nodes.verify_grounding import verify_answer_grounding
from fleet_copilot.agent.schemas import Answer, Claim
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import Evidence
from fleet_copilot.services.charts import (
    VEGA_LITE_V5_SCHEMA,
    ChartRequest,
    UnsafeChartSpecError,
    validate_vega_lite_spec,
)


def test_validator_accepts_safe_template_specs() -> None:
    spec = {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": "Disk Free % (30d)",
        "data": {"values": [{"date": "2026-06-12", "value": 2.0, "series": "acme-macbook-4"}]},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "date", "type": "temporal"},
            "y": {"field": "value", "type": "quantitative"},
            "color": {"field": "series", "type": "nominal"},
        },
    }
    validate_vega_lite_spec(spec)


@pytest.mark.parametrize(
    "bad_spec",
    [
        # Foreign $schema
        {
            "$schema": "https://evil.example.com/vega-lite/v5.json",
            "data": {"values": []},
            "mark": "bar",
        },
        # External URL in data
        {
            "$schema": VEGA_LITE_V5_SCHEMA,
            "data": {"url": "https://evil.example.com/leak.json"},
            "mark": "bar",
        },
        # Nested URL inside mark/tooltip/layer
        {
            "$schema": VEGA_LITE_V5_SCHEMA,
            "data": {"values": []},
            "mark": {"type": "image", "url": "https://evil.example.com/pixel.png"},
        },
        # Signals disallowed
        {
            "$schema": VEGA_LITE_V5_SCHEMA,
            "data": {"values": []},
            "signals": [{"name": "x", "value": 1}],
            "mark": "bar",
        },
        # calculate / expr transforms disallowed
        {
            "$schema": VEGA_LITE_V5_SCHEMA,
            "data": {"values": []},
            "transform": [{"calculate": "datum.a * 2", "as": "b"}],
            "mark": "bar",
        },
        {
            "$schema": VEGA_LITE_V5_SCHEMA,
            "data": {"values": []},
            "encoding": {"x": {"condition": {"expr": "true"}}},
            "mark": "bar",
        },
        # Named remote dataset without inline values
        {
            "$schema": VEGA_LITE_V5_SCHEMA,
            "data": {"name": "external_table"},
            "mark": "bar",
        },
    ],
)
def test_validator_rejects_url_foreign_schema_signals_and_expr_calculate(
    bad_spec: dict[str, object],
) -> None:
    with pytest.raises(UnsafeChartSpecError):
        validate_vega_lite_spec(bad_spec)


def test_chart_type_validated_against_kind() -> None:
    # metric_over_time rejects stacked_bar
    with pytest.raises(ValueError, match="chart_type"):
        ChartRequest(
            title="Bad chart",
            kind="metric_over_time",
            metric="disk_free_pct",
            chart_type="stacked_bar",
        )
    # aggregate_by_group requires group_by and metric
    with pytest.raises(ValueError):
        ChartRequest(
            title="Missing group_by",
            kind="aggregate_by_group",
            metric="battery_cycles",
            stat="avg",
            chart_type="bar",
        )
    # from_query requires query_handle starting with Q
    with pytest.raises(ValueError):
        ChartRequest(
            title="Bad handle",
            kind="from_query",
            query_handle="E1",
            x="model",
            y="avg_cycles",
            chart_type="bar",
        )


def test_evidence_ledger_supports_e_c_and_q_handles_roundtrip() -> None:
    ts = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)
    ledger = EvidenceLedger()
    e1 = ledger.add(
        Evidence(
            snapshot_id="snap_1",
            device_id="MT7PJB7N5LRE",
            collected_at=ts,
            metric="disk_free_pct",
            value=2.0,
            unit="%",
            label="disk free 2.0%",
        )
    )
    c1 = ledger.add_artifact(
        "C",
        Evidence(
            snapshot_id="chart_01",
            device_id="fleet",
            collected_at=ts,
            metric="chart:disk_free_pct",
            value=2,
            label="Disk free chart",
        ),
    )
    q1 = ledger.add_artifact(
        "Q",
        Evidence(
            snapshot_id="query_01",
            device_id="fleet",
            collected_at=ts,
            metric="sql_query",
            value=3,
            label="Avg cycles by model",
        ),
    )
    assert (e1, c1, q1) == ("E1", "C1", "Q1")

    restored = EvidenceLedger.from_json(ledger.to_json())
    assert restored.resolve("E1") is not None
    assert restored.resolve("C1") is not None
    assert restored.resolve("Q1") is not None
    c2 = restored.add_artifact(
        "C",
        Evidence(
            snapshot_id="chart_02",
            device_id="fleet",
            collected_at=ts,
            metric="chart:ram_used_pct",
            value=1,
            label="RAM chart",
        ),
    )
    assert c2 == "C2"


def test_verifier_checks_claim_numbers_against_chart_data_rows() -> None:
    ts = "2026-06-12T11:00:00+00:00"
    evidence_map = {
        "C1": {
            "handle": "C1",
            "snapshot_id": "chart_abc",
            "device_id": "MT7PJB7N5LRE",
            "hostname": "acme-macbook-4",
            "collected_at": ts,
            "metric": "chart:disk_free_pct",
            "value": 2.0,
            "unit": "%",
            "label": "Disk Free % chart",
        }
    }
    charts = [
        {
            "chart_id": "chart_abc",
            "handle": "C1",
            "title": "Disk Free % (30d)",
            "data_rows": [
                {
                    "device_id": "MT7PJB7N5LRE",
                    "hostname": "acme-macbook-4",
                    "date": "2026-06-12",
                    "value": 2.0,
                    "snapshot_id": "snap_mt7p",
                },
                {
                    "device_id": "8NM23J95R5I6",
                    "hostname": "acme-thinkpad-8",
                    "date": "2026-06-12",
                    "value": 2.59,
                    "snapshot_id": "snap_8nm2",
                },
            ],
        }
    ]
    good = Answer(
        summary="MT7PJB7N5LRE is at 2.0% and 8NM23J95R5I6 is at 2.59% free disk.",
        result_device_ids=["MT7PJB7N5LRE", "8NM23J95R5I6"],
        claims=[
            Claim(
                text="Chart [C1] shows MT7PJB7N5LRE at 2.0% and 8NM23J95R5I6 at 2.59% free disk.",
                citations=["C1"],
                device_ids=["MT7PJB7N5LRE", "8NM23J95R5I6"],
                numbers=[2.0, 2.59],
            )
        ],
    )
    hard, soft = verify_answer_grounding(
        good,
        evidence_map=evidence_map,
        tool_outputs=[],
        resolved_policies={},
        insights=[],
        charts=charts,
        user_message="Chart disk free %",
        telemetry_as_of=ts,
    )
    assert hard == []
    assert soft == []

    # Fabricated number 47.3 not in chart rows triggers soft violation
    bad_num = Answer(
        summary="MT7PJB7N5LRE is at 47.3% free disk.",
        result_device_ids=["MT7PJB7N5LRE"],
        claims=[
            Claim(
                text="Chart [C1] shows MT7PJB7N5LRE at 47.3% free disk.",
                citations=["C1"],
                device_ids=["MT7PJB7N5LRE"],
                numbers=[47.3],
            )
        ],
    )
    hard2, soft2 = verify_answer_grounding(
        bad_num,
        evidence_map=evidence_map,
        tool_outputs=[],
        resolved_policies={},
        insights=[],
        charts=charts,
        user_message="Chart disk free %",
        telemetry_as_of=ts,
    )
    assert hard2 == []
    assert any(v["code"] == "unverified_number" for v in soft2)
