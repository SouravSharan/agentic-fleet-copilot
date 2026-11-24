"""Verify independent evals/oracle.py matches dataset ground-truth metrics and anomalies."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

from evals import oracle

ORACLE_PATH = Path(__file__).resolve().parents[2] / "evals" / "oracle.py"


def test_oracle_does_not_import_fleet_copilot() -> None:
    tree = ast.parse(ORACLE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("fleet_copilot")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("fleet_copilot")


def test_oracle_as_of_and_laptop_counts() -> None:
    assert oracle.as_of("acme-001") == datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)
    assert oracle.as_of("globex-002") == datetime(2026, 6, 12, 10, 47, 0, tzinfo=UTC)
    assert oracle.as_of("initech-003") == datetime(2026, 6, 12, 10, 57, 0, tzinfo=UTC)

    assert oracle.overview("acme-001")["by_form_factor"]["laptop"] == 9
    assert oracle.overview("globex-002")["by_form_factor"]["laptop"] == 8
    assert oracle.overview("initech-003")["by_form_factor"]["laptop"] == 5


def test_oracle_low_disk_and_high_severity_and_macos() -> None:
    assert oracle.low_disk("acme-001") == {"MT7PJB7N5LRE", "8NM23J95R5I6"}
    assert oracle.low_disk("globex-002") == {"JRZSGXVMKE6M", "E4V0SB5I0MYU"}
    assert oracle.low_disk("initech-003") == {"RLD7WV7J82SH", "DARFCP0BQM2G"}

    for company in ("acme-001", "globex-002", "initech-003"):
        assert oracle.laptops_failing(company, "high") == set()

    assert oracle.macos_older_than("acme-001", 15) == {
        "1LYSSFD074BB",
        "7VP16KHV88LM",
        "MT7PJB7N5LRE",
        "XSU9UUDGU6AC",
    }
    assert oracle.macos_older_than("globex-002", 15) == {"4SEFDLPTKXAM", "QPZSXKINA7GP"}
    assert oracle.macos_older_than("initech-003", 15) == {
        "DARFCP0BQM2G",
        "RLD7WV7J82SH",
        "SPKRRDF0L786",
        "XZYK11QTPWEC",
    }


def test_oracle_planted_trends_match_ground_truth() -> None:
    all_battery = (
        oracle.battery_eol("acme-001")
        | oracle.battery_eol("globex-002")
        | oracle.battery_eol("initech-003")
    )
    assert all_battery == {"7VP16KHV88LM", "8NM23J95R5I6", "M4XVHUV1MEPZ"}

    all_ram = (
        oracle.ram_sustained("acme-001")
        | oracle.ram_sustained("globex-002")
        | oracle.ram_sustained("initech-003")
    )
    assert all_ram == {"M4XVHUV1MEPZ", "I3JLKILKQTAC", "JRZSGXVMKE6M", "CUXW0BQSV1B7"}

    all_depleting = (
        oracle.disk_depleting("acme-001")
        | oracle.disk_depleting("globex-002")
        | oracle.disk_depleting("initech-003")
    )
    assert all_depleting == {
        "8NM23J95R5I6",
        "MT7PJB7N5LRE",
        "E4V0SB5I0MYU",
        "JRZSGXVMKE6M",
        "DARFCP0BQM2G",
        "RLD7WV7J82SH",
    }

    assert oracle.screen_lock_drift("acme-001") == {
        "1LYSSFD074BB": "2026-05-22",
        "8NM23J95R5I6": "2026-05-30",
        "DLT35MHGTH2D": "2026-06-01",
        "DSYPSES7SKBB": "2026-06-01",
        "MT7PJB7N5LRE": "2026-05-28",
    }
    assert oracle.screen_lock_drift("globex-002") == {
        "CUXW0BQSV1B7": "2026-06-05",
        "JRZSGXVMKE6M": "2026-05-30",
    }
    assert oracle.screen_lock_drift("initech-003") == {
        "2MAN6T4DJD95": "2026-06-05",
        "SPKRRDF0L786": "2026-05-26",
        "XZYK11QTPWEC": "2026-06-01",
    }

    assert oracle.non_baseline_software("acme-001") == {
        "1LYSSFD074BB": {"uTorrent": "2026-05-31"},
        "7VP16KHV88LM": {"CleanMyMac X": "2026-05-25"},
    }
    assert oracle.non_baseline_software("globex-002") == {
        "Q1AFVLH3YV33": {"uTorrent": "2026-05-20"},
    }
    assert oracle.non_baseline_software("initech-003") == {
        "DARFCP0BQM2G": {"TeamViewer": "2026-05-21"},
        "I3JLKILKQTAC": {"uTorrent": "2026-05-29"},
        "W2656QDAE2ZJ": {"uTorrent": "2026-06-02"},
    }
