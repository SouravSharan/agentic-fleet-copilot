"""Unit tests for B3 sqlglot read-only SQL validator (allowlist, blocklist, LIMIT clamping)."""

from __future__ import annotations

import pytest

from fleet_copilot.services.sql_query import SqlValidationError, validate_readonly_sql


def test_validator_accepts_safe_selects_and_injects_or_clamps_limit() -> None:
    # Plain SELECT without LIMIT -> LIMIT 200 injected
    q1 = validate_readonly_sql(
        "SELECT model_name, ROUND(AVG(battery_cycle_count)::numeric, 2) AS avg_cycles "
        "FROM telemetry.v_latest_device_state "
        "WHERE battery_present = true GROUP BY model_name ORDER BY model_name"
    )
    assert "LIMIT 200" in q1.upper()

    # Existing LIMIT > 200 -> clamped to 200
    q2 = validate_readonly_sql(
        "SELECT device_id, os_hostname FROM telemetry.v_latest_device_state LIMIT 10000"
    )
    assert "LIMIT 200" in q2.upper()
    assert "10000" not in q2

    # Existing LIMIT <= 200 -> preserved
    q3 = validate_readonly_sql(
        "SELECT device_id, os_hostname FROM telemetry.v_latest_device_state LIMIT 5"
    )
    assert "LIMIT 5" in q3.upper()

    # CTE + date_trunc + coalesce + percentile_cont allowed
    q4 = validate_readonly_sql(
        """
        WITH daily AS (
            SELECT date_trunc('day', collected_at) AS day,
                   COALESCE(ram_used_pct, 0) AS ram
            FROM telemetry.v_snapshots_deduped
        )
        SELECT day,
               ROUND(AVG(ram)::numeric, 2) AS avg_ram,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY ram) AS p95_ram
        FROM daily
        GROUP BY day
        ORDER BY day
        """
    )
    assert "LIMIT 200" in q4.upper()


@pytest.mark.parametrize(
    "bad_sql",
    [
        "",
        "   ",
        # Comments
        "SELECT device_id FROM telemetry.v_latest_device_state -- comment",
        "SELECT /* block */ device_id FROM telemetry.v_latest_device_state",
        # Multiple statements
        "SELECT device_id FROM telemetry.v_latest_device_state; SELECT 1",
        # DML / DDL / utility
        "DELETE FROM telemetry.telemetry_snapshots",
        "UPDATE telemetry.companies SET display_name = 'x'",
        "INSERT INTO telemetry.companies VALUES ('x', 'y')",
        "DROP TABLE telemetry.telemetry_snapshots",
        "COPY telemetry.v_latest_device_state TO STDOUT",
        "SET LOCAL app.current_tenant = 'globex-002'",
        "RESET app.current_tenant",
        # Forbidden tables / schemas / PII views
        "SELECT device_id FROM telemetry.telemetry_snapshots",
        "SELECT address, mac FROM telemetry.v_network_interfaces",
        "SELECT secret FROM telemetry.tenant_lock_secret",
        "SELECT proposal_id FROM copilot.action_proposals",
        "SELECT rolname FROM pg_catalog.pg_roles",
        "SELECT table_name FROM information_schema.tables",
        "SELECT device_id FROM v_latest_device_state",  # missing telemetry schema
        # Wildcard / sensitive columns
        "SELECT * FROM telemetry.v_latest_device_state",
        "SELECT device_id, hardware_uuid FROM telemetry.v_latest_device_state",
        "SELECT device_id, serial_number FROM telemetry.v_latest_device_state",
        "SELECT snapshot_id, payload FROM telemetry.v_snapshots_deduped",
        # Disallowed functions
        "SELECT set_config('app.current_tenant', 'globex-002', true)",
        "SELECT pg_sleep(5)",
        "SELECT current_setting('app.current_tenant', true)",
        "SELECT telemetry.lock_tenant('globex-002', now(), 'forged')",
        "SELECT version()",
        # Locking clause
        "SELECT device_id FROM telemetry.v_latest_device_state FOR UPDATE",
    ],
)
def test_validator_rejects_dml_ddl_multi_stmt_comments_forbidden_tables_and_functions(
    bad_sql: str,
) -> None:
    with pytest.raises(SqlValidationError):
        validate_readonly_sql(bad_sql)
