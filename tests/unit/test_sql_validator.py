"""Unit tests for sqlglot read-only SQL validator (allowlist, blocklist, LIMIT clamping)."""

from __future__ import annotations

import pytest

from fleet_copilot.services.sql_query import (
    SQL_ALLOWED_COLUMNS,
    SqlPiiInResultError,
    SqlValidationError,
    contains_pii_value,
    validate_readonly_sql,
)


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

    # CTE + date_trunc + coalesce + percentile_cont + output alias in ORDER BY allowed
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
        ORDER BY avg_ram DESC
        """
    )
    assert "LIMIT 200" in q4.upper()

    # Qualified JOIN across two allowed views + unambiguous column in WHERE
    q5 = validate_readonly_sql(
        """
        SELECT l.device_id, l.model_name, d.free_pct
        FROM telemetry.v_latest_device_state l
        JOIN telemetry.v_disk_volumes d ON l.snapshot_id = d.snapshot_id
        WHERE free_pct < 15
        ORDER BY d.free_pct ASC
        LIMIT 20
        """
    )
    assert "LIMIT 20" in q5.upper()

    # Derived table (subquery in FROM) + scalar subquery in WHERE
    q6 = validate_readonly_sql(
        """
        SELECT sub.model_name, COUNT(*) AS n
        FROM (
            SELECT device_id, model_name
            FROM telemetry.v_latest_device_state
            WHERE collected_at <= (SELECT MAX(collected_at) FROM telemetry.v_snapshots_deduped)
        ) sub
        GROUP BY sub.model_name
        ORDER BY n DESC
        """
    )
    assert "LIMIT 200" in q6.upper()

    # Ensure positive allowlist covers all 5 views and omits the 4 excluded columns
    assert set(SQL_ALLOWED_COLUMNS) == {
        "v_latest_device_state",
        "v_snapshots_deduped",
        "v_disk_volumes",
        "v_compliance_results",
        "v_installed_software",
    }
    for view, cols in SQL_ALLOWED_COLUMNS.items():
        assert not (cols & {"serial_number", "hardware_uuid", "payload", "id"}), view


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
        # Wildcard / sensitive / excluded / unresolved columns
        "SELECT * FROM telemetry.v_latest_device_state",
        "SELECT l.* FROM telemetry.v_latest_device_state l",
        "SELECT device_id, hardware_uuid FROM telemetry.v_latest_device_state",
        "SELECT device_id, serial_number FROM telemetry.v_latest_device_state",
        'SELECT "serial_number" FROM telemetry.v_latest_device_state',
        (
            "SELECT device_id FROM telemetry.v_latest_device_state "
            "WHERE substr(serial_number, 1, 4) = '1LYS'"
        ),
        "SELECT snapshot_id, payload FROM telemetry.v_snapshots_deduped",
        "SELECT id, device_id FROM telemetry.v_snapshots_deduped",
        "SELECT l.ctid FROM telemetry.v_latest_device_state l",
        "SELECT nonexistent_column FROM telemetry.v_latest_device_state",
        "SELECT unknown_alias.device_id FROM telemetry.v_latest_device_state l",
        # Ambiguous unqualified column across JOIN
        (
            "SELECT device_id FROM telemetry.v_latest_device_state l "
            "JOIN telemetry.v_disk_volumes d ON l.snapshot_id = d.snapshot_id"
        ),
        # Whole-row reference attacks
        "SELECT l AS x FROM telemetry.v_latest_device_state l LIMIT 1",
        "SELECT concat(l) AS x FROM telemetry.v_latest_device_state l LIMIT 1",
        "SELECT concat(s) AS x FROM telemetry.v_snapshots_deduped s LIMIT 1",
        "SELECT array_agg(l) FROM telemetry.v_latest_device_state l",
        "SELECT row_to_json(l) FROM telemetry.v_latest_device_state l",
        "SELECT v_latest_device_state FROM telemetry.v_latest_device_state",
        "SELECT l::text FROM telemetry.v_latest_device_state l",
        "SELECT (l).hardware_uuid FROM telemetry.v_latest_device_state l",
        "SELECT count(l) FROM telemetry.v_latest_device_state l",
        # Alias shadowing a column name
        "SELECT device_id FROM telemetry.v_latest_device_state AS device_id",
        "SELECT status FROM telemetry.v_latest_device_state AS status",
        # CTE re-exposing an aliased row or whole CTE row
        (
            "WITH c AS (SELECT l AS row_val FROM telemetry.v_latest_device_state l) "
            "SELECT row_val FROM c"
        ),
        "WITH c AS (SELECT device_id FROM telemetry.v_latest_device_state) SELECT c FROM c",
        "WITH c AS (SELECT device_id FROM telemetry.v_latest_device_state) SELECT concat(c) FROM c",
        "WITH c AS (SELECT device_id FROM telemetry.v_latest_device_state) SELECT c.* FROM c",
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


def test_result_side_pii_guard_detects_ip_mac_uuid_and_allows_benign_telemetry() -> None:
    # PII patterns detected (MAC, IPv4, IPv6, UUID)
    assert contains_pii_value([{"x": "192.168.1.42"}]) is True
    assert contains_pii_value([{"x": "row has 10.20.30.40 inside"}]) is True
    assert contains_pii_value([{"x": "fe80::1"}]) is True
    assert contains_pii_value([{"x": "2001:db8::8a2e:370:7334"}]) is True
    assert contains_pii_value([{"x": "aa:bb:cc:dd:ee:ff"}]) is True
    assert contains_pii_value([{"x": "AA-BB-CC-DD-EE-FF"}]) is True
    assert contains_pii_value([{"x": "550e8400-e29b-41d4-a716-446655440000"}]) is True
    assert contains_pii_value([{"nested": ["ok", {"val": "aa:bb:cc:dd:ee:ff"}]}]) is True

    # Benign telemetry values must NOT false-positive
    benign_rows = [
        {
            "device_id": "1LYSSFD074BB",
            "snapshot_id": "snap_c9c1828f3d303599",
            "os_hostname": "acme-macbook-2",
            "os_product_name": "macOS",
            "os_product_version": "14.5",
            "win_version": "10.0.22631",
            "collected_at": "2026-06-13T12:10:00+00:00",
            "avg_cycles": 559.2,
            "count": 5,
        }
    ]
    assert contains_pii_value(benign_rows) is False


def test_run_readonly_sql_tool_returns_pii_in_result_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Result-side PII guard returns {"ok":false,"error":"pii_in_result"}."""
    import json
    from types import SimpleNamespace

    from fleet_copilot.domain.evidence import EvidenceLedger
    from fleet_copilot.domain.models import TenantContext
    from fleet_copilot.tools import sql_tool
    from fleet_copilot.tools.registry import ToolDeps, TurnToolSink

    def _raise_pii(*_a: object, **_kw: object) -> object:
        raise SqlPiiInResultError("blocked")

    monkeypatch.setattr(sql_tool, "execute_readonly_sql", _raise_pii)

    deps = ToolDeps(
        telemetry_repo=SimpleNamespace(),  # type: ignore[arg-type]
        copilot_store=SimpleNamespace(),  # type: ignore[arg-type]
        policy_svc=SimpleNamespace(),  # type: ignore[arg-type]
        action_svc=SimpleNamespace(),  # type: ignore[arg-type]
        clock=SimpleNamespace(),  # type: ignore[arg-type]
        db=object(),
    )
    spec = sql_tool.build_sql_tool_spec(deps)
    sink = TurnToolSink(ledger=EvidenceLedger())
    ctx = TenantContext(company_id="acme-001", actor_id="a", role="it_admin", trace_id="t")
    runtime = SimpleNamespace(
        context=ctx,
        config={"configurable": {"_turn_sink": sink}},
        state={},
        tool_call_id="tc_sql_pii",
    )
    raw = spec.tool.func(  # type: ignore[attr-defined]
        sql="SELECT device_id FROM telemetry.v_latest_device_state LIMIT 1",
        purpose="test pii",
        runtime=runtime,
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"] == "pii_in_result"
    assert sink.queries == []
    assert sink.ledger.to_json() == {}
