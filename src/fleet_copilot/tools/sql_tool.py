"""LLM-facing governed `run_readonly_sql` tool assigning `Q<n>` handles."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import Evidence, TenantContext
from fleet_copilot.services.sql_query import (
    SqlPiiInResultError,
    SqlValidationError,
    execute_readonly_sql,
)
from fleet_copilot.tools.registry import (
    ToolDeps,
    ToolSpec,
    TurnToolSink,
    sanitize_telemetry_str,
)
from fleet_copilot.tools.sanitize import neutralize_untrusted_value


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


def build_sql_tool_spec(deps: ToolDeps) -> ToolSpec:
    """Build the viewer + it_admin governed `run_readonly_sql` tool spec."""

    @tool
    def run_readonly_sql(
        sql: str,
        purpose: str,
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Run a governed read-only PostgreSQL SELECT over tenant-scoped `telemetry.v_*` views.

        Prefer dedicated tools (`find_devices`, `query_compliance`, `get_fleet_overview`,
        `get_device_details`, `get_employee_devices`, `analyze_fleet_trends`) whenever they can
        answer the question. Use `run_readonly_sql` only for ad-hoc aggregations, custom group-bys
        (e.g. average battery cycles by model, devices per employee, weekly trends), or questions
        dedicated tools cannot answer.

        Rules: single SELECT only; allowed views are `telemetry.v_latest_device_state`,
        `telemetry.v_snapshots_deduped`, `telemetry.v_disk_volumes`,
        `telemetry.v_compliance_results`, and `telemetry.v_installed_software`; select explicit
        columns (never `SELECT *`); LIMIT <= 200; 3 s timeout under tenant RLS. Returns a `Q<n>`
        handle usable in citations and `create_chart`.
        """
        sink, ledger = _sink_and_ledger(runtime)
        if deps.db is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "sql_unavailable",
                    "summary": "Database connection is not configured for ad-hoc SQL.",
                }
            )

        clean_purpose = sanitize_telemetry_str(purpose, max_len=120)
        try:
            res = execute_readonly_sql(
                runtime.context,
                deps.db,
                sql,
                purpose=clean_purpose,
            )
        except SqlValidationError as exc:
            msg = sanitize_telemetry_str(str(exc), max_len=160)
            if sink is not None:
                tc_id = runtime.tool_call_id or "run_readonly_sql"
                sink.call_meta[tc_id] = {
                    "ok": False,
                    "evaluated": 0,
                    "matched": 0,
                    "evidence_count": 0,
                    "summary": msg,
                }
            return json.dumps({"ok": False, "error": "sql_validation_failed", "summary": msg})
        except SqlPiiInResultError:
            msg = "Query result blocked: restricted PII patterns (IP/MAC/UUID) detected."
            if sink is not None:
                tc_id = runtime.tool_call_id or "run_readonly_sql"
                sink.call_meta[tc_id] = {
                    "ok": False,
                    "evaluated": 0,
                    "matched": 0,
                    "evidence_count": 0,
                    "summary": msg,
                }
            return json.dumps({"ok": False, "error": "pii_in_result", "summary": msg})

        sql_hash = hashlib.sha256(res.sql.encode("utf-8")).hexdigest()[:12]
        anchor_snap = res.snapshot_ids[0] if res.snapshot_ids else f"query_{sql_hash}"
        anchor_dev = res.device_ids[0] if len(res.device_ids) == 1 else "fleet"
        as_of = res.telemetry_as_of or datetime.now(UTC)

        handle = ledger.add_artifact(
            "Q",
            Evidence(
                snapshot_id=anchor_snap,
                device_id=anchor_dev,
                collected_at=as_of,
                metric="sql_query",
                value=res.row_count,
                label=f"SQL [{clean_purpose or 'ad-hoc query'}] ({res.row_count} rows)",
            ),
        )
        summary = (
            f"SQL query [{handle}] returned {res.row_count} row(s) "
            f"(as of {as_of.date().isoformat()})."
        )
        query_artifact = {
            "handle": handle,
            "sql": res.sql,
            "purpose": clean_purpose,
            "columns": res.columns,
            "rows": res.rows,
            "row_count": res.row_count,
            "truncated": res.truncated,
            "snapshot_ids": res.snapshot_ids,
            "device_ids": res.device_ids,
            "telemetry_as_of": as_of.isoformat(),
        }
        if sink is not None:
            sink.queries.append(query_artifact)
            tc_id = runtime.tool_call_id or "run_readonly_sql"
            sink.call_meta[tc_id] = {
                "ok": True,
                "evaluated": res.row_count,
                "matched": res.row_count,
                "evidence_count": max(1, len(res.snapshot_ids)),
                "summary": summary,
            }

        llm_rows, sql_suspicious = neutralize_untrusted_value(res.rows[:20], evidence_handle=handle)
        payload: dict[str, object] = {
            "ok": True,
            "query_handle": handle,
            "sql": res.sql,
            "purpose": clean_purpose,
            "columns": res.columns,
            "rows": llm_rows,
            "row_count": res.row_count,
            "truncated": res.truncated or res.row_count > 20,
            "evidence": [
                {
                    "h": handle,
                    "device": anchor_dev,
                    "metric": "sql_query",
                    "value": res.row_count,
                    "at": as_of.isoformat(),
                }
            ],
            "telemetry_as_of": as_of.isoformat(),
            "summary": summary,
        }
        if sql_suspicious:
            payload["suspicious_text"] = True
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    return ToolSpec(name="run_readonly_sql", min_role="viewer", kind="read", tool=run_readonly_sql)
