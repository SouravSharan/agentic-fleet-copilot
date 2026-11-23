"""Verify committed telemetry dataset integrity and expected shape."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = REPO_ROOT / "data" / "device-telemetry-dataset.ndjson"
EXPECTED_SHA256 = "fb0ee31ffeeb731b919fc4d5124098c9c5b68cf3fba35eed5092a9f73655c4de"


def test_dataset_sha256_and_shape() -> None:
    assert DATASET_PATH.exists(), f"Missing dataset at {DATASET_PATH}"
    raw_bytes = DATASET_PATH.read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    assert actual_sha == EXPECTED_SHA256

    lines = [line for line in raw_bytes.decode("utf-8").splitlines() if line.strip()]
    assert len(lines) == 750

    records = [json.loads(line) for line in lines]
    companies = {r["company_id"] for r in records}
    devices = {r["device_id"] for r in records}
    assert companies == {"acme-001", "globex-002", "initech-003"}
    assert len(devices) == 25
