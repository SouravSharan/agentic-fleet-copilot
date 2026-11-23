"""Pure statistical helpers (OLS linear fit, shares, percentiles, threshold projections)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict


class Ols(BaseModel):
    """Ordinary least-squares linear regression result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slope: float
    intercept: float
    r2: float


def ols(xs: Sequence[float], ys: Sequence[float]) -> Ols | None:
    """Fit `y = slope * x + intercept` via OLS; requires >= 3 points with non-zero x variance."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None

    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)

    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    dx = x - x_mean
    ss_xx = float(np.dot(dx, dx))
    if ss_xx <= 0.0:
        return None

    dy = y - y_mean
    slope = float(np.dot(dx, dy) / ss_xx)
    intercept = float(y_mean - slope * x_mean)

    ss_tot = float(np.dot(dy, dy))
    if ss_tot <= 0.0:
        r2 = 0.0
    else:
        y_pred = slope * x + intercept
        residuals = y - y_pred
        ss_res = float(np.dot(residuals, residuals))
        r2 = max(0.0, min(1.0, 1.0 - (ss_res / ss_tot)))

    return Ols(slope=slope, intercept=intercept, r2=r2)


def share_at_or_above(values: Sequence[float], threshold: float) -> float:
    """Return the fraction of `values` that are >= `threshold` (0.0 when empty)."""
    if not values:
        return 0.0
    hits = sum(1 for v in values if v >= threshold)
    return hits / len(values)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Return the `p`-th percentile (`0 <= p <= 100`) of `values`, or `None` when empty."""
    if p < 0.0 or p > 100.0:
        raise ValueError(f"Percentile must be between 0 and 100, got {p}")
    if not values:
        return None
    arr = np.asarray(values, dtype=np.float64)
    return float(np.percentile(arr, p))


def days_until(current: float, slope: float, target: float) -> float | None:
    """Estimate days until a linear trajectory from `current` at `slope`/day hits `target`.

    Returns `None` when `slope == 0` or the trajectory moves away from `target`.
    Returns `0.0` when `current == target` and `slope != 0`.
    """
    if slope == 0.0:
        return None
    delta = target - current
    if delta == 0.0:
        return 0.0
    days = delta / slope
    if days <= 0.0:
        return None
    return float(days)
