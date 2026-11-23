"""Unit tests for configuration URL database swapping and test DB overrides."""

from __future__ import annotations

from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from fleet_copilot.config import Settings, test_database_overrides, with_database

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_env_example_has_dev_keys_and_demo_vars() -> None:
    """A4: .env.example supplies non-empty dev AES/pseudonym keys (rotate for hosting) + DEMO_*."""
    env_example = REPO_ROOT / ".env.example"
    raw = env_example.read_text(encoding="utf-8")
    assert "rotate for hosting" in raw.lower()

    kv: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        kv[k.strip()] = v.strip()

    assert len(kv.get("LANGGRAPH_AES_KEY", "").encode("utf-8")) == 32
    assert len(kv.get("PSEUDONYM_KEY", "")) == 64
    assert len(kv.get("TENANT_LOCK_KEY", "")) == 64
    assert float(kv.get("DEMO_SPEND_CAP_USD", "0")) > 0
    assert int(kv.get("DEMO_TURNS_PER_ACCESS_CODE", "0")) > 0

    parsed = Settings(
        _env_file=None,
        langgraph_aes_key=kv["LANGGRAPH_AES_KEY"],  # type: ignore[arg-type]
        pseudonym_key=kv["PSEUDONYM_KEY"],  # type: ignore[arg-type]
        tenant_lock_key=kv["TENANT_LOCK_KEY"],  # type: ignore[arg-type]
        demo_spend_cap_usd=float(kv["DEMO_SPEND_CAP_USD"]),
        demo_turns_per_access_code=int(kv["DEMO_TURNS_PER_ACCESS_CODE"]),
    )
    assert parsed.langgraph_aes_key is not None
    assert parsed.pseudonym_key is not None
    assert parsed.tenant_lock_key is not None
    assert parsed.demo_spend_cap_usd == 3.0
    assert parsed.demo_turns_per_access_code == 25


def test_makefile_has_bootstrap_target() -> None:
    """A4: Makefile defines .PHONY bootstrap running up, migrate, seed, test-db, eval."""
    mk = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    phony_line = next(line for line in mk.splitlines() if line.startswith(".PHONY:"))
    assert "bootstrap" in phony_line.split()
    assert "bootstrap: up migrate seed test-db eval" in mk
