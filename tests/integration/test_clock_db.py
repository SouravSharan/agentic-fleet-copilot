"""Integration tests for tenant-scoped telemetry_as_of() against PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from fleet_copilot.clock import telemetry_as_of
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db import DatabaseManager
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        yield manager


@pytest.mark.parametrize(
    ("company_id", "expected"),
    [
        ("acme-001", datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)),
        ("globex-002", datetime(2026, 6, 12, 10, 47, 0, tzinfo=UTC)),
        ("initech-003", datetime(2026, 6, 12, 10, 57, 0, tzinfo=UTC)),
    ],
)
def test_telemetry_as_of_per_tenant(
    db_manager: DatabaseManager,
    company_id: str,
    expected: datetime,
) -> None:
    """Data clock per tenant equals max(collected_at) within that tenant's RLS scope."""
    settings = Settings(fleet_as_of=None)
    with db_manager.telemetry_ro_cursor(company_id) as cur:
        as_of = telemetry_as_of(cur, settings)
    assert as_of == expected
    assert as_of.tzinfo == UTC
