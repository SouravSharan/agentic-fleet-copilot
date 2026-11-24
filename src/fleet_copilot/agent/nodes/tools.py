"""`tools` node: schema validation, cooperative deadlines, budgets, ledger, and per-call audit."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError

from fleet_copilot.agent.deadline import check_turn_deadline
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import AccessDeniedPayload, ToolCalledPayload
from fleet_copilot.tools.registry import TurnToolSink

logger = logging.getLogger(__name__)


def _noop_stream_writer(_chunk: Any) -> None:
    return None


def _args_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate_tool_args(
    tool_obj: Any,
    clean_args: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate `clean_args` against `tool.tool_call_schema`; return `(validated, bad_fields)`."""
    schema = getattr(tool_obj, "tool_call_schema", None)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        return clean_args, []

    bad_fields: set[str] = set()
    allowed_fields = set(schema.model_fields.keys())
    for k in clean_args:
        if k not in allowed_fields:
            bad_fields.add(str(k))

    try:
        model_inst = schema.model_validate(clean_args)
    except ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc")
            if loc:
                bad_fields.add(str(loc[0]))
            else:
                bad_fields.add("_args")
        return None, sorted(bad_fields)

    if bad_fields:
        return None, sorted(bad_fields)
    # Use model_dump(mode="python") so validated types (e.g. ints, literals, lists) pass cleanly
    return {k: getattr(model_inst, k) for k in allowed_fields}, []


def make_tools_node(deps: Any) -> Any:
    """Build the `tools` node enforcing deadlines, validation, budgets, RBAC, ledger, and audit."""

    def tools_node(
        state: CopilotState,
        config: RunnableConfig,
        runtime: Runtime[TenantContext],
    ) -> dict[str, Any]:
        check_turn_deadline(config)
        ctx = runtime.context
        messages = state.get("messages") or []
        if not messages or not isinstance(messages[-1], AIMessage):
            return {}
        last_ai = messages[-1]
        if not last_ai.tool_calls:
            return {}

        allowed_names = set(state.get("allowed_tool_names", []))
        specs_by_name = {spec.name: spec for spec in deps.all_tool_specs}

        ledger = EvidenceLedger.from_json(state.get("evidence") or {})
        sink = TurnToolSink(ledger=ledger)

        step_count = int(state.get("step_count", 0))
        budget_exhausted = bool(state.get("budget_exhausted", False))
        max_calls = deps.runtime_cfg.budgets.max_tool_calls
        existing_trace = list(state.get("tool_trace") or [])
        per_tool_counts: dict[str, int] = {}
        for tr in existing_trace:
            tname = str(tr.get("tool_name", ""))
            per_tool_counts[tname] = per_tool_counts.get(tname, 0) + 1

        tool_messages: list[ToolMessage] = []
        new_trace: list[dict[str, Any]] = []
        new_outputs: list[dict[str, Any]] = []
        new_warnings: list[str] = []

        turn_id = state.get("turn_id", "")
        thread_id = state.get("thread_id", "")

        for tc in last_ai.tool_calls:
            check_turn_deadline(config)
            tool_name = str(tc["name"])
            tc_id = str(tc.get("id") or f"tc_{step_count}")
            raw_args = dict(tc.get("args") or {})
            t0 = time.monotonic()

            if step_count >= max_calls:
                budget_exhausted = True
                msg = (
                    f"Tool call budget exhausted ({max_calls} calls max). "
                    "Synthesize with gathered evidence."
                )
                new_warnings.append(msg)
                tool_messages.append(
                    ToolMessage(
                        content=json.dumps(
                            {"ok": False, "error": "budget_exhausted", "summary": msg}
                        ),
                        tool_call_id=tc_id,
                        name=tool_name,
                    )
                )
                continue

            tool_cap = deps.runtime_cfg.budgets.cap_for(tool_name)
            if per_tool_counts.get(tool_name, 0) >= tool_cap:
                # N3: Capped rejections count toward the step budget so the model cannot loop.
                step_count += 1
                if step_count >= max_calls:
                    budget_exhausted = True
                msg = f"Per-tool call cap reached for {tool_name} ({tool_cap} max)."
                new_warnings.append(msg)
                tool_messages.append(
                    ToolMessage(
                        content=json.dumps(
                            {"ok": False, "error": "tool_cap_reached", "summary": msg}
                        ),
                        tool_call_id=tc_id,
                        name=tool_name,
                    )
                )
                continue

            if tool_name not in allowed_names or tool_name not in specs_by_name:
                step_count += 1
                if step_count >= max_calls:
                    budget_exhausted = True
                reason_code = "role_forbidden" if tool_name in specs_by_name else "tool_disabled"
                deps.audit_svc.record_standalone(
                    ctx,
                    "access_denied",
                    AccessDeniedPayload(reason_code=reason_code, tool_name=tool_name),
                    thread_id=thread_id,
                )
                err_payload = {
                    "ok": False,
                    "error": reason_code,
                    "summary": f"Tool {tool_name!r} is not permitted for role {ctx.role!r}.",
                }
                tool_messages.append(
                    ToolMessage(
                        content=json.dumps(err_payload),
                        tool_call_id=tc_id,
                        name=tool_name,
                    )
                )
                continue

            spec = specs_by_name[tool_name]
            step_count += 1
            per_tool_counts[tool_name] = per_tool_counts.get(tool_name, 0) + 1

            # Inject ToolRuntime with the live TurnToolSink via RunnableConfig
            base_cfg: dict[str, Any] = dict(config) if isinstance(config, dict) else {}
            raw_conf = base_cfg.get("configurable")
            configurable: dict[str, Any] = dict(raw_conf) if isinstance(raw_conf, dict) else {}
            configurable["_turn_sink"] = sink
            base_cfg["configurable"] = configurable

            tool_rt: ToolRuntime[TenantContext, CopilotState] = ToolRuntime(
                state={**state, "evidence": ledger.to_json()},
                context=ctx,
                config=base_cfg,  # type: ignore[arg-type]
                stream_writer=_noop_stream_writer,
                tool_call_id=tc_id,
                store=None,
            )

            # 11 §1 #8: Strip stray company_id and validate args against tool_call_schema
            clean_args = {k: v for k, v in raw_args.items() if k != "company_id"}
            validated_args, bad_fields = _validate_tool_args(spec.tool, clean_args)

            ok = True
            if validated_args is None:
                ok = False
                output_str = json.dumps(
                    {"ok": False, "error": "invalid_arguments", "fields": bad_fields},
                    sort_keys=True,
                )
            else:
                try:
                    func = getattr(spec.tool, "func", None)
                    if func is None:
                        raise RuntimeError("Tool missing callable func")
                    output_str = str(func(**validated_args, runtime=tool_rt))
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    logger.warning(
                        "tool_execution_error",
                        extra={
                            "company_id": ctx.company_id,
                            "turn_id": turn_id,
                            "tool_name": tool_name,
                            "error_class": type(exc).__name__,
                        },
                    )
                    output_str = json.dumps(
                        {"ok": False, "error": "tool_error", "code": type(exc).__name__},
                        sort_keys=True,
                    )

            duration_ms = int((time.monotonic() - t0) * 1000)
            meta = sink.call_meta.get(tc_id) or {
                "ok": ok,
                "evaluated": 0,
                "matched": 0,
                "evidence_count": 0,
                "summary": "",
            }

            deps.audit_svc.record_standalone(
                ctx,
                "tool_called",
                ToolCalledPayload(
                    turn_id=turn_id,
                    tool_name=tool_name,
                    args_hash=_args_hash(raw_args),
                    ok=bool(meta["ok"]),
                    evaluated=int(meta["evaluated"]),
                    matched=int(meta["matched"]),
                    evidence_count=int(meta["evidence_count"]),
                    duration_ms=duration_ms,
                ),
                thread_id=thread_id,
            )

            logger.info(
                "tool_called",
                extra={
                    "company_id": ctx.company_id,
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                    "ok": bool(meta["ok"]),
                    "duration_ms": duration_ms,
                },
            )

            tool_messages.append(
                ToolMessage(content=output_str, tool_call_id=tc_id, name=tool_name)
            )
            new_trace.append(
                {
                    "tool_call_id": tc_id,
                    "tool_name": tool_name,
                    "args": {k: v for k, v in raw_args.items() if k != "company_id"},
                    "ok": bool(meta["ok"]),
                    "evaluated": int(meta["evaluated"]),
                    "matched": int(meta["matched"]),
                    "evidence_count": int(meta["evidence_count"]),
                    "duration_ms": duration_ms,
                    "summary": str(meta.get("summary", "")),
                }
            )
            try:
                parsed_out = json.loads(output_str)
            except Exception:  # noqa: BLE001
                parsed_out = {"raw": output_str[:500]}
            new_outputs.append({"tool_name": tool_name, "output": parsed_out})

        return {
            "messages": tool_messages,
            "evidence": ledger.to_json(),
            "tool_trace": new_trace,
            "tool_outputs": new_outputs,
            "insights": sink.insights,
            "charts": sink.charts,
            "queries": sink.queries,
            "drafts": sink.drafts,
            "verdicts": sink.verdicts,
            "step_count": step_count,
            "budget_exhausted": budget_exhausted,
            "warnings": new_warnings,
        }

    return tools_node
