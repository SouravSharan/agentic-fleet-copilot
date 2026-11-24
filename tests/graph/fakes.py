"""Offline test doubles for LangGraph tests (`ScriptedChatModel` + in-memory store/repo fakes)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, PrivateAttr

from fleet_copilot.domain.models import (
    ComplianceRecord,
    DeviceState,
    PolicyRow,
    SnapshotMetricPoint,
    SoftwareRecord,
    ToolConfigRow,
)

AS_OF = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)


class ScriptedChatModel(BaseChatModel):
    """Deterministic fake `BaseChatModel` implementing `bind_tools` and `with_structured_output`.

    Emits Gemini-3-style list content blocks and `__gemini_function_call_thought_signatures__`
    in `additional_kwargs`, and records every bound tool list and input message sequence.
    """

    script: list[Any]
    bound_tool_names: list[list[str]] = []
    recorded_inputs: list[list[BaseMessage]] = []
    _cursor: int = PrivateAttr(default=0)
    _bound_tools: list[Any] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted_chat_model"

    def _next_item(self, messages: list[BaseMessage]) -> Any:
        self.recorded_inputs.append(list(messages))
        if self._cursor >= len(self.script):
            return self.script[-1]
        item = self.script[self._cursor]
        self._cursor += 1
        return item

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        item = self._next_item(messages)
        if callable(item):
            item = item(messages)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, AIMessage):
            msg = item
        elif isinstance(item, str):
            # Emit Gemini-3-style list-shaped content
            msg = AIMessage(
                content=[{"type": "text", "text": item}],
                usage_metadata={"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
            )
        else:
            msg = AIMessage(
                content=[{"type": "text", "text": str(item)}],
                usage_metadata={"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        names = [getattr(t, "name", str(t)) for t in tools]
        self.bound_tool_names.append(names)
        self._bound_tools = list(tools)
        return self

    def with_structured_output(
        self,
        schema: type[BaseModel] | dict[str, Any],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        def _run(messages: Any) -> Any:
            msg_list = list(messages) if isinstance(messages, (list, tuple)) else [messages]
            item = self._next_item(msg_list)
            if callable(item):
                item = item(msg_list)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, dict) and "parsed" in item and "raw" in item:
                return item if include_raw else item["parsed"]
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                parsed = item if isinstance(item, schema) else schema.model_validate(item)
            else:
                parsed = item
            raw = AIMessage(
                content=[
                    {
                        "type": "text",
                        "text": parsed.model_dump_json()
                        if isinstance(parsed, BaseModel)
                        else str(parsed),
                    }
                ],
                usage_metadata={"input_tokens": 40, "output_tokens": 30, "total_tokens": 70},
            )
            if include_raw:
                return {"raw": raw, "parsed": parsed, "parsing_error": None}
            return parsed

        return RunnableLambda(_run)


def make_tool_call_ai(
    tool_calls: list[dict[str, Any]],
    *,
    text: str = "",
) -> AIMessage:
    """Create a Gemini-3-style AIMessage with list content and thought signatures."""
    sigs = {tc["id"]: f"sig_{tc['id']}_abc123==" for tc in tool_calls}
    content: list[Any] = [{"type": "text", "text": text}] if text else []
    return AIMessage(
        content=content,
        tool_calls=tool_calls,
        additional_kwargs={"__gemini_function_call_thought_signatures__": sigs},
        usage_metadata={"input_tokens": 60, "output_tokens": 25, "total_tokens": 85},
    )


class InMemoryTelemetryRepo:
    """Minimal deterministic telemetry repo for offline graph tests."""

    def __init__(self) -> None:
        self._devices = [
            DeviceState(
                snapshot_id="snap_mt7p_latest",
                company_id="acme-001",
                device_id="MT7PJB7N5LRE",
                employee_id="emp-acme-1003",
                collected_at=AS_OF,
                agent_version="42.0.0",
                os_platform="darwin",
                os_product_name="macOS",
                os_product_version="14.5",
                os_hostname="acme-macbook-4",
                model_name="MacBook Pro",
                model_identifier="Mac14,5",
                ram_total_gb=16.0,
                ram_used_pct=50.6,
                disk_free_pct=2.0,
                disk_total_gb=512.0,
                has_battery_block=True,
                battery_present=True,
                battery_condition="Normal",
                battery_cycle_count=112,
                first_seen_at=AS_OF - timedelta(days=29),
                last_seen_at=AS_OF,
                snapshot_count=30,
                ever_had_battery=True,
                form_factor="laptop",
            ),
            DeviceState(
                snapshot_id="snap_8nm2_latest",
                company_id="acme-001",
                device_id="8NM23J95R5I6",
                employee_id="emp-acme-1005",
                collected_at=AS_OF,
                agent_version="42.0.0",
                os_platform="win32",
                os_product_name="Windows",
                os_product_version="10 22H2",
                os_hostname="acme-thinkpad-8",
                model_name="ThinkPad X1 Carbon",
                model_identifier="21CB",
                ram_total_gb=16.0,
                ram_used_pct=63.5,
                disk_free_pct=2.6,
                disk_total_gb=1024.0,
                has_battery_block=True,
                battery_present=True,
                battery_condition="Service Recommended",
                battery_cycle_count=1157,
                first_seen_at=AS_OF - timedelta(days=29),
                last_seen_at=AS_OF,
                snapshot_count=30,
                ever_had_battery=True,
                form_factor="laptop",
            ),
        ]

    def tenant_as_of(self, company_id: str) -> datetime:
        return AS_OF

    def latest_states(self, company_id: str) -> list[DeviceState]:
        return [d for d in self._devices if d.company_id == company_id]

    def series(
        self,
        company_id: str,
        since: datetime,
        device_ids: Sequence[str] | None = None,
    ) -> list[SnapshotMetricPoint]:
        return [
            SnapshotMetricPoint(
                snapshot_id=d.snapshot_id,
                device_id=d.device_id,
                employee_id=d.employee_id,
                hostname=d.os_hostname,
                collected_at=d.collected_at,
                ram_used_pct=d.ram_used_pct,
                disk_free_pct=d.disk_free_pct,
                disk_total_gb=d.disk_total_gb,
                has_battery_block=d.has_battery_block,
                battery_present=d.battery_present,
                battery_condition=d.battery_condition,
                battery_cycle_count=d.battery_cycle_count,
            )
            for d in self.latest_states(company_id)
            if device_ids is None or d.device_id in device_ids
        ]

    def compliance(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[ComplianceRecord]:
        return [
            ComplianceRecord(
                snapshot_id=d.snapshot_id,
                device_id=d.device_id,
                employee_id=d.employee_id,
                hostname=d.os_hostname,
                collected_at=d.collected_at,
                check_id="disk_encryption",
                status="pass",
                severity="high",
            )
            for d in self.latest_states(company_id)
            if device_ids is None or d.device_id in device_ids
        ]

    def software(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[SoftwareRecord]:
        return []

    def resolve_device(self, company_id: str, identifier: str) -> DeviceState | None:
        for d in self.latest_states(company_id):
            if d.device_id == identifier or (
                d.os_hostname and d.os_hostname.lower() == identifier.lower()
            ):
                return d
        return None

    def employee_devices(self, company_id: str, employee_id: str) -> list[DeviceState]:
        return [d for d in self.latest_states(company_id) if d.employee_id == employee_id]

    def existing_snapshot_ids(self, company_id: str, ids: Sequence[str]) -> set[str]:
        valid = {d.snapshot_id for d in self.latest_states(company_id)}
        return set(ids) & valid


class InMemoryCopilotStore:
    """In-memory store fake for offline graph tests."""

    def __init__(self) -> None:
        self.threads: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}
        self.proposals: dict[str, dict[str, Any]] = {}

    def policies(self, company_id: str) -> list[PolicyRow]:
        defaults = {
            "low_disk_free_pct": 15.0,
            "high_ram_used_pct": 80.0,
            "ram_sustained_ratio": 0.7,
            "battery_eol_cycles": 800,
            "battery_capacity_drop_pct": 3.0,
            "stale_device_days": 7,
            "max_telemetry_lag_days": None,
            "proposal_ttl_hours": 72,
            "notify_message_max_chars": 800,
            "replacement_min_signals": 2,
            "flagged_software_list": ["uTorrent", "TeamViewer", "CleanMyMac X"],
        }
        return [
            PolicyRow(
                company_id="*",
                scope_type="global",
                scope_value="",
                metric_key=k,
                value=v,
            )
            for k, v in defaults.items()
        ]

    def tool_configs(self, company_id: str) -> list[ToolConfigRow]:
        reads = (
            "get_fleet_overview",
            "find_devices",
            "query_compliance",
            "get_device_details",
            "get_employee_devices",
            "analyze_fleet_trends",
            "list_action_proposals",
            "create_chart",
            "run_readonly_sql",
        )
        actions = (
            "create_upgrade_order",
            "open_remediation_ticket",
            "flag_device_for_replacement",
            "notify_employee",
        )
        rows = [
            ToolConfigRow(
                company_id="*",
                tool_name=name,
                enabled=True,
                requires_approval=False,
                min_role="viewer",
            )
            for name in reads
        ]
        rows.extend(
            ToolConfigRow(
                company_id="*",
                tool_name=name,
                enabled=True,
                requires_approval=True,
                min_role="it_admin",
            )
            for name in actions
        )
        return rows

    def is_tool_enabled(self, company_id: str, tool_name: str) -> bool:
        return True

    def hardware_catalog(self, company_id: str) -> dict[str, Any]:
        return {}

    def ensure_thread(
        self, company_id: str, thread_id: str, actor_id: str, *, title: str | None = None
    ) -> None:
        self.threads.setdefault(
            thread_id,
            {
                "thread_id": thread_id,
                "company_id": company_id,
                "actor_id": actor_id,
                "title": title,
                "status": "active",
                "hidden": False,
                "run_lease_until": None,
            },
        )

    def get_thread(self, company_id: str, thread_id: str) -> dict[str, Any] | None:
        row = self.threads.get(thread_id)
        if row is None or row.get("company_id") != company_id:
            return None
        return dict(row)

    def list_threads(
        self,
        company_id: str,
        *,
        actor_id: str,
        include_hidden: bool = False,
        search: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.threads.values()
            if r.get("company_id") == company_id
            and r.get("actor_id") == actor_id
            and (include_hidden or not r.get("hidden", False))
            and (not search or search.strip().lower() in str(r.get("title") or "").lower())
        ]
        return rows[:limit]

    def rename_thread(self, company_id: str, thread_id: str, actor_id: str, title: str) -> None:
        row = self.threads.get(thread_id)
        if (
            row is not None
            and row.get("company_id") == company_id
            and row.get("actor_id") == actor_id
        ):
            row["title"] = title

    def hide_thread(
        self, company_id: str, thread_id: str, actor_id: str, *, hidden: bool = True
    ) -> None:
        row = self.threads.get(thread_id)
        if (
            row is not None
            and row.get("company_id") == company_id
            and row.get("actor_id") == actor_id
        ):
            row["hidden"] = hidden

    def count_turns(self, company_id: str, thread_id: str) -> int:
        return sum(
            1
            for r in self.turns.values()
            if r.get("company_id") == company_id and r.get("thread_id") == thread_id
        )

    def list_turns(self, company_id: str, thread_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.turns.values()
            if r.get("company_id") == company_id and r.get("thread_id") == thread_id
        ]

    def acquire_run_lease(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        row = self.threads.get(thread_id)
        if row is None or row.get("company_id") != company_id or row.get("actor_id") != actor_id:
            return False
        cur_lease = row.get("run_lease_until")
        if cur_lease is not None and cur_lease > now:
            return False
        row["run_lease_until"] = lease_until
        return True

    def release_run_lease(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        *,
        lease_until: datetime,
    ) -> None:
        row = self.threads.get(thread_id)
        if (
            row is not None
            and row.get("company_id") == company_id
            and row.get("actor_id") == actor_id
            and row.get("run_lease_until") == lease_until
        ):
            row["run_lease_until"] = None

    def ensure_turn(
        self,
        company_id: str,
        thread_id: str,
        turn_id: str,
        trace_id: str,
        actor_id: str,
        user_message: str,
        *,
        status: str = "running",
        input_hash: str | None = None,
    ) -> None:
        self.turns[turn_id] = {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "company_id": company_id,
            "thread_id": thread_id,
            "actor_id": actor_id,
            "status": status,
            "user_message": user_message,
            "input_hash": input_hash,
        }

    def update_turn(self, company_id: str, turn_id: str, **kwargs: Any) -> None:
        self.turns.setdefault(turn_id, {}).update(kwargs)

    def get_proposal(self, company_id: str, proposal_id: str) -> dict[str, Any] | None:
        return self.proposals.get(proposal_id)

    def list_proposals(
        self,
        company_id: str,
        *,
        status: str | None = None,
        device_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            p
            for p in self.proposals.values()
            if p.get("company_id", company_id) == company_id
            and (status is None or p.get("status") == status)
            and (
                device_id is None
                or (p.get("target_type") == "device" and p.get("target_id") == device_id)
            )
            and (thread_id is None or p.get("thread_id") == thread_id)
        ]


class InMemoryAuditService:
    """In-memory audit recorder for offline graph tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def record_standalone(
        self, ctx: Any, event_type: str, payload: Any, *, thread_id: str | None = None
    ) -> int:
        self.events.append((event_type, payload))
        return len(self.events)
