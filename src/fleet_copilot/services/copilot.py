"""High-level `CopilotService` orchestrating turns, HITL resumes, auto-defer, and thread history."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from fleet_copilot.agent.graph import AgentDeps, build_agent_graph
from fleet_copilot.agent.llm import NodeName, cassette_path_for, make_chat_model
from fleet_copilot.agent.nodes.finalize import render_answer_markdown
from fleet_copilot.agent.replay import CassetteMiss, CassetteStore
from fleet_copilot.agent.schemas import Answer, Plan, TurnResponse, TurnUsage
from fleet_copilot.clock import Clock, SystemClock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db.postgres import DatabaseManager, make_checkpointer
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.observability.logging import bind_trace_id
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.runtime_config import RuntimeConfig, get_runtime_config
from fleet_copilot.services.actions import ActionService
from fleet_copilot.services.adapters.base import ActionAdapter
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import (
    AccessDeniedPayload,
    AuditService,
    TurnCompletedPayload,
)
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.tools.registry import ToolDeps, build_all_tool_specs

logger = logging.getLogger(__name__)

_RUN_LEASE_SECONDS = 90
_MAX_TURNS_PER_THREAD = 30
_MAX_THREAD_TITLE_LEN = 60


def _title_from_message(message: str, *, max_len: int = _MAX_THREAD_TITLE_LEN) -> str:
    collapsed = " ".join(message.split())
    if not collapsed:
        return "New conversation"
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


class ThreadNotOwnedError(PermissionError):
    """Raised when a caller attempts to access or resume a thread outside their tenant or actor."""


class CopilotService:
    """Facade managing graph execution, thread ownership, auto-defer, and usage accounting."""

    def __init__(
        self,
        *,
        db: DatabaseManager | None = None,
        telemetry_repo: Any | None = None,
        copilot_store: Any | None = None,
        policy_svc: PolicyService | None = None,
        audit_svc: Any | None = None,
        action_svc: ActionService | None = None,
        adapter: ActionAdapter | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        clock: Clock | None = None,
        settings: Settings | None = None,
        runtime_cfg: RuntimeConfig | None = None,
        model_factory: Callable[[NodeName], BaseChatModel] | None = None,
        model_id: str | None = None,
        cassette_name: str | None = None,
        cassette_store: CassetteStore | None = None,
        thinking_override: str | None = None,
        skip_planner: bool = False,
        skip_verifier: bool = False,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._runtime_cfg = runtime_cfg if runtime_cfg is not None else get_runtime_config()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._db = db

        if telemetry_repo is None or copilot_store is None or audit_svc is None:
            if db is None:
                raise ValueError("Provide either `db` or explicit repo/store/audit dependencies")
            resolved_repo: Any = telemetry_repo or PgTelemetryRepository(db, self._settings)
            resolved_store: Any = copilot_store or PgCopilotStore(db)
            resolved_audit: Any = audit_svc or AuditService(db)
        else:
            resolved_repo = telemetry_repo
            resolved_store = copilot_store
            resolved_audit = audit_svc

        self._repo: Any = resolved_repo
        self._store: Any = resolved_store
        self._policy_svc = policy_svc or PolicyService(resolved_store)
        self._audit: Any = resolved_audit
        self._adapter: ActionAdapter = adapter or MockAdapter()
        self._action_svc = action_svc or ActionService(
            db,  # type: ignore[arg-type]
            resolved_repo,
            resolved_store,
            self._policy_svc,
            resolved_audit,
            self._adapter,
            settings=self._settings,
        )

        self._model_id = model_id or self._settings.llm_model
        replay_mode = self._settings.llm_replay_mode.strip().lower()
        shared_store = cassette_store
        if shared_store is None and replay_mode in ("record", "replay"):
            c_path = cassette_path_for(
                cassette_dir=self._settings.llm_cassette_dir,
                model_spec=self._model_id,
                case_or_session=cassette_name or "default",
            )
            shared_store = CassetteStore(c_path)
        self._cassette_store = shared_store

        if model_factory is None:

            def _default_factory(node: NodeName) -> BaseChatModel:
                return make_chat_model(
                    node,
                    settings=self._settings,
                    runtime_cfg=self._runtime_cfg,
                    cassette_name=cassette_name,
                    cassette_store=self._cassette_store,
                    model_override=self._model_id,
                    thinking_override=thinking_override,  # type: ignore[arg-type]
                )

            self._model_factory: Callable[[NodeName], BaseChatModel] = _default_factory
        else:
            self._model_factory = model_factory

        tool_deps = ToolDeps(
            telemetry_repo=self._repo,
            copilot_store=self._store,
            policy_svc=self._policy_svc,
            action_svc=self._action_svc,
            clock=self._clock,
            db=self._db,
            audit_svc=self._audit,
        )
        all_specs = build_all_tool_specs(tool_deps)

        self._deps = AgentDeps(
            telemetry_repo=self._repo,
            copilot_store=self._store,
            policy_svc=self._policy_svc,
            action_svc=self._action_svc,
            audit_svc=self._audit,
            clock=self._clock,
            runtime_cfg=self._runtime_cfg,
            all_tool_specs=all_specs,
            model_factory=self._model_factory,
            model_id=self._model_id,
            actions_enabled=self._settings.actions_enabled,
            skip_planner=skip_planner,
            skip_verifier=skip_verifier,
        )
        self._checkpointer = (
            checkpointer
            if checkpointer is not None
            else (make_checkpointer(db, settings=self._settings) if db is not None else None)
        )
        self._graph = build_agent_graph(self._deps, checkpointer=self._checkpointer)
        self._turn_timeout_override_s: float | None = None

    def _turn_deadline(self, t0: float) -> float:
        timeout_s = (
            self._turn_timeout_override_s
            if self._turn_timeout_override_s is not None
            else float(self._runtime_cfg.budgets.turn_timeout_s)
        )
        return t0 + timeout_s

    def _paused_turn_id(self, config: dict[str, Any]) -> str | None:
        if self._checkpointer is None:
            return None
        try:
            snap = self._graph.get_state(config)
        except Exception:  # noqa: BLE001
            return None
        if snap is None or not isinstance(getattr(snap, "values", None), dict):
            return None
        tid = snap.values.get("turn_id")
        return str(tid) if tid else None

    def _truthful_proposal_status_note(self, ctx: TenantContext, turn_id: str) -> str:
        """Query persisted proposals for `turn_id` and summarize their actual DB statuses."""
        try:
            all_props = self._store.list_proposals(ctx.company_id)
            turn_props = [p for p in all_props if str(p.get("turn_id", "")) == turn_id]
        except Exception:  # noqa: BLE001
            return "Unable to verify proposal state for this turn; check the Approvals queue."
        if not turn_props:
            return "No proposals were staged or executed for this turn."
        counts = Counter(str(p.get("status", "unknown")) for p in turn_props)
        summary = ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))
        return f"Current proposal statuses for this turn: {summary}."

    def _handle_turn_failure(
        self,
        *,
        ctx: TenantContext,
        thread_id: str,
        turn_id: str,
        exc: Exception,
        t0: float,
    ) -> TurnResponse:
        latency_ms = max(0, int((time.monotonic() - t0) * 1000))
        error_code = type(exc).__name__[:64]
        status_note = self._truthful_proposal_status_note(ctx, turn_id)
        friendly = (
            f"This request could not be completed (`{error_code}`). {status_note} Please retry."
        )
        answer_hash = hashlib.sha256(friendly.encode("utf-8")).hexdigest()
        try:
            self._store.update_turn(
                ctx.company_id,
                turn_id,
                status="failed",
                assistant_response=friendly,
                answer_hash=answer_hash,
                latency_ms=latency_ms,
                completed_at=self._clock.now(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed_to_update_turn_status_on_error")
        try:
            self._audit.record_standalone(
                ctx,
                "turn_completed",
                TurnCompletedPayload(
                    turn_id=turn_id,
                    status="failed",
                    answer_hash=answer_hash,
                    citations=0,
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd_est=0.0,
                    latency_ms=latency_ms,
                    error_code=error_code,
                ),
                thread_id=thread_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed_to_audit_turn_failure")
        logger.error(
            "turn_failed",
            extra={
                "company_id": ctx.company_id,
                "turn_id": turn_id,
                "error_class": error_code,
                "latency_ms": latency_ms,
            },
        )
        return TurnResponse(
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=ctx.trace_id,
            status="failed",
            answer_markdown=friendly,
            answer=Answer(summary=friendly),
            usage=TurnUsage(latency_ms=latency_ms),
            warnings=[f"Turn failed ({error_code})."],
        )

    @property
    def action_service(self) -> ActionService:
        return self._action_svc

    @property
    def audit_service(self) -> Any:
        return self._audit

    def _assert_thread_owned(
        self,
        ctx: TenantContext,
        thread_id: str,
        *,
        require_existing: bool = False,
    ) -> dict[str, Any] | None:
        """Enforce tenant-prefix AND actor ownership (`company_id` + `actor_id`)."""
        if not thread_id.startswith(f"{ctx.company_id}:"):
            self._audit.record_standalone(
                ctx,
                "access_denied",
                AccessDeniedPayload(reason_code="thread_not_owned", tool_name="copilot_service"),
            )
            raise ThreadNotOwnedError(f"Thread {thread_id!r} does not belong to {ctx.company_id}")

        existing = self._store.get_thread(ctx.company_id, thread_id)
        if existing is None:
            if require_existing:
                self._audit.record_standalone(
                    ctx,
                    "access_denied",
                    AccessDeniedPayload(
                        reason_code="thread_not_owned", tool_name="copilot_service"
                    ),
                )
                raise ThreadNotOwnedError(
                    f"Thread {thread_id!r} does not exist or is not owned by {ctx.actor_id}"
                )
            return None

        if str(existing.get("actor_id", "")) != ctx.actor_id:
            self._audit.record_standalone(
                ctx,
                "access_denied",
                AccessDeniedPayload(reason_code="thread_not_owned", tool_name="copilot_service"),
            )
            raise ThreadNotOwnedError(f"Thread {thread_id!r} does not belong to {ctx.actor_id}")
        return existing

    def _thread_busy_response(
        self,
        *,
        ctx: TenantContext,
        thread_id: str,
        turn_id: str,
        t0: float,
    ) -> TurnResponse:
        latency_ms = max(0, int((time.monotonic() - t0) * 1000))
        friendly = (
            "This conversation already has a request in progress (`thread_busy`). "
            "Please wait a moment and retry."
        )
        logger.warning(
            "thread_busy",
            extra={
                "company_id": ctx.company_id,
                "turn_id": turn_id,
                "error_class": "thread_busy",
                "latency_ms": latency_ms,
            },
        )
        return TurnResponse(
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=ctx.trace_id,
            status="failed",
            answer_markdown=friendly,
            answer=Answer(summary=friendly),
            usage=TurnUsage(latency_ms=latency_ms),
            warnings=["Turn failed (thread_busy)."],
        )

    def _safe_release_lease(
        self,
        ctx: TenantContext,
        thread_id: str,
        lease_until: Any,
    ) -> None:
        try:
            self._store.release_run_lease(
                ctx.company_id,
                thread_id,
                ctx.actor_id,
                lease_until=lease_until,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed_to_release_thread_run_lease")

    def _persist_response_json(self, ctx: TenantContext, resp: TurnResponse) -> None:
        if not resp.turn_id:
            return
        try:
            self._store.update_turn(
                ctx.company_id,
                resp.turn_id,
                response_json=resp.model_dump(mode="json"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed_to_persist_response_json")

    def _pending_interrupt_packet(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if self._checkpointer is None:
            return None
        try:
            snap = self._graph.get_state(config)
        except Exception:  # noqa: BLE001
            return None
        if snap is None:
            return None
        interrupts = getattr(snap, "interrupts", None)
        if interrupts:
            val = getattr(interrupts[0], "value", None)
            if isinstance(val, dict):
                return val
        for task in getattr(snap, "tasks", ()) or ():
            for intr in getattr(task, "interrupts", ()) or ():
                val = getattr(intr, "value", None)
                if isinstance(val, dict):
                    return val
        return None

    def _build_turn_response(
        self,
        *,
        ctx: TenantContext,
        thread_id: str,
        state: dict[str, Any],
        approval_packet: dict[str, Any] | None,
        latency_ms: int,
    ) -> TurnResponse:
        turn_id = str(state.get("turn_id") or "")
        evidence_map = dict(state.get("evidence") or {})
        raw_plan = state.get("plan")
        plan_obj = Plan.model_validate(raw_plan) if raw_plan else None
        raw_answer = state.get("answer")
        answer_obj = Answer.model_validate(raw_answer) if raw_answer else None
        warnings = list(state.get("warnings") or [])
        telemetry_as_of = state.get("telemetry_as_of")

        usage_list = list(state.get("usage") or [])
        tokens_in = sum(int(u.get("input_tokens", 0) or 0) for u in usage_list)
        tokens_out = sum(int(u.get("output_tokens", 0) or 0) for u in usage_list)
        cost_est = self._runtime_cfg.estimate_cost_usd(self._model_id, tokens_in, tokens_out)

        if approval_packet is not None:
            md, citations = render_answer_markdown(
                answer=answer_obj,
                plan=plan_obj,
                evidence_map=evidence_map,
                execution_results=[],
                warnings=warnings,
                telemetry_as_of=telemetry_as_of or "",
            )
            status = "waiting_approval"
        else:
            md = str(state.get("answer_markdown") or "")
            _, citations = render_answer_markdown(
                answer=answer_obj,
                plan=plan_obj,
                evidence_map=evidence_map,
                execution_results=list(state.get("execution_results") or []),
                warnings=warnings,
                telemetry_as_of=telemetry_as_of or "",
            )
            raw_status = str(state.get("status") or "completed")
            status = (
                raw_status
                if raw_status in ("completed", "waiting_approval", "refused", "failed")
                else "completed"
            )

        return TurnResponse(
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=ctx.trace_id,
            status=status,  # type: ignore[arg-type]
            answer_markdown=md,
            answer=answer_obj,
            citations=citations,
            insights=list(state.get("insights") or []),
            charts=list(state.get("charts") or []),
            queries=list(state.get("queries") or []),
            proposals=list(state.get("staged_proposals") or []),
            verdicts=list(state.get("verdicts") or []),
            approval_packet=approval_packet,
            plan=plan_obj,
            tool_trace=list(state.get("tool_trace") or []),
            telemetry_as_of=telemetry_as_of,
            usage=TurnUsage(
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                total_tokens=tokens_in + tokens_out,
                cost_usd_est=cost_est,
                latency_ms=latency_ms,
            ),
            warnings=warnings,
        )

    def run_turn(
        self,
        ctx: TenantContext,
        thread_id: str | None,
        message: str,
        *,
        turn_id: str | None = None,
    ) -> TurnResponse:
        """Process one user message, auto-deferring any pending HITL interrupt on the thread."""
        with bind_trace_id(ctx.trace_id):
            t0 = time.monotonic()
            tid = thread_id or f"{ctx.company_id}:{uuid.uuid4().hex[:12]}"
            resolved_turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
            self._assert_thread_owned(ctx, tid)

            if self._store.count_turns(ctx.company_id, tid) >= _MAX_TURNS_PER_THREAD:
                latency_ms = max(0, int((time.monotonic() - t0) * 1000))
                msg = (
                    f"This conversation has reached the {_MAX_TURNS_PER_THREAD}-turn limit "
                    "(`thread_turn_limit`). Please start a new chat."
                )
                return TurnResponse(
                    thread_id=tid,
                    turn_id=resolved_turn_id,
                    trace_id=ctx.trace_id,
                    status="failed",
                    answer_markdown=msg,
                    answer=Answer(summary=msg),
                    usage=TurnUsage(latency_ms=latency_ms),
                    warnings=[f"Turn limit reached ({_MAX_TURNS_PER_THREAD} turns max)."],
                )

            self._store.ensure_thread(
                ctx.company_id,
                tid,
                ctx.actor_id,
                title=_title_from_message(message),
            )
            now = self._clock.now()
            lease_until = now + timedelta(seconds=_RUN_LEASE_SECONDS)
            if not self._store.acquire_run_lease(
                ctx.company_id,
                tid,
                ctx.actor_id,
                now=now,
                lease_until=lease_until,
            ):
                # Re-check ownership in case two actors raced on the same new thread_id
                self._assert_thread_owned(ctx, tid)
                return self._thread_busy_response(
                    ctx=ctx, thread_id=tid, turn_id=resolved_turn_id, t0=t0
                )

            config: dict[str, Any] = {
                "configurable": {
                    "thread_id": tid,
                    "_turn_t0": t0,
                    "_turn_deadline": self._turn_deadline(t0),
                },
                "recursion_limit": self._runtime_cfg.budgets.recursion_limit,
            }

            try:
                # Pending-interrupt rule (05 §3.2): auto-defer undecided proposals before new msg
                pending_packet = self._pending_interrupt_packet(config)
                if pending_packet is not None:
                    pending_ids = [
                        str(p["proposal_id"]) for p in pending_packet.get("proposals", [])
                    ]
                    if pending_ids:
                        self._graph.invoke(
                            Command(
                                resume={
                                    "decisions": {pid: "defer" for pid in pending_ids},
                                    "note": "Auto-deferred by new user message",
                                }
                            ),
                            config=config,
                            context=ctx,
                        )

                result_state = self._graph.invoke(
                    {
                        "thread_id": tid,
                        "turn_id": resolved_turn_id,
                        "user_message": message,
                        "messages": [HumanMessage(content=message)],
                    },
                    config=config,
                    context=ctx,
                )
            except (ThreadNotOwnedError, CassetteMiss):
                raise
            except Exception as exc:  # noqa: BLE001
                return self._handle_turn_failure(
                    ctx=ctx,
                    thread_id=tid,
                    turn_id=resolved_turn_id,
                    exc=exc,
                    t0=t0,
                )
            finally:
                self._safe_release_lease(ctx, tid, lease_until)

            packet = self._pending_interrupt_packet(config)
            if (
                packet is None
                and isinstance(result_state, dict)
                and "__interrupt__" in result_state
            ):
                intrs = result_state["__interrupt__"]
                if intrs:
                    val = getattr(intrs[0], "value", None)
                    if isinstance(val, dict):
                        packet = val

            latency_ms = max(0, int((time.monotonic() - t0) * 1000))
            resp = self._build_turn_response(
                ctx=ctx,
                thread_id=tid,
                state=dict(result_state),
                approval_packet=packet,
                latency_ms=latency_ms,
            )
            self._persist_response_json(ctx, resp)
            return resp

    def resume(
        self,
        ctx: TenantContext,
        thread_id: str,
        decisions: dict[str, str],
        *,
        note: str | None = None,
    ) -> TurnResponse:
        """Resume a paused HITL interrupt with reviewer decisions (`approve|reject|defer`)."""
        with bind_trace_id(ctx.trace_id):
            t0 = time.monotonic()
            self._assert_thread_owned(ctx, thread_id, require_existing=True)
            config: dict[str, Any] = {
                "configurable": {"thread_id": thread_id, "_turn_t0": t0},
                "recursion_limit": self._runtime_cfg.budgets.recursion_limit,
            }
            paused_turn_id = self._paused_turn_id(config) or f"resume_{uuid.uuid4().hex[:8]}"

            now = self._clock.now()
            lease_until = now + timedelta(seconds=_RUN_LEASE_SECONDS)
            if not self._store.acquire_run_lease(
                ctx.company_id,
                thread_id,
                ctx.actor_id,
                now=now,
                lease_until=lease_until,
            ):
                return self._thread_busy_response(
                    ctx=ctx, thread_id=thread_id, turn_id=paused_turn_id, t0=t0
                )

            try:
                result_state = self._graph.invoke(
                    Command(resume={"decisions": decisions, "note": note}),
                    config=config,
                    context=ctx,
                )
            except (ThreadNotOwnedError, CassetteMiss):
                raise
            except Exception as exc:  # noqa: BLE001
                return self._handle_turn_failure(
                    ctx=ctx,
                    thread_id=thread_id,
                    turn_id=paused_turn_id,
                    exc=exc,
                    t0=t0,
                )
            finally:
                self._safe_release_lease(ctx, thread_id, lease_until)

            packet = self._pending_interrupt_packet(config)
            latency_ms = max(0, int((time.monotonic() - t0) * 1000))
            resp = self._build_turn_response(
                ctx=ctx,
                thread_id=thread_id,
                state=dict(result_state),
                approval_packet=packet,
                latency_ms=latency_ms,
            )
            self._persist_response_json(ctx, resp)
            return resp

    def list_threads(
        self,
        ctx: TenantContext,
        *,
        include_hidden: bool = False,
        search: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List conversation threads owned by `(ctx.company_id, ctx.actor_id)`."""
        return list(
            self._store.list_threads(
                ctx.company_id,
                actor_id=ctx.actor_id,
                include_hidden=include_hidden,
                search=search,
                limit=limit,
            )
        )

    def rename_thread(self, ctx: TenantContext, thread_id: str, title: str) -> str:
        """Rename an actor-owned thread (trimmed to <= 60 chars)."""
        self._assert_thread_owned(ctx, thread_id, require_existing=True)
        clean = _title_from_message(title)
        self._store.rename_thread(ctx.company_id, thread_id, ctx.actor_id, clean)
        return clean

    def hide_thread(self, ctx: TenantContext, thread_id: str, *, hidden: bool = True) -> None:
        """Soft-hide (or unhide) an actor-owned thread while keeping turns for audit."""
        self._assert_thread_owned(ctx, thread_id, require_existing=True)
        self._store.hide_thread(ctx.company_id, thread_id, ctx.actor_id, hidden=hidden)

    def get_thread(self, ctx: TenantContext, thread_id: str) -> dict[str, Any] | None:
        """Return one thread and its turns owned by `(ctx.company_id, ctx.actor_id)`."""
        thread = self._assert_thread_owned(ctx, thread_id)
        if thread is None:
            return None
        turns = list(self._store.list_turns(ctx.company_id, thread_id))
        return {"thread": dict(thread), "turns": turns}
