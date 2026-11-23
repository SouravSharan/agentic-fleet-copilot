"""Unit tests for EvidenceLedger handle assignment, dedupe, and JSON round-trip."""

from __future__ import annotations

from datetime import UTC, datetime

from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import Evidence


def _make_ev(snap: str, metric: str, value: float = 10.0) -> Evidence:
    return Evidence(
        snapshot_id=snap,
        device_id="DEV001",
        hostname="acme-laptop-1",
        collected_at=datetime(2026, 6, 12, 10, 0, 0, tzinfo=UTC),
        metric=metric,
        value=value,
        unit="%",
        label=f"{metric}={value}",
    )


def test_evidence_ledger_assigns_sequential_handles_and_dedupes() -> None:
    ledger = EvidenceLedger()
    h1 = ledger.add(_make_ev("snap_1", "disk_free_pct", 2.0))
    h2 = ledger.add(_make_ev("snap_1", "ram_used_pct", 88.0))
    h1_dup = ledger.add(_make_ev("snap_1", "disk_free_pct", 2.0))
    h3 = ledger.add(_make_ev("snap_2", "disk_free_pct", 3.0))

    assert h1 == "E1"
    assert h2 == "E2"
    assert h1_dup == "E1"
    assert h3 == "E3"
    assert len(ledger) == 3

    resolved = ledger.resolve("E2")
    assert resolved is not None
    assert resolved.handle == "E2"
    assert resolved.metric == "ram_used_pct"
    assert ledger.resolve("E99") is None


def test_evidence_ledger_json_roundtrip_preserves_handles_and_dedupe() -> None:
    ledger = EvidenceLedger()
    ledger.add(_make_ev("snap_1", "disk_free_pct", 2.0))
    ledger.add(_make_ev("snap_2", "battery_condition", 0.0))

    serialized = ledger.to_json()
    restored = EvidenceLedger.from_json(serialized)

    assert len(restored) == 2
    assert restored.resolve("E1") == ledger.resolve("E1")
    assert restored.resolve("E2") == ledger.resolve("E2")

    # Adding an already-present (snapshot_id, metric) returns the existing handle
    assert restored.add(_make_ev("snap_1", "disk_free_pct", 2.0)) == "E1"
    # Adding a new item continues sequentially at E3
    assert restored.add(_make_ev("snap_3", "disk_free_pct", 5.0)) == "E3"
