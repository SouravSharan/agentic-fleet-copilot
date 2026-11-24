"""Strict audit event payload schemas and tamper-evident audit logging service."""

from __future__ import annotations

import json
from typing import Any, Literal

import psycopg
from psycopg import errors
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext

MAX_AUDIT_STR_LEN = 128

FORBIDDEN_AUDIT_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "message",
        "note",
        "prompt",
        "answer",
        "text",
        "content",
        "hostname",
        "employee_id",
    }
)


def _check_value_bounds(val: Any, path: str = "") -> None:
    if isinstance(val, str):
        if len(val) > MAX_AUDIT_STR_LEN:
            raise ValueError(
                f"Audit string field {path!r} exceeds {MAX_AUDIT_STR_LEN} characters "
                f"(got {len(val)})"
            )
    elif isinstance(val, list):
        for idx, item in enumerate(val):
            _check_value_bounds(item, f"{path}[{idx}]")
    elif isinstance(val, dict):
        for k, v in val.items():
            key_str = str(k)
            if key_str in FORBIDDEN_AUDIT_FIELD_NAMES:
                raise ValueError(f"Forbidden audit key name: {key_str!r}")
            if len(key_str) > MAX_AUDIT_STR_LEN:
                raise ValueError(f"Audit dict key exceeds {MAX_AUDIT_STR_LEN} characters")
            _check_value_bounds(v, f"{path}.{key_str}" if path else key_str)


class AuditPayload(BaseModel):
    """Base class for all structured audit payloads: no extras, no free text, <=128 char strings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _validate_bounds_and_keys(self) -> AuditPayload:
        for field_name in type(self).model_fields:
            if field_name in FORBIDDEN_AUDIT_FIELD_NAMES:
                raise ValueError(f"Forbidden audit field name: {field_name!r}")
            _check_value_bounds(getattr(self, field_name), field_name)
        return self


class TurnStartedPayload(AuditPayload):
    turn_id: str
    thread_id: str
    role: Literal["viewer", "it_admin"]
    model_id: str
    prompt_version: str
    input_hash: str


class PlanCreatedPayload(AuditPayload):
    turn_id: str
    intent: Literal["qa", "insight", "action", "mixed", "out_of_scope"]
    steps_count: int
    refers_to_other_org: bool


class ToolCalledPayload(AuditPayload):
    turn_id: str
    tool_name: str
    args_hash: str
    ok: bool
    evaluated: int
    matched: int
    evidence_count: int
    duration_ms: int


class AccessDeniedPayload(AuditPayload):
    reason_code: Literal[
        "cross_tenant_not_found",
        "role_forbidden",
        "tool_disabled",
        "thread_not_owned",
    ]
    tool_name: str


class RefusalPayload(AuditPayload):
    reason_code: str


class GroundingViolationPayload(AuditPayload):
    turn_id: str
    codes: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    outcome: Literal["retried", "stripped", "warned"]


class ProposalRefusedPayload(AuditPayload):
    tool_name: str
    target_type: Literal["device", "employee"]
    target_ref: str
    failed_checks: list[str] = Field(default_factory=list)
    policy_version: str


class ProposalStagedPayload(AuditPayload):
    proposal_id: str
    tool_name: str
    target_type: Literal["device", "employee"]
    target_ref: str
    dedupe_key_hash: str
    approval_hash: str
    evidence_snapshot_ids: list[str] = Field(default_factory=list)
    policy_version: str
    expires_at: str


class ApprovalRequestedPayload(AuditPayload):
    batch_id: str
    proposal_ids: list[str] = Field(default_factory=list)


class ProposalDecidedPayload(AuditPayload):
    proposal_id: str
    decision: Literal["approved", "rejected", "deferred"]
    via: Literal["chat", "queue", "api"]


class FreshnessBlockedPayload(AuditPayload):
    proposal_id: str
    failed_checks: list[str] = Field(default_factory=list)
    new_snapshot_ids: list[str] = Field(default_factory=list)


class ActionExecutedPayload(AuditPayload):
    proposal_id: str
    adapter: str
    receipt_id: str
    receipt_hash: str


class ActionFailedPayload(AuditPayload):
    proposal_id: str
    adapter: str
    error_code: str
    receipt_hash: str | None = None


class ProposalExpiredPayload(AuditPayload):
    proposal_id: str


class TurnCompletedPayload(AuditPayload):
    turn_id: str
    status: Literal["completed", "waiting_approval", "refused", "failed"]
    answer_hash: str
    citations: int
    tokens_in: int
    tokens_out: int
    cost_usd_est: float
    latency_ms: int
    error_code: str | None = None


class DemoResetPayload(AuditPayload):
    reason: str


class ChartCreatedPayload(AuditPayload):
    chart_id: str
    kind: str
    row_count: int


class ChartPinnedPayload(AuditPayload):
    chart_id: str
    pinned: bool


class ChartRefreshedPayload(AuditPayload):
    chart_id: str
    row_count: int


class ChartDeletedPayload(AuditPayload):
    chart_id: str


AUDIT_PAYLOAD_MODELS: dict[str, type[AuditPayload]] = {
    "turn_started": TurnStartedPayload,
    "plan_created": PlanCreatedPayload,
    "tool_called": ToolCalledPayload,
    "access_denied": AccessDeniedPayload,
    "refusal": RefusalPayload,
    "grounding_violation": GroundingViolationPayload,
    "proposal_refused": ProposalRefusedPayload,
    "proposal_staged": ProposalStagedPayload,
    "approval_requested": ApprovalRequestedPayload,
    "proposal_decided": ProposalDecidedPayload,
    "freshness_blocked": FreshnessBlockedPayload,
    "action_executed": ActionExecutedPayload,
    "action_failed": ActionFailedPayload,
    "proposal_expired": ProposalExpiredPayload,
    "turn_completed": TurnCompletedPayload,
    "demo_reset": DemoResetPayload,
    "chart_created": ChartCreatedPayload,
    "chart_pinned": ChartPinnedPayload,
    "chart_refreshed": ChartRefreshedPayload,
    "chart_deleted": ChartDeletedPayload,
}


def validate_audit_payload(
    event_type: str,
    payload: AuditPayload | dict[str, Any],
) -> AuditPayload:
    """Validate `payload` against the strict Pydantic model registered for `event_type`."""
    model_cls = AUDIT_PAYLOAD_MODELS.get(event_type)
    if model_cls is None:
        raise ValueError(f"Unknown audit event_type: {event_type!r}")
    if isinstance(payload, model_cls):
        return payload
    if isinstance(payload, AuditPayload):
        raise TypeError(
            f"Event {event_type!r} expects {model_cls.__name__}, got {type(payload).__name__}"
        )
    return model_cls.model_validate(payload)


class AuditService:
    """Writes validated events to `copilot.audit_logs` and verifies per-tenant hash chains."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def record(
        self,
        cur: psycopg.Cursor[Any],
        ctx: TenantContext,
        event_type: str,
        payload: AuditPayload | dict[str, Any],
        *,
        thread_id: str | None = None,
    ) -> int:
        """Insert a validated audit event inside the caller's existing transaction."""
        validated = validate_audit_payload(event_type, payload)
        payload_json = json.dumps(validated.model_dump(mode="json"), sort_keys=True)
        cur.execute(
            """
            INSERT INTO copilot.audit_logs (
                trace_id, thread_id, company_id, actor_id, actor_role, event_type, payload_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING seq_no
            """,
            (
                ctx.trace_id,
                thread_id,
                ctx.company_id,
                ctx.actor_id,
                ctx.role,
                event_type,
                payload_json,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Audit insert returned no seq_no")
        return int(row["seq_no"]) if isinstance(row, dict) else int(row[0])

    def record_standalone(
        self,
        ctx: TenantContext,
        event_type: str,
        payload: AuditPayload | dict[str, Any],
        *,
        thread_id: str | None = None,
    ) -> int:
        """Write an audit event in its own short transaction (retries once on serialisation)."""
        for attempt in range(2):
            try:
                with self._db.copilot_cursor(ctx.company_id) as cur:
                    return self.record(cur, ctx, event_type, payload, thread_id=thread_id)
            except errors.SerializationFailure:
                if attempt == 1:
                    raise
        raise RuntimeError("Unreachable")

    def list(
        self,
        ctx: TenantContext,
        *,
        event_type: str | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List audit events for `ctx.company_id` ordered chronologically by `seq_no`."""
        from psycopg import sql

        clauses: list[sql.SQL] = [sql.SQL("1=1")]
        params: list[Any] = []
        if event_type is not None:
            clauses.append(sql.SQL("event_type = %s"))
            params.append(event_type)
        if trace_id is not None:
            clauses.append(sql.SQL("trace_id = %s"))
            params.append(trace_id)
        if thread_id is not None:
            clauses.append(sql.SQL("thread_id = %s"))
            params.append(thread_id)
        params.append(limit)

        query = sql.SQL(
            """
            SELECT audit_id, seq_no, trace_id, thread_id, company_id, actor_id, actor_role,
                   event_type, payload_json, created_at
            FROM copilot.audit_logs
            WHERE {where}
            ORDER BY seq_no ASC, audit_id ASC
            LIMIT %s
            """
        ).format(where=sql.SQL(" AND ").join(clauses))

        with self._db.copilot_cursor(ctx.company_id) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def verify(self, ctx: TenantContext) -> dict[str, Any]:
        """Verify the per-tenant hash chain via `copilot.verify_audit_chain()` under RLS."""
        with self._db.copilot_cursor(ctx.company_id) as cur:
            cur.execute("SELECT ok, rows_checked, first_bad_seq FROM copilot.verify_audit_chain()")
            row = cur.fetchone()
            if row is None:
                return {"ok": False, "rows_checked": 0, "first_bad_seq": 1}
            return {
                "ok": bool(row["ok"]),
                "rows_checked": int(row["rows_checked"]),
                "first_bad_seq": row["first_bad_seq"],
            }
