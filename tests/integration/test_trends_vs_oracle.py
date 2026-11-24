"""Integration tests verifying 30-day trend analyzers match evals/oracle.py across all tenants."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from evals import oracle
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.models import Insight, TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset

TENANTS = ("acme-001", "globex-002", "initech-003")
CATEGORIES = ("battery", "ram", "disk", "compliance_drift", "software")


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


@pytest.fixture(scope="module")
def deps(db_manager: DatabaseManager) -> tuple[PgTelemetryRepository, PolicyService]:
    return PgTelemetryRepository(db_manager), PolicyService(PgCopilotStore(db_manager))


def _ctx(company_id: str) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=f"{company_id}_admin",
        role="it_admin",
        trace_id=f"trace-trends-{company_id}",
    )


@pytest.mark.parametrize("company_id", TENANTS)
@pytest.mark.parametrize("category", CATEGORIES)
def test_trends_match_oracle_per_tenant_and_category(
    deps: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
    category: str,
) -> None:
    repo, policy_svc = deps
    res = analyze_fleet_trends(_ctx(company_id), [category], repo, policy_svc)
    assert res.ok is True

    insights = [Insight.model_validate(row) for row in res.data]
    matched_devices = {ins.device_id for ins in insights if ins.category == category}

    if category == "battery":
        assert matched_devices == oracle.battery_eol(company_id)
    elif category == "ram":
        assert matched_devices == oracle.ram_sustained(company_id)
    elif category == "disk":
        assert matched_devices == (oracle.low_disk(company_id) | oracle.disk_depleting(company_id))
    elif category == "compliance_drift":
        expected_drift = oracle.screen_lock_drift(company_id)
        assert matched_devices == set(expected_drift.keys())
        for ins in insights:
            if ins.category == "compliance_drift":
                assert ins.stats["first_fail_date"] == expected_drift[ins.device_id]
        assert any(a["type"] == "daily_compliance_pass_rate" for a in res.artifacts)
    elif category == "software":
        expected_sw = oracle.non_baseline_software(company_id)
        assert matched_devices == set(expected_sw.keys())
        for ins in insights:
            if ins.category == "software":
                app_name = ins.stats["software_name"]
                assert ins.stats["first_seen_date"] == expected_sw[ins.device_id][app_name]


def test_no_false_positives_on_healthy_controls(
    deps: tuple[PgTelemetryRepository, PolicyService],
) -> None:
    repo, policy_svc = deps
    controls_by_tenant = {
        "acme-001": {"XSU9UUDGU6AC", "Q3VUITEGQ400"},
        "globex-002": {"DXUCJ272DCYX", "QPZSXKINA7GP"},
    }
    checked_categories = {"battery", "ram", "disk", "compliance_drift"}

    for company_id, healthy_ids in controls_by_tenant.items():
        res = analyze_fleet_trends(_ctx(company_id), ["all"], repo, policy_svc)
        assert res.ok is True
        insights = [Insight.model_validate(row) for row in res.data]
        flagged = {
            (ins.device_id, ins.category)
            for ins in insights
            if ins.device_id in healthy_ids and ins.category in checked_categories
        }
        assert flagged == set(), f"False positives on healthy controls: {flagged}"


@pytest.mark.parametrize("company_id", TENANTS)
def test_insight_shape(
    deps: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = deps
    res = analyze_fleet_trends(_ctx(company_id), ["all"], repo, policy_svc)
    assert res.ok is True
    insights = [Insight.model_validate(row) for row in res.data]
    assert len(insights) > 0

    all_snap_ids = [ev.snapshot_id for ins in insights for ev in ins.evidence]
    existing_ids = repo.existing_snapshot_ids(company_id, all_snap_ids)

    for ins in insights:
        assert ins.finding.strip()
        assert ins.explanation.strip()
        assert len(ins.evidence) >= 1
        for ev in ins.evidence:
            assert ev.snapshot_id in existing_ids
            assert ev.device_id == ins.device_id

        # Check first / transition / latest markers where defined
        labels_lower = " ".join(ev.label.lower() for ev in ins.evidence)
        if ins.category == "battery":
            assert "first" in labels_lower
            assert "latest" in labels_lower
            if ins.stats.get("transition_date") is not None:
                assert "transition" in labels_lower
        elif ins.category == "disk":
            assert "first" in labels_lower
            assert "latest" in labels_lower
            if ins.stats.get("flattened_near_floor"):
                assert "flattened" in ins.explanation.lower()
        elif ins.category == "compliance_drift":
            assert "first" in labels_lower
            assert "transition" in labels_lower
            assert "latest" in labels_lower
