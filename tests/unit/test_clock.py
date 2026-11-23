"""Unit tests for the two-clock abstraction (wall clock and data clock override)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from fleet_copilot.clock import FixedClock, NoTelemetryError, SystemClock, telemetry_as_of
from fleet_copilot.config import Settings


class _FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.executed = False

    def execute(self, _sql: str, _params: Any = None) -> None:
        self.executed = True

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


def test_fixed_clock_advance() -> None:
    start = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
    clock = FixedClock(start)
    assert clock.now() == start

    clock.advance(timedelta(hours=72, minutes=5))
    assert clock.now() == datetime(2026, 11, 26, 12, 5, 0, tzinfo=UTC)

    # Reject naive datetimes
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 11, 23, 12, 0, 0))

    # SystemClock returns UTC-aware datetime
    sys_now = SystemClock().now()
    assert sys_now.tzinfo is not None
    assert sys_now.utcoffset() == timedelta(0)


def test_fleet_as_of_override_requires_utc() -> None:
    # Naive FLEET_AS_OF must raise ValueError
    naive_settings = Settings(fleet_as_of=datetime(2026, 6, 12, 11, 0, 0))
    cur = _FakeCursor({"as_of": datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)})
    with pytest.raises(ValueError, match="timezone-aware"):
        telemetry_as_of(cur, naive_settings)
    assert cur.executed is False

    # UTC override returns without querying DB
    utc_dt = datetime(2026, 6, 10, 9, 30, 0, tzinfo=UTC)
    utc_settings = Settings(fleet_as_of=utc_dt)
    assert telemetry_as_of(cur, utc_settings) == utc_dt
    assert cur.executed is False

    # Offset-aware override is normalized to UTC without querying DB
    plus2 = timezone(timedelta(hours=2))
    offset_settings = Settings(fleet_as_of=datetime(2026, 6, 10, 11, 30, 0, tzinfo=plus2))
    result = telemetry_as_of(cur, offset_settings)
    assert result == utc_dt
    assert result.tzinfo == UTC
    assert cur.executed is False

    # Empty tenant raises NoTelemetryError when no override is configured
    empty_cur = _FakeCursor({"as_of": None})
    no_override_settings = Settings(fleet_as_of=None)
    with pytest.raises(NoTelemetryError):
        telemetry_as_of(empty_cur, no_override_settings)
    assert empty_cur.executed is True
