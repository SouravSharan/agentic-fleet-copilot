"""LLM-facing read and analytics tools wrapping Step 2-3 services via `ToolRuntime`."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import ComplianceQuery, DeviceFilters, TenantContext, ToolResult
from fleet_copilot.services.fleet import (
    find_devices as svc_find_devices,
)
from fleet_copilot.services.fleet import (
    get_device_details as svc_get_device_details,
)
from fleet_copilot.services.fleet import (
    get_employee_devices as svc_get_employee_devices,
)
from fleet_copilot.services.fleet import (
    get_fleet_overview as svc_get_fleet_overview,
)
from fleet_copilot.services.fleet import (
    query_compliance as svc_query_compliance,
)
from fleet_copilot.services.trends import analyze_fleet_trends as svc_analyze_fleet_trends
from fleet_copilot.tools.registry import ToolDeps, ToolSpec, TurnToolSink, format_tool_output


def _sink_and_ledger(
    runtime: ToolRuntime[TenantContext, CopilotState],
) -> tuple[TurnToolSink | None, EvidenceLedger]:
    cfg = runtime.config if isinstance(runtime.config, dict) else {}
    configurable = cfg.get("configurable") if isinstance(cfg, dict) else None
    sink = configurable.get("_turn_sink") if isinstance(configurable, dict) else None
    if isinstance(sink, TurnToolSink):
        return sink, sink.ledger
    raw = runtime.state.get("evidence") if isinstance(runtime.state, dict) else None
    return None, EvidenceLedger.from_json(raw or {})


def _emit(
    result: ToolResult,
    runtime: ToolRuntime[TenantContext, CopilotState],
    *,
    capture_insights: bool = False,
) -> str:
    sink, ledger = _sink_and_ledger(runtime)
    out_json, ev_entries = format_tool_output(result, ledger)
    if sink is not None:
        if capture_insights and result.ok:
            sink.insights.extend(result.data)
        tc_id = runtime.tool_call_id or result.tool_name
        sink.call_meta[tc_id] = {
            "ok": result.ok,
            "evaluated": result.coverage.evaluated if result.coverage else 0,
            "matched": result.coverage.matched if result.coverage else 0,
            "evidence_count": len(ev_entries),
            "summary": result.summary,
        }
    return out_json


def build_read_tool_specs(deps: ToolDeps) -> list[ToolSpec]:
    """Build the 7 viewer-accessible read and analytics tool specs bound to `deps`."""

    @tool
    def get_fleet_overview(
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Return tenant fleet summary counts by form factor, OS, model, compliance, and thresholds.

        Use for broad inventory questions ("how many devices/laptops do we have?", "fleet summary").
        Do NOT use for specific device filters (`find_devices`) or trends (`analyze_fleet_trends`).
        """
        result = svc_get_fleet_overview(runtime.context, deps.telemetry_repo, deps.policy_svc)
        return _emit(result, runtime)

    @tool
    def find_devices(
        runtime: ToolRuntime[TenantContext, CopilotState],
        form_factor: Literal["laptop", "desktop", "unknown"] | None = None,
        os_family: Literal["macos", "windows"] | None = None,
        os_version_lt: str | None = None,
        model: str | None = None,
        employee_id: str | None = None,
        hostname: str | None = None,
        low_disk_only: bool = False,
        max_disk_free_pct: float | None = None,
        min_ram_used_pct: float | None = None,
        battery_condition: str | None = None,
        software_name: str | None = None,
        limit: int = 20,
    ) -> str:
        """Find devices matching latest-state hardware, OS, disk, RAM, battery, or software filters.

        Use for questions like "which devices are low on disk space?" (`low_disk_only=True`) or
        "how many devices run an OS older than macOS 15?" (`os_family="macos", os_version_lt="15"`).
        `os_version_lt` requires `os_family`. Do NOT use for compliance (`query_compliance`)
        or 30-day trends (use `analyze_fleet_trends`).
        """
        filters = DeviceFilters(
            form_factor=form_factor,
            os_family=os_family,
            os_version_lt=os_version_lt,
            model=model,
            employee_id=employee_id,
            hostname=hostname,
            low_disk_only=low_disk_only,
            max_disk_free_pct=max_disk_free_pct,
            min_ram_used_pct=min_ram_used_pct,
            battery_condition=battery_condition,
            software_name=software_name,
            limit=max(1, min(limit, 50)),
        )
        result = svc_find_devices(runtime.context, filters, deps.telemetry_repo, deps.policy_svc)
        return _emit(result, runtime)

    @tool
    def query_compliance(
        runtime: ToolRuntime[TenantContext, CopilotState],
        severity: Literal["low", "medium", "high"] | None = None,
        status: Literal["pass", "fail"] | None = None,
        check_id: str | None = None,
        form_factor: Literal["laptop", "desktop", "unknown"] | None = None,
        scope: Literal["latest", "window"] = "latest",
    ) -> str:
        """Query compliance check results by severity, status, check_id, form_factor, and scope.

        Use for questions like "show me laptops failing high-severity compliance checks"
        (`severity="high", status="fail", form_factor="laptop"`). On zero matches it returns
        per-device pass evidence and lists all existing checks. Do NOT use for pass->fail drift
        dates (use `analyze_fleet_trends(categories=["compliance_drift"])`).
        """
        q = ComplianceQuery(
            severity=severity,
            status=status,
            check_id=check_id,
            form_factor=form_factor,
            scope=scope,
        )
        result = svc_query_compliance(runtime.context, q, deps.telemetry_repo, deps.policy_svc)
        return _emit(result, runtime)

    @tool
    def get_device_details(
        identifier: str,
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Inspect one device by exact device_id, hostname, or serial number.

        Use when asked about a single specific device's profile, 30-day metrics, compliance, or
        installed software. Returns `not_found` if absent from the caller's tenant. Do NOT use to
        scan the whole fleet (use `find_devices` or `analyze_fleet_trends`).
        """
        result = svc_get_device_details(
            runtime.context, identifier, deps.telemetry_repo, deps.policy_svc
        )
        return _emit(result, runtime)

    @tool
    def get_employee_devices(
        employee_id: str,
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """List all devices owned by one `employee_id` in the caller's tenant.

        Use before proposing `notify_employee` or answering "what devices does emp-... have?".
        Do NOT use to list all devices across the company (use `find_devices`).
        """
        result = svc_get_employee_devices(
            runtime.context, employee_id, deps.telemetry_repo, deps.policy_svc
        )
        return _emit(result, runtime)

    @tool
    def analyze_fleet_trends(
        categories: list[Literal["battery", "ram", "disk", "compliance_drift", "software", "all"]],
        runtime: ToolRuntime[TenantContext, CopilotState],
        device_ids: list[str] | None = None,
    ) -> str:
        """Run 30-day trend analyzers across battery, RAM, disk, compliance drift, and software.

        Use for health reports, proactive insights, degradation/depletion trajectories, compliance
        drift dates, non-baseline software installs, and replacement candidates. Do NOT use for
        simple latest-state inventory filtering (use `find_devices`).
        """
        catalog = deps.copilot_store.hardware_catalog(runtime.context.company_id)
        result = svc_analyze_fleet_trends(
            runtime.context,
            categories,
            deps.telemetry_repo,
            deps.policy_svc,
            device_ids=device_ids,
            catalog_by_model=catalog,
        )
        return _emit(result, runtime, capture_insights=True)

    @tool
    def list_action_proposals(
        runtime: ToolRuntime[TenantContext, CopilotState],
        status: Literal[
            "pending_approval",
            "approved",
            "rejected",
            "expired",
            "executing",
            "executed",
            "failed",
            "stale_blocked",
        ]
        | None = None,
        device_id: str | None = None,
    ) -> str:
        """List staged, pending, or historical action proposals for the current tenant.

        Use when asked "what proposals are pending?" or to avoid proposing duplicate actions.
        Do NOT use to approve or execute proposals (humans decide outside the LLM).
        """
        sink, _ = _sink_and_ledger(runtime)
        rows = deps.action_svc.list(runtime.context, status=status, device_id=device_id)
        compact: list[dict[str, Any]] = [
            {
                "proposal_id": r["proposal_id"],
                "tool_name": r["tool_name"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "status": r["status"],
                "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
            }
            for r in rows[:20]
        ]
        summary = f"Found {len(rows)} proposal(s)."
        if sink is not None:
            tc_id = runtime.tool_call_id or "list_action_proposals"
            sink.call_meta[tc_id] = {
                "ok": True,
                "evaluated": len(rows),
                "matched": len(compact),
                "evidence_count": 0,
                "summary": summary,
            }
        return json.dumps(
            {
                "ok": True,
                "summary": summary,
                "rows": compact,
                "evidence": [],
                "notes": [],
                "policy": {},
                "truncated": len(rows) > 20,
            },
            sort_keys=True,
        )

    return [
        ToolSpec(
            name="get_fleet_overview", min_role="viewer", kind="read", tool=get_fleet_overview
        ),
        ToolSpec(name="find_devices", min_role="viewer", kind="read", tool=find_devices),
        ToolSpec(name="query_compliance", min_role="viewer", kind="read", tool=query_compliance),
        ToolSpec(
            name="get_device_details", min_role="viewer", kind="read", tool=get_device_details
        ),
        ToolSpec(
            name="get_employee_devices", min_role="viewer", kind="read", tool=get_employee_devices
        ),
        ToolSpec(
            name="analyze_fleet_trends", min_role="viewer", kind="read", tool=analyze_fleet_trends
        ),
        ToolSpec(
            name="list_action_proposals",
            min_role="viewer",
            kind="read",
            tool=list_action_proposals,
        ),
    ]
