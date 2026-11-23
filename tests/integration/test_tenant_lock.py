"""Integration tests for credentialed tenant locking, advisory xact markers, and RLS."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.db.postgres import sign_tenant_lock
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
    """Calling lock_tenant() a second time in the same transaction must fail even with valid MAC."""
    issued_at, mac = sign_tenant_lock("globex-002")
    if schema == "telemetry":
        with pytest.raises(errors.InsufficientPrivilege, match="already locked"):
            with db_manager.telemetry_ro_cursor("acme-001") as cur:
                cur.execute(
                    "SELECT telemetry.lock_tenant(%s, %s, %s)",
                    ("globex-002", issued_at, mac),
                )
    else:
        with pytest.raises(errors.InsufficientPrivilege, match="already locked"):
            with db_manager.copilot_cursor("acme-001") as cur:
                cur.execute(
                    "SELECT copilot.lock_tenant(%s, %s, %s)",
                    ("globex-002", issued_at, mac),
                )


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

    now = datetime.now(UTC)
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with pytest.raises(errors.InvalidParameterValue, match="invalid company_id"):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {fn}(%s, %s, %s)", (bad_id, now, "unused"))


def test_lock_is_transaction_scoped(db_manager: DatabaseManager) -> None:
    """Tenant lock resets at transaction boundary; next unlocked transaction sees 0 rows."""
    settings = get_settings()
    issued_at, mac = sign_tenant_lock("acme-001")

    with psycopg.connect(
        settings.telemetry_ro_database_url, prepare_threshold=None, row_factory=dict_row
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT telemetry.lock_tenant(%s, %s, %s)",
                    ("acme-001", issued_at, mac),
                )
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
                cur.execute(
                    "SELECT copilot.lock_tenant(%s, %s, %s)",
                    ("acme-001", issued_at, mac),
                )
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


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
def test_set_local_spoof_is_blocked(db_manager: DatabaseManager, schema: str) -> None:
    """11 §2 A2: `SET LOCAL app.current_tenant` invalidates the signature and fails closed."""
    if schema == "telemetry":
        with db_manager.telemetry_ro_cursor("acme-001") as cur:
            cur.execute("SET LOCAL app.current_tenant = 'globex-002'")
            cur.execute("SELECT telemetry.current_tenant() AS t")
            row = cur.fetchone()
            assert row is not None and row["t"] is None
            cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
            cnt = cur.fetchone()
            assert cnt is not None and cnt["n"] == 0
    else:
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute("SET LOCAL app.current_tenant = 'globex-002'")
            cur.execute("SELECT copilot.current_tenant() AS t")
            row = cur.fetchone()
            assert row is not None and row["t"] is None
            cur.execute("SELECT COUNT(*) AS n FROM copilot.tenant_policies")
            cnt = cur.fetchone()
            assert cnt is not None and cnt["n"] == 0


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
def test_session_set_spoof_is_blocked(db_manager: DatabaseManager, schema: str) -> None:
    """11 §2 A2: Session-level `SET app.current_tenant` without valid signature fails closed."""
    settings = get_settings()
    issued_at, mac = sign_tenant_lock("acme-001")
    if schema == "telemetry":
        with psycopg.connect(
            settings.telemetry_ro_database_url, prepare_threshold=None, row_factory=dict_row
        ) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT telemetry.lock_tenant(%s, %s, %s)",
                        ("acme-001", issued_at, mac),
                    )
                    cur.execute("SET app.current_tenant = 'globex-002'")
                    cur.execute("SELECT telemetry.current_tenant() AS t")
                    row = cur.fetchone()
                    assert row is not None and row["t"] is None
                    cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
                    cnt = cur.fetchone()
                    assert cnt is not None and cnt["n"] == 0
            # Next transaction on same connection still fails closed even though session SET stuck
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
                    cnt = cur.fetchone()
                    assert cnt is not None and cnt["n"] == 0
    else:
        with psycopg.connect(
            settings.copilot_database_url, prepare_threshold=None, row_factory=dict_row
        ) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT copilot.lock_tenant(%s, %s, %s)",
                        ("acme-001", issued_at, mac),
                    )
                    cur.execute("SET app.current_tenant = 'globex-002'")
                    cur.execute("SELECT copilot.current_tenant() AS t")
                    row = cur.fetchone()
                    assert row is not None and row["t"] is None
                    cur.execute("SELECT COUNT(*) AS n FROM copilot.tenant_policies")
                    cnt = cur.fetchone()
                    assert cnt is not None and cnt["n"] == 0


def test_reset_spoof_is_blocked(db_manager: DatabaseManager) -> None:
    """11 §2 A2: `RESET app.current_tenant` fails closed."""
    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute("RESET app.current_tenant")
        cur.execute("SELECT telemetry.current_tenant() AS t")
        row = cur.fetchone()
        assert row is not None and row["t"] is None
        cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
        cnt = cur.fetchone()
        assert cnt is not None and cnt["n"] == 0

    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute("RESET app.current_tenant")
        cur.execute("SELECT copilot.current_tenant() AS t")
        row = cur.fetchone()
        assert row is not None and row["t"] is None
        cur.execute("SELECT COUNT(*) AS n FROM copilot.tenant_policies")
        cnt = cur.fetchone()
        assert cnt is not None and cnt["n"] == 0


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
def test_relock_after_reset_is_blocked(db_manager: DatabaseManager, schema: str) -> None:
    """12 §3.4: RESET app.tenant_locked + app.tenant_sig cannot clear the advisory xact marker."""
    issued_at, mac = sign_tenant_lock("globex-002")
    cursor_fn = (
        db_manager.telemetry_ro_cursor if schema == "telemetry" else db_manager.copilot_cursor
    )
    with pytest.raises(errors.InsufficientPrivilege, match="already locked"):
        with cursor_fn("acme-001") as cur:
            cur.execute("RESET app.tenant_locked")
            cur.execute("RESET app.tenant_sig")
            cur.execute("RESET app.current_tenant")
            # Even with a legitimately signed MAC for globex-002, re-locking must raise!
            cur.execute(
                f"SELECT {schema}.lock_tenant(%s, %s, %s)",
                ("globex-002", issued_at, mac),
            )


@pytest.mark.parametrize("schema", ["telemetry", "copilot"])
def test_lock_requires_valid_mac(db_manager: DatabaseManager, schema: str) -> None:
    """12 §3.4: 1-arg dropped; missing/forged/other-tenant/stale (60s) MACs all raise."""
    settings = get_settings()
    dsn = (
        settings.telemetry_ro_database_url
        if schema == "telemetry"
        else settings.copilot_database_url
    )
    now = datetime.now(UTC)
    acme_ts, acme_mac = sign_tenant_lock("acme-001", issued_at=now)
    stale_ts = now - timedelta(seconds=60)
    _, stale_mac = sign_tenant_lock("globex-002", issued_at=stale_ts)
    future_ts = now + timedelta(seconds=60)
    _, future_mac = sign_tenant_lock("globex-002", issued_at=future_ts)

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        # Old 1-arg lock_tenant(text) must be dropped
        with pytest.raises(errors.UndefinedFunction):
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {schema}.lock_tenant(%s)", ("globex-002",))

        bad_calls = [
            ("globex-002", now, ""),
            ("globex-002", now, None),
            ("globex-002", now, "forged-mac"),
            ("globex-002", acme_ts, acme_mac),  # MAC signed for a different tenant
            ("globex-002", stale_ts, stale_mac),  # 60s old (> 30s freshness window)
            ("globex-002", future_ts, future_mac),  # 60s in the future (> 30s)
        ]
        for company_id, ts, mac in bad_calls:
            with pytest.raises(errors.InsufficientPrivilege):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT {schema}.lock_tenant(%s, %s, %s)",
                            (company_id, ts, mac),
                        )


def test_savepoint_rollback_cannot_release_lock(db_manager: DatabaseManager) -> None:
    """12 §3.4: Rolling back to a SAVEPOINT cannot release a top-level lock or allow re-lock."""
    globex_ts, globex_mac = sign_tenant_lock("globex-002")

    with db_manager.telemetry_ro_cursor("acme-001") as cur:
        cur.execute("SAVEPOINT sp1")
        cur.execute("RESET app.tenant_locked")
        cur.execute("RESET app.tenant_sig")
        cur.execute("RESET app.current_tenant")
        cur.execute("ROLLBACK TO SAVEPOINT sp1")

        # Still locked to acme-001; re-locking to globex-002 raises
        cur.execute("SELECT telemetry.current_tenant() AS t")
        row = cur.fetchone()
        assert row is not None and row["t"] == "acme-001"

        cur.execute("SAVEPOINT sp2")
        with pytest.raises(errors.InsufficientPrivilege, match="already locked"):
            cur.execute(
                "SELECT telemetry.lock_tenant(%s, %s, %s)",
                ("globex-002", globex_ts, globex_mac),
            )
        cur.execute("ROLLBACK TO SAVEPOINT sp2")

    # Even if lock_tenant was called inside a subtransaction that rolls back,
    # rolling back clears both GUC and advisory lock (fail-closed) and forged relock fails.
    settings = get_settings()
    acme_ts, acme_mac = sign_tenant_lock("acme-001")
    with psycopg.connect(
        settings.telemetry_ro_database_url, prepare_threshold=None, row_factory=dict_row
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_inner")
                cur.execute(
                    "SELECT telemetry.lock_tenant(%s, %s, %s)",
                    ("acme-001", acme_ts, acme_mac),
                )
                cur.execute("ROLLBACK TO SAVEPOINT sp_inner")
                cur.execute("SELECT telemetry.current_tenant() AS t")
                row = cur.fetchone()
                assert row is not None and row["t"] is None
                cur.execute("SELECT COUNT(*) AS n FROM telemetry.v_latest_device_state")
                cnt = cur.fetchone()
                assert cnt is not None and cnt["n"] == 0
                with pytest.raises(errors.InsufficientPrivilege):
                    cur.execute(
                        "SELECT telemetry.lock_tenant(%s, %s, %s)",
                        ("globex-002", datetime.now(UTC), "forged"),
                    )


def test_pool_connection_is_reset_on_return() -> None:
    """11 §2 A2: Pooled connections run `RESET ALL` on return, clearing session-level SETs."""
    settings = get_settings()
    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=1,
    ) as single_db:
        # Pollute session GUC on the single pooled telemetry connection
        with single_db.telemetry_ro_cursor("acme-001") as cur:
            cur.execute("SET app.current_tenant = 'globex-002'")
            cur.execute("SET app.custom_pollution = 'leaked'")

        # Next checkout from the same 1-connection pool must have cleared session GUCs
        pool = single_db._get_telemetry_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
            r1 = cur.fetchone()
            assert r1 is not None and (r1["t"] is None or r1["t"] == "")
            cur.execute("SELECT current_setting('app.custom_pollution', true) AS p")
            r2 = cur.fetchone()
            assert r2 is not None and (r2["p"] is None or r2["p"] == "")

        # Same check on copilot pool, AND verify copilot_app search_path remains 'copilot'
        with single_db.copilot_cursor("acme-001") as cur:
            cur.execute("SET app.current_tenant = 'globex-002'")

        cpool = single_db._get_copilot_pool()
        with cpool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_tenant', true) AS t")
            r1 = cur.fetchone()
            assert r1 is not None and (r1["t"] is None or r1["t"] == "")
            cur.execute("SHOW search_path")
            sp = cur.fetchone()
            assert sp is not None and "copilot" in str(sp["search_path"])


def test_tenant_lock_key_not_readable_by_app_roles(db_manager: DatabaseManager) -> None:
    """12 §3.4: No application role may read telemetry/copilot tenant_lock_secret tables."""
    settings = get_settings()
    dsns = [
        settings.telemetry_ro_database_url,
        settings.copilot_database_url,
        settings.telemetry_writer_database_url,
    ]
    for dsn in dsns:
        for table in ("telemetry.tenant_lock_secret", "copilot.tenant_lock_secret"):
            with psycopg.connect(dsn, prepare_threshold=None) as conn:
                with pytest.raises(errors.InsufficientPrivilege):
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(f"SELECT secret FROM {table}")
