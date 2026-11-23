"""Integration tests for tenant-locking functions and set_config() revocation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    """Ensure schema + hardening are applied and telemetry baseline is seeded."""
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


@pytest.mark.parametrize("role", ["telemetry_ro", "copilot_app", "telemetry_writer"])
def test_raw_set_config_is_denied(db_manager: DatabaseManager, role: str) -> None:
    """Direct pg_catalog.set_config() must be denied for all three application roles."""
    settings = get_settings()
    dsn_by_role = {
        "telemetry_ro": settings.telemetry_ro_database_url,
        "copilot_app": settings.copilot_database_url,
        "telemetry_writer": settings.telemetry_writer_database_url,
    }
    with psycopg.connect(dsn_by_role[role], prepare_threshold=None) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('app.current_tenant', 'acme-001', true)")


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
def test_lock_tenant_scopes_rls(db_manager: DatabaseManager, schema: str) -> None:
    """lock_tenant() must persist app.current_tenant in the transaction and scope RLS."""
    if schema == "telemetry":
        expected = {
            "acme-001": (10, 300),
            "globex-002": (8, 240),
            "initech-003": (7, 210),
        }
        for company_id, (dev_count, snap_count) in expected.items():
            with db_manager.telemetry_ro_cursor(company_id) as cur:
                cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
                row = cur.fetchone()
                assert row is not None and row["t"] == company_id

                cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
                row = cur.fetchone()
                assert row is not None and row["n"] == dev_count

                cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_snapshots_deduped")
                row = cur.fetchone()
                assert row is not None and row["n"] == snap_count
    else:
        thread_id = f"acme-001:lock-scope-{uuid.uuid4().hex[:8]}"
        try:
            with db_manager.copilot_cursor("acme-001") as cur:
                cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
                row = cur.fetchone()
                assert row is not None and row["t"] == "acme-001"

                cur.execute(
                    """
                    INSERT INTO copilot.threads (thread_id, company_id, actor_id)
                    VALUES (%s, 'acme-001', 'admin-acme-1')
                    """,
                    (thread_id,),
                )
                cur.execute(
                    "SELECT COUNT(*) AS n FROM copilot.threads WHERE thread_id = %s",
                    (thread_id,),
                )
                row = cur.fetchone()
                assert row is not None and row["n"] == 1

            with db_manager.copilot_cursor("globex-002") as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM copilot.threads WHERE thread_id = %s",
                    (thread_id,),
                )
                row = cur.fetchone()
                assert row is not None and row["n"] == 0
        finally:
            with psycopg.connect(get_settings().admin_database_url, prepare_threshold=None) as conn:
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM copilot.threads WHERE thread_id = %s",
                        (thread_id,),
                    )


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
def test_lock_tenant_twice_raises(db_manager: DatabaseManager, schema: str) -> None:
    """Calling lock_tenant() a second time in the same transaction must fail."""
    if schema == "telemetry":
        with pytest.raises(errors.InsufficientPrivilege, match="already locked"):
            with db_manager.telemetry_ro_cursor("acme-001") as cur:
                cur.execute("SELECT telemetry.lock_tenant(%s)", ("globex-002",))
    else:
        with pytest.raises(errors.InsufficientPrivilege, match="already locked"):
            with db_manager.copilot_cursor("acme-001") as cur:
                cur.execute("SELECT copilot.lock_tenant(%s)", ("globex-002",))


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
@pytest.mark.parametrize("bad_id", ["", " ", "*", "acme 001", "-x"])
def test_lock_tenant_rejects_invalid_ids(
    db_manager: DatabaseManager,
    schema: str,
    bad_id: str,
) -> None:
    """lock_tenant() must reject empty, wildcard, spaced, or malformed tenant IDs."""
    settings = get_settings()
    if schema == "telemetry":
        dsn = settings.telemetry_ro_database_url
        fn = "telemetry.lock_tenant"
    else:
        dsn = settings.copilot_database_url
        fn = "copilot.lock_tenant"

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with pytest.raises(errors.InvalidParameterValue, match="invalid company_id"):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {fn}(%s)", (bad_id,))


def test_lock_is_transaction_scoped(db_manager: DatabaseManager) -> None:
    """Tenant lock resets at transaction boundary; next unlocked transaction sees 0 rows."""
    settings = get_settings()

    with psycopg.connect(
        settings.telemetry_ro_database_url, prepare_threshold=None, row_factory=dict_row
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT telemetry.lock_tenant(%s)", ("acme-001",))
                cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
                row = cur.fetchone()
                assert row is not None and row["n"] == 10

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
                row = cur.fetchone()
                assert row is not None and row["n"] == 0

    with psycopg.connect(
        settings.copilot_database_url, prepare_threshold=None, row_factory=dict_row
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT copilot.lock_tenant(%s)", ("acme-001",))
                cur.execute("SELECT COUNT(*) AS n FROM copilot.tenant_policies")
                row = cur.fetchone()
                assert row is not None and row["n"] > 0

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM copilot.tenant_policies")
                row = cur.fetchone()
                assert row is not None and row["n"] == 0


def test_spoof_probe_is_blocked(db_manager: DatabaseManager) -> None:
    """Mirrors audit/scripts/probe_set_config_spoof.py: mid-tx set_config() spoof fails."""
    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.telemetry_ro_cursor("acme-001") as cur:
            cur.execute("SELECT set_config('app.current_tenant', 'globex-002', true)")

    with pytest.raises(errors.InsufficientPrivilege):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute("SELECT set_config('app.current_tenant', 'globex-002', true)")
