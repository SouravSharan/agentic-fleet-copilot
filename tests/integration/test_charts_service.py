"""Integration tests for B2: ChartService, personal RLS charts, refresh in place, insight charts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from evals import oracle
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.charts import ChartRequest, ChartService, validate_vega_lite_spec
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from scripts.db_setup import run_setup
from scripts.telemetry_sim import ingest_snapshot, reset_dataset


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)
    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=2,
        max_size=6,
    ) as manager:
        yield manager


def _ctx(
    company_id: str = "acme-001",
    actor_id: str = "rev_01:acme_admin",
    role: str = "it_admin",
) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role=role,  # type: ignore[arg-type]
        trace_id="trace-b2",
    )


@pytest.mark.integration
def test_create_chart_metric_over_time_matches_oracle_low_disk(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    audit = AuditService(db_manager)
    svc = ChartService(repo, PolicyService(store), store=store, audit_svc=audit)

    ctx = _ctx("acme-001", "rev_01:acme_viewer", role="viewer")
    req = ChartRequest(
        title="Acme Low-Disk Devices (30d)",
        kind="metric_over_time",
        metric="disk_free_pct",
        low_disk_only=True,
        window_days=30,
        chart_type="line",
    )
    art = svc.create(ctx, req)
    validate_vega_lite_spec(art.vega_lite_spec)
    assert art.row_count > 0
    assert len(art.evidence_snapshot_ids) > 0

    chart_devices = {str(r["device_id"]) for r in art.data_rows}
    assert chart_devices == oracle.low_disk("acme-001")


@pytest.mark.integration
def test_create_chart_aggregate_by_group_matches_oracle_avg_cycles(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    svc = ChartService(repo, PolicyService(store), store=store, audit_svc=AuditService(db_manager))

    ctx = _ctx("globex-002", "rev_01:globex_admin")
    req = ChartRequest(
        title="Globex Average Battery Cycles by Model",
        kind="aggregate_by_group",
        metric="battery_cycles",
        stat="avg",
        group_by="model",
        chart_type="bar",
    )
    art = svc.create(ctx, req)
    validate_vega_lite_spec(art.vega_lite_spec)

    # Compute independent expected avg cycles per model from oracle latest snapshots
    latest = oracle.latest_by_device("globex-002")
    by_model: dict[str, list[int]] = {}
    for snap in latest.values():
        bat = snap.get("battery") or {}
        if bat.get("battery_present") and bat.get("cycle_count") is not None:
            model = str((snap.get("device_identity") or {}).get("model_name") or "unknown")
            by_model.setdefault(model, []).append(int(bat["cycle_count"]))
    expected = {m: round(sum(vals) / len(vals), 2) for m, vals in by_model.items()}
    got = {str(r["group"]): round(float(r["value"]), 2) for r in art.data_rows}
    assert got == expected


@pytest.mark.integration
def test_saved_charts_personal_rls_pin_refresh_in_place_and_delete(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
    restore_telemetry_baseline: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    audit = AuditService(db_manager)
    svc = ChartService(repo, PolicyService(store), store=store, audit_svc=audit)

    ctx_a = _ctx("acme-001", "rev_01:acme_admin")
    ctx_b = _ctx("acme-001", "rev_02:acme_admin")
    ctx_g = _ctx("globex-002", "rev_01:globex_admin")

    art = svc.create(
        ctx_a,
        ChartRequest(
            title="Acme Low-Disk Trend",
            kind="metric_over_time",
            metric="disk_free_pct",
            low_disk_only=True,
            chart_type="line",
        ),
    )
    # Personal-only isolation: other actors and tenants cannot see or mutate Actor A's chart
    assert svc.list_charts(ctx_b) == []
    assert svc.list_charts(ctx_g) == []
    assert svc.pin_chart(ctx_b, art.chart_id, pinned=True) is None
    assert svc.refresh_chart(ctx_b, art.chart_id) is None
    assert svc.delete_chart(ctx_b, art.chart_id) is False

    # Actor A pins chart -> visible in pinned_only listing
    pinned = svc.pin_chart(ctx_a, art.chart_id, pinned=True)
    assert pinned is not None and pinned["pinned"] is True
    assert [c["chart_id"] for c in svc.list_charts(ctx_a, pinned_only=True)] == [art.chart_id]

    # Inject a newer telemetry snapshot beyond tenant_as_of and refresh IN PLACE
    latest_mt7p = oracle.latest_by_device("acme-001")["MT7PJB7N5LRE"]
    new_snap = dict(latest_mt7p)
    new_ts = art.telemetry_as_of + timedelta(hours=2)
    new_snap["collected_at"] = new_ts.isoformat().replace("+00:00", "Z")
    ingest_snapshot(new_snap, writer_url=get_settings().telemetry_writer_database_url)

    refreshed = svc.refresh_chart(ctx_a, art.chart_id)
    assert refreshed is not None
    assert refreshed.chart_id == art.chart_id  # refreshed in place, not a new row!
    assert refreshed.telemetry_as_of == new_ts
    assert refreshed.row_count == art.row_count + 1
    assert len(svc.list_charts(ctx_a, pinned_only=True)) == 1

    # Delete removes chart cleanly
    assert svc.delete_chart(ctx_a, art.chart_id) is True
    assert svc.list_charts(ctx_a) == []

    # Verify all 4 chart audit events recorded and hash chain intact
    events = [e["event_type"] for e in audit.list(ctx_a)]
    assert "chart_created" in events
    assert "chart_pinned" in events
    assert "chart_refreshed" in events
    assert "chart_deleted" in events
    assert audit.verify(ctx_a)["ok"] is True


@pytest.mark.integration
def test_analyze_fleet_trends_auto_attaches_valid_charts_to_insights(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    res = analyze_fleet_trends(_ctx("acme-001"), ["all"], repo, PolicyService(store))
    assert res.ok is True
    assert len(res.data) > 0
    for ins in res.data:
        chart = ins.get("chart")
        assert isinstance(chart, dict), f"Insight {ins['insight_id']} missing auto-attached chart"
        validate_vega_lite_spec(chart["vega_lite_spec"])
        assert len(chart["data_rows"]) > 0
