"""Verify every eval YAML case parses cleanly and every literal set/count equals evals/oracle.py."""

from __future__ import annotations

from typing import Any

import pytest

from evals import oracle
from evals.schema import EvalCase, ExpectSpec, load_cases


def _oracle_to_device_set(result: Any) -> set[str]:
    if isinstance(result, set):
        return {str(x) for x in result}
    if isinstance(result, dict):
        return {str(k) for k in result}
    raise TypeError(f"Expected set or dict from oracle for device_set, got {type(result).__name__}")


def _oracle_to_count(result: Any) -> int:
    if isinstance(result, int):
        return result
    if isinstance(result, (set, list, tuple, dict)):
        return len(result)
    raise TypeError(f"Expected int or sized collection for count, got {type(result).__name__}")


def test_eval_suite_shape_and_unique_ids() -> None:
    cases = load_cases()
    assert len(cases) >= 84
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    # Verify all buckets are represented
    tags_union = {t for c in cases for t in c.tags}
    assert {"qa", "insights", "actions", "multiturn", "redteam", "charts", "sql"}.issubset(
        tags_union
    )
    # Every case must be tagged strictly `core` OR `extended`
    core_cases = [c for c in cases if "core" in c.tags]
    ext_cases = [c for c in cases if "extended" in c.tags]
    assert 48 <= len(core_cases) <= 52, f"Expected ~49-50 core cases, got {len(core_cases)}"
    assert len(core_cases) + len(ext_cases) == len(cases)
    assert not ({c.id for c in core_cases} & {c.id for c in ext_cases})


def test_literal_device_set_without_oracle_ref_is_rejected() -> None:
    with pytest.raises(ValueError, match="oracle_ref"):
        ExpectSpec(device_set=["MT7PJB7N5LRE"])


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_case_literal_expectations_match_oracle(case: EvalCase) -> None:
    exp = case.expect
    if exp.oracle_ref is None:
        assert exp.device_set is None
        assert exp.expected_group_values is None
        return

    actual = oracle.resolve_oracle_ref(exp.oracle_ref.fn, exp.oracle_ref.args)
    if exp.device_set is not None:
        expected_set = _oracle_to_device_set(actual)
        assert set(exp.device_set) == expected_set, (
            f"Case {case.id}: YAML device_set={sorted(exp.device_set)} "
            f"!= oracle {exp.oracle_ref.fn}({exp.oracle_ref.args})={sorted(expected_set)}"
        )
    if exp.count is not None:
        expected_count = _oracle_to_count(actual)
        assert exp.count == expected_count, (
            f"Case {case.id}: YAML count={exp.count} "
            f"!= oracle {exp.oracle_ref.fn}({exp.oracle_ref.args})={expected_count}"
        )
    if exp.expected_group_values is not None:
        assert isinstance(actual, dict)
        expected_map = {str(k): round(float(v), 2) for k, v in actual.items()}
        got_map = {str(k): round(float(v), 2) for k, v in exp.expected_group_values.items()}
        assert got_map == expected_map, (
            f"Case {case.id}: YAML expected_group_values={got_map} != oracle {expected_map}"
        )
