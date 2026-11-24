"""Centralized rate-limit, turn-cap, and cumulative-spend guard wrapping `CopilotService`.

All interactive entrypoints (UI, API, CLI) invoke `GuardedCopilot` so demo budgets and
turn limits are enforced consistently in one place backed by PostgreSQL (`copilot.spend_ledger`).
Offline/live eval runners call `CopilotService` directly so evaluations are never capped by
demo limits and never write to `copilot.spend_ledger`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

from fleet_copilot.agent.schemas import Answer, TurnResponse, TurnUsage
from fleet_copilot.clock import Clock
from fleet_copilot.config import Settings
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.actions import ActionService
from fleet_copilot.services.copilot import CopilotService, ThreadNotOwnedError

SPEND_RESERVE_USD = 0.05


def extract_rev_id(actor_id: str) -> str:
    """Extract reviewer identifier prefix from `actor_id` (`<rev_id>:<persona>` or raw actor)."""
    if ":" in actor_id:
        return actor_id.split(":", 1)[0]
    return actor_id


class GuardedCopilot:
    """Facade enforcing DB-backed demo limits before every new `CopilotService` turn."""

    def __init__(
        self,
        copilot: CopilotService,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
        require_api_key: bool | None = None,
    ) -> None:
        self._copilot = copilot
        self._settings = settings if settings is not None else copilot._settings
        self._clock = clock if clock is not None else copilot._clock
        self._store = copilot._store
        if require_api_key is None:
            self._require_api_key = self._settings.llm_replay_mode.strip().lower() != "replay"
        else:
            self._require_api_key = require_api_key

    @property
    def copilot(self) -> CopilotService:
        """Underlying `CopilotService` instance."""
        return self._copilot

    @property
    def action_service(self) -> ActionService:
        """Underlying `ActionService` instance."""
        return self._copilot.action_service

    def _has_live_api_key(self) -> bool:
        key = self._settings.gemini_api_key
        return key is not None and bool(key.get_secret_value().strip())

    def mode(self, company_id: str = "acme-001") -> Literal["live", "examples_only"]:
        """Return `'live'` when live turns can execute, else `'examples_only'`."""
        if self._require_api_key and not self._has_live_api_key():
            return "examples_only"
        spent = self._store.spend_total_usd(company_id)
        if spent + SPEND_RESERVE_USD > self._settings.demo_spend_cap_usd:
            return "examples_only"
        return "live"

    def _limited_response(
        self,
        *,
        ctx: TenantContext,
        thread_id: str,
        turn_id: str,
        code: str,
        message: str,
    ) -> TurnResponse:
        return TurnResponse(
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=ctx.trace_id,
            status="limited",
            answer_markdown=message,
            answer=Answer(summary=message),
            usage=TurnUsage(),
            warnings=[code],
        )

    def _check_limits(
        self,
        ctx: TenantContext,
        thread_id: str,
        turn_id: str,
    ) -> TurnResponse | None:
        if self._require_api_key and not self._has_live_api_key():
            return self._limited_response(
                ctx=ctx,
                thread_id=thread_id,
                turn_id=turn_id,
                code="live_unavailable",
                message=(
                    "Live AI turns are unavailable because no Gemini API key is configured. "
                    "You can still browse recorded examples in How it works -> Examples."
                ),
            )

        spent = self._store.spend_total_usd(ctx.company_id)
        if spent + SPEND_RESERVE_USD > self._settings.demo_spend_cap_usd:
            return self._limited_response(
                ctx=ctx,
                thread_id=thread_id,
                turn_id=turn_id,
                code="demo_budget_exhausted",
                message=(
                    "This demo has used its live AI budget. "
                    "You can still browse recorded examples in How it works -> Examples."
                ),
            )

        rev_id = extract_rev_id(ctx.actor_id)
        code_turns = self._store.turns_by_rev_since(ctx.company_id, rev_id, None)
        if code_turns >= self._settings.demo_turns_per_code:
            return self._limited_response(
                ctx=ctx,
                thread_id=thread_id,
                turn_id=turn_id,
                code="turn_cap_reached",
                message=(
                    f"This access code has reached its {self._settings.demo_turns_per_code}-turn "
                    "demo limit. You can still browse recorded examples in "
                    "How it works -> Examples."
                ),
            )

        since_1h = self._clock.now() - timedelta(hours=1)
        actor_turns = self._store.turns_by_actor_since(ctx.company_id, ctx.actor_id, since_1h)
        if actor_turns >= self._settings.demo_turns_per_hour:
            return self._limited_response(
                ctx=ctx,
                thread_id=thread_id,
                turn_id=turn_id,
                code="rate_limited",
                message=(
                    f"Rate limit reached ({self._settings.demo_turns_per_hour} turns per hour). "
                    "Please wait a bit before sending another message."
                ),
            )

        return None

    def _record_turn_attempt(
        self,
        ctx: TenantContext,
        turn_id: str,
        cost_usd: float,
    ) -> None:
        self._store.record_spend(
            ctx.company_id,
            actor_id=ctx.actor_id,
            rev_id=extract_rev_id(ctx.actor_id),
            turn_id=turn_id,
            cost_usd=max(0.0, float(cost_usd)),
            created_at=self._clock.now(),
        )

    def run_turn(
        self,
        ctx: TenantContext,
        thread_id: str | None,
        message: str,
        *,
        turn_id: str | None = None,
    ) -> TurnResponse:
        """Check all demo limits, execute a turn, and persist a spend-ledger row."""
        resolved_tid = thread_id or f"{ctx.company_id}:{uuid.uuid4().hex[:12]}"
        resolved_turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"

        limited = self._check_limits(ctx, resolved_tid, resolved_turn_id)
        if limited is not None:
            return limited

        resp: TurnResponse | None = None
        try:
            resp = self._copilot.run_turn(ctx, resolved_tid, message, turn_id=resolved_turn_id)
        except ThreadNotOwnedError:
            raise
        except Exception:
            self._record_turn_attempt(ctx, resolved_turn_id, 0.0)
            raise

        # Record every live turn attempt (including failed turns with $0 when usage is unknown);
        # skip only thread_busy rejections where no turn ran.
        if not any("thread_busy" in w for w in resp.warnings):
            self._record_turn_attempt(ctx, resp.turn_id, resp.usage.cost_usd_est)
        return resp

    def resume(
        self,
        ctx: TenantContext,
        thread_id: str,
        decisions: dict[str, str],
        *,
        note: str | None = None,
    ) -> TurnResponse:
        """Resume a paused approval interrupt (never counted as a new LLM turn)."""
        return self._copilot.resume(ctx, thread_id, decisions, note=note)

    def list_threads(
        self,
        ctx: TenantContext,
        *,
        include_hidden: bool = False,
        search: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List conversation threads owned by `(ctx.company_id, ctx.actor_id)`."""
        return self._copilot.list_threads(
            ctx, include_hidden=include_hidden, search=search, limit=limit
        )

    def get_thread(self, ctx: TenantContext, thread_id: str) -> dict[str, Any] | None:
        """Return one thread and its turns owned by `(ctx.company_id, ctx.actor_id)`."""
        return self._copilot.get_thread(ctx, thread_id)

    def rename_thread(self, ctx: TenantContext, thread_id: str, title: str) -> str:
        """Rename a thread owned by `(ctx.company_id, ctx.actor_id)`."""
        return self._copilot.rename_thread(ctx, thread_id, title)

    def hide_thread(self, ctx: TenantContext, thread_id: str, *, hidden: bool = True) -> None:
        """Hide or restore a thread owned by `(ctx.company_id, ctx.actor_id)`."""
        self._copilot.hide_thread(ctx, thread_id, hidden=hidden)
