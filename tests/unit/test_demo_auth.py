"""Unit tests for demo personas, constant-time access-code sign-in, and TenantContext derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fleet_copilot.config import Settings
from fleet_copilot.services.demo_auth import (
    DemoAuthConfigError,
    DemoAuthError,
    load_personas,
    sign_in,
    to_tenant_context,
    validate_auth_config,
)


def test_personas_yaml_defines_four_expected_personas() -> None:
    personas = load_personas()
    assert set(personas.keys()) == {
        "acme_admin",
        "acme_viewer",
        "globex_admin",
        "initech_admin",
    }
    assert personas["acme_admin"].company_id == "acme-001"
    assert personas["acme_admin"].role == "it_admin"
    assert personas["acme_viewer"].company_id == "acme-001"
    assert personas["acme_viewer"].role == "viewer"
    assert personas["globex_admin"].company_id == "globex-002"
    assert personas["globex_admin"].role == "it_admin"
    assert personas["initech_admin"].company_id == "initech-003"
    assert personas["initech_admin"].role == "it_admin"

    for spec in personas.values():
        assert spec.label.strip()
        assert spec.what_to_try.strip()
        assert len(spec.suggested_prompts) == 3
        assert all(p.strip() for p in spec.suggested_prompts)


def test_valid_sign_in_produces_scoped_actor_id_and_tenant_context() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="prod",
        demo_access_codes="code-abc:rev_01,code-def:rev_02",
    )
    session = sign_in("code-abc", "acme_admin", settings=settings)
    assert session.rev_id == "rev_01"
    assert session.persona == "acme_admin"
    assert session.company_id == "acme-001"
    assert session.role == "it_admin"
    assert session.actor_id == "rev_01:acme_admin"

    # Second reviewer on the same persona gets a distinct actor_id
    session2 = sign_in("code-def", "acme_admin", settings=settings)
    assert session2.actor_id == "rev_02:acme_admin"
    assert session2.actor_id != session.actor_id

    ctx = to_tenant_context(session, trace_id="trace-demo-1")
    assert ctx.company_id == "acme-001"
    assert ctx.actor_id == "rev_01:acme_admin"
    assert ctx.role == "it_admin"
    assert ctx.trace_id == "trace-demo-1"


def test_wrong_code_and_unknown_persona_return_identical_error() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="prod",
        demo_access_codes="code-abc:rev_01",
    )
    with pytest.raises(DemoAuthError) as wrong_code_exc:
        sign_in("wrong-code", "acme_admin", settings=settings)
    with pytest.raises(DemoAuthError) as bad_persona_exc:
        sign_in("code-abc", "nonexistent_persona", settings=settings)

    assert str(wrong_code_exc.value) == str(bad_persona_exc.value)


def test_dev_mode_allows_dev_code_when_no_codes_configured() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="dev",
        demo_access_codes="",
    )
    session = sign_in("dev", "globex_admin", settings=settings)
    assert session.rev_id == "rev_dev"
    assert session.company_id == "globex-002"
    assert session.actor_id == "rev_dev:globex_admin"

    with pytest.raises(DemoAuthError):
        sign_in("anything-else", "globex_admin", settings=settings)


def test_prod_without_access_codes_refuses_to_start() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="prod",
        demo_access_codes="",
    )
    with pytest.raises(DemoAuthConfigError):
        validate_auth_config(settings)
    with pytest.raises(DemoAuthConfigError):
        sign_in("dev", "acme_admin", settings=settings)


def test_session_and_tenant_cannot_be_overridden_by_input() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="prod",
        demo_access_codes="code-abc:rev_01",
    )
    # Trying to smuggle a different company or role via persona or code fails closed
    with pytest.raises(DemoAuthError):
        sign_in("code-abc", "acme-001", settings=settings)
    with pytest.raises(DemoAuthError):
        sign_in("code-abc:globex-002", "acme_admin", settings=settings)

    # DemoSession is frozen so company_id/role cannot be mutated after sign_in
    session = sign_in("code-abc", "acme_viewer", settings=settings)
    with pytest.raises(ValidationError):
        session.company_id = "globex-002"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        session.role = "it_admin"  # type: ignore[misc]
