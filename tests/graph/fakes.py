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
        self._software: list[SoftwareRecord] = []
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
        _ = (since, latest_only)
        valid_ids = {d.device_id for d in self.latest_states(company_id)}
        return [
            s
            for s in getattr(self, "_software", ())
            if s.device_id in valid_ids and (device_ids is None or s.device_id in device_ids)
        ]

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
        self.spend_ledger: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.charts: dict[str, dict[str, Any]] = {}

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

    def record_spend(
        self,
        company_id: str,
        *,
        actor_id: str,
        rev_id: str,
        turn_id: str,
        cost_usd: float,
        created_at: datetime,
    ) -> None:
        self.spend_ledger.append(
            {
                "company_id": company_id,
                "actor_id": actor_id,
                "rev_id": rev_id,
                "turn_id": turn_id,
                "cost_usd": max(0.0, float(cost_usd)),
                "created_at": created_at,
            }
        )

    def spend_total_usd(self, company_id: str) -> float:  # noqa: ARG002
        return sum(float(r["cost_usd"]) for r in self.spend_ledger)

    def turns_by_rev_since(
        self,
        company_id: str,  # noqa: ARG002
        rev_id: str,
        since: datetime | None = None,
    ) -> int:
        return sum(
            1
            for r in self.spend_ledger
            if r["rev_id"] == rev_id and (since is None or r["created_at"] >= since)
        )

    def turns_by_actor_since(
        self,
        company_id: str,
        actor_id: str,
        since: datetime,
    ) -> int:
        return sum(
            1
            for r in self.spend_ledger
            if r["company_id"] == company_id
            and r["actor_id"] == actor_id
            and r["created_at"] >= since
        )

    def list_outbox(
        self,
        company_id: str,
        *,
        channel: str | None = None,
        proposal_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for r in self.outbox
            if r["company_id"] == company_id
            and (channel is None or r["channel"] == channel)
            and (proposal_id is None or r["proposal_id"] == proposal_id)
        ]
        return rows[:limit]

    def insert_chart(
        self,
        company_id: str,
        *,
        chart_id: str,
        owner_actor_id: str,
        title: str,
        request_json: dict[str, Any],
        spec_json: dict[str, Any],
        data_json: list[dict[str, Any]],
        evidence_snapshot_ids: list[str],
        telemetry_as_of: datetime,
        thread_id: str | None = None,
        turn_id: str | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        row = {
            "chart_id": chart_id,
            "company_id": company_id,
            "owner_actor_id": owner_actor_id,
            "title": title,
            "request_json": dict(request_json),
            "spec_json": dict(spec_json),
            "data_json": list(data_json),
            "evidence_snapshot_ids": list(evidence_snapshot_ids),
            "telemetry_as_of": telemetry_as_of,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "pinned": pinned,
        }
        self.charts[chart_id] = row
        return dict(row)

    def list_charts(
        self,
        company_id: str,
        *,
        owner_actor_id: str,
        pinned_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.charts.values()
            if r.get("company_id") == company_id
            and r.get("owner_actor_id") == owner_actor_id
            and (not pinned_only or bool(r.get("pinned", False)))
        ]
        return rows[:limit]

    def get_chart(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
    ) -> dict[str, Any] | None:
        r = self.charts.get(chart_id)
        if r is None or r.get("company_id") != company_id:
            return None
        if r.get("owner_actor_id") != owner_actor_id:
            return None
        return dict(r)

    def pin_chart(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
        pinned: bool = True,
    ) -> dict[str, Any] | None:
        r = self.charts.get(chart_id)
        if r is None or r.get("company_id") != company_id:
            return None
        if r.get("owner_actor_id") != owner_actor_id:
            return None
        r["pinned"] = pinned
        return dict(r)

    def update_chart_in_place(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
        spec_json: dict[str, Any],
        data_json: list[dict[str, Any]],
        evidence_snapshot_ids: list[str],
        telemetry_as_of: datetime,
    ) -> dict[str, Any] | None:
        r = self.charts.get(chart_id)
        if r is None or r.get("company_id") != company_id:
            return None
        if r.get("owner_actor_id") != owner_actor_id:
            return None
        r["spec_json"] = dict(spec_json)
        r["data_json"] = list(data_json)
        r["evidence_snapshot_ids"] = list(evidence_snapshot_ids)
        r["telemetry_as_of"] = telemetry_as_of
        return dict(r)

    def delete_chart(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
    ) -> bool:
        r = self.charts.get(chart_id)
        if r is None or r.get("company_id") != company_id:
            return False
        if r.get("owner_actor_id") != owner_actor_id:
            return False
        del self.charts[chart_id]
        return True


class InMemoryAuditService:
    """In-memory audit recorder for offline graph tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    def record_standalone(
        self, ctx: Any, event_type: str, payload: Any, *, thread_id: str | None = None
    ) -> int:
        self.events.append((event_type, payload))
        seq = len(self.events)
        payload_dict = (
            payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        )
        self.records.append(
            {
                "audit_id": seq,
                "seq_no": seq,
                "trace_id": getattr(ctx, "trace_id", "trace-test"),
                "thread_id": thread_id,
                "company_id": getattr(ctx, "company_id", "acme-001"),
                "actor_id": getattr(ctx, "actor_id", "rev_test:acme_admin"),
                "actor_role": getattr(ctx, "role", "it_admin"),
                "event_type": event_type,
                "payload_json": payload_dict,
                "created_at": AS_OF,
            }
        )
        return seq

    def list(
        self,
        ctx: Any,
        *,
        event_type: str | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.records
            if r["company_id"] == getattr(ctx, "company_id", "")
            and (event_type is None or r["event_type"] == event_type)
            and (trace_id is None or r["trace_id"] == trace_id)
            and (thread_id is None or r["thread_id"] == thread_id)
        ]
        return rows[:limit]

    def verify(self, ctx: Any) -> dict[str, Any]:
        company_rows = [
            r for r in self.records if r["company_id"] == getattr(ctx, "company_id", "")
        ]
        return {"ok": True, "rows_checked": len(company_rows), "first_bad_seq": None}
