"""Thread-safe background turn job registry decoupling long turns from UI reruns.

Intentionally has zero Streamlit imports so it can be tested and reused independently.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.agent.schemas import ProgressEvent, TurnResponse
from fleet_copilot.clock import Clock, SystemClock
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.guarded import GuardedCopilot

logger = logging.getLogger(__name__)

TurnJobStatus = Literal["queued", "running", "done", "failed"]
_FRIENDLY_JOB_ERROR = (
    "Something went wrong while running this request. Please try again in a moment."
)


class TurnJobSnapshot(BaseModel):
    """Immutable point-in-time snapshot of a background turn job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    actor_id: str
    thread_id: str
    status: TurnJobStatus
    events: list[ProgressEvent] = Field(default_factory=list)
    result: TurnResponse | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


@dataclass
class _JobRecord:
    job_id: str
    actor_id: str
    thread_id: str
    active_key: tuple[str, str]
    status: TurnJobStatus
    created_at: datetime
    events: list[ProgressEvent] = field(default_factory=list)
    result: TurnResponse | None = None
    error: str | None = None
    completed_at: datetime | None = None

    def snapshot(self) -> TurnJobSnapshot:
        return TurnJobSnapshot(
            job_id=self.job_id,
            actor_id=self.actor_id,
            thread_id=self.thread_id,
            status=self.status,
            events=list(self.events),
            result=self.result,
            error=self.error,
            created_at=self.created_at,
            completed_at=self.completed_at,
        )


class TurnJobs:
    """Thread-safe background worker registry for streaming copilot turns."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        ttl_seconds: int = 900,
        clock: Clock | None = None,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="fleet-turn-job",
        )
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._jobs: dict[str, _JobRecord] = {}
        self._active_by_key: dict[tuple[str, str], str] = {}

    def _cleanup_locked(self) -> None:
        now = self._clock.now()
        expired = [
            jid
            for jid, rec in self._jobs.items()
            if rec.completed_at is not None and (now - rec.completed_at) >= self._ttl
        ]
        for jid in expired:
            rec = self._jobs.pop(jid, None)
            if rec is not None and self._active_by_key.get(rec.active_key) == jid:
                self._active_by_key.pop(rec.active_key, None)

    def submit(
        self,
        guarded: GuardedCopilot,
        ctx: TenantContext,
        thread_id: str | None,
        message: str,
        *,
        hidden: bool = False,
    ) -> str:
        """Submit a background turn job; deduplicates active jobs per `(actor_id, resolved_tid)`."""
        # Resolve thread_id BEFORE computing active_key so two new threads never collide (S2).
        resolved_tid = thread_id or f"{ctx.company_id}:{uuid.uuid4().hex[:12]}"
        active_key = (ctx.actor_id, resolved_tid)

        with self._lock:
            self._cleanup_locked()
            existing_id = self._active_by_key.get(active_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and existing.status in ("queued", "running"):
                    return existing_id

            job_id = f"job_{uuid.uuid4().hex[:12]}"
            rec = _JobRecord(
                job_id=job_id,
                actor_id=ctx.actor_id,
                thread_id=resolved_tid,
                active_key=active_key,
                status="queued",
                created_at=self._clock.now(),
            )
            self._jobs[job_id] = rec
            self._active_by_key[active_key] = job_id

        self._executor.submit(self._run_worker, job_id, guarded, ctx, resolved_tid, message, hidden)
        return job_id

    def _run_worker(
        self,
        job_id: str,
        guarded: GuardedCopilot,
        ctx: TenantContext,
        thread_id: str,
        message: str,
        hidden: bool = False,
    ) -> None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            rec.status = "running"

        final_resp: TurnResponse | None = None
        try:
            stream = (
                guarded.run_turn_stream(ctx, thread_id, message, hidden=True)
                if hidden
                else guarded.run_turn_stream(ctx, thread_id, message)
            )
            for ev in stream:
                with self._lock:
                    rec = self._jobs.get(job_id)
                    if rec is not None:
                        rec.events.append(ev)
                if ev.response is not None:
                    final_resp = ev.response
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "turn_job_failed",
                extra={
                    "company_id": ctx.company_id,
                    "trace_id": ctx.trace_id,
                    "error_class": type(exc).__name__,
                },
            )
            with self._lock:
                rec = self._jobs.get(job_id)
                if rec is not None:
                    rec.status = "failed"
                    rec.error = _FRIENDLY_JOB_ERROR
                    rec.completed_at = self._clock.now()
                    if self._active_by_key.get(rec.active_key) == job_id:
                        self._active_by_key.pop(rec.active_key, None)
            return

        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is not None:
                rec.result = final_resp
                rec.status = "done" if final_resp is not None else "failed"
                if final_resp is None:
                    rec.error = _FRIENDLY_JOB_ERROR
                rec.completed_at = self._clock.now()
                if self._active_by_key.get(rec.active_key) == job_id:
                    self._active_by_key.pop(rec.active_key, None)

    def get(self, job_id: str) -> TurnJobSnapshot | None:
        """Return an immutable snapshot of `job_id`, or `None` if unknown or expired."""
        with self._lock:
            self._cleanup_locked()
            rec = self._jobs.get(job_id)
            return rec.snapshot() if rec is not None else None

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the background worker pool."""
        self._executor.shutdown(wait=wait)
