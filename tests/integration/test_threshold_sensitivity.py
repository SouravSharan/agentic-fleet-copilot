"""Integration tests verifying threshold sensitivity bands match 05 §3.5 and 07 Step 3."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from evals import oracle
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.models import Insight, PolicyValue, TenantContext
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.trends.battery import BatteryAnalyzer
from fleet_copilot.services.trends.disk import DiskAnalyzer
from fleet_copilot.services.trends.ram import RamAnalyzer
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset

TENANTS = ("acme-001", "globex-002", "initech-003")


@pytest.fixture(scope="module")
def repo() -> Iterator[PgTelemetryRepository]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        yield PgTelemetryRepository(manager)


def _ctx(company_id: str) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id="sensitivity-test",
        role="it_admin",
        trace_id=f"trace-sens-{company_id}",
    )


def _policies(**overrides: Any) -> dict[str, PolicyValue]:
    base: dict[str, Any] = {
        "low_disk_free_pct": 15.0,
        "disk_burn_min_slope_pct_per_day": 0.1,
        "disk_trend_min_r2": 0.8,
        "high_ram_used_pct": 80.0,
        "ram_sustained_ratio": 0.7,
        "battery_eol_cycles": 800,
        "battery_capacity_drop_pct": 3.0,
    }
    base.update(overrides)
    return {k: PolicyValue(value=v, source="tenant") for k, v in base.items()}


def _run_disk_low_fleet(repo: PgTelemetryRepository, threshold: float) -> set[str]:
    analyzer = DiskAnalyzer()
    # Set slope threshold very high so only `is_low` triggers when checking low_disk_free_pct
    pols = _policies(low_disk_free_pct=threshold, disk_burn_min_slope_pct_per_day=999.0)
    hits: set[str] = set()
    for co in TENANTS:
        as_of = repo.tenant_as_of(co)
        out = analyzer.analyze(_ctx(co), repo, pols, as_of)
        co_hits = {i.device_id for i in out.insights if i.stats.get("is_low")}
        assert co_hits == oracle.low_disk(co, threshold=threshold)
        hits |= co_hits
    return hits


def test_low_disk_sensitivity_band(repo: PgTelemetryRepository) -> None:
    inside_sets = [_run_disk_low_fleet(repo, t) for t in (3.0, 10.0, 15.0, 23.0)]
    assert all(len(s) == 6 for s in inside_sets)
    assert len({frozenset(s) for s in inside_sets}) == 1

    below = _run_disk_low_fleet(repo, 2.5)
    above = _run_disk_low_fleet(repo, 24.0)
    assert len(below) < 6
    assert len(above) > 6


def test_ram_sensitivity_band(repo: PgTelemetryRepository) -> None:
    analyzer = RamAnalyzer()

    def _fleet_ram(t: float, r: float) -> set[str]:
        pols = _policies(high_ram_used_pct=t, ram_sustained_ratio=r)
        hits: set[str] = set()
        for co in TENANTS:
            as_of = repo.tenant_as_of(co)
            out = analyzer.analyze(_ctx(co), repo, pols, as_of)
            co_hits = {i.device_id for i in out.insights}
            assert co_hits == oracle.ram_sustained(co, t=t, r=r)
            hits |= co_hits
        return hits

    inside = [_fleet_ram(t, r) for t, r in ((70.0, 0.5), (75.0, 0.7), (80.0, 0.7), (80.0, 0.9))]
    assert all(len(s) == 4 for s in inside)
    assert len({frozenset(s) for s in inside}) == 1

    outside = _fleet_ram(85.0, 0.7)
    assert len(outside) == 3


def test_battery_eol_cycles_sensitivity_band(repo: PgTelemetryRepository) -> None:
    analyzer = BatteryAnalyzer()

    def _fleet_battery(cycles: int) -> set[str]:
        pols = _policies(battery_eol_cycles=cycles)
        hits: set[str] = set()
        for co in TENANTS:
            as_of = repo.tenant_as_of(co)
            out = analyzer.analyze(_ctx(co), repo, pols, as_of)
            co_hits = {i.device_id for i in out.insights}
            assert co_hits == oracle.battery_eol(co, cycles=cycles)
            hits |= co_hits
        return hits

    inside = [_fleet_battery(c) for c in (617, 800, 2000)]
    assert all(len(s) == 3 for s in inside)
    assert len({frozenset(s) for s in inside}) == 1

    outside = _fleet_battery(616)
    assert len(outside) == 4
    assert "JRZSGXVMKE6M" in outside


def test_disk_depleting_slope_sensitivity_band(repo: PgTelemetryRepository) -> None:
    analyzer = DiskAnalyzer()

    def _fleet_depleting(slope_s: float) -> set[str]:
        pols = _policies(
            low_disk_free_pct=0.0,
            disk_burn_min_slope_pct_per_day=slope_s,
            disk_trend_min_r2=0.8,
        )
        hits: set[str] = set()
        for co in TENANTS:
            as_of = repo.tenant_as_of(co)
            out = analyzer.analyze(_ctx(co), repo, pols, as_of)
            co_hits = {i.device_id for i in out.insights if i.stats.get("is_depleting")}
            assert co_hits == oracle.disk_depleting(co, slope=slope_s, r2=0.8)
            hits |= co_hits
        return hits

    inside = [_fleet_depleting(s) for s in (0.03, 0.1, 0.19)]
    assert all(len(s) == 6 for s in inside)
    assert len({frozenset(s) for s in inside}) == 1

    outside = _fleet_depleting(0.2)
    assert len(outside) == 5
    assert "MT7PJB7N5LRE" not in outside
    # Ensure Insight type check stays clean
    assert Insight is not None
