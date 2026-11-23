"""Unit tests for configuration URL database swapping and test DB overrides."""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

from fleet_copilot.config import Settings, test_database_overrides, with_database


def test_with_database_swaps_only_dbname() -> None:
    original = "postgresql://telemetry_ro:secret_pw@db.example.com:5433/fleet_copilot"
    swapped = with_database(original, "fleet_copilot_test")

    orig_dict = conninfo_to_dict(original)
    swap_dict = conninfo_to_dict(swapped)

    assert swap_dict["dbname"] == "fleet_copilot_test"
    for key in ("user", "password", "host", "port"):
        assert swap_dict[key] == orig_dict[key]


def test_test_overrides_cover_all_four_urls() -> None:
    settings = Settings(
        admin_database_url="postgresql://postgres:p1@localhost:5432/fleet_copilot",
        telemetry_writer_database_url="postgresql://telemetry_writer:p2@localhost:5432/fleet_copilot",
        telemetry_ro_database_url="postgresql://telemetry_ro:p3@localhost:5432/fleet_copilot",
        copilot_database_url="postgresql://copilot_app:p4@localhost:5432/fleet_copilot",
        test_db_name="custom_test_db",
    )
    overrides = test_database_overrides(settings, "custom_test_db")

    expected_keys = {
        "ADMIN_DATABASE_URL",
        "TELEMETRY_WRITER_DATABASE_URL",
        "TELEMETRY_RO_DATABASE_URL",
        "COPILOT_DATABASE_URL",
    }
    assert set(overrides.keys()) == expected_keys
    for env_key, conninfo in overrides.items():
        parsed = conninfo_to_dict(conninfo)
        assert parsed["dbname"] == "custom_test_db", env_key
        assert parsed["host"] == "localhost"
        assert parsed["port"] == "5432"
