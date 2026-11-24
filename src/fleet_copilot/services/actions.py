"""Action orchestration service: precheck, stage, decide, execute, expire, recover."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import errors
from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.clock import Clock
from fleet_copilot.config import Settings, get_settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import Evidence, Insight, PolicyValue, TenantContext
from fleet_copilot.domain.policy_rules import (
    ActionToolName,
    DeviceFacts,
    EmployeeFacts,
    HardwareCatalogRow,
    PolicyVerdict,
    ProposalDraft,
    evaluate,
    proposal_approval_hash,
)
from fleet_copilot.privacy import subject_ref
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.adapters.base import ActionAdapter, Receipt
from fleet_copilot.services.audit import (
    AccessDeniedPayload,
    ActionExecutedPayload,
    ActionFailedPayload,
    AuditService,
    FreshnessBlockedPayload,
    ProposalDecidedPayload,
    ProposalExpiredPayload,
    ProposalRefusedPayload,
    ProposalStagedPayload,
)
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends


class _ActionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DraftInput(_ActionModel):
    """Caller-supplied action proposal input referencing evidence handles in the active ledger."""

    tool_name: ActionToolName
    target_type: Literal["device", "employee"]
    target_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    evidence_handles: list[str] = Field(default_factory=list)
    proposal_id: str | None = None


class PrecheckResult(_ActionModel):
    """Output of `ActionService.precheck()`."""

    verdict: PolicyVerdict
    draft: ProposalDraft
    evidence_as_of: datetime


class StageResult(_ActionModel):
    """Output of `ActionService.stage()`."""

    batch_id: str
    staged: list[dict[str, Any]] = Field(default_factory=list)
    refused: list[PolicyVerdict] = Field(default_factory=list)


class DecideResult(_ActionModel):
    """Output of `ActionService.decide()`."""

    applied: bool
    proposal: dict[str, Any] | None
    message: str


class ExecuteResult(_ActionModel):
    """Output of `ActionService.execute()`."""

    status: str
    proposal: dict[str, Any] | None
    receipt: Receipt | None = None
    message: str = ""


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _target_ref(company_id: str, target_type: str, target_id: str) -> str:
    if target_type == "employee":
        return subject_ref(company_id, "employee", target_id)
    return target_id


def _cooldown_delta(tool_name: str, policies: dict[str, PolicyValue]) -> timedelta:
    def _num(key: str, default: float) -> float:
        pv = policies.get(key)
        return float(pv.value) if pv is not None else default

    if tool_name == "create_upgrade_order":
        return timedelta(days=_num("cooldown_days_upgrade", 30))
    if tool_name == "flag_device_for_replacement":
        return timedelta(days=_num("cooldown_days_replacement", 90))
    if tool_name == "open_remediation_ticket":
        return timedelta(days=_num("cooldown_days_ticket", 7))
    if tool_name == "notify_employee":
        return timedelta(hours=_num("cooldown_hours_notify", 72))
    return timedelta(days=7)


MAX_EXECUTION_ATTEMPTS = 3


class ActionService:
    """Enforces deterministic prechecks, HITL staging/CAS decisions, TOCTOU recheck, and audit."""

    def __init__(
        self,
        db: DatabaseManager,
        telemetry_repo: TelemetryRepository,
        copilot_store: PgCopilotStore,
        policy_svc: PolicyService,
        audit_svc: AuditService,
        adapter: ActionAdapter,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._repo = telemetry_repo
        self._store = copilot_store
        self._policy_svc = policy_svc
        self._audit = audit_svc
        self._adapter = adapter
        self._settings = settings

    def _actions_enabled(self) -> bool:
        cfg = self._settings if self._settings is not None else get_settings()
        return bool(cfg.actions_enabled)

    def _build_device_facts(
        self,
        ctx: TenantContext,
        device_id: str,
        catalog: dict[str, HardwareCatalogRow],
    ) -> tuple[DeviceFacts | None, HardwareCatalogRow | None, datetime, str | None]:
        as_of = self._repo.tenant_as_of(ctx.company_id)
        dev = self._repo.resolve_device(ctx.company_id, device_id)
        if dev is None or dev.device_id != device_id:
            return None, None, as_of, None

        trends_res = analyze_fleet_trends(
            ctx,
            ["all"],
            self._repo,
            self._policy_svc,
            device_ids=[dev.device_id],
            catalog_by_model=catalog,
        )
        insights = [Insight.model_validate(row) for row in trends_res.data]

        ram_sustained = any(i.category == "ram" for i in insights)
        disk_ins = next((i for i in insights if i.category == "disk"), None)
        disk_depleting = bool(disk_ins.stats.get("is_depleting")) if disk_ins else False
        days_to_low = disk_ins.stats.get("days_to_low") if disk_ins else None

        non_baseline_apps = sorted(
            {
                str(i.stats["software_name"])
                for i in insights
                if i.category == "software" and i.stats.get("still_present_latest", True)
            }
        )

        latest_comp = self._repo.compliance(
            ctx.company_id,
            latest_only=True,
            device_ids=[dev.device_id],
        )
        failing_checks = sorted({c.check_id for c in latest_comp if c.status == "fail"})

        rollup_art = next(
            (a for a in trends_res.artifacts if a.get("type") == "device_signal_rollup"),
            None,
        )
        active_signals: list[str] = []
        if rollup_art is not None:
            for r in rollup_art.get("rows", []):
                if r.get("device_id") == dev.device_id:
                    active_signals = list(r.get("active_signals", []))
                    break

        cat_row = catalog.get(dev.model_identifier) if dev.model_identifier else None
        facts = DeviceFacts(
            device_id=dev.device_id,
            hostname=dev.os_hostname,
            model_identifier=dev.model_identifier,
            form_factor=dev.form_factor,
            ever_had_battery=dev.ever_had_battery,
            battery_condition=dev.battery_condition,
            cycles=dev.battery_cycle_count,
            ram_sustained=ram_sustained,
            ram_total_gb=dev.ram_total_gb,
            disk_latest_free_pct=dev.disk_free_pct,
            disk_depleting=disk_depleting,
            days_to_low=float(days_to_low) if days_to_low is not None else None,
            storage_total_gb=dev.disk_total_gb,
            failing_checks=failing_checks,
            non_baseline_apps=non_baseline_apps,
            active_signals=active_signals,
            latest_snapshot_at=dev.collected_at,
        )
        return facts, cat_row, as_of, dev.snapshot_id

    def _build_employee_facts(
        self,
        ctx: TenantContext,
        employee_id: str,
        catalog: dict[str, HardwareCatalogRow],
    ) -> tuple[EmployeeFacts | None, datetime, list[str]]:
        as_of = self._repo.tenant_as_of(ctx.company_id)
        all_devices = self._repo.latest_states(ctx.company_id)
        owned = [d for d in all_devices if d.employee_id == employee_id]
        if not owned:
            return None, as_of, []

        dev_facts: list[DeviceFacts] = []
        latest_snap_ids: list[str] = []
        for d in owned:
            f, _, _, snap_id = self._build_device_facts(ctx, d.device_id, catalog)
            if f is not None:
                dev_facts.append(f)
            if snap_id is not None:
                latest_snap_ids.append(snap_id)

        other_devs = tuple(
            sorted({d.device_id for d in all_devices if d.employee_id != employee_id})
        )
        other_hosts = tuple(
            sorted(
                {
                    d.os_hostname
                    for d in all_devices
                    if d.employee_id != employee_id and d.os_hostname
                }
            )
        )
        other_emps = tuple(
            sorted({d.employee_id for d in all_devices if d.employee_id != employee_id})
        )

        facts = EmployeeFacts(
            employee_id=employee_id,
            devices=dev_facts,
            hostnames=[d.os_hostname for d in owned if d.os_hostname],
            other_device_ids=other_devs,
            other_hostnames=other_hosts,
            other_employee_ids=other_emps,
        )
        return facts, as_of, latest_snap_ids

    def precheck(
        self,
        ctx: TenantContext,
        draft_in: DraftInput,
        ledger: EvidenceLedger,
        clock: Clock,
        *,
        audit_refusal: bool = True,
    ) -> PrecheckResult:
        """Evaluate an action draft against telemetry facts, catalog, policies, and dedupe."""
        resolved_ev: list[Evidence] = []
        valid_evidence = len(draft_in.evidence_handles) > 0
        for h in draft_in.evidence_handles:
            ev = ledger.resolve(h)
            if ev is None:
                valid_evidence = False
            else:
                resolved_ev.append(ev)

        if valid_evidence and resolved_ev:
            snap_ids = [ev.snapshot_id for ev in resolved_ev]
            existing = self._repo.existing_snapshot_ids(ctx.company_id, snap_ids)
            if not set(snap_ids).issubset(existing):
                valid_evidence = False

        draft = ProposalDraft(
            tool_name=draft_in.tool_name,
            target_type=draft_in.target_type,
            target_id=draft_in.target_id,
            arguments=dict(draft_in.arguments),
            rationale=draft_in.rationale,
            evidence=resolved_ev,
        )

        catalog = self._store.hardware_catalog(ctx.company_id)
        tool_enabled = self._store.is_tool_enabled(ctx.company_id, draft_in.tool_name)
        actions_enabled = self._actions_enabled()

        facts: DeviceFacts | EmployeeFacts | None
        cat_row: HardwareCatalogRow | None = None
        if draft_in.target_type == "device":
            facts, cat_row, as_of, _ = self._build_device_facts(ctx, draft_in.target_id, catalog)
            policies = self._policy_svc.resolve(ctx, device_id=draft_in.target_id)
        else:
            facts, as_of, _ = self._build_employee_facts(ctx, draft_in.target_id, catalog)
            policies = self._policy_svc.resolve(ctx)

        from fleet_copilot.domain.policy_rules import dedupe_key as compute_dk

        dk = compute_dk(draft, facts)
        has_dup = self._store.has_active_duplicate(ctx.company_id, draft.tool_name, dk)
        cooldown_since = clock.now() - _cooldown_delta(draft.tool_name, policies)
        in_cooldown = self._store.has_recent_execution(
            ctx.company_id, draft.tool_name, dk, cooldown_since
        )

        verdict = evaluate(
            draft,
            facts,
            policies,
            cat_row,
            clock,
            as_of,
            actor_role=ctx.role,
            tool_enabled=tool_enabled,
            actions_enabled=actions_enabled,
            has_active_duplicate=has_dup,
            in_cooldown=in_cooldown,
            valid_evidence=valid_evidence,
        )

        if verdict.decision == "refuse" and audit_refusal:
            failed = [c.name for c in verdict.checks if not c.passed]
            if facts is None:
                self._audit.record_standalone(
                    ctx,
                    "access_denied",
                    AccessDeniedPayload(
                        reason_code="cross_tenant_not_found",
                        tool_name=draft.tool_name,
                    ),
                )
            elif ctx.role != "it_admin":
                self._audit.record_standalone(
                    ctx,
                    "access_denied",
                    AccessDeniedPayload(
                        reason_code="role_forbidden",
                        tool_name=draft.tool_name,
                    ),
                )
            elif not actions_enabled or not tool_enabled:
                self._audit.record_standalone(
                    ctx,
                    "access_denied",
                    AccessDeniedPayload(
                        reason_code="tool_disabled",
                        tool_name=draft.tool_name,
                    ),
                )
            else:
                self._audit.record_standalone(
                    ctx,
                    "proposal_refused",
                    ProposalRefusedPayload(
                        tool_name=draft.tool_name,
                        target_type=draft.target_type,
                        target_ref=_target_ref(ctx.company_id, draft.target_type, draft.target_id),
                        failed_checks=failed,
                        policy_version=verdict.policy_version,
                    ),
                )

        return PrecheckResult(verdict=verdict, draft=draft, evidence_as_of=as_of)

    def stage(
        self,
        ctx: TenantContext,
        drafts: Sequence[DraftInput],
        thread_id: str,
        turn_id: str,
        ledger: EvidenceLedger,
        clock: Clock,
        *,
        batch_id: str | None = None,
    ) -> StageResult:
        """Precheck and idempotently stage allowed proposals, auditing in the same transaction."""
        resolved_batch_id = batch_id or f"batch_{uuid.uuid4().hex[:12]}"
        staged_rows: list[dict[str, Any]] = []
        refused_verdicts: list[PolicyVerdict] = []

        policies = self._policy_svc.resolve(ctx)
        ttl_hours = (
            int(policies["proposal_ttl_hours"].value) if "proposal_ttl_hours" in policies else 72
        )
        expires_at = clock.now() + timedelta(hours=ttl_hours)

        for idx, draft_in in enumerate(drafts):
            pre = self.precheck(ctx, draft_in, ledger, clock, audit_refusal=True)
            if pre.verdict.decision == "refuse":
                refused_verdicts.append(pre.verdict)
                continue

            prop_id = draft_in.proposal_id or str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{ctx.company_id}:{thread_id}:{turn_id}:{idx}:{pre.verdict.dedupe_key}",
                )
            )
            ev_snaps = sorted({ev.snapshot_id for ev in pre.draft.evidence})
            app_hash = proposal_approval_hash(
                tool_name=pre.draft.tool_name,
                target_type=pre.draft.target_type,
                target_id=pre.draft.target_id,
                arguments=pre.draft.arguments,
                evidence_snapshot_ids=ev_snaps,
                dedupe_key=pre.verdict.dedupe_key,
            )

            try:
                with self._db.copilot_cursor(ctx.company_id) as cur:
                    inserted = self._store.insert_proposal(
                        cur,
                        proposal_id=prop_id,
                        batch_id=resolved_batch_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        company_id=ctx.company_id,
                        proposed_by_actor_id=ctx.actor_id,
                        tool_name=pre.draft.tool_name,
                        target_type=pre.draft.target_type,
                        target_id=pre.draft.target_id,
                        arguments_json=pre.draft.arguments,
                        rationale=pre.draft.rationale,
                        evidence_snapshot_ids=ev_snaps,
                        evidence_as_of=pre.evidence_as_of,
                        policy_check_json=pre.verdict.model_dump(mode="json"),
                        dedupe_key=pre.verdict.dedupe_key,
                        approval_hash=app_hash,
                        policy_version=pre.verdict.policy_version,
                        expires_at=expires_at,
                        idempotency_key=prop_id,
                    )
                    if inserted is not None:
                        self._audit.record(
                            cur,
                            ctx,
                            "proposal_staged",
                            ProposalStagedPayload(
                                proposal_id=prop_id,
                                tool_name=pre.draft.tool_name,
                                target_type=pre.draft.target_type,
                                target_ref=_target_ref(
                                    ctx.company_id,
                                    pre.draft.target_type,
                                    pre.draft.target_id,
                                ),
                                dedupe_key_hash=_sha256_hex(pre.verdict.dedupe_key),
                                approval_hash=app_hash,
                                evidence_snapshot_ids=ev_snaps,
                                policy_version=pre.verdict.policy_version,
                                expires_at=expires_at.isoformat(),
                            ),
                            thread_id=thread_id,
                        )
                        staged_rows.append(inserted)
                    else:
                        existing = self._store.get_proposal_in_tx(cur, prop_id)
                        if existing is not None:
                            staged_rows.append(existing)
            except errors.UniqueViolation:
                # Active dedupe index caught a concurrent duplicate race
                refused_verdicts.append(
                    pre.verdict.model_copy(
                        update={
                            "decision": "refuse",
                            "reasons": [
                                f"Concurrent active duplicate for {pre.verdict.dedupe_key}."
                            ],
                        }
                    )
                )

        return StageResult(
            batch_id=resolved_batch_id,
            staged=staged_rows,
            refused=refused_verdicts,
        )

    _EXPIRED_MESSAGE = "This proposal expired; ask the copilot again for a fresh one."

    def _expire_if_due_in_tx(
        self,
        cur: Any,
        ctx: TenantContext,
        proposal: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        """If `proposal` is pending past `expires_at`, mark it expired + audit in the same tx."""
        expires_at = proposal.get("expires_at")
        if (
            proposal.get("status") != "pending_approval"
            or not isinstance(expires_at, datetime)
            or expires_at > now
        ):
            return None
        pid = str(proposal["proposal_id"])
        expired = self._store.mark_expired(cur, pid)
        if expired is not None:
            self._audit.record(
                cur,
                ctx,
                "proposal_expired",
                ProposalExpiredPayload(proposal_id=pid),
                thread_id=str(proposal["thread_id"]),
            )
            return expired
        latest = self._store.get_proposal_in_tx(cur, pid)
        if latest is not None and latest.get("status") == "expired":
            return latest
        return None

    def decide(
        self,
        ctx: TenantContext,
        proposal_id: str,
        decision: Literal["approve", "reject", "defer"],
        via: Literal["chat", "queue", "api"],
        clock: Clock,
        *,
        note: str | None = None,
    ) -> DecideResult:
        """Apply a compare-and-set human decision (`approve`, `reject`, or `defer`) with audit."""
        if ctx.role != "it_admin":
            self._audit.record_standalone(
                ctx,
                "access_denied",
                AccessDeniedPayload(reason_code="role_forbidden", tool_name="decide_proposal"),
            )
            return DecideResult(
                applied=False,
                proposal=None,
                message=f"Role {ctx.role!r} cannot decide proposals.",
            )

        policies = self._policy_svc.resolve(ctx)
        require_distinct = bool(
            policies["require_distinct_approver"].value
            if "require_distinct_approver" in policies
            else False
        )
        now = clock.now()

        with self._db.copilot_cursor(ctx.company_id) as cur:
            current = self._store.get_proposal_in_tx(cur, proposal_id)
            if current is None:
                return DecideResult(applied=False, proposal=None, message="Proposal not found.")

            if decision == "defer":
                expired_row = self._expire_if_due_in_tx(cur, ctx, current, now)
                if expired_row is not None:
                    return DecideResult(
                        applied=False,
                        proposal=expired_row,
                        message=self._EXPIRED_MESSAGE,
                    )
                if current["status"] == "expired":
                    return DecideResult(
                        applied=False,
                        proposal=current,
                        message=self._EXPIRED_MESSAGE,
                    )
                if current["status"] != "pending_approval":
                    return DecideResult(
                        applied=False,
                        proposal=current,
                        message=(
                            f"already decided: {current['status']} by "
                            f"{current.get('reviewer_id')} at {current.get('reviewed_at')}"
                        ),
                    )
                self._audit.record(
                    cur,
                    ctx,
                    "proposal_decided",
                    ProposalDecidedPayload(
                        proposal_id=proposal_id,
                        decision="deferred",
                        via=via,
                    ),
                    thread_id=current["thread_id"],
                )
                return DecideResult(applied=True, proposal=current, message="Deferred.")

            if (
                decision == "approve"
                and require_distinct
                and current["proposed_by_actor_id"] == ctx.actor_id
            ):
                return DecideResult(
                    applied=False,
                    proposal=current,
                    message="Policy require_distinct_approver forbids self-approval.",
                )

            new_status: Literal["approved", "rejected"] = (
                "approved" if decision == "approve" else "rejected"
            )
            updated = self._store.cas_decide_proposal(
                cur,
                proposal_id=proposal_id,
                new_status=new_status,
                reviewer_id=ctx.actor_id,
                via=via,
                reviewed_at=now,
                review_note=note,
                now=now,
            )
            if updated is None:
                latest = self._store.get_proposal_in_tx(cur, proposal_id)
                if latest is None:
                    return DecideResult(applied=False, proposal=None, message="Proposal not found.")
                expired_row = self._expire_if_due_in_tx(cur, ctx, latest, now)
                if expired_row is not None:
                    return DecideResult(
                        applied=False,
                        proposal=expired_row,
                        message=self._EXPIRED_MESSAGE,
                    )
                if latest["status"] == "expired":
                    return DecideResult(
                        applied=False,
                        proposal=latest,
                        message=self._EXPIRED_MESSAGE,
                    )
                return DecideResult(
                    applied=False,
                    proposal=latest,
                    message=(
                        f"already decided: {latest['status']} by "
                        f"{latest.get('reviewer_id')} at {latest.get('reviewed_at')}"
                    ),
                )

            self._audit.record(
                cur,
                ctx,
                "proposal_decided",
                ProposalDecidedPayload(
                    proposal_id=proposal_id,
                    decision=new_status,
                    via=via,
                ),
                thread_id=updated["thread_id"],
            )
            return DecideResult(applied=True, proposal=updated, message=f"Marked {new_status}.")

    def _run_execution_steps(
        self,
        ctx: TenantContext,
        proposal: dict[str, Any],
        clock: Clock,
        *,
        already_executing: bool,
    ) -> ExecuteResult:
        proposal_id = str(proposal["proposal_id"])
        thread_id = str(proposal["thread_id"])
        tool_name = str(proposal["tool_name"])
        target_type = str(proposal["target_type"])
        target_id = str(proposal["target_id"])
        arguments = dict(proposal["arguments_json"] or {})
        ev_snaps = list(proposal["evidence_snapshot_ids"] or [])
        dk = str(proposal.get("dedupe_key") or "")
        stored_hash = str(proposal.get("approval_hash") or "")
        idem_key = str(proposal.get("idempotency_key") or proposal_id)

        # Step 1: Recompute approval_hash; mismatch -> failed + audit
        computed_hash = proposal_approval_hash(
            tool_name=tool_name,
            target_type=target_type,
            target_id=target_id,
            arguments=arguments,
            evidence_snapshot_ids=ev_snaps,
            dedupe_key=dk,
        )
        if computed_hash != stored_hash:
            with self._db.copilot_cursor(ctx.company_id) as cur:
                failed_row = self._store.mark_terminal(
                    cur,
                    proposal_id=proposal_id,
                    status="failed",
                    external_receipt_json={"error_code": "approval_hash_mismatch"},
                    executed_at=clock.now(),
                )
                self._audit.record(
                    cur,
                    ctx,
                    "action_failed",
                    ActionFailedPayload(
                        proposal_id=proposal_id,
                        adapter=self._adapter.name,
                        error_code="approval_hash_mismatch",
                    ),
                    thread_id=thread_id,
                )
            return ExecuteResult(
                status="failed",
                proposal=failed_row,
                receipt=None,
                message="approval_hash mismatch",
            )

        # Step 2: Compare-and-set approved -> executing (skipped when recovering stuck executing)
        if not already_executing:
            with self._db.copilot_cursor(ctx.company_id) as cur:
                claimed = self._store.cas_mark_executing(
                    cur,
                    proposal_id=proposal_id,
                    executing_since=clock.now(),
                )
            if claimed is None:
                latest = self._store.get_proposal(ctx.company_id, proposal_id)
                return ExecuteResult(
                    status=str(latest["status"]) if latest else "not_found",
                    proposal=latest,
                    receipt=None,
                    message="Proposal is not in approved state.",
                )
            proposal = claimed

        # Step 3: Re-run the policy rule on FRESH telemetry (TOCTOU freshness check)
        catalog = self._store.hardware_catalog(ctx.company_id)
        tool_enabled = self._store.is_tool_enabled(ctx.company_id, tool_name)
        actions_enabled = self._actions_enabled()

        facts: DeviceFacts | EmployeeFacts | None
        cat_row: HardwareCatalogRow | None = None
        new_snap_ids: list[str] = []
        fresh_ev: list[Evidence] = []

        if target_type == "device":
            facts, cat_row, as_of, snap_id = self._build_device_facts(ctx, target_id, catalog)
            policies = self._policy_svc.resolve(ctx, device_id=target_id)
            if snap_id is not None:
                new_snap_ids = [snap_id]
                assert facts is not None
                fresh_ev = [
                    Evidence(
                        handle="E_fresh",
                        snapshot_id=snap_id,
                        device_id=target_id,
                        hostname=facts.hostname,
                        collected_at=facts.latest_snapshot_at,
                        metric="fresh_recheck",
                        value=True,
                        label="Fresh telemetry recheck",
                    )
                ]
        else:
            facts, as_of, new_snap_ids = self._build_employee_facts(ctx, target_id, catalog)
            policies = self._policy_svc.resolve(ctx)
            if facts is not None:
                for d_fact, s_id in zip(facts.devices, new_snap_ids, strict=False):
                    if d_fact.active_signals or d_fact.failing_checks or d_fact.non_baseline_apps:
                        fresh_ev.append(
                            Evidence(
                                handle=f"E_fresh_{d_fact.device_id}",
                                snapshot_id=s_id,
                                device_id=d_fact.device_id,
                                hostname=d_fact.hostname,
                                collected_at=d_fact.latest_snapshot_at,
                                metric="fresh_recheck",
                                value=True,
                                label="Fresh telemetry recheck",
                            )
                        )

        fresh_draft = ProposalDraft(
            tool_name=tool_name,  # type: ignore[arg-type]
            target_type=target_type,  # type: ignore[arg-type]
            target_id=target_id,
            arguments=arguments,
            rationale=str(proposal["rationale"]),
            evidence=fresh_ev,
        )
        fresh_verdict = evaluate(
            fresh_draft,
            facts,
            policies,
            cat_row,
            clock,
            as_of,
            actor_role="it_admin",
            tool_enabled=tool_enabled,
            actions_enabled=actions_enabled,
            has_active_duplicate=False,
            in_cooldown=False,
            valid_evidence=bool(fresh_ev),
        )
        if fresh_verdict.decision == "refuse":
            failed_checks = [c.name for c in fresh_verdict.checks if not c.passed]
            with self._db.copilot_cursor(ctx.company_id) as cur:
                blocked = self._store.mark_terminal(
                    cur,
                    proposal_id=proposal_id,
                    status="stale_blocked",
                    external_receipt_json={
                        "reason": "freshness_blocked",
                        "failed_checks": failed_checks,
                        "new_snapshot_ids": new_snap_ids,
                    },
                    executed_at=clock.now(),
                )
                self._audit.record(
                    cur,
                    ctx,
                    "freshness_blocked",
                    FreshnessBlockedPayload(
                        proposal_id=proposal_id,
                        failed_checks=failed_checks,
                        new_snapshot_ids=new_snap_ids,
                    ),
                    thread_id=thread_id,
                )
            return ExecuteResult(
                status="stale_blocked",
                proposal=blocked,
                receipt=None,
                message=f"Blocked by fresh telemetry recheck: {', '.join(failed_checks)}",
            )

        # Step 4: Call external adapter OUTSIDE a DB transaction with idempotency_key
        try:
            receipt = self._adapter.execute(proposal, idem_key)
        except Exception as exc:  # noqa: BLE001
            next_attempt = int(proposal.get("attempts") or 0) + 1
            if next_attempt < MAX_EXECUTION_ATTEMPTS:
                with self._db.copilot_cursor(ctx.company_id) as cur:
                    updated_attempt = self._store.record_execution_attempt(
                        cur,
                        proposal_id=proposal_id,
                    )
                return ExecuteResult(
                    status="executing",
                    proposal=updated_attempt,
                    receipt=None,
                    message=(
                        f"adapter_exception (attempt {next_attempt}/{MAX_EXECUTION_ATTEMPTS}): "
                        f"{type(exc).__name__}"
                    ),
                )

            err_payload = {
                "error_code": "adapter_exception",
                "exception_type": type(exc).__name__,
                "attempts": next_attempt,
            }
            err_hash = _sha256_hex(json.dumps(err_payload, sort_keys=True, separators=(",", ":")))
            with self._db.copilot_cursor(ctx.company_id) as cur:
                failed_row = self._store.mark_terminal(
                    cur,
                    proposal_id=proposal_id,
                    status="failed",
                    external_receipt_json=err_payload,
                    executed_at=clock.now(),
                    increment_attempts=True,
                )
                self._audit.record(
                    cur,
                    ctx,
                    "action_failed",
                    ActionFailedPayload(
                        proposal_id=proposal_id,
                        adapter=self._adapter.name,
                        error_code="adapter_exception",
                        receipt_hash=err_hash,
                    ),
                    thread_id=thread_id,
                )
            return ExecuteResult(
                status="failed",
                proposal=failed_row,
                receipt=None,
                message=f"adapter_exception after {next_attempt} attempts: {type(exc).__name__}",
            )

        receipt_dict = receipt.model_dump(mode="json")
        receipt_hash = _sha256_hex(json.dumps(receipt_dict, sort_keys=True, separators=(",", ":")))

        # Step 5: Persist final status + receipt + audit in ONE transaction
        final_status = "executed" if receipt.status == "ok" else "failed"
        with self._db.copilot_cursor(ctx.company_id) as cur:
            updated = self._store.mark_terminal(
                cur,
                proposal_id=proposal_id,
                status=final_status,
                external_receipt_json=receipt_dict,
                executed_at=clock.now(),
            )
            if final_status == "executed":
                self._audit.record(
                    cur,
                    ctx,
                    "action_executed",
                    ActionExecutedPayload(
                        proposal_id=proposal_id,
                        adapter=receipt.adapter,
                        receipt_id=receipt.receipt_id,
                        receipt_hash=receipt_hash,
                    ),
                    thread_id=thread_id,
                )
            else:
                self._audit.record(
                    cur,
                    ctx,
                    "action_failed",
                    ActionFailedPayload(
                        proposal_id=proposal_id,
                        adapter=receipt.adapter,
                        error_code=receipt.error_code or "adapter_failed",
                        receipt_hash=receipt_hash,
                    ),
                    thread_id=thread_id,
                )

        return ExecuteResult(
            status=final_status,
            proposal=updated,
            receipt=receipt,
            message=receipt.receipt_id
            if final_status == "executed"
            else (receipt.error_code or "failed"),
        )

    def execute(self, ctx: TenantContext, proposal_id: str, clock: Clock) -> ExecuteResult:
        """Execute an approved proposal with hash check, CAS lock, TOCTOU re-check, and audit."""
        proposal = self._store.get_proposal(ctx.company_id, proposal_id)
        if proposal is None:
            return ExecuteResult(status="not_found", proposal=None, message="Proposal not found.")
        if proposal["status"] != "approved":
            return ExecuteResult(
                status=str(proposal["status"]),
                proposal=proposal,
                message=f"Proposal is {proposal['status']}, expected approved.",
            )
        return self._run_execution_steps(ctx, proposal, clock, already_executing=False)

    def expire_due(self, ctx: TenantContext, clock: Clock) -> list[str]:
        """Expire all pending proposals in `ctx.company_id` whose `expires_at <= clock.now()`."""
        expired_ids: list[str] = []
        with self._db.copilot_cursor(ctx.company_id) as cur:
            due = self._store.find_due_pending(cur, clock.now())
            for row in due:
                pid = str(row["proposal_id"])
                updated = self._store.mark_expired(cur, pid)
                if updated is not None:
                    self._audit.record(
                        cur,
                        ctx,
                        "proposal_expired",
                        ProposalExpiredPayload(proposal_id=pid),
                        thread_id=str(row["thread_id"]),
                    )
                    expired_ids.append(pid)
        return expired_ids

    def recover_executing(
        self,
        ctx: TenantContext,
        clock: Clock,
        *,
        min_age_minutes: int = 5,
    ) -> list[ExecuteResult]:
        """Retry proposals stuck in `executing` longer than `min_age_minutes` with same key."""
        cutoff = clock.now() - timedelta(minutes=min_age_minutes)
        stuck = self._store.find_stuck_executing(ctx.company_id, cutoff)
        results: list[ExecuteResult] = []
        for row in stuck:
            results.append(self._run_execution_steps(ctx, row, clock, already_executing=True))
        return results

    def list(
        self,
        ctx: TenantContext,
        *,
        status: str | None = None,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List proposals for `ctx.company_id` with optional status/device filters."""
        return self._store.list_proposals(ctx.company_id, status=status, device_id=device_id)
