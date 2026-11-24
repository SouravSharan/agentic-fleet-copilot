"""Integration and unit tests for deterministic Fleet Briefing (`services/briefing.py`)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from evals import oracle
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.models import DeviceState, TenantContext
from fleet_copilot.interfaces.cli import app as cli_app
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.briefing import build_briefing
from fleet_copilot.services.policy import PolicyService
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset
from tests.graph.fakes import InMemoryCopilotStore, InMemoryTelemetryRepo

TENANTS = ("acme-001", "globex-002", "initech-003")


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


@pytest.mark.parametrize("company_id", TENANTS)
def test_briefing_matches_oracle_sets_writes_zero_audit_and_is_stable(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    company_id: str,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)

    ctx = TenantContext(
        company_id=company_id,
        actor_id=f"rev_01:{company_id}_admin",
        role="it_admin",
        trace_id=f"trace-brief-{company_id}",
    )

    b1 = build_briefing(ctx, repo, store, policy_svc)
    b2 = build_briefing(ctx, repo, store, policy_svc)

    # Ordering and output are completely deterministic
    assert b1.model_dump() == b2.model_dump()

    # Viewing the briefing MUST write 0 audit rows (no prechecks!)
    assert audit_svc.list(ctx) == []

    # KPIs match oracle overview; devices_needing_attention counts ONLY HIGH and MEDIUM items
    ov = oracle.overview(company_id)
    assert b1.kpis.devices == ov["total_devices"]
    assert b1.kpis.laptops == ov["by_form_factor"].get("laptop", 0)
    assert b1.kpis.open_approvals == 0
    expected_high_med = {
        did for item in b1.items if item.severity in ("high", "medium") for did in item.device_ids
    }
    assert b1.kpis.devices_needing_attention == len(expected_high_med)
    if company_id == "acme-001":
        # Acme has 10 devices total; LOW chronic os_up_to_date covers 9, HIGH/MEDIUM covers fewer
        assert b1.kpis.devices_needing_attention < b1.kpis.devices

    # Category device sets match oracle and analyzer sets exactly
    by_cat = {item.category: set(item.device_ids) for item in b1.items}
    expected_sets = {
        "battery": oracle.battery_eol(company_id),
        "ram": oracle.ram_sustained(company_id),
        "disk": oracle.disk_depleting(company_id) | oracle.low_disk(company_id),
        "compliance_drift": set(oracle.screen_lock_drift(company_id).keys()),
        "software": set(oracle.non_baseline_software(company_id).keys()),
    }
    for cat, expected in expected_sets.items():
        if expected:
            assert by_cat.get(cat) == expected, (
                f"{company_id}/{cat}: {by_cat.get(cat)} != {expected}"
            )
        else:
            assert cat not in by_cat

    # Verify required item fields and severity ordering
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    for prev, nxt in zip(b1.items, b1.items[1:], strict=False):
        assert sev_rank[prev.severity] <= sev_rank[nxt.severity]

    for item in b1.items:
        assert item.id
        assert item.title
        assert item.explanation
        assert item.device_ids
        assert len(item.hostnames) == len(item.device_ids)
        assert item.evidence_snapshot_ids
        assert item.suggested_prompt
        assert item.suggested_action_prompt is not None
        # G4: proper "an" before vowels and multi-device naming for ticket/notify <= 5 devices
        assert " a os_" not in item.suggested_action_prompt.lower()
        if item.category in ("compliance_drift", "compliance_chronic", "software", "stale_devices"):
            if len(item.hostnames) <= 5:
                for host in item.hostnames:
                    assert host in item.suggested_action_prompt


def test_briefing_viewer_hides_action_prompts_and_includes_stale_devices() -> None:
    as_of = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
    stale_dev = DeviceState(
        snapshot_id="snap-stale-brief-1",
        company_id="acme-001",
        device_id="STALEBRIEF01",
        employee_id="emp-acme-1001",
        collected_at=as_of - timedelta(days=12),
        agent_version="42.0.0",
        os_platform="darwin",
        os_product_name="macOS",
        os_product_version="15.1",
        os_hostname="acme-offline-1",
        model_name="MacBook Pro",
        model_identifier="Mac14,5",
        ram_total_gb=16.0,
        ram_used_pct=40.0,
        disk_total_gb=512.0,
        disk_free_pct=50.0,
        has_battery_block=True,
        battery_present=True,
        battery_condition="Normal",
        battery_cycle_count=100,
        first_seen_at=as_of - timedelta(days=30),
        last_seen_at=as_of - timedelta(days=12),
        snapshot_count=10,
        ever_had_battery=True,
        form_factor="laptop",
    )

    class _StaleRepo(InMemoryTelemetryRepo):
        def __init__(self) -> None:
            super().__init__()
            self._devices.append(stale_dev)

        def tenant_as_of(self, company_id: str) -> datetime:
            return as_of

    repo = _StaleRepo()
    store = InMemoryCopilotStore()
    policy_svc = PolicyService(store)

    viewer_ctx = TenantContext(
        company_id="acme-001",
        actor_id="rev_01:acme_viewer",
        role="viewer",
        trace_id="trace-brief-viewer",
    )
    briefing = build_briefing(viewer_ctx, repo, store, policy_svc)
    assert all(item.suggested_action_prompt is None for item in briefing.items)
    stale_items = [i for i in briefing.items if i.category == "stale_devices"]
    assert len(stale_items) == 1
    assert stale_items[0].device_ids == ["STALEBRIEF01"]
    assert stale_items[0].hostnames == ["acme-offline-1"]
    assert "7" in stale_items[0].explanation


def test_cli_brief_command_runs_for_persona(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    runner = CliRunner()
    res = runner.invoke(cli_app, ["brief", "--persona", "acme_admin"])
    assert res.exit_code == 0, res.output
    assert "Acme" in res.output
    assert "Devices" in res.output or "devices" in res.output.lower()
