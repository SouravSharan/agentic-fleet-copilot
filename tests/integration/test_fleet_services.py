"""Integration tests for Step 2 read services against Postgres test DB and evals/oracle.py."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from evals import oracle
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.models import ComplianceQuery, DeviceFilters, TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.fleet import (
    find_devices,
    get_device_details,
    get_employee_devices,
    get_fleet_overview,
    query_compliance,
)
from fleet_copilot.services.policy import PolicyService
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset

TENANTS = ("acme-001", "globex-002", "initech-003")
DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "device-telemetry-dataset.ndjson"

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")


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
def repos(db_manager: DatabaseManager) -> tuple[PgTelemetryRepository, PolicyService]:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    return repo, PolicyService(store)


def _ctx(company_id: str) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=f"{company_id}_admin",
        role="it_admin",
        trace_id=f"trace-{company_id}",
    )


@pytest.mark.parametrize("company_id", TENANTS)
def test_low_disk_matches_oracle(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    res = find_devices(_ctx(company_id), DeviceFilters(low_disk_only=True), repo, policy_svc)

    assert res.ok is True
    returned = {row["device_id"] for row in res.data}
    assert returned == oracle.low_disk(company_id)
    assert res.coverage is not None
    assert res.coverage.telemetry_as_of == oracle.as_of(company_id)
    assert res.coverage.matched == len(returned)
    assert "low_disk_free_pct" in res.policy_applied
    assert len(res.evidence) == len(returned)
    assert all(ev.handle is None for ev in res.evidence)


@pytest.mark.parametrize("company_id", TENANTS)
def test_high_severity_laptop_failures_zero_with_pass_evidence(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    res = query_compliance(
        _ctx(company_id),
        ComplianceQuery(severity="high", status="fail", form_factor="laptop", scope="latest"),
        repo,
        policy_svc,
    )

    assert res.ok is True
    assert res.data == []
    expected_laptops = oracle.overview(company_id)["by_form_factor"]["laptop"]
    assert res.coverage is not None
    assert res.coverage.evaluated == expected_laptops
    assert res.coverage.matched == 0
    assert len(res.evidence) == expected_laptops
    assert all(ev.value == "pass" for ev in res.evidence)
    joined_notes = " ".join(res.coverage.notes)
    assert "disk_encryption" in joined_notes
    assert "high" in joined_notes
    assert "screen_lock" in joined_notes


@pytest.mark.parametrize("company_id", TENANTS)
def test_macos_older_than_15_matches_oracle_and_notes_windows_excluded(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    res = find_devices(
        _ctx(company_id),
        DeviceFilters(os_family="macos", os_version_lt="15"),
        repo,
        policy_svc,
    )

    assert res.ok is True
    returned = {row["device_id"] for row in res.data}
    assert returned == oracle.macos_older_than(company_id, 15)
    assert res.coverage is not None
    assert any("Windows" in note for note in res.coverage.notes)

    # os_version_lt without os_family returns actionable error
    bad = find_devices(_ctx(company_id), DeviceFilters(os_version_lt="15"), repo, policy_svc)
    assert bad.ok is False
    assert bad.error is not None


@pytest.mark.parametrize("company_id", TENANTS)
def test_resolve_by_device_id_hostname_serial(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    latest = oracle.latest_by_device(company_id)
    sample_id = sorted(latest.keys())[0]
    sample_row = latest[sample_id]
    hostname = sample_row["os"]["hostname"]
    serial = sample_row["device_identity"]["serial_number"]

    for identifier in (sample_id, hostname, hostname.upper(), serial):
        res = get_device_details(_ctx(company_id), identifier, repo, policy_svc)
        assert res.ok is True
        assert res.data[0]["device_id"] == sample_id


def test_cross_tenant_identifier_returns_not_found(
    repos: tuple[PgTelemetryRepository, PolicyService],
) -> None:
    repo, policy_svc = repos
    cross_tenant = get_device_details(_ctx("acme-001"), "E4V0SB5I0MYU", repo, policy_svc)
    nonexistent = get_device_details(_ctx("acme-001"), "DOESNOTEXIST99", repo, policy_svc)

    assert cross_tenant.ok is False
    assert cross_tenant.error == "not_found"
    assert nonexistent.ok is False
    assert nonexistent.error == "not_found"
    # Identical message prevents existence oracle
    assert cross_tenant.summary == nonexistent.summary


@pytest.mark.parametrize("company_id", TENANTS)
def test_employee_devices_match_oracle(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    latest = oracle.latest_by_device(company_id)
    employees = sorted({row["employee_id"] for row in latest.values()})
    for emp_id in employees:
        res = get_employee_devices(_ctx(company_id), emp_id, repo, policy_svc)
        assert res.ok is True
        assert {d["device_id"] for d in res.data} == oracle.employee_devices(company_id, emp_id)


@pytest.mark.parametrize("company_id", TENANTS)
def test_overview_counts_match_oracle(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    res = get_fleet_overview(_ctx(company_id), repo, policy_svc)

    assert res.ok is True
    assert res.coverage is not None
    assert res.coverage.telemetry_as_of == oracle.as_of(company_id)
    ov = res.data[0]
    expected = oracle.overview(company_id)
    assert ov["total_devices"] == expected["total_devices"]
    assert ov["by_form_factor"] == expected["by_form_factor"]
    assert ov["by_os"] == expected["by_os"]
    assert ov["by_model"] == expected["by_model"]
    assert ov["compliance"] == expected["compliance"]


@pytest.mark.parametrize("company_id", TENANTS)
def test_results_never_include_network_or_hardware_identifiers(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    raw_rows = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()]
    all_hw_uuids = {
        r["device_identity"]["hardware_uuid"]
        for r in raw_rows
        if isinstance(r.get("device_identity"), dict) and r["device_identity"].get("hardware_uuid")
    }

    ctx = _ctx(company_id)
    latest = oracle.latest_by_device(company_id)
    first_dev = sorted(latest.keys())[0]
    first_emp = latest[first_dev]["employee_id"]

    results = [
        get_fleet_overview(ctx, repo, policy_svc),
        find_devices(ctx, DeviceFilters(), repo, policy_svc),
        query_compliance(ctx, ComplianceQuery(), repo, policy_svc),
        get_device_details(ctx, first_dev, repo, policy_svc),
        get_employee_devices(ctx, first_emp, repo, policy_svc),
    ]

    for res in results:
        payload = res.model_dump_json()
        assert not _IPV4_RE.search(payload), f"IPv4 leaked in {res.tool_name}"
        assert not _MAC_RE.search(payload), f"MAC leaked in {res.tool_name}"
        assert "serial_number" not in payload, f"serial_number key leaked in {res.tool_name}"
        assert "hardware_uuid" not in payload, f"hardware_uuid key leaked in {res.tool_name}"
        for uuid_val in all_hw_uuids:
            assert uuid_val not in payload, f"hardware_uuid value leaked in {res.tool_name}"


def test_policy_applied_reports_source(
    db_manager: DatabaseManager,
    repos: tuple[PgTelemetryRepository, PolicyService],
) -> None:
    repo, policy_svc = repos
    res_global = find_devices(_ctx("acme-001"), DeviceFilters(low_disk_only=True), repo, policy_svc)
    assert res_global.policy_applied["low_disk_free_pct"].source == "global"
    assert res_global.policy_applied["low_disk_free_pct"].value == 15.0

    # Insert tenant override and verify source switches to "tenant"
    try:
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                INSERT INTO copilot.tenant_policies (
                    company_id, scope_type, scope_value, metric_key, value_json, updated_by
                )
                VALUES ('acme-001', 'tenant', '', 'low_disk_free_pct', '10.0'::jsonb, 'test')
                ON CONFLICT (company_id, scope_type, scope_value, metric_key)
                DO UPDATE SET value_json = EXCLUDED.value_json
                """
            )
        res_tenant = find_devices(
            _ctx("acme-001"), DeviceFilters(low_disk_only=True), repo, policy_svc
        )
        assert res_tenant.policy_applied["low_disk_free_pct"].source == "tenant"
        assert res_tenant.policy_applied["low_disk_free_pct"].value == 10.0
    finally:
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                """
                DELETE FROM copilot.tenant_policies
                WHERE company_id = 'acme-001' AND metric_key = 'low_disk_free_pct'
                """
            )


@pytest.mark.parametrize("company_id", TENANTS)
def test_output_ordering_is_deterministic(
    repos: tuple[PgTelemetryRepository, PolicyService],
    company_id: str,
) -> None:
    repo, policy_svc = repos
    ctx = _ctx(company_id)
    r1 = find_devices(ctx, DeviceFilters(), repo, policy_svc)
    r2 = find_devices(ctx, DeviceFilters(), repo, policy_svc)
    assert r1.model_dump_json() == r2.model_dump_json()
    device_ids = [d["device_id"] for d in r1.data]
    assert device_ids == sorted(device_ids)

    c1 = query_compliance(ctx, ComplianceQuery(scope="window"), repo, policy_svc)
    c2 = query_compliance(ctx, ComplianceQuery(scope="window"), repo, policy_svc)
    assert c1.model_dump_json() == c2.model_dump_json()
    ev_keys = [(ev.device_id, ev.collected_at) for ev in c1.evidence]
    assert ev_keys == sorted(ev_keys)
