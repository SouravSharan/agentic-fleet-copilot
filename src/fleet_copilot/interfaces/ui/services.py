"""Process-wide cached service container (`cache_resource`) and read caches (`cache_data`)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
    cached_resolved_policies.clear()
    cached_hostname_map.clear()
    cached_device_summaries.clear()
    cached_briefing_software_rows.clear()
    cached_fleet_devices.clear()
    cached_device_detail.clear()
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
def cached_resolved_policies(company_id: str, telemetry_as_of: str) -> dict[str, Any]:
    """Resolve effective policy thresholds for `company_id`, keyed by `(company_id, as_of)`."""
    _ = telemetry_as_of
    container = get_ui_container()
    ctx = TenantContext(
        company_id=company_id,
        actor_id="ui:policy_reader",
        role="viewer",
        trace_id=f"ui-pol-{company_id}",
    )
    resolved = container.policy_svc.resolve(ctx)
    return {k: v.value for k, v in resolved.items()}


def get_resolved_policies(company_id: str, telemetry_as_of: str) -> dict[str, Any]:
    """Return `{metric_key: value}` using cache in production or live container in tests."""
    if _CONTAINER_OVERRIDE is not None:
        ctx = TenantContext(
            company_id=company_id,
            actor_id="ui:policy_reader",
            role="viewer",
            trace_id=f"ui-pol-{company_id}",
        )
        return {k: v.value for k, v in _CONTAINER_OVERRIDE.policy_svc.resolve(ctx).items()}
    return cached_resolved_policies(company_id, telemetry_as_of)


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


@st.cache_data(ttl=60, show_spinner=False)
def cached_device_summaries(company_id: str, telemetry_as_of: str) -> dict[str, dict[str, Any]]:
    """Return compact current device metadata keyed by `device_id` for evidence display."""
    _ = telemetry_as_of
    container = get_ui_container()
    devices = container.telemetry_repo.latest_states(company_id)
    return {
        d.device_id: {
            "hostname": d.os_hostname or d.device_id,
            "model": d.model_name or "",
            "form_factor": d.form_factor,
            "battery_present": bool(d.battery_present),
            "battery_condition": d.battery_condition or "",
            "battery_cycles": d.battery_cycle_count,
        }
        for d in devices
    }


def get_device_summaries(company_id: str, telemetry_as_of: str) -> dict[str, dict[str, Any]]:
    """Resolve compact current device summaries (bypassing cache under test container overrides)."""
    if _CONTAINER_OVERRIDE is not None:
        devices = _CONTAINER_OVERRIDE.telemetry_repo.latest_states(company_id)
        return {
            d.device_id: {
                "hostname": d.os_hostname or d.device_id,
                "model": d.model_name or "",
                "form_factor": d.form_factor,
                "battery_present": bool(d.battery_present),
                "battery_condition": d.battery_condition or "",
                "battery_cycles": d.battery_cycle_count,
            }
            for d in devices
        }
    return cached_device_summaries(company_id, telemetry_as_of)


@st.cache_data(ttl=60, show_spinner=False)
def cached_briefing_software_rows(company_id: str, telemetry_as_of: str) -> list[dict[str, Any]]:
    """Fetch deterministic non-baseline software table rows for the Home briefing card."""
    from fleet_copilot.services.trends import analyze_fleet_trends

    _ = telemetry_as_of
    container = get_ui_container()
    repo = container.telemetry_repo
    hosts = {d.device_id: (d.os_hostname or d.device_id) for d in repo.latest_states(company_id)}
    ctx = TenantContext(
        company_id=company_id,
        actor_id="ui:brief_software",
        role="viewer",
        trace_id=f"ui-sw-{company_id}",
    )
    sw_res = analyze_fleet_trends(ctx, ["software"], repo, container.policy_svc)
    rows: list[dict[str, Any]] = []
    for ins in sw_res.data:
        stats = ins.get("stats") or {}
        did = str(ins.get("device_id") or "")
        rows.append(
            {
                "Device": hosts.get(did, did),
                "Software": str(stats.get("software_name") or ""),
                "First seen": str(stats.get("first_seen_date") or "—"),
            }
        )
    rows.sort(key=lambda r: (r["First seen"], r["Device"], r["Software"]))
    return rows


def get_briefing_software_rows(company_id: str, telemetry_as_of: str) -> list[dict[str, Any]]:
    """Return non-baseline software table rows (live in test override, cached 60 s otherwise)."""
    if _CONTAINER_OVERRIDE is not None:
        from fleet_copilot.services.trends import analyze_fleet_trends

        repo = _CONTAINER_OVERRIDE.telemetry_repo
        hosts = {
            d.device_id: (d.os_hostname or d.device_id) for d in repo.latest_states(company_id)
        }
        ctx = TenantContext(
            company_id=company_id,
            actor_id="ui:brief_software",
            role="viewer",
            trace_id=f"ui-sw-{company_id}",
        )
        sw_res = analyze_fleet_trends(ctx, ["software"], repo, _CONTAINER_OVERRIDE.policy_svc)
        rows = [
            {
                "Device": hosts.get(
                    str(ins.get("device_id") or ""), str(ins.get("device_id") or "")
                ),
                "Software": str((ins.get("stats") or {}).get("software_name") or ""),
                "First seen": str((ins.get("stats") or {}).get("first_seen_date") or "—"),
            }
            for ins in sw_res.data
        ]
        rows.sort(key=lambda r: (r["First seen"], r["Device"], r["Software"]))
        return rows
    return cached_briefing_software_rows(company_id, telemetry_as_of)


@st.cache_data(ttl=60, show_spinner=False)
def cached_fleet_devices(company_id: str, telemetry_as_of: str) -> list[dict[str, Any]]:
    """Build cached fleet table rows keyed by `(company_id, telemetry_as_of)`."""
    from datetime import timedelta

    from fleet_copilot.services.briefing import build_briefing
    from fleet_copilot.services.staleness import resolve_stale_device_days, stale_days_ago

    _ = telemetry_as_of
    container = get_ui_container()
    repo = container.telemetry_repo
    as_of = repo.tenant_as_of(company_id)
    devices = sorted(repo.latest_states(company_id), key=lambda d: d.os_hostname or d.device_id)
    latest_comp = repo.compliance(company_id, latest_only=True)

    fail_counts: dict[str, int] = {}
    for row in latest_comp:
        if row.status == "fail":
            fail_counts[row.device_id] = fail_counts.get(row.device_id, 0) + 1

    ctx = TenantContext(
        company_id=company_id,
        actor_id="ui:fleet_reader",
        role="viewer",
        trace_id=f"ui-fleet-{company_id}",
    )
    policies = container.policy_svc.resolve(ctx)
    low_disk_thresh = float(
        policies["low_disk_free_pct"].value if "low_disk_free_pct" in policies else 15.0
    )
    stale_days = resolve_stale_device_days(policies)
    brief = build_briefing(ctx, repo, container.store, container.policy_svc)
    attention_ids = {
        did
        for item in brief.items
        if item.severity in ("high", "medium")
        for did in item.device_ids
    }
    _ = timedelta  # keep local import tidy

    rows: list[dict[str, Any]] = []
    for d in devices[:200]:
        stale_n = stale_days_ago(d, as_of, stale_days)
        is_stale = stale_n is not None
        if d.battery_present and d.battery_condition:
            cyc = f"{int(d.battery_cycle_count):,} cyc" if d.battery_cycle_count is not None else ""
            batt_str = f"{d.battery_condition} ({cyc})" if cyc else d.battery_condition
        elif d.form_factor == "desktop":
            batt_str = "Desktop (no battery)"
        else:
            batt_str = "Unavailable"

        os_label = f"{d.os_product_name or 'Unknown'} {d.os_product_version or ''}".strip()
        disk_pct = round(float(d.disk_free_pct), 1) if d.disk_free_pct is not None else None
        ram_pct = round(float(d.ram_used_pct), 1) if d.ram_used_pct is not None else None
        disk_low = disk_pct is not None and disk_pct < low_disk_thresh
        last_seen_date = d.collected_at.date().isoformat()
        last_seen_label = f"{last_seen_date} · Stale ({stale_n}d)" if is_stale else last_seen_date

        rows.append(
            {
                "device_id": d.device_id,
                "hostname": d.os_hostname or d.device_id,
                "model": d.model_name or "Unknown",
                "os": os_label,
                "os_family": (
                    "macOS"
                    if "mac" in os_label.lower() or (d.os_platform or "").lower() == "darwin"
                    else ("Windows" if "win" in os_label.lower() else "Other")
                ),
                "form_factor": d.form_factor,
                "disk_free_pct": disk_pct,
                "disk_low": disk_low,
                "ram_used_pct": ram_pct,
                "battery": batt_str,
                "failing_checks": fail_counts.get(d.device_id, 0),
                "last_seen": last_seen_label,
                "is_stale": is_stale,
                "needs_attention": d.device_id in attention_ids or is_stale or disk_low,
                "employee_id": d.employee_id,
            }
        )
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def cached_device_detail(
    company_id: str,
    telemetry_as_of: str,
    device_id: str,
) -> dict[str, Any] | None:
    """Fetch cached device profile, 30-day series, compliance timeline, and software list."""
    from datetime import timedelta

    from fleet_copilot.services.staleness import resolve_stale_device_days, stale_days_ago
    from fleet_copilot.services.trends import analyze_fleet_trends

    _ = telemetry_as_of
    container = get_ui_container()
    repo = container.telemetry_repo
    as_of = repo.tenant_as_of(company_id)
    dev = repo.resolve_device(company_id, device_id)
    if dev is None:
        return None

    ctx = TenantContext(
        company_id=company_id,
        actor_id="ui:device_reader",
        role="viewer",
        trace_id=f"ui-dev-{device_id}",
    )
    policies = container.policy_svc.resolve(ctx)
    stale_days = resolve_stale_device_days(policies)
    stale_n = stale_days_ago(dev, as_of, stale_days)

    since = as_of - timedelta(days=30)
    series_pts = repo.series(company_id, since, device_ids=[dev.device_id])
    comp_rows = repo.compliance(company_id, since=since, device_ids=[dev.device_id])
    sw_rows = repo.software(company_id, latest_only=True, device_ids=[dev.device_id])

    # Identify non-baseline apps from deterministic software trend analyzer
    sw_trends = analyze_fleet_trends(ctx, ["software"], repo, container.policy_svc)
    non_baseline_apps = {
        str((ins.get("stats") or {}).get("software_name") or "").lower()
        for ins in sw_trends.data
        if str(ins.get("device_id") or "") == dev.device_id
    }

    return {
        "profile": dev.model_dump(mode="json"),
        "as_of": as_of.isoformat(),
        "stale_days_ago": stale_n,
        "series": [
            {
                "date": pt.collected_at.date().isoformat(),
                "disk_free_pct": pt.disk_free_pct,
                "ram_used_pct": pt.ram_used_pct,
                "battery_cycles": pt.battery_cycle_count if pt.battery_present else None,
            }
            for pt in series_pts
        ],
        "compliance": [
            {
                "date": c.collected_at.date().isoformat(),
                "check_id": c.check_id,
                "status": c.status,
                "severity": c.severity,
            }
            for c in comp_rows
        ],
        "software": [
            {
                "name": sw.software_name,
                "version": sw.version or "—",
                "publisher": sw.publisher or "—",
                "non_baseline": sw.software_name.lower() in non_baseline_apps,
            }
            for sw in sorted(sw_rows, key=lambda s: s.software_name.lower())
        ],
    }


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


def examples_only_reason() -> str:
    """Short reason shown when live turns are unavailable (no API key vs. demo budget used)."""
    settings = get_ui_container().settings
    key = settings.gemini_api_key
    has_key = key is not None and bool(key.get_secret_value().strip())
    if settings.llm_replay_mode.strip().lower() != "replay" and not has_key:
        return "no API key"
    return "demo budget used"


def clear_company_read_caches() -> None:
    """Invalidate UI read caches after demo reset or scenario injection."""
    cached_telemetry_as_of.clear()
    cached_briefing_dict.clear()
    cached_resolved_policies.clear()
    cached_hostname_map.clear()
    cached_device_summaries.clear()
    cached_briefing_software_rows.clear()
    cached_fleet_devices.clear()
    cached_device_detail.clear()
    cached_thread_list.clear()
    cached_thread_turns.clear()


def run_ui_demo_reset(company_id: str, *, reason: str = "ui_demo_reset") -> dict[str, Any]:
    """Reset one company's demo state strictly via `demo_ops` (`DEMO_OPS_DATABASE_URL`).

    Never touches `ADMIN_DATABASE_URL` or `copilot.spend_ledger`.
    """
    from fleet_copilot.ops.demo_reset import reset_company

    container = get_ui_container()
    summary = reset_company(
        company_id,
        reason=reason,
        demo_ops_url=container.settings.demo_ops_database_url,
        use_admin=False,
    )
    clear_company_read_caches()
    return summary


def run_ui_inject_scenario(company_id: str, scenario: str) -> str:
    """Inject a demo telemetry scenario strictly via `demo_ops` (`DEMO_OPS_DATABASE_URL`)."""
    from fleet_copilot.ops.scenarios import inject_scenario_via_demo_ops

    container = get_ui_container()
    snap_id = inject_scenario_via_demo_ops(
        company_id,
        scenario,
        demo_ops_url=container.settings.demo_ops_database_url,
    )
    clear_company_read_caches()
    return snap_id


REPO_ROOT = Path(__file__).resolve().parents[4]


@st.cache_data(ttl=300, show_spinner=False)
def cached_eval_reports() -> dict[str, Any]:
    """Load committed evaluation report JSONs from `evals/reports/`."""
    import json

    reports_dir = REPO_ROOT / "evals" / "reports"
    latest_path = reports_dir / "latest.json"
    ablation_path = reports_dir / "ablation-skip-planner.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else {}
    ablation = (
        json.loads(ablation_path.read_text(encoding="utf-8")) if ablation_path.exists() else {}
    )
    return {"latest": latest, "ablation": ablation}


@st.cache_data(ttl=300, show_spinner=False)
def cached_example_runs() -> dict[str, dict[str, Any]]:
    """Load all recorded `evals/reports/runs/*.json` transcripts ($0)."""
    import json

    runs_dir = REPO_ROOT / "evals" / "reports" / "runs"
    out: dict[str, dict[str, Any]] = {}
    if not runs_dir.exists():
        return out
    for p in sorted(runs_dir.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return out


@st.cache_data(ttl=300, show_spinner=False)
def cached_eval_cases_meta() -> dict[str, dict[str, Any]]:
    """Load case prompts and metadata from `evals/cases/*.yaml` ($0, read-only)."""
    import yaml

    cases_dir = REPO_ROOT / "evals" / "cases"
    out: dict[str, dict[str, Any]] = {}
    if not cases_dir.exists():
        return out
    for p in sorted(cases_dir.glob("*.yaml")):
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(loaded, list):
            continue
        for item in loaded:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            cid = str(item["id"])
            prompts = [
                str(t["user"]).strip()
                for t in (item.get("turns") or [])
                if isinstance(t, dict) and t.get("user")
            ]
            out[cid] = {
                "prompts": prompts,
                "tenant": str(item.get("tenant") or ""),
                "role": str(item.get("role") or ""),
                "tags": list(item.get("tags") or []),
            }
    return out


@st.cache_data(ttl=300, show_spinner=False)
def cached_repo_doc(rel_path: str) -> str:
    """Read a committed repository markdown document relative to the repo root."""
    target = (REPO_ROOT / rel_path).resolve()
    if not target.is_file() or REPO_ROOT not in target.parents:
        return ""
    return target.read_text(encoding="utf-8")
