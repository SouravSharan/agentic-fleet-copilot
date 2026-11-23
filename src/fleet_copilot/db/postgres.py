"""Tenant-scoped PostgreSQL connection managers with credentialed write-once tenant locking.

Security invariants:
- `telemetry_ro_cursor` connects exclusively via the read-only `telemetry_ro` role,
  opens a `READ ONLY` transaction, and binds `app.current_tenant` once via
  `telemetry.lock_tenant(%s, %s, %s)` authenticated with HMAC-SHA256 (`TENANT_LOCK_KEY`).
- `copilot_cursor` connects exclusively via `copilot_app` (search_path = copilot)
  and binds `app.current_tenant` once via `copilot.lock_tenant(%s, %s, %s)`.
- `prepare_threshold=None` disables server-side prepared statements for clean
  compatibility with transaction-level connection pooling.
"""

import hashlib
import hmac
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Self, cast

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from fleet_copilot.config import Settings, get_settings

_COMPANY_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")


class InvalidTenantError(ValueError):
    """Raised when a missing, wildcard, untrimmed, or malformed company_id is supplied."""


def validate_company_id(company_id: str) -> str:
    """Validate a concrete single-tenant company_id (rejects wildcards & untrimmed whitespace)."""
    if not isinstance(company_id, str):
        raise InvalidTenantError("company_id must be a non-empty string")
    if (
        not company_id
        or company_id != company_id.strip()
        or company_id == "*"
        or not _COMPANY_ID_RE.match(company_id)
    ):
        raise InvalidTenantError(f"Invalid company_id: {company_id!r}")
    return company_id


def _resolve_tenant_lock_key(explicit_key: str | None = None) -> str:
    """Resolve `TENANT_LOCK_KEY` from explicit arg, Settings, or env; raise if missing."""
    raw = explicit_key
    if raw is None:
        secret = get_settings().tenant_lock_key
        raw = secret.get_secret_value() if secret is not None else os.getenv("TENANT_LOCK_KEY")
    if not raw or not raw.strip():
        raise ValueError(
            "TENANT_LOCK_KEY must be configured (64 hex chars, e.g. openssl rand -hex 32)"
        )
    return raw.strip()


def sign_tenant_lock(
    company_id: str,
    *,
    key: str | None = None,
    issued_at: datetime | None = None,
) -> tuple[datetime, str]:
    """Compute `(issued_at, hex_hmac_sha256)` authenticating `lock_tenant(company_id, ...)`."""
    ts = (issued_at or datetime.now(UTC)).astimezone(UTC)
    key_str = _resolve_tenant_lock_key(key)
    msg = f"{company_id}|{ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}".encode()
    mac = hmac.new(key_str.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return ts, mac


def _reset_pooled_connection(conn: psycopg.Connection[Any]) -> None:
    """Run `RESET ALL` and clear session advisory locks when returning a pooled connection."""
    conn.execute("RESET ALL")
    conn.execute("SELECT pg_advisory_unlock_all()")
    if not conn.autocommit:
        conn.commit()


class DatabaseManager:
    """Manages thread-safe isolated connection pools for `telemetry_ro` and `copilot_app`."""

    def __init__(
        self,
        telemetry_ro_dsn: str,
        copilot_dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        tenant_lock_key: str | None = None,
    ) -> None:
        self._telemetry_ro_dsn = telemetry_ro_dsn
        self._copilot_dsn = copilot_dsn
        self._min_size = min_size
        self._max_size = max_size
        self._tenant_lock_key = tenant_lock_key
        self._lock = threading.Lock()
        self._telemetry_pool: ConnectionPool[psycopg.Connection[DictRow]] | None = None
        self._copilot_pool: ConnectionPool[psycopg.Connection[DictRow]] | None = None
        self._checkpoint_pool: ConnectionPool[psycopg.Connection[DictRow]] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get_telemetry_pool(self) -> ConnectionPool[psycopg.Connection[DictRow]]:
        with self._lock:
            if self._telemetry_pool is None:
                self._telemetry_pool = cast(
                    ConnectionPool[psycopg.Connection[DictRow]],
                    ConnectionPool(
                        conninfo=self._telemetry_ro_dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=True,
                        check=ConnectionPool.check_connection,
                        reset=_reset_pooled_connection,
                        kwargs={"prepare_threshold": None, "row_factory": dict_row},
                    ),
                )
            return self._telemetry_pool

    def _get_copilot_pool(self) -> ConnectionPool[psycopg.Connection[DictRow]]:
        with self._lock:
            if self._copilot_pool is None:
                self._copilot_pool = cast(
                    ConnectionPool[psycopg.Connection[DictRow]],
                    ConnectionPool(
                        conninfo=self._copilot_dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=True,
                        check=ConnectionPool.check_connection,
                        reset=_reset_pooled_connection,
                        kwargs={"prepare_threshold": None, "row_factory": dict_row},
                    ),
                )
            return self._copilot_pool

    @contextmanager
    def telemetry_ro_cursor(self, company_id: str) -> Iterator[psycopg.Cursor[DictRow]]:
        """Yield a read-only cursor scoped to a single verified tenant via Postgres RLS."""
        tenant = validate_company_id(company_id)
        issued_at, mac = sign_tenant_lock(tenant, key=self._tenant_lock_key)
        pool = self._get_telemetry_pool()
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(
                        "SELECT telemetry.lock_tenant(%s, %s, %s)",
                        (tenant, issued_at, mac),
                    )
                    yield cur

    @contextmanager
    def copilot_cursor(self, company_id: str) -> Iterator[psycopg.Cursor[DictRow]]:
        """Yield a read/write cursor on `copilot` scoped to a single verified tenant."""
        tenant = validate_company_id(company_id)
        issued_at, mac = sign_tenant_lock(tenant, key=self._tenant_lock_key)
        pool = self._get_copilot_pool()
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT copilot.lock_tenant(%s, %s, %s)",
                        (tenant, issued_at, mac),
                    )
                    yield cur

    def checkpoint_pool(self) -> ConnectionPool[psycopg.Connection[DictRow]]:
        """Dedicated `copilot_app` pool (`autocommit=True`, `dict_row`) for `PostgresSaver`."""
        with self._lock:
            if self._checkpoint_pool is None:
                self._checkpoint_pool = cast(
                    ConnectionPool[psycopg.Connection[DictRow]],
                    ConnectionPool(
                        conninfo=self._copilot_dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=True,
                        check=ConnectionPool.check_connection,
                        reset=_reset_pooled_connection,
                        kwargs={
                            "autocommit": True,
                            "prepare_threshold": None,
                            "row_factory": dict_row,
                        },
                    ),
                )
            return self._checkpoint_pool

    def close(self) -> None:
        """Close open connection pools cleanly."""
        with self._lock:
            if self._telemetry_pool is not None:
                self._telemetry_pool.close()
                self._telemetry_pool = None
            if self._copilot_pool is not None:
                self._copilot_pool.close()
                self._copilot_pool = None
            if self._checkpoint_pool is not None:
                self._checkpoint_pool.close()
                self._checkpoint_pool = None


def make_checkpointer(
    db: DatabaseManager | None = None,
    *,
    settings: Settings | None = None,
) -> PostgresSaver:
    """Create a sync `PostgresSaver` with `EncryptedSerializer.from_pycryptodome_aes()`."""
    cfg = settings if settings is not None else get_settings()
    aes_secret = cfg.langgraph_aes_key
    key_str = (
        aes_secret.get_secret_value() if aes_secret is not None else os.getenv("LANGGRAPH_AES_KEY")
    )
    if not key_str or not key_str.strip():
        raise ValueError(
            "LANGGRAPH_AES_KEY must be configured (16, 24, or 32 UTF-8 bytes, e.g. 32 hex chars)"
        )
    key_bytes = key_str.strip().encode("utf-8")
    if len(key_bytes) not in (16, 24, 32):
        raise ValueError(
            f"LANGGRAPH_AES_KEY must encode to 16, 24, or 32 bytes (got {len(key_bytes)})"
        )
    serde = EncryptedSerializer.from_pycryptodome_aes(key=key_bytes)
    manager = db if db is not None else get_db_manager()
    pool = cast(Any, manager.checkpoint_pool())
    return PostgresSaver(pool, serde=serde)


@lru_cache(maxsize=1)
def get_db_manager() -> DatabaseManager:
    """Return a singleton DatabaseManager wired from application Settings."""
    settings: Settings = get_settings()
    return DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
    )
