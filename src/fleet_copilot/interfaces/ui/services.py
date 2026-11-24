"""Process-wide cached service container (`cache_resource`) and read caches (`cache_data`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st

from fleet_copilot.clock import Clock, SystemClock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db.postgres import DatabaseManager, get_db_manager, make_checkpointer
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.observability.logging import configure_logging
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.briefing import Briefing, build_briefing
from fleet_copilot.services.charts import ChartService
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.demo_auth import PersonaSpec, load_personas, validate_auth_config
from fleet_copilot.services.guarded import GuardedCopilot
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.turn_jobs import TurnJobs


@dataclass
class UIContainer:
    """In-process backend services shared across Streamlit reruns (built once per process)."""

    settings: Settings
    db: DatabaseManager | None
    telemetry_repo: Any
    store: Any
    policy_svc: PolicyService
    audit_svc: Any
    copilot: CopilotService
    guarded: GuardedCopilot
    chart_svc: ChartService
    turn_jobs: TurnJobs
    clock: Clock


_CONTAINER_OVERRIDE: UIContainer | None = None


def set_ui_container_override(container: UIContainer | None) -> None:
    """Inject a test container (used by AppTest smoke tests); pass `None` to restore."""
    global _CONTAINER_OVERRIDE
    _CONTAINER_OVERRIDE = container
    cached_telemetry_as_of.clear()
    cached_briefing_dict.clear()
    cached_hostname_map.clear()
    cached_thread_list.clear()
    cached_thread_turns.clear()


@st.cache_resource(show_spinner=False)
def _build_default_container() -> UIContainer:
    settings = get_settings()
    configure_logging(log_format=settings.log_format, log_level=settings.log_level)
    validate_auth_config(settings)

    db = get_db_manager()
    repo = PgTelemetryRepository(db, settings=settings)
    store = PgCopilotStore(db)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db)
    clock = SystemClock()
    copilot = CopilotService(
        db=db,
        telemetry_repo=repo,
        copilot_store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        adapter=MockAdapter(),
        checkpointer=make_checkpointer(db, settings=settings),
        clock=clock,
        settings=settings,
    )
    guarded = GuardedCopilot(copilot, settings=settings, clock=clock)
    chart_svc = ChartService(
        telemetry_repo=repo,
        policy_svc=policy_svc,
        store=store,
        audit_svc=audit_svc,
    )
    turn_jobs = TurnJobs(max_workers=4, ttl_seconds=900, clock=clock)
    return UIContainer(
        settings=settings,
        db=db,
        telemetry_repo=repo,
        store=store,
        policy_svc=policy_svc,
        audit_svc=audit_svc,
        copilot=copilot,
        guarded=guarded,
        chart_svc=chart_svc,
        turn_jobs=turn_jobs,
        clock=clock,
    )


def get_ui_container() -> UIContainer:
    """Return the process-cached `UIContainer` (or active test override)."""
    if _CONTAINER_OVERRIDE is not None:
        return _CONTAINER_OVERRIDE
    return _build_default_container()


@st.cache_data(ttl=60, show_spinner=False)
def cached_personas() -> dict[str, PersonaSpec]:
    """Load persona catalog from `config/personas.yaml`."""
    return load_personas()


@st.cache_data(ttl=60, show_spinner=False)
def cached_telemetry_as_of(company_id: str) -> str:
    """Fetch the latest telemetry timestamp for `company_id` (cached 60 s)."""
    container = get_ui_container()
    dt = container.telemetry_repo.tenant_as_of(company_id)
    return dt.isoformat()


@st.cache_data(ttl=60, show_spinner=False)
def cached_briefing_dict(
    company_id: str,
    telemetry_as_of: str,
    role: str,
    actor_id: str,
) -> dict[str, Any]:
    """Compute and cache the deterministic Fleet Briefing keyed by `(company_id, as_of, role)`."""
    _ = telemetry_as_of  # explicit cache key discriminator
    container = get_ui_container()
    ctx = TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role="it_admin" if role == "it_admin" else "viewer",
        trace_id=f"ui-brief-{company_id}",
    )
    brief = build_briefing(ctx, container.telemetry_repo, container.store, container.policy_svc)
    return brief.model_dump(mode="json")


def get_cached_briefing(
    company_id: str,
    telemetry_as_of: str,
    role: str,
    actor_id: str,
) -> Briefing:
    """Return a validated `Briefing` object from the 60 s read cache."""
    if _CONTAINER_OVERRIDE is not None:
        ctx = TenantContext(
            company_id=company_id,
            actor_id=actor_id,
            role="it_admin" if role == "it_admin" else "viewer",
            trace_id=f"ui-brief-{company_id}",
        )
        return build_briefing(
            ctx,
            _CONTAINER_OVERRIDE.telemetry_repo,
            _CONTAINER_OVERRIDE.store,
            _CONTAINER_OVERRIDE.policy_svc,
        )
    data = cached_briefing_dict(company_id, telemetry_as_of, role, actor_id)
    return Briefing.model_validate(data)


@st.cache_data(ttl=60, show_spinner=False)
def cached_hostname_map(company_id: str, telemetry_as_of: str) -> dict[str, str]:
    """Return `{device_id: os_hostname}` for `company_id`, keyed by `(company_id, as_of)`."""
    _ = telemetry_as_of
    container = get_ui_container()
    devices = container.telemetry_repo.latest_states(company_id)
    return {d.device_id: (d.os_hostname or d.device_id) for d in devices}


def get_hostname_map(company_id: str, telemetry_as_of: str) -> dict[str, str]:
    """Resolve `{device_id: hostname}` using cache in production or live container in tests."""
    if _CONTAINER_OVERRIDE is not None:
        devices = _CONTAINER_OVERRIDE.telemetry_repo.latest_states(company_id)
        return {d.device_id: (d.os_hostname or d.device_id) for d in devices}
    return cached_hostname_map(company_id, telemetry_as_of)


@st.cache_data(ttl=30, show_spinner=False)
def cached_thread_list(
    company_id: str,
    actor_id: str,
    role: str,
    version: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Cache actor's thread list keyed by `(company_id, actor_id, version, search)`."""
    _ = version
    container = get_ui_container()
    ctx = TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role="it_admin" if role == "it_admin" else "viewer",
        trace_id=f"ui-threads-{company_id}",
    )
    return container.guarded.list_threads(ctx, search=search, limit=50)


@st.cache_data(ttl=30, show_spinner=False)
def cached_thread_turns(
    company_id: str,
    actor_id: str,
    role: str,
    thread_id: str,
    thread_updated_at: str,
) -> dict[str, Any] | None:
    """Cache stored thread turns keyed by `(company_id, actor_id, thread_id, updated_at)`."""
    _ = thread_updated_at
    container = get_ui_container()
    ctx = TenantContext(
        company_id=company_id,
        actor_id=actor_id,
        role="it_admin" if role == "it_admin" else "viewer",
        trace_id=f"ui-thread-{thread_id[:12]}",
    )
    return container.guarded.get_thread(ctx, thread_id)
