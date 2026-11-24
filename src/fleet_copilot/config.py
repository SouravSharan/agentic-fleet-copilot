"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ENV = Path(__file__).resolve().parents[2] / ".env"


def with_database(url: str, dbname: str) -> str:
    """Return a psycopg conninfo string identical to `url` except for `dbname`."""
    if not dbname or not dbname.strip():
        raise ValueError("dbname must be a non-empty string")
    params = conninfo_to_dict(url)
    params["dbname"] = dbname
    return make_conninfo("", **params)


class Settings(BaseSettings):
    """Configuration for local dev, CI, and Docker deployments."""

    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ENV), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL connections (separate roles per security boundary)
    admin_database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/fleet_copilot"
    )
    telemetry_writer_database_url: str = Field(
        default="postgresql://telemetry_writer:telemetry_writer@localhost:5432/fleet_copilot"
    )
    telemetry_ro_database_url: str = Field(
        default="postgresql://telemetry_ro:telemetry_ro@localhost:5432/fleet_copilot"
    )
    copilot_database_url: str = Field(
        default="postgresql://copilot_app:copilot_app@localhost:5432/fleet_copilot"
    )
    demo_ops_database_url: str = Field(
        default="postgresql://demo_ops:demo_ops@localhost:5432/fleet_copilot"
    )

    # Role passwords synchronized by scripts/db_setup.py
    telemetry_writer_password: str = Field(default="telemetry_writer")
    telemetry_ro_password: str = Field(default="telemetry_ro")
    copilot_app_password: str = Field(default="copilot_app")
    demo_ops_password: str = Field(default="demo_ops")

    # Testing & data-clock controls
    test_db_name: str = Field(default="fleet_copilot_test")
    fleet_as_of: datetime | None = Field(default=None)

    # LLM & cassette replay configuration
    llm_model: str = Field(default="google_genai:gemini-3.8-flash")
    gemini_api_key: SecretStr | None = Field(default=None)
    llm_replay_mode: str = Field(default="off")
    llm_cassette_dir: str = Field(default="evals/cassettes")

    # Actions kill-switch & privacy keys
    actions_enabled: bool = Field(default=True)
    pseudonym_key: SecretStr | None = Field(default=None)
    tenant_lock_key: SecretStr | None = Field(default=None)
    langgraph_aes_key: SecretStr | None = Field(default=None)

    # Ops logging
    log_format: str = Field(default="json")
    log_level: str = Field(default="INFO")

    # Demo API / UI auth, mode & budgets
    app_env: str = Field(default="dev")
    demo_mode: bool = Field(default=True)
    session_signing_key: SecretStr | None = Field(default=None)
    demo_access_codes: str = Field(default="")
    demo_admin_token: SecretStr | None = Field(default=None)
    demo_spend_cap_usd: float = Field(default=2.00)
    demo_turns_per_code: int = Field(
        default=25,
        validation_alias=AliasChoices("DEMO_TURNS_PER_CODE", "DEMO_TURNS_PER_ACCESS_CODE"),
    )
    demo_turns_per_hour: int = Field(
        default=20,
        validation_alias=AliasChoices("DEMO_TURNS_PER_HOUR", "RATE_LIMIT_TURNS_PER_HOUR"),
    )
    daily_token_budget: int = Field(default=2_000_000)
    turn_retention_days: int = Field(default=7)

    @property
    def demo_turns_per_access_code(self) -> int:
        return self.demo_turns_per_code

    @property
    def rate_limit_turns_per_hour(self) -> int:
        return self.demo_turns_per_hour

    @field_validator("fleet_as_of", mode="before")
    @classmethod
    def _empty_datetime_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "gemini_api_key",
        "pseudonym_key",
        "tenant_lock_key",
        "langgraph_aes_key",
        "session_signing_key",
        "demo_admin_token",
        mode="before",
    )
    @classmethod
    def _empty_secret_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


def test_database_overrides(settings: Settings, name: str | None = None) -> dict[str, str]:
    """Build environment variable overrides pointing all four DB URLs at `name`."""
    dbname = name or settings.test_db_name
    return {
        "ADMIN_DATABASE_URL": with_database(settings.admin_database_url, dbname),
        "TELEMETRY_WRITER_DATABASE_URL": with_database(
            settings.telemetry_writer_database_url, dbname
        ),
        "TELEMETRY_RO_DATABASE_URL": with_database(settings.telemetry_ro_database_url, dbname),
        "COPILOT_DATABASE_URL": with_database(settings.copilot_database_url, dbname),
        "DEMO_OPS_DATABASE_URL": with_database(settings.demo_ops_database_url, dbname),
    }


test_database_overrides.__test__ = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
