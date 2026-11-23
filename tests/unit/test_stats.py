"""Unit tests for pure statistical helpers (OLS, shares, percentiles, projections)."""

from __future__ import annotations

import pytest

from fleet_copilot.domain.stats import days_until, ols, percentile, share_at_or_above


def test_ols_perfect_line_and_noise() -> None:
    fit = ols([0.0, 1.0, 2.0, 3.0, 4.0], [20.0, 18.0, 16.0, 14.0, 12.0])
    assert fit is not None
    assert fit.slope == pytest.approx(-2.0, abs=1e-9)
    assert fit.intercept == pytest.approx(20.0, abs=1e-9)
    assert fit.r2 == pytest.approx(1.0, abs=1e-9)

    noisy = ols([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], [50.0, 54.0, 49.0, 53.0, 48.0, 52.0])
    assert noisy is not None
    assert noisy.r2 < 0.3

    # Constant y -> slope 0, r2 0 (not falsely treated as a strong trend)
    flat = ols([0.0, 1.0, 2.0, 3.0], [10.0, 10.0, 10.0, 10.0])
    assert flat is not None
    assert flat.slope == pytest.approx(0.0, abs=1e-9)
    assert flat.r2 == pytest.approx(0.0, abs=1e-9)


def test_ols_requires_three_points_and_distinct_x() -> None:
    assert ols([], []) is None
    assert ols([0.0], [1.0]) is None
    assert ols([0.0, 1.0], [1.0, 2.0]) is None
    assert ols([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert ols([0.0, 1.0, 2.0], [1.0, 2.0]) is None


def test_share_and_percentile() -> None:
    assert share_at_or_above([], 80.0) == 0.0
    assert share_at_or_above([79.9, 80.0, 85.0, 90.0], 80.0) == pytest.approx(0.75)
    assert share_at_or_above([10.0, 20.0], 80.0) == 0.0

    assert percentile([], 50.0) is None
    assert percentile([10.0, 20.0, 30.0, 40.0, 50.0], 50.0) == pytest.approx(30.0)
    assert percentile([10.0, 20.0, 30.0, 40.0, 50.0], 95.0) == pytest.approx(48.0)

    with pytest.raises(ValueError):
        percentile([1.0, 2.0], 101.0)


def test_days_until() -> None:
    # Moving downward toward target
    assert days_until(current=20.0, slope=-0.5, target=15.0) == pytest.approx(10.0)
    assert days_until(current=2.0, slope=-0.5, target=0.0) == pytest.approx(4.0)

    # Moving upward toward target
    assert days_until(current=700.0, slope=5.0, target=800.0) == pytest.approx(20.0)

    # Moving away from target or already past target
    assert days_until(current=2.0, slope=-0.5, target=15.0) is None
    assert days_until(current=20.0, slope=0.5, target=15.0) is None
    assert days_until(current=20.0, slope=0.0, target=15.0) is None
    assert days_until(current=15.0, slope=-0.5, target=15.0) == pytest.approx(0.0)
