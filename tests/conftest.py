"""Pytest configuration: isolated test database routing, directory markers, DB probe."""

from __future__ import annotations

import os
import sys
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
            if marker_name in _DB_MARKERS:
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
