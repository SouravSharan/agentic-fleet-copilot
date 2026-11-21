"""Application configuration loaded from environment variables / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """12-factor configuration for local Docker, VPS, or Supabase/Railway deployments."""

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

    # Role passwords synchronized by scripts/db_setup.py
    telemetry_writer_password: str = Field(default="telemetry_writer")
    telemetry_ro_password: str = Field(default="telemetry_ro")
    copilot_app_password: str = Field(default="copilot_app")

    # OpenAI-compatible LLM settings (used from Milestone 3 onward)
    llm_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="gemini-2.5-flash")
    llm_temperature: float = Field(default=0.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
