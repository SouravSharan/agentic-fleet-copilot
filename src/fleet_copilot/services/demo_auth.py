"""Demo persona registry and constant-time access-code sign-in.

`to_tenant_context()` is the sole sanctioned path for the UI and API to construct a
`TenantContext`, ensuring `company_id` and `role` come exclusively from the verified
persona catalog and never from arbitrary user input.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.config import Settings, get_settings
from fleet_copilot.domain.models import TenantContext

DEFAULT_PERSONAS_PATH = Path(__file__).resolve().parents[3] / "config" / "personas.yaml"
_GENERIC_AUTH_ERROR = "Invalid access code or persona."


class DemoAuthError(PermissionError):
    """Raised on invalid access code or unknown persona with an identical generic message."""


class DemoAuthConfigError(RuntimeError):
    """Raised when demo authentication configuration is missing or malformed."""


class PersonaSpec(BaseModel):
    """Static demo persona definition loaded from `config/personas.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    company_id: str
    role: Literal["viewer", "it_admin"]
    what_to_try: str
    suggested_prompts: list[str] = Field(default_factory=list)


class DemoSession(BaseModel):
    """Immutable authenticated demo session returned by `sign_in()`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rev_id: str
    persona: str
    company_id: str
    role: Literal["viewer", "it_admin"]
    actor_id: str
    label: str = ""


@lru_cache(maxsize=4)
def _load_personas_cached(path_str: str) -> dict[str, PersonaSpec]:
    path = Path(path_str)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("personas")
    if not isinstance(entries, dict) or not entries:
        raise DemoAuthConfigError(f"No personas configured in {path}")
    result: dict[str, PersonaSpec] = {}
    for persona_id, body in entries.items():
        if not isinstance(body, dict):
            raise DemoAuthConfigError(f"Invalid persona entry {persona_id!r} in {path}")
        spec = PersonaSpec(id=str(persona_id), **body)
        result[spec.id] = spec
    return result


def load_personas(path: Path | None = None) -> dict[str, PersonaSpec]:
    """Load persona definitions from `config/personas.yaml`."""
    target = (path or DEFAULT_PERSONAS_PATH).resolve()
    return dict(_load_personas_cached(str(target)))


def parse_access_codes(raw: str, *, app_env: str) -> dict[str, str]:
    """Parse `DEMO_ACCESS_CODES` (`code:rev_id,...`) enforcing dev/prod startup rules."""
    env_norm = (app_env or "dev").strip().lower()
    mapping: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        if ":" not in item:
            raise DemoAuthConfigError(
                "Malformed DEMO_ACCESS_CODES entry; expected 'code:rev_id' pairs."
            )
        code, rev_id = item.split(":", 1)
        code = code.strip()
        rev_id = rev_id.strip()
        if not code or not rev_id:
            raise DemoAuthConfigError(
                "Malformed DEMO_ACCESS_CODES entry; both code and rev_id must be non-empty."
            )
        mapping[code] = rev_id

    if not mapping:
        if env_norm == "dev":
            return {"dev": "rev_dev"}
        raise DemoAuthConfigError(
            f"DEMO_ACCESS_CODES must be configured when APP_ENV={env_norm!r}."
        )
    return mapping


def validate_auth_config(settings: Settings | None = None) -> dict[str, str]:
    """Validate access-code config at startup; raises `DemoAuthConfigError` in prod if empty."""
    cfg = settings if settings is not None else get_settings()
    return parse_access_codes(cfg.demo_access_codes, app_env=cfg.app_env)


def sign_in(
    code: str,
    persona: str,
    *,
    settings: Settings | None = None,
    personas: Mapping[str, PersonaSpec] | None = None,
) -> DemoSession:
    """Authenticate an access code and persona in constant time, returning a `DemoSession`."""
    cfg = settings if settings is not None else get_settings()
    codes = parse_access_codes(cfg.demo_access_codes, app_env=cfg.app_env)
    catalog = dict(personas) if personas is not None else load_personas()

    provided = (code or "").strip().encode("utf-8")
    matched_rev_id: str | None = None
    for valid_code, rev_id in codes.items():
        if hmac.compare_digest(provided, valid_code.encode("utf-8")):
            matched_rev_id = rev_id

    spec = catalog.get((persona or "").strip())
    if matched_rev_id is None or spec is None:
        raise DemoAuthError(_GENERIC_AUTH_ERROR)

    return DemoSession(
        rev_id=matched_rev_id,
        persona=spec.id,
        company_id=spec.company_id,
        role=spec.role,
        actor_id=f"{matched_rev_id}:{spec.id}",
        label=spec.label,
    )


def to_tenant_context(session: DemoSession, trace_id: str) -> TenantContext:
    """Derive a `TenantContext` strictly from a verified `DemoSession`."""
    return TenantContext(
        company_id=session.company_id,
        actor_id=session.actor_id,
        role=session.role,
        trace_id=trace_id,
    )
