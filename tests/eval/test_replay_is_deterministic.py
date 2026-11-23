"""Verify two offline replay eval runs produce identical JSON apart from volatile metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.run import run_eval_suite
from fleet_copilot.agent.llm import cassette_path_for
from fleet_copilot.config import get_settings


def _strip_volatile(report: dict[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(report))
    cleaned["metadata"].pop("generated_at", None)
    cleaned["summary"].pop("mean_latency_ms", None)
    for c in cleaned.get("cases", []):
        c.pop("mean_latency_ms", None)
    for r in cleaned.get("runs", []):
        r.pop("latency_ms", None)
    return cleaned


@pytest.mark.integration
def test_replay_is_deterministic(tmp_path: Path) -> None:
    settings = get_settings()
    model = settings.llm_model
    cassette_dir = Path(settings.llm_cassette_dir)

    sample_cases = ["qa-acme-low-disk", "rt-cross-tenant-acme-from-globex"]
    missing = [
        cid
        for cid in sample_cases
        if not cassette_path_for(
            cassette_dir=cassette_dir, model_spec=model, case_or_session=cid
        ).exists()
    ]
    if missing:
        pytest.skip(f"Committed cassettes not yet recorded for {missing}")

    run1 = run_eval_suite(
        mode="replay",
        model=model,
        repeats=1,
        case_ids=sample_cases,
        out_prefix=tmp_path / "run1",
        cassette_dir=cassette_dir,
    )
    run2 = run_eval_suite(
        mode="replay",
        model=model,
        repeats=1,
        case_ids=sample_cases,
        out_prefix=tmp_path / "run2",
        cassette_dir=cassette_dir,
    )

    assert _strip_volatile(run1) == _strip_volatile(run2)
    assert run1["gates"]["passed"] is True
