"""HS256 JWT issuance and verification mapping tokens strictly to `TenantContext`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import HTTPException, Request, status

from fleet_copilot.config import Settings
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.demo_auth import DemoSession, to_tenant_context

SESSION_TTL_SECONDS = 8 * 3600


def _signing_key(settings: Settings) -> str:
    key = settings.session_signing_key
    if key is None or not key.get_secret_value().strip():
        raise RuntimeError(
            "SESSION_SIGNING_KEY must be configured before issuing or verifying API tokens."
        )
    return key.get_secret_value().strip()


def issue_session_token(
    session: DemoSession,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """Issue a signed HS256 JWT (8 h expiry) encoding actor, company, role, and reviewer id."""
    issued = now if now is not None else datetime.now(UTC)
    expires = issued + timedelta(seconds=SESSION_TTL_SECONDS)
    claims: dict[str, Any] = {
        "sub": session.actor_id,
        "company_id": session.company_id,
        "role": session.role,
        "rev_id": session.rev_id,
        "persona": session.persona,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return str(jwt.encode(claims, _signing_key(settings), algorithm="HS256"))


def decode_session_token(
    token: str,
    *,
    settings: Settings,
) -> DemoSession:
    """Verify HS256 JWT signature and expiry, returning a `DemoSession` or raising 401."""
    try:
        payload = jwt.decode(
            token,
            _signing_key(settings),
            algorithms=["HS256"],
            options={"require": ["sub", "company_id", "role", "rev_id", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        ) from exc

    role = payload.get("role")
    if role not in ("viewer", "it_admin"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session role claim.",
        )

    return DemoSession(
        rev_id=str(payload["rev_id"]),
        persona=str(payload.get("persona") or ""),
        company_id=str(payload["company_id"]),
        role=role,
        actor_id=str(payload["sub"]),
    )


def require_tenant_context(request: Request, settings: Settings) -> TenantContext:
    """Extract Bearer JWT from `Authorization` header and derive a verified `TenantContext`."""
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    session = decode_session_token(token, settings=settings)
    trace_id = str(getattr(request.state, "trace_id", "") or "api-unknown")
    return to_tenant_context(session, trace_id=trace_id)
