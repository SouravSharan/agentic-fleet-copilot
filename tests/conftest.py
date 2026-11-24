"""Pytest configuration: isolated test database routing, directory markers, DB probe."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from fleet_copilot.config import Settings, get_settings, test_database_overrides

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Route all tests to the isolated test database (never the dev `fleet_copilot` DB).
_BASE_SETTINGS = Settings()
for _env_key, _env_val in test_database_overrides(_BASE_SETTINGS).items():
    os.environ[_env_key] = _env_val
# Provide deterministic fallback test keys for CI / environments without a local .env.
if _BASE_SETTINGS.pseudonym_key is None and not os.environ.get("PSEUDONYM_KEY"):
    os.environ["PSEUDONYM_KEY"] = "0123456789abcdef" * 4
if _BASE_SETTINGS.tenant_lock_key is None and not os.environ.get("TENANT_LOCK_KEY"):
    os.environ["TENANT_LOCK_KEY"] = "fedcba9876543210" * 4
if _BASE_SETTINGS.langgraph_aes_key is None and not os.environ.get("LANGGRAPH_AES_KEY"):
    os.environ["LANGGRAPH_AES_KEY"] = "0123456789abcdef" * 2
if _BASE_SETTINGS.session_signing_key is None and not os.environ.get("SESSION_SIGNING_KEY"):
    os.environ["SESSION_SIGNING_KEY"] = "a1b2c3d4e5f60718" * 4
get_settings.cache_clear()

_DIR_MARKERS = {
    "unit": "unit",
    "integration": "integration",
    "graph": "graph",
    "api": "api",
    "ui": "ui",
    "eval": "eval",
}

_DB_MARKERS = {"integration", "api"}


def _probe_test_db() -> tuple[bool, str]:
    dsn = get_settings().admin_database_url
    try:
        with psycopg.connect(dsn, connect_timeout=2, prepare_threshold=None) as conn:
            conn.execute("SELECT 1")
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-assign markers by directory and skip/error DB tests when Postgres is unreachable."""
    tests_root = Path(__file__).resolve().parent
    db_items: list[pytest.Item] = []

    for item in items:
        try:
            rel_parts = item.path.resolve().relative_to(tests_root).parts
        except ValueError:
            rel_parts = ()

        if rel_parts and rel_parts[0] in _DIR_MARKERS:
            marker_name = _DIR_MARKERS[rel_parts[0]]
            item.add_marker(getattr(pytest.mark, marker_name))

        if any(item.get_closest_marker(m) is not None for m in _DB_MARKERS):
            db_items.append(item)

    if not db_items:
        return

    ok, err = _probe_test_db()
    if ok:
        return

    if os.environ.get("REQUIRE_DB") == "1":
        raise pytest.UsageError(
            f"REQUIRE_DB=1 but test database is unreachable ({err}). Run `make up test-db`."
        )

    skip_marker = pytest.mark.skip(reason="Postgres unreachable; run `make up test-db`")
    for item in db_items:
        item.add_marker(skip_marker)


@pytest.fixture()
def reset_copilot_state() -> Iterator[None]:
    """Truncate copilot operational tables and audit chain heads on the test DB before each test."""
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute(
                """
                TRUNCATE TABLE
                    copilot.charts,
                    copilot.action_proposals,
                    copilot.turns,
                    copilot.threads,
                    copilot.audit_logs,
                    copilot.audit_chain_heads
                RESTART IDENTITY CASCADE
                """
            )
    yield


@pytest.fixture()
def restore_telemetry_baseline() -> Iterator[None]:
    """Restore the 750-row telemetry baseline on the test DB after mutating tests."""
    from scripts.telemetry_sim import reset_dataset

    yield
    reset_dataset(writer_url=get_settings().telemetry_writer_database_url)
