"""Integration tests for B3: execute_readonly_sql, oracle checks, RLS bypass proof, Q->C chart."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors

from evals import oracle
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.charts import ChartRequest, ChartService, validate_vega_lite_spec
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.sql_query import (
    SQL_ALLOWED_COLUMNS,
    SqlPiiInResultError,
    SqlValidationError,
    execute_readonly_sql,
)
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
        min_size=2,
        max_size=6,
    ) as manager:
        yield manager


def _ctx(
    company_id: str = "acme-001",
    actor_id: str = "rev_01:acme_viewer",
    role: str = "viewer",
) -> TenantContext:
    return TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role=role,  # type: ignore[arg-type]
        trace_id="trace-b3",
    )


@pytest.mark.integration
def test_execute_readonly_sql_avg_cycles_by_model_matches_oracle(
    db_manager: DatabaseManager,
) -> None:
    res = execute_readonly_sql(
        _ctx("acme-001", role="viewer"),
        db_manager,
        """
        SELECT model_name,
               ROUND(AVG(battery_cycle_count)::numeric, 2) AS avg_cycles,
               COUNT(*) AS laptop_count
        FROM telemetry.v_latest_device_state
        WHERE battery_present = true AND battery_cycle_count IS NOT NULL
        GROUP BY model_name
        ORDER BY model_name
        """,
        purpose="Average battery cycles by model",
    )
    assert res.row_count > 0

    latest = oracle.latest_by_device("acme-001")
    expected: dict[str, list[int]] = {}
    for snap in latest.values():
        bat = snap.get("battery") or {}
        if bat.get("battery_present") and bat.get("cycle_count") is not None:
            model = str((snap.get("device_identity") or {}).get("model_name") or "unknown")
            expected.setdefault(model, []).append(int(bat["cycle_count"]))
    expected_avg = {m: round(sum(v) / len(v), 2) for m, v in expected.items()}
    got_avg = {str(r["model_name"]): round(float(r["avg_cycles"]), 2) for r in res.rows}
    assert got_avg == expected_avg


@pytest.mark.integration
def test_execute_readonly_sql_devices_per_employee_matches_oracle(
    db_manager: DatabaseManager,
) -> None:
    res = execute_readonly_sql(
        _ctx("globex-002", role="it_admin"),
        db_manager,
        """
        SELECT employee_id, COUNT(*) AS device_count
        FROM telemetry.v_latest_device_state
        GROUP BY employee_id
        ORDER BY employee_id
        """,
        purpose="Devices per employee",
    )
    expected: dict[str, int] = {}
    for snap in oracle.latest_by_device("globex-002").values():
        emp = str(snap["employee_id"])
        expected[emp] = expected.get(emp, 0) + 1
    got = {str(r["employee_id"]): int(r["device_count"]) for r in res.rows}
    assert got == expected


@pytest.mark.integration
def test_rls_holds_even_when_validator_is_bypassed(db_manager: DatabaseManager) -> None:
    """10 §6 & 11 §2 B3: Bypassing the SQL validator still cannot read another tenant or mutate."""
    ctx = _ctx("acme-001", role="viewer")

    # Direct cross-tenant query with validator bypassed returns 0 rows under RLS
    res = execute_readonly_sql(
        ctx,
        db_manager,
        "SELECT device_id, company_id FROM telemetry.v_latest_device_state "
        "WHERE company_id = 'globex-002'",
        bypass_validator=True,
    )
    assert res.rows == []
    assert res.row_count == 0

    # Even attempting SET LOCAL + SELECT via bypassed validator fails closed (0 rows)
    with pytest.raises((errors.InsufficientPrivilege, psycopg.Error)):
        execute_readonly_sql(
            ctx,
            db_manager,
            "SELECT telemetry.lock_tenant('globex-002', now(), 'forged')",
            bypass_validator=True,
        )

    # Read-only transaction blocks DML even if validator is bypassed
    with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
        execute_readonly_sql(
            ctx,
            db_manager,
            "DELETE FROM telemetry.telemetry_snapshots",
            bypass_validator=True,
        )


@pytest.mark.integration
def test_statement_timeout_is_enforced(db_manager: DatabaseManager) -> None:
    """11 §2 B3: statement_timeout is enforced inside the read-only transaction."""
    ctx = _ctx("acme-001", role="viewer")
    with pytest.raises(errors.QueryCanceled):
        execute_readonly_sql(
            ctx,
            db_manager,
            "SELECT pg_sleep(0.5)",
            timeout_ms=100,
            bypass_validator=True,
        )


@pytest.mark.integration
def test_from_query_chart_plots_q_handle_rows(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    """B2 + B3: ChartService `from_query` builds a validated Vega-Lite chart from a `Q1` query."""
    ctx = _ctx("acme-001", role="viewer")
    q_res = execute_readonly_sql(
        ctx,
        db_manager,
        """
        SELECT model_name,
               ROUND(AVG(battery_cycle_count)::numeric, 2) AS avg_cycles
        FROM telemetry.v_latest_device_state
        WHERE battery_present = true AND battery_cycle_count IS NOT NULL
        GROUP BY model_name
        ORDER BY model_name
        """,
        purpose="Average cycles by model",
    )
    q_lookup = {
        "Q1": {
            "handle": "Q1",
            "sql": q_res.sql,
            "rows": q_res.rows,
            "snapshot_ids": q_res.snapshot_ids,
        }
    }
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    chart_svc = ChartService(repo, PolicyService(store), store=store)
    art = chart_svc.create(
        ctx,
        ChartRequest(
            title="Avg Cycles by Model (from SQL)",
            kind="from_query",
            query_handle="Q1",
            x="model_name",
            y="avg_cycles",
            chart_type="bar",
        ),
        query_lookup=q_lookup,
        persist=False,
    )
    validate_vega_lite_spec(art.vega_lite_spec)
    assert art.row_count == q_res.row_count
    assert art.data_rows[0]["model_name"] == q_res.rows[0]["model_name"]


@pytest.mark.integration
def test_sql_allowed_columns_matches_live_view_schemas(db_manager: DatabaseManager) -> None:
    """Audit/13 §2.1: SQL_ALLOWED_COLUMNS equals live view columns minus restricted 4."""
    excluded = {"serial_number", "hardware_uuid", "payload", "id"}
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url) as conn:
        with conn.cursor() as cur:
            for view_name, expected_cols in SQL_ALLOWED_COLUMNS.items():
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'telemetry' AND table_name = %s
                    """,
                    (view_name,),
                )
                live_cols = {str(r[0]).lower() for r in cur.fetchall()} - excluded
                assert expected_cols == live_cols, f"Mismatch on telemetry.{view_name}"


@pytest.mark.integration
def test_whole_row_attacks_blocked_and_result_pii_guard_catches_bypass(
    db_manager: DatabaseManager,
) -> None:
    """Audit/13 §2: Whole-row attacks fail validation; result guard catches PII even on bypass."""
    ctx = _ctx("acme-001", role="it_admin")
    for attack in (
        "SELECT l AS x FROM telemetry.v_latest_device_state l LIMIT 1",
        "SELECT concat(l) AS x FROM telemetry.v_latest_device_state l LIMIT 1",
        "SELECT concat(s) AS x FROM telemetry.v_snapshots_deduped s LIMIT 1",
        "SELECT array_agg(l) FROM telemetry.v_latest_device_state l",
        "SELECT row_to_json(l) FROM telemetry.v_latest_device_state l",
        "SELECT l.* FROM telemetry.v_latest_device_state l LIMIT 1",
        "WITH c AS (SELECT l AS row_val FROM telemetry.v_latest_device_state l) "
        "SELECT row_val FROM c",
    ):
        with pytest.raises(SqlValidationError):
            execute_readonly_sql(ctx, db_manager, attack)

    # Even if validator is bypassed, result-side PII scan drops hardware_uuid / payload IP+MAC rows
    with pytest.raises(SqlPiiInResultError):
        execute_readonly_sql(
            ctx,
            db_manager,
            "SELECT concat(l) AS x FROM telemetry.v_latest_device_state l LIMIT 1",
            bypass_validator=True,
        )
    with pytest.raises(SqlPiiInResultError):
        execute_readonly_sql(
            ctx,
            db_manager,
            "SELECT concat(s) AS x FROM telemetry.v_snapshots_deduped s LIMIT 1",
            bypass_validator=True,
        )
