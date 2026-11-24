"""Tenant isolation security probe against live PostgreSQL roles.

Run from the repository root (`agentic-fleet-copilot/`):
    make security-probe
    # or: uv run python scripts/security_probe.py

Each probe begins with an application-locked `acme-001` cursor (or a fresh unlocked
connection), attempts a spoofing or re-locking attack targeting `globex-002`, and reads
the effective visible tenant. All 29 checks must report `BLOCKED`. All transactions roll
back cleanly without mutating data.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import psycopg

from fleet_copilot.config import get_settings
from fleet_copilot.db import get_db_manager

READ: dict[str, str] = {
    "telemetry": "SELECT array_agg(DISTINCT company_id) FROM telemetry.v_latest_device_state",
    "copilot": "SELECT array_remove(ARRAY[NULLIF(copilot.current_tenant(), '')], NULL)",
}


def relock(schema: str) -> list[str]:
    """Return forged/uncredentialed `lock_tenant` statements for `schema`."""
    return [
        f"RESET app.tenant_locked; RESET app.tenant_sig; SELECT {schema}.lock_tenant('globex-002')",
        f"SELECT {schema}.lock_tenant('globex-002', now(), 'forged-mac')",
    ]


SPOOFS: dict[str, Callable[[str], list[str]]] = {
    "set_config": lambda sc: ["SELECT set_config('app.current_tenant', 'globex-002', true)"],
    "SET LOCAL": lambda sc: ["SET LOCAL app.current_tenant = 'globex-002'"],
    "SET (session)": lambda sc: ["SET app.current_tenant = 'globex-002'"],
    "RESET": lambda sc: ["RESET app.current_tenant"],
    "RESET+relock": relock,
}


def _first(row: Any) -> Any:
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _verdict(seen: list[str] | None) -> str:
    items = seen or []
    leaked = [t for t in items if t != "acme-001"]
    return f"LEAK {leaked}" if leaked else f"BLOCKED (visible: {items})"


def run_locked(role: str, schema: str, cursor_factory: Any) -> int:
    """Run all mid-transaction spoofing probes; return leak count."""
    leaks = 0
    for name, make_stmts in SPOOFS.items():
        for stmt in make_stmts(schema):
            try:
                with cursor_factory("acme-001") as cur:
                    for part in [p for p in stmt.split(";") if p.strip()]:
                        cur.execute(part)
                    cur.execute(READ[schema])
                    out = _verdict(_first(cur.fetchone()))
                    raise psycopg.Rollback()
            except Exception as exc:  # noqa: BLE001
                out = f"BLOCKED ({type(exc).__name__})"
            print(f"{role:13} {name:15} {out}")
            if out.startswith("LEAK"):
                leaks += 1
                break
    return leaks


def run_fresh(role: str, url: str, schema: str) -> int:
    """Run fresh-transaction self-lock spoofing probes; return leak count."""
    leaks = 0
    for stmt in relock(schema):
        with psycopg.connect(url) as conn:
            cur = conn.cursor()
            try:
                for part in [p for p in stmt.split(";") if p.strip()]:
                    cur.execute(part)  # type: ignore[arg-type]
                cur.execute(READ[schema])  # type: ignore[arg-type]
                out = _verdict(_first(cur.fetchone()))
            except Exception as exc:  # noqa: BLE001
                out = f"BLOCKED ({type(exc).__name__})"
            finally:
                conn.rollback()
        print(f"{role:13} {'fresh-txn lock':15} {out}")
        if out.startswith("LEAK"):
            leaks += 1
            break
    return leaks


def run_table_deny(role: str, url: str, table: str, *, verb: str = "SELECT") -> int:
    """Verify an app role cannot directly SELECT or DELETE a restricted table."""
    stmt = (
        f"SELECT 1 FROM {table} LIMIT 1" if verb == "SELECT" else f"DELETE FROM {table} WHERE false"
    )
    with psycopg.connect(url) as conn:
        cur = conn.cursor()
        try:
            cur.execute(stmt)  # type: ignore[arg-type]
            out = f"LEAK ({verb} allowed on {table})"
            leaked = 1
        except Exception as exc:  # noqa: BLE001
            out = f"BLOCKED ({type(exc).__name__})"
            leaked = 0
        finally:
            conn.rollback()
    print(f"{role:13} {verb + ' ' + table:34} {out}")
    return leaked


def main() -> int:
    """Execute tenant-isolation and restricted-table probes across application roles."""
    s = get_settings()
    db = get_db_manager()
    leaks = 0
    leaks += run_locked("telemetry_ro", "telemetry", db.telemetry_ro_cursor)
    leaks += run_fresh("telemetry_ro", str(s.telemetry_ro_database_url), "telemetry")
    leaks += run_locked("copilot_app", "copilot", db.copilot_cursor)
    leaks += run_fresh("copilot_app", str(s.copilot_database_url), "copilot")
    leaks += run_table_deny(
        "telemetry_ro", str(s.telemetry_ro_database_url), "copilot.spend_ledger"
    )
    leaks += run_table_deny("copilot_app", str(s.copilot_database_url), "copilot.spend_ledger")
    leaks += run_table_deny(
        "telemetry_ro", str(s.telemetry_ro_database_url), "copilot.integration_outbox"
    )
    for tbl in (
        "telemetry.telemetry_snapshots",
        "copilot.action_proposals",
        "copilot.audit_logs",
        "copilot.spend_ledger",
        "ops.runtime_settings",
    ):
        leaks += run_table_deny("demo_ops", str(s.demo_ops_database_url), tbl, verb="SELECT")
        leaks += run_table_deny("demo_ops", str(s.demo_ops_database_url), tbl, verb="DELETE")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
