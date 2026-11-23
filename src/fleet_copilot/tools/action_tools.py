"""LLM-facing action precheck tools (`it_admin` only; propose-only, never execute directly)."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.actions import DraftInput
from fleet_copilot.tools.registry import ToolDeps, ToolSpec, TurnToolSink


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


def _run_precheck(
    deps: ToolDeps,
    draft_in: DraftInput,
    runtime: ToolRuntime[TenantContext, CopilotState],
) -> str:
    sink, ledger = _sink_and_ledger(runtime)
    res = deps.action_svc.precheck(runtime.context, draft_in, ledger, deps.clock)
    verdict_dict = res.verdict.model_dump(mode="json")
    allowed = verdict_dict["decision"] in ("allow", "allow_with_warnings")
    tc_id = runtime.tool_call_id or f"{draft_in.tool_name}:{draft_in.target_id}"
    dk = str(verdict_dict["dedupe_key"])
    draft_id = (
        f"draft_{uuid.uuid5(uuid.NAMESPACE_URL, f'{tc_id}:{dk}').hex[:12]}" if allowed else None
    )

    if sink is not None:
        sink.verdicts.append(
            {
                "tool_call_id": tc_id,
                "tool_name": draft_in.tool_name,
                "target_type": draft_in.target_type,
                "target_id": draft_in.target_id,
                "draft_id": draft_id,
                **verdict_dict,
            }
        )
        if allowed:
            sink.drafts.append(
                {
                    "draft_id": draft_id,
                    "tool_call_id": tc_id,
                    "tool_name": draft_in.tool_name,
                    "target_type": draft_in.target_type,
                    "target_id": draft_in.target_id,
                    "arguments": dict(draft_in.arguments),
                    "rationale": draft_in.rationale,
                    "evidence_handles": list(draft_in.evidence_handles),
                    "dedupe_key": verdict_dict["dedupe_key"],
                }
            )
        sink.call_meta[tc_id] = {
            "ok": allowed,
            "evaluated": 1,
            "matched": 1 if allowed else 0,
            "evidence_count": len(draft_in.evidence_handles),
            "summary": f"{draft_in.tool_name}({draft_in.target_id}): {verdict_dict['decision']}",
        }

    payload: dict[str, Any] = {
        "ok": allowed,
        "decision": verdict_dict["decision"],
        "draft_id": draft_id,
        "tool_name": draft_in.tool_name,
        "target_type": draft_in.target_type,
        "target_id": draft_in.target_id,
        "reasons": verdict_dict.get("reasons", []),
        "warnings": verdict_dict.get("warnings", []),
        "alternatives": verdict_dict.get("alternatives", []),
        "dedupe_key": verdict_dict.get("dedupe_key", ""),
        "policy_version": verdict_dict.get("policy_version", ""),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def build_action_tool_specs(deps: ToolDeps) -> list[ToolSpec]:
    """Build the 4 `it_admin` action precheck tool specs (never execute; stage awaits HITL)."""

    @tool
    def create_upgrade_order(
        device_id: str,
        component: Literal["ram", "storage", "battery"],
        spec: str,
        rationale: str,
        evidence: list[str],
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Precheck and draft a hardware upgrade order (ram, storage, or battery) for approval.

        Use ONLY when the user requested an upgrade or confirmed your recommendation AND you have
        fresh evidence handles (`["E1", ...]`) from this session. Checks hardware upgradability in
        the catalog; if refused with alternatives (e.g. soldered RAM/SSD), inspect the returned
        alternatives instead of retrying the refused upgrade. Never executes without human approval.
        """
        draft_in = DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id=device_id,
            arguments={"device_id": device_id, "component": component, "spec": spec},
            rationale=rationale,
            evidence_handles=list(evidence),
        )
        return _run_precheck(deps, draft_in, runtime)

    @tool
    def open_remediation_ticket(
        device_id: str,
        check_id: str,
        note: str,
        evidence: list[str],
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Precheck and draft an IT remediation ticket for a failing check or resource issue.

        Valid `check_id` values: any currently failing telemetry check (e.g. `screen_lock`,
        `os_up_to_date`), or `low_disk_space`, `memory_pressure`, `non_baseline_software`.
        Use ONLY when requested/confirmed by the user with valid evidence handles (`["E1", ...]`).
        Do NOT call for passing checks (e.g. `disk_encryption`). Never executes without approval.
        """
        draft_in = DraftInput(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id=device_id,
            arguments={"device_id": device_id, "check_id": check_id, "note": note},
            rationale=note,
            evidence_handles=list(evidence),
        )
        return _run_precheck(deps, draft_in, runtime)

    @tool
    def flag_device_for_replacement(
        device_id: str,
        reason: str,
        evidence: list[str],
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Precheck and draft a device replacement flag for multi-signal or non-upgradable hardware.

        Use ONLY when requested/confirmed AND supported by battery Service Recommended, >= 2 active
        signals, or sustained RAM / low disk on non-upgradable hardware, citing evidence handles.
        Do NOT call for healthy devices. Never executes without human approval.
        """
        draft_in = DraftInput(
            tool_name="flag_device_for_replacement",
            target_type="device",
            target_id=device_id,
            arguments={"device_id": device_id, "reason": reason},
            rationale=reason,
            evidence_handles=list(evidence),
        )
        return _run_precheck(deps, draft_in, runtime)

    @tool
    def notify_employee(
        employee_id: str,
        message: str,
        evidence: list[str],
        runtime: ToolRuntime[TenantContext, CopilotState],
    ) -> str:
        """Precheck and draft an employee notification about active findings on their devices.

        Use ONLY after verifying ownership with `get_employee_devices` and citing active finding
        evidence handles (`["E1", ...]`). The message MUST name >= 1 of that employee's cited
        devices and MUST NOT mention any other device or employee. Never executes without approval.
        """
        draft_in = DraftInput(
            tool_name="notify_employee",
            target_type="employee",
            target_id=employee_id,
            arguments={"employee_id": employee_id, "message": message},
            rationale="Employee notification for active device findings",
            evidence_handles=list(evidence),
        )
        return _run_precheck(deps, draft_in, runtime)

    return [
        ToolSpec(
            name="create_upgrade_order",
            min_role="it_admin",
            kind="action",
            tool=create_upgrade_order,
        ),
        ToolSpec(
            name="open_remediation_ticket",
            min_role="it_admin",
            kind="action",
            tool=open_remediation_ticket,
        ),
        ToolSpec(
            name="flag_device_for_replacement",
            min_role="it_admin",
            kind="action",
            tool=flag_device_for_replacement,
        ),
        ToolSpec(
            name="notify_employee",
            min_role="it_admin",
            kind="action",
            tool=notify_employee,
        ),
    ]
