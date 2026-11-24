"""LLM-facing `create_chart` tool building validated Vega-Lite v5 charts with `C<n>` handles."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import Evidence, TenantContext
from fleet_copilot.services.charts import (
    ChartGroupBy,
    ChartKind,
    ChartMetric,
    ChartRequest,
    ChartService,
    ChartStat,
    ChartType,
)
from fleet_copilot.tools.registry import (
    ToolDeps,
    ToolSpec,
    TurnToolSink,
    sanitize_telemetry_str,
)


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


def build_chart_tool_spec(deps: ToolDeps) -> ToolSpec:
    """Build the viewer-accessible `create_chart` tool spec."""

    chart_svc = ChartService(
        deps.telemetry_repo,
        deps.policy_svc,
        store=deps.copilot_store,
        audit_svc=deps.audit_svc,
    )

    @tool
    def create_chart(
        title: str,
        kind: ChartKind,
        runtime: ToolRuntime[TenantContext, CopilotState],
        metric: ChartMetric | None = None,
        device_ids: list[str] | None = None,
        form_factor: Literal["laptop", "desktop", "unknown"] | None = None,
        os_family: Literal["macos", "windows"] | None = None,
        os_version_lt: str | None = None,
        model: str | None = None,
        employee_id: str | None = None,
        low_disk_only: bool = False,
        max_disk_free_pct: float | None = None,
        min_ram_used_pct: float | None = None,
        battery_condition: str | None = None,
        software_name: str | None = None,
        stat: ChartStat | None = None,
        group_by: ChartGroupBy | None = None,
        window_days: int = 30,
        query_handle: str | None = None,
        x: str | None = None,
        y: str | None = None,
        series: str | None = None,
        chart_type: ChartType = "line",
    ) -> str:
        """Create a grounded Vega-Lite chart from live telemetry or a prior `Q<n>` SQL query.

        Use when the user asks to plot, chart, graph, or visualize metrics over time
        (`kind="metric_over_time"`), group aggregates (`kind="aggregate_by_group"`), or SQL
        query results (`kind="from_query"` with `query_handle="Q1"`, `x`, `y`).
        The server computes all numbers directly from telemetry and assigns a `C<n>` handle.
        """
        sink, ledger = _sink_and_ledger(runtime)
        state = runtime.state if isinstance(runtime.state, dict) else {}

        # Build Q<n> lookup from both prior state queries and queries run earlier in this step
        q_lookup: dict[str, dict[str, Any]] = {}
        for q in state.get("queries") or []:
            if isinstance(q, dict) and q.get("handle"):
                q_lookup[str(q["handle"])] = q
        if sink is not None:
            for q in sink.queries:
                if isinstance(q, dict) and q.get("handle"):
                    q_lookup[str(q["handle"])] = q

        try:
            req = ChartRequest(
                title=sanitize_telemetry_str(title, max_len=80),
                kind=kind,
                metric=metric,
                device_ids=device_ids,
                form_factor=form_factor,
                os_family=os_family,
                os_version_lt=os_version_lt,
                model=model,
                employee_id=employee_id,
                low_disk_only=low_disk_only,
                max_disk_free_pct=max_disk_free_pct,
                min_ram_used_pct=min_ram_used_pct,
                battery_condition=battery_condition,
                software_name=software_name,
                stat=stat,
                group_by=group_by,
                window_days=max(1, min(window_days, 30)),
                query_handle=query_handle,
                x=x,
                y=y,
                series=series,
                chart_type=chart_type,
            )
            artifact = chart_svc.create(
                runtime.context,
                req,
                thread_id=str(state.get("thread_id") or "") or None,
                turn_id=str(state.get("turn_id") or "") or None,
                tool_call_id=runtime.tool_call_id,
                query_lookup=q_lookup,
                persist=True,
            )
        except ValueError as exc:
            err_msg = sanitize_telemetry_str(str(exc), max_len=160)
            if sink is not None:
                tc_id = runtime.tool_call_id or "create_chart"
                sink.call_meta[tc_id] = {
                    "ok": False,
                    "evaluated": 0,
                    "matched": 0,
                    "evidence_count": 0,
                    "summary": err_msg,
                }
            return json.dumps({"ok": False, "error": "invalid_chart_request", "summary": err_msg})

        anchor_snap = (
            artifact.evidence_snapshot_ids[0]
            if artifact.evidence_snapshot_ids
            else artifact.chart_id
        )
        chart_devs = sorted({str(r["device_id"]) for r in artifact.data_rows if r.get("device_id")})
        anchor_dev = chart_devs[0] if len(chart_devs) == 1 else "fleet"
        handle = ledger.add_artifact(
            "C",
            Evidence(
                snapshot_id=anchor_snap,
                device_id=anchor_dev,
                collected_at=artifact.telemetry_as_of,
                metric=f"chart:{req.metric or req.kind}",
                value=artifact.row_count,
                label=f"Chart [{artifact.title}] ({artifact.row_count} points)",
            ),
        )
        stamped = artifact.model_copy(update={"handle": handle})
        summary = (
            f"Created chart [{handle}] '{stamped.title}' ({stamped.row_count} data points, "
            f"as of {stamped.telemetry_as_of.date().isoformat()})."
        )

        if sink is not None:
            sink.charts.append(stamped.model_dump(mode="json"))
            tc_id = runtime.tool_call_id or "create_chart"
            sink.call_meta[tc_id] = {
                "ok": True,
                "evaluated": stamped.row_count,
                "matched": stamped.row_count,
                "evidence_count": max(1, len(stamped.evidence_snapshot_ids)),
                "summary": summary,
            }

        # Return compact summary + sample/latest rows (<= 20) for the LLM; full spec stays in state
        preview_rows = stamped.data_rows[-20:] if stamped.row_count > 20 else stamped.data_rows
        payload = {
            "ok": True,
            "chart_handle": handle,
            "chart_id": stamped.chart_id,
            "title": stamped.title,
            "kind": req.kind,
            "chart_type": req.chart_type,
            "row_count": stamped.row_count,
            "device_ids": chart_devs,
            "rows": preview_rows,
            "evidence": [
                {
                    "h": handle,
                    "device": anchor_dev,
                    "metric": f"chart:{req.metric or req.kind}",
                    "value": stamped.row_count,
                    "at": stamped.telemetry_as_of.isoformat(),
                }
            ],
            "telemetry_as_of": stamped.telemetry_as_of.isoformat(),
            "truncated": stamped.row_count > 20,
            "summary": summary,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    return ToolSpec(name="create_chart", min_role="viewer", kind="read", tool=create_chart)
