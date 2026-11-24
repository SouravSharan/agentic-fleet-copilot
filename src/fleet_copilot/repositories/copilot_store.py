"""Tenant-scoped copilot repository for policies, tool configs, catalog, threads, and proposals."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import DictRow

from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import PolicyRow, ToolConfigRow
from fleet_copilot.domain.policy_rules import HardwareCatalogRow


class CopilotStore(Protocol):
    """Protocol for accessing copilot schema operational tables."""

    def policies(self, company_id: str) -> list[PolicyRow]: ...

    def tool_configs(self, company_id: str) -> list[ToolConfigRow]: ...

    def hardware_catalog(self, company_id: str) -> dict[str, HardwareCatalogRow]: ...


class PgCopilotStore:
    """PostgreSQL implementation backed by `copilot_cursor` and RLS."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def policies(self, company_id: str) -> list[PolicyRow]:
        """Return global `'*'` and tenant policy rows visible under RLS for `company_id`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT company_id, scope_type, scope_value, metric_key, value_json
                FROM copilot.tenant_policies
                ORDER BY company_id ASC, scope_type ASC, scope_value ASC, metric_key ASC
                """
            )
            return [
                PolicyRow(
                    company_id=str(r["company_id"]),
                    scope_type=r["scope_type"],
                    scope_value=str(r["scope_value"]),
                    metric_key=str(r["metric_key"]),
                    value=r["value_json"],
                )
                for r in cur.fetchall()
            ]

    def tool_configs(self, company_id: str) -> list[ToolConfigRow]:
        """Return global `'*'` and tenant tool configuration rows visible under RLS."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT company_id, tool_name, enabled, requires_approval, min_role,
                       adapter_config_json
                FROM copilot.tenant_tool_configs
                ORDER BY company_id ASC, tool_name ASC
                """
            )
            return [
                ToolConfigRow(
                    company_id=str(r["company_id"]),
                    tool_name=str(r["tool_name"]),
                    enabled=bool(r["enabled"]),
                    requires_approval=bool(r["requires_approval"]),
                    min_role=r["min_role"],
                    adapter_config=dict(r["adapter_config_json"] or {}),
                )
                for r in cur.fetchall()
            ]

    def is_tool_enabled(self, company_id: str, tool_name: str) -> bool:
        """Evaluate global kill-switch (`'*'`) first, then tenant override for `tool_name`."""
        configs = [c for c in self.tool_configs(company_id) if c.tool_name == tool_name]
        global_cfg = next((c for c in configs if c.company_id == "*"), None)
        tenant_cfg = next((c for c in configs if c.company_id == company_id), None)
        if global_cfg is not None and not global_cfg.enabled:
            return False
        if tenant_cfg is not None:
            return tenant_cfg.enabled
        return global_cfg.enabled if global_cfg is not None else False

    def hardware_catalog(self, company_id: str) -> dict[str, HardwareCatalogRow]:
        """Load all platform hardware catalog rows keyed by `model_identifier`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT model_identifier, model_name, form_factor, ram_upgradable,
                       ram_max_gb, storage_upgradable, battery_serviceable,
                       source_urls, confidence, verified_at
                FROM copilot.hardware_catalog
                ORDER BY model_identifier ASC
                """
            )
            out: dict[str, HardwareCatalogRow] = {}
            for r in cur.fetchall():
                row = HardwareCatalogRow(
                    model_identifier=str(r["model_identifier"]),
                    model_name=str(r["model_name"]),
                    form_factor=r["form_factor"],
                    ram_upgradable=bool(r["ram_upgradable"]),
                    ram_max_gb=r["ram_max_gb"],
                    storage_upgradable=bool(r["storage_upgradable"]),
                    battery_serviceable=bool(r["battery_serviceable"]),
                    source_urls=list(r["source_urls"] or []),
                    confidence=r["confidence"],
                    verified_at=str(r["verified_at"]),
                )
                out[row.model_identifier] = row
            return out

    def catalog_lookup(
        self,
        company_id: str,
        model_identifier: str | None,
    ) -> HardwareCatalogRow | None:
        """Look up a single hardware catalog row by `model_identifier`."""
        if not model_identifier:
            return None
        return self.hardware_catalog(company_id).get(model_identifier)

    # --- Threads & Turns ---

    def ensure_thread(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        *,
        title: str | None = None,
    ) -> None:
        """Create a thread row idempotently inside `company_id`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                INSERT INTO copilot.threads (thread_id, company_id, actor_id, title)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (thread_id) DO NOTHING
                """,
                (thread_id, company_id, actor_id, title),
            )

    def get_thread(self, company_id: str, thread_id: str) -> dict[str, Any] | None:
        """Fetch one thread row inside `company_id` (RLS-scoped)."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT thread_id, company_id, actor_id, title, status,
                       hidden, run_lease_until, created_at, updated_at
                FROM copilot.threads
                WHERE thread_id = %s
                """,
                (thread_id,),
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def list_threads(
        self,
        company_id: str,
        *,
        actor_id: str,
        include_hidden: bool = False,
        search: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List conversation threads owned by `(company_id, actor_id)`."""
        clauses: list[sql.SQL] = [sql.SQL("actor_id = %s")]
        params: list[Any] = [actor_id]
        if not include_hidden:
            clauses.append(sql.SQL("hidden = false"))
        if search and search.strip():
            clauses.append(sql.SQL("COALESCE(title, '') ILIKE %s"))
            params.append(f"%{search.strip()}%")
        params.append(limit)
        query = sql.SQL(
            """
            SELECT thread_id, company_id, actor_id, title, status,
                   hidden, run_lease_until, created_at, updated_at
            FROM copilot.threads
            WHERE {where}
            ORDER BY updated_at DESC, thread_id ASC
            LIMIT %s
            """
        ).format(where=sql.SQL(" AND ").join(clauses))
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def rename_thread(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        title: str,
    ) -> None:
        """Update the title of an actor-owned thread."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                UPDATE copilot.threads
                SET title = %s,
                    updated_at = now()
                WHERE thread_id = %s
                  AND actor_id = %s
                """,
                (title, thread_id, actor_id),
            )

    def hide_thread(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        *,
        hidden: bool = True,
    ) -> None:
        """Soft-hide (or unhide) an actor-owned thread while retaining turns for audit."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                UPDATE copilot.threads
                SET hidden = %s,
                    updated_at = now()
                WHERE thread_id = %s
                  AND actor_id = %s
                """,
                (hidden, thread_id, actor_id),
            )

    def count_turns(self, company_id: str, thread_id: str) -> int:
        """Return total persisted turn count for `thread_id` within `company_id`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM copilot.turns WHERE thread_id = %s",
                (thread_id,),
            )
            row = cur.fetchone()
            return int(row["n"]) if row is not None else 0

    def list_turns(self, company_id: str, thread_id: str) -> list[dict[str, Any]]:
        """Return ordered turn trace rows for `thread_id` within `company_id`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT turn_id, trace_id, actor_id, user_message, plan_json, tool_calls_json,
                       evidence_json, assistant_response, response_json, status, model_id,
                       prompt_version, input_hash, answer_hash, input_tokens, output_tokens,
                       cost_usd_est, latency_ms, created_at, completed_at, redacted_at
                FROM copilot.turns
                WHERE thread_id = %s
                ORDER BY created_at ASC
                """,
                (thread_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def acquire_run_lease(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """Atomically acquire a compare-and-set run lease on `(thread_id, actor_id)`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                UPDATE copilot.threads
                SET run_lease_until = %s,
                    updated_at = %s
                WHERE thread_id = %s
                  AND actor_id = %s
                  AND (run_lease_until IS NULL OR run_lease_until <= %s)
                RETURNING thread_id
                """,
                (lease_until, now, thread_id, actor_id, now),
            )
            return cur.fetchone() is not None

    def release_run_lease(
        self,
        company_id: str,
        thread_id: str,
        actor_id: str,
        *,
        lease_until: datetime,
    ) -> None:
        """Release a run lease only if it still matches the caller's `lease_until` token."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                UPDATE copilot.threads
                SET run_lease_until = NULL
                WHERE thread_id = %s
                  AND actor_id = %s
                  AND run_lease_until = %s
                """,
                (thread_id, actor_id, lease_until),
            )

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
        """Create a turn row idempotently inside `company_id`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                INSERT INTO copilot.turns (
                    turn_id, trace_id, thread_id, company_id, actor_id,
                    user_message, status, input_hash
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (turn_id) DO NOTHING
                """,
                (
                    turn_id,
                    trace_id,
                    thread_id,
                    company_id,
                    actor_id,
                    user_message,
                    status,
                    input_hash,
                ),
            )

    def update_turn(
        self,
        company_id: str,
        turn_id: str,
        *,
        status: str | None = None,
        plan_json: dict[str, Any] | None = None,
        tool_calls_json: list[dict[str, Any]] | None = None,
        evidence_json: dict[str, Any] | list[dict[str, Any]] | None = None,
        assistant_response: str | None = None,
        response_json: dict[str, Any] | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        input_hash: str | None = None,
        answer_hash: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd_est: float | None = None,
        latency_ms: int | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Update mutable trace fields on an existing turn row."""
        sets: list[sql.Composable] = []
        params: list[Any] = []
        mapping: list[tuple[str, Any, bool]] = [
            ("status", status, False),
            ("plan_json", plan_json, True),
            ("tool_calls_json", tool_calls_json, True),
            ("evidence_json", evidence_json, True),
            ("assistant_response", assistant_response, False),
            ("response_json", response_json, True),
            ("model_id", model_id, False),
            ("prompt_version", prompt_version, False),
            ("input_hash", input_hash, False),
            ("answer_hash", answer_hash, False),
            ("input_tokens", input_tokens, False),
            ("output_tokens", output_tokens, False),
            ("cost_usd_est", cost_usd_est, False),
            ("latency_ms", latency_ms, False),
            ("completed_at", completed_at, False),
        ]
        for col, val, is_json in mapping:
            if val is not None:
                if is_json:
                    sets.append(sql.SQL("{} = %s::jsonb").format(sql.Identifier(col)))
                    params.append(json.dumps(val))
                else:
                    sets.append(sql.SQL("{} = %s").format(sql.Identifier(col)))
                    params.append(val)
        if not sets:
            return
        params.append(turn_id)
        query = sql.SQL("UPDATE copilot.turns SET {sets} WHERE turn_id = %s").format(
            sets=sql.SQL(", ").join(sets)
        )
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(query, params)

    # --- Action Proposals ---

    def insert_proposal(
        self,
        cur: psycopg.Cursor[DictRow],
        *,
        proposal_id: str,
        batch_id: str,
        thread_id: str,
        turn_id: str,
        company_id: str,
        proposed_by_actor_id: str,
        tool_name: str,
        target_type: str,
        target_id: str,
        arguments_json: dict[str, Any],
        rationale: str,
        evidence_snapshot_ids: list[str],
        evidence_as_of: datetime,
        policy_check_json: dict[str, Any],
        dedupe_key: str,
        approval_hash: str,
        policy_version: str,
        expires_at: datetime,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Idempotently insert a pending proposal (`ON CONFLICT (proposal_id) DO NOTHING`)."""
        cur.execute(
            """
            INSERT INTO copilot.action_proposals (
                proposal_id, batch_id, thread_id, turn_id, company_id,
                proposed_by_actor_id, tool_name, target_type, target_id,
                arguments_json, rationale, evidence_snapshot_ids,
                evidence_as_of, policy_check_json, status,
                dedupe_key, approval_hash, policy_version, expires_at, idempotency_key
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s::jsonb, %s, %s::jsonb,
                %s, %s::jsonb, 'pending_approval',
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (proposal_id) DO NOTHING
            RETURNING *
            """,
            (
                proposal_id,
                batch_id,
                thread_id,
                turn_id,
                company_id,
                proposed_by_actor_id,
                tool_name,
                target_type,
                target_id,
                json.dumps(arguments_json, sort_keys=True),
                rationale,
                json.dumps(sorted(set(evidence_snapshot_ids))),
                evidence_as_of,
                json.dumps(policy_check_json, sort_keys=True),
                dedupe_key,
                approval_hash,
                policy_version,
                expires_at,
                idempotency_key,
            ),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def get_proposal(self, company_id: str, proposal_id: str) -> dict[str, Any] | None:
        """Fetch one proposal inside `company_id`."""
        with self._db.copilot_cursor(company_id) as cur:
            return self.get_proposal_in_tx(cur, proposal_id)

    def get_proposal_in_tx(
        self,
        cur: psycopg.Cursor[DictRow],
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Fetch one proposal using the caller's tenant-locked cursor."""
        cur.execute(
            "SELECT * FROM copilot.action_proposals WHERE proposal_id = %s",
            (proposal_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def list_proposals(
        self,
        company_id: str,
        *,
        status: str | None = None,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List proposals for `company_id` ordered by `(created_at DESC, proposal_id)`."""
        clauses: list[sql.SQL] = [sql.SQL("1=1")]
        params: list[Any] = []
        if status is not None:
            clauses.append(sql.SQL("status = %s"))
            params.append(status)
        if device_id is not None:
            clauses.append(sql.SQL("target_type = 'device' AND target_id = %s"))
            params.append(device_id)

        query = sql.SQL(
            """
            SELECT *
            FROM copilot.action_proposals
            WHERE {where}
            ORDER BY created_at DESC, proposal_id ASC
            """
        ).format(where=sql.SQL(" AND ").join(clauses))

        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def has_active_duplicate(self, company_id: str, tool_name: str, dedupe_key: str) -> bool:
        """Return True if an active (`pending_approval`/`approved`/`executing`) match exists."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT 1
                FROM copilot.action_proposals
                WHERE tool_name = %s
                  AND dedupe_key = %s
                  AND status IN ('pending_approval', 'approved', 'executing')
                LIMIT 1
                """,
                (tool_name, dedupe_key),
            )
            return cur.fetchone() is not None

    def has_recent_execution(
        self,
        company_id: str,
        tool_name: str,
        dedupe_key: str,
        since: datetime,
    ) -> bool:
        """Return True if a matching `executed` proposal ran at or after `since`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT 1
                FROM copilot.action_proposals
                WHERE tool_name = %s
                  AND dedupe_key = %s
                  AND status = 'executed'
                  AND executed_at >= %s
                LIMIT 1
                """,
                (tool_name, dedupe_key, since),
            )
            return cur.fetchone() is not None

    def cas_decide_proposal(
        self,
        cur: psycopg.Cursor[DictRow],
        *,
        proposal_id: str,
        new_status: str,
        reviewer_id: str,
        via: str,
        reviewed_at: datetime,
        review_note: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Compare-and-set transition `pending_approval -> approved | rejected` if unexpired."""
        cutoff = now if now is not None else reviewed_at
        cur.execute(
            """
            UPDATE copilot.action_proposals
            SET status = %s,
                reviewer_id = %s,
                decided_via = %s,
                reviewed_at = %s,
                review_note = %s
            WHERE proposal_id = %s
              AND status = 'pending_approval'
              AND (expires_at IS NULL OR expires_at > %s)
            RETURNING *
            """,
            (new_status, reviewer_id, via, reviewed_at, review_note, proposal_id, cutoff),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def cas_mark_executing(
        self,
        cur: psycopg.Cursor[DictRow],
        *,
        proposal_id: str,
        executing_since: datetime,
    ) -> dict[str, Any] | None:
        """Compare-and-set transition `approved -> executing`."""
        cur.execute(
            """
            UPDATE copilot.action_proposals
            SET status = 'executing',
                executing_since = %s
            WHERE proposal_id = %s
              AND status = 'approved'
            RETURNING *
            """,
            (executing_since, proposal_id),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def record_execution_attempt(
        self,
        cur: psycopg.Cursor[DictRow],
        *,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Increment `attempts` by 1 while keeping a proposal in `executing` status."""
        cur.execute(
            """
            UPDATE copilot.action_proposals
            SET attempts = attempts + 1
            WHERE proposal_id = %s
              AND status = 'executing'
            RETURNING *
            """,
            (proposal_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def mark_terminal(
        self,
        cur: psycopg.Cursor[DictRow],
        *,
        proposal_id: str,
        status: str,
        external_receipt_json: dict[str, Any] | None = None,
        executed_at: datetime | None = None,
        increment_attempts: bool = False,
    ) -> dict[str, Any] | None:
        """Transition an approved/executing proposal to executed, failed, or stale_blocked."""
        if increment_attempts:
            cur.execute(
                """
                UPDATE copilot.action_proposals
                SET status = %s,
                    attempts = attempts + 1,
                    external_receipt_json = %s::jsonb,
                    executed_at = %s
                WHERE proposal_id = %s
                  AND status = 'executing'
                RETURNING *
                """,
                (
                    status,
                    json.dumps(external_receipt_json, sort_keys=True)
                    if external_receipt_json is not None
                    else None,
                    executed_at,
                    proposal_id,
                ),
            )
        else:
            cur.execute(
                """
                UPDATE copilot.action_proposals
                SET status = %s,
                    external_receipt_json = %s::jsonb,
                    executed_at = %s
                WHERE proposal_id = %s
                  AND status IN ('approved', 'executing')
                RETURNING *
                """,
                (
                    status,
                    json.dumps(external_receipt_json, sort_keys=True)
                    if external_receipt_json is not None
                    else None,
                    executed_at,
                    proposal_id,
                ),
            )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def find_due_pending(self, cur: psycopg.Cursor[DictRow], now: datetime) -> list[dict[str, Any]]:
        """Lock and return pending proposals whose `expires_at <= now`."""
        cur.execute(
            """
            SELECT *
            FROM copilot.action_proposals
            WHERE status = 'pending_approval'
              AND expires_at IS NOT NULL
              AND expires_at <= %s
            ORDER BY expires_at ASC, proposal_id ASC
            FOR UPDATE SKIP LOCKED
            """,
            (now,),
        )
        return [dict(r) for r in cur.fetchall()]

    def mark_expired(
        self,
        cur: psycopg.Cursor[DictRow],
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Transition a single pending proposal to `expired`."""
        cur.execute(
            """
            UPDATE copilot.action_proposals
            SET status = 'expired'
            WHERE proposal_id = %s
              AND status = 'pending_approval'
            RETURNING *
            """,
            (proposal_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def find_stuck_executing(self, company_id: str, cutoff: datetime) -> list[dict[str, Any]]:
        """Return `executing` proposals with `executing_since <= cutoff`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT *
                FROM copilot.action_proposals
                WHERE status = 'executing'
                  AND executing_since IS NOT NULL
                  AND executing_since <= %s
                ORDER BY executing_since ASC, proposal_id ASC
                """,
                (cutoff,),
            )
            return [dict(r) for r in cur.fetchall()]

    # --- Personal Saved Charts ---

    def insert_chart(
        self,
        company_id: str,
        *,
        chart_id: str,
        owner_actor_id: str,
        thread_id: str | None,
        turn_id: str | None,
        title: str,
        request_json: dict[str, Any],
        spec_json: dict[str, Any],
        data_json: list[dict[str, Any]],
        evidence_snapshot_ids: list[str],
        telemetry_as_of: datetime,
        pinned: bool = False,
    ) -> dict[str, Any]:
        """Insert a personal saved chart inside `company_id` (RLS-scoped)."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                INSERT INTO copilot.charts (
                    chart_id, company_id, owner_actor_id, thread_id, turn_id,
                    title, request_json, spec_json, data_json,
                    evidence_snapshot_ids, telemetry_as_of, pinned
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s
                )
                ON CONFLICT (chart_id) DO UPDATE SET
                    spec_json = EXCLUDED.spec_json,
                    data_json = EXCLUDED.data_json,
                    evidence_snapshot_ids = EXCLUDED.evidence_snapshot_ids,
                    telemetry_as_of = EXCLUDED.telemetry_as_of,
                    updated_at = now()
                RETURNING *
                """,
                (
                    chart_id,
                    company_id,
                    owner_actor_id,
                    thread_id,
                    turn_id,
                    title,
                    json.dumps(request_json, sort_keys=True),
                    json.dumps(spec_json, sort_keys=True),
                    json.dumps(data_json, sort_keys=True),
                    json.dumps(sorted(set(evidence_snapshot_ids))),
                    telemetry_as_of,
                    pinned,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return dict(row)

    def get_chart(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
    ) -> dict[str, Any] | None:
        """Fetch one personal chart owned by `(company_id, owner_actor_id)`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT *
                FROM copilot.charts
                WHERE chart_id = %s
                  AND owner_actor_id = %s
                """,
                (chart_id, owner_actor_id),
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def list_charts(
        self,
        company_id: str,
        *,
        owner_actor_id: str,
        pinned_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List personal saved charts owned by `(company_id, owner_actor_id)`."""
        clauses: list[sql.SQL] = [sql.SQL("owner_actor_id = %s")]
        params: list[Any] = [owner_actor_id]
        if pinned_only:
            clauses.append(sql.SQL("pinned = true"))
        params.append(limit)
        query = sql.SQL(
            """
            SELECT *
            FROM copilot.charts
            WHERE {where}
            ORDER BY pinned DESC, updated_at DESC, chart_id ASC
            LIMIT %s
            """
        ).format(where=sql.SQL(" AND ").join(clauses))
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def pin_chart(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
        pinned: bool,
    ) -> dict[str, Any] | None:
        """Toggle `pinned` on a personal chart owned by `(company_id, owner_actor_id)`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                UPDATE copilot.charts
                SET pinned = %s,
                    updated_at = now()
                WHERE chart_id = %s
                  AND owner_actor_id = %s
                RETURNING *
                """,
                (pinned, chart_id, owner_actor_id),
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None

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
        """Refresh a saved chart's spec, data, snapshot IDs, and `telemetry_as_of` in place."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                UPDATE copilot.charts
                SET spec_json = %s::jsonb,
                    data_json = %s::jsonb,
                    evidence_snapshot_ids = %s::jsonb,
                    telemetry_as_of = %s,
                    updated_at = now()
                WHERE chart_id = %s
                  AND owner_actor_id = %s
                RETURNING *
                """,
                (
                    json.dumps(spec_json, sort_keys=True),
                    json.dumps(data_json, sort_keys=True),
                    json.dumps(sorted(set(evidence_snapshot_ids))),
                    telemetry_as_of,
                    chart_id,
                    owner_actor_id,
                ),
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def delete_chart(
        self,
        company_id: str,
        chart_id: str,
        *,
        owner_actor_id: str,
    ) -> bool:
        """Delete a personal chart owned by `(company_id, owner_actor_id)`."""
        with self._db.copilot_cursor(company_id) as cur:
            cur.execute(
                """
                DELETE FROM copilot.charts
                WHERE chart_id = %s
                  AND owner_actor_id = %s
                RETURNING chart_id
                """,
                (chart_id, owner_actor_id),
            )
            return cur.fetchone() is not None
