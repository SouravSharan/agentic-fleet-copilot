"""Two-clock utilities separating wall-clock time (`Clock`) from data time (`telemetry_as_of`).

- Wall clock (`Clock`): used for proposal expiry, cooldowns, audit timestamps, sessions,
  rate limits, and lag banners. Injected everywhere; never call `datetime.now()` directly
  outside this module.
- Data clock (`telemetry_as_of`): `max(collected_at)` within the tenant's RLS scope (or
  explicit `FLEET_AS_OF` override), used for 30-day windows, staleness, and insights.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fleet_copilot.config import Settings, get_settings


class NoTelemetryError(RuntimeError):
    """Raised when no telemetry snapshots exist for the current tenant."""


class Clock(Protocol):
    """Protocol for an injectable UTC wall clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC timestamp."""
        ...


class SystemClock:
    """Production wall clock returning `datetime.now(UTC)`."""

    def now(self) -> datetime:
        """Return current UTC time."""
        return datetime.now(UTC)


class FixedClock:
    """Deterministic wall clock for tests and offline evaluation."""

    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._current = current.astimezone(UTC)

    def now(self) -> datetime:
        """Return the pinned UTC timestamp."""
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        """Advance the pinned clock by `delta` and return the new UTC timestamp."""
        self._current = self._current + delta
        return self._current


class _CursorLike(Protocol):
    def execute(self, query: Any, params: Any = None) -> Any: ...
    def fetchone(self) -> Any: ...


def telemetry_as_of(cur: _CursorLike, settings: Settings | None = None) -> datetime:
    """Return the tenant's data-clock timestamp (UTC).

    Uses `FLEET_AS_OF` if configured (must be timezone-aware, normalized to UTC);
    otherwise queries `max(collected_at)` from `telemetry.v_snapshots_deduped`
    through the caller's tenant-locked cursor.
    """
    cfg = settings if settings is not None else get_settings()
    if cfg.fleet_as_of is not None:
        override = cfg.fleet_as_of
        if override.tzinfo is None or override.utcoffset() is None:
            raise ValueError("FLEET_AS_OF must be a timezone-aware UTC timestamp")
        return override.astimezone(UTC)

    cur.execute("SELECT MAX(collected_at) AS as_of FROM telemetry.v_snapshots_deduped")
    row = cur.fetchone()
    if row is None:
        raise NoTelemetryError("No telemetry snapshots available for tenant")

    value = row["as_of"] if isinstance(row, dict) else row[0]
    if value is None:
        raise NoTelemetryError("No telemetry snapshots available for tenant")
    if not isinstance(value, datetime):
        raise TypeError(f"Expected datetime for telemetry_as_of, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
