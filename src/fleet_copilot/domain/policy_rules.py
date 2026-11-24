"""Pure deterministic policy gate for Fleet Copilot actions (zero DB I/O)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fleet_copilot.clock import Clock
from fleet_copilot.domain.models import Evidence, PolicyValue

ActionToolName = Literal[
    "create_upgrade_order",
    "open_remediation_ticket",
    "flag_device_for_replacement",
    "notify_employee",
]

_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(tb|tib|gb|gib)\b", re.IGNORECASE)
_EMP_TOKEN_RE = re.compile(r"\bemp-[a-z0-9-]+\b", re.IGNORECASE)
_DEVICE_ID_TOKEN_RE = re.compile(r"\b[A-Z0-9]{12}\b")


class _RuleModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HardwareCatalogRow(_RuleModel):
    """Platform hardware upgradability reference record."""

    model_identifier: str
    model_name: str
    form_factor: Literal["laptop", "desktop", "unknown"]
    ram_upgradable: bool
    ram_max_gb: int | None = None
    storage_upgradable: bool
    battery_serviceable: bool
    source_urls: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    verified_at: str


class DeviceFacts(_RuleModel):
    """Deterministic facts assembled for a single device prior to policy evaluation."""

    device_id: str
    model_identifier: str | None = None
    form_factor: Literal["laptop", "desktop", "unknown"]
    ever_had_battery: bool
    battery_condition: str | None = None
    cycles: int | None = None
    ram_sustained: bool = False
    ram_total_gb: float | None = None
    disk_latest_free_pct: float | None = None
    disk_depleting: bool = False
    days_to_low: float | None = None
    storage_total_gb: float | None = None
    failing_checks: list[str] = Field(default_factory=list)
    non_baseline_apps: list[str] = Field(default_factory=list)
    active_signals: list[str] = Field(default_factory=list)
    latest_snapshot_at: datetime
    hostname: str | None = None


class EmployeeFacts(_RuleModel):
    """Deterministic facts assembled for an employee notification target."""

    employee_id: str
    devices: list[DeviceFacts] = Field(default_factory=list)
    hostnames: list[str] = Field(default_factory=list)
    other_device_ids: tuple[str, ...] = ()
    other_hostnames: tuple[str, ...] = ()
    other_employee_ids: tuple[str, ...] = ()


class ProposalDraft(_RuleModel):
    """Action proposal draft with resolved evidence ready for pure policy evaluation."""

    tool_name: ActionToolName
    target_type: Literal["device", "employee"]
    target_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)


class PolicyCheck(_RuleModel):
    """Single evaluated policy predicate recorded on a verdict."""

    name: str
    observed: Any
    threshold: Any
    source: str
    passed: bool


class Alternative(_RuleModel):
    """Verified alternative action suggested on refusal."""

    tool: ActionToolName
    args_hint: dict[str, Any] = Field(default_factory=dict)


class PolicyVerdict(_RuleModel):
    """Complete deterministic output of `evaluate()`."""

    decision: Literal["allow", "refuse", "allow_with_warnings"]
    checks: list[PolicyCheck] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dedupe_key: str
    policy_version: str


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def approval_hash(fields: dict[str, Any]) -> str:
    """Compute SHA-256 hex digest over canonical JSON (`sort_keys`, compact, UTF-8)."""
    return hashlib.sha256(_canonical_json(fields).encode("utf-8")).hexdigest()


def proposal_approval_hash(
    *,
    tool_name: str,
    target_type: str,
    target_id: str,
    arguments: dict[str, Any],
    evidence_snapshot_ids: Sequence[str],
    dedupe_key_val: str | None = None,
    dedupe_key: str | None = None,
) -> str:
    """Compute canonical approval hash binding tool, target, args, sorted evidence, and dedupe."""
    dk = dedupe_key if dedupe_key is not None else (dedupe_key_val or "")
    return approval_hash(
        {
            "tool_name": tool_name,
            "target_type": target_type,
            "target_id": target_id,
            "arguments": arguments,
            "evidence_snapshot_ids": sorted(set(evidence_snapshot_ids)),
            "dedupe_key": dk,
        }
    )


def dedupe_key(
    draft: ProposalDraft,
    facts: DeviceFacts | EmployeeFacts | None = None,  # noqa: ARG001
) -> str:
    """Compute the canonical deduplication key (independent of free text)."""
    if draft.tool_name == "create_upgrade_order":
        component = str(draft.arguments.get("component", "")).strip().lower()
        return f"upgrade:{draft.target_id}:{component}"
    if draft.tool_name == "open_remediation_ticket":
        check_id = str(draft.arguments.get("check_id", "")).strip()
        return f"ticket:{draft.target_id}:{check_id}"
    if draft.tool_name == "flag_device_for_replacement":
        return f"replace:{draft.target_id}"
    if draft.tool_name == "notify_employee":
        tokens = sorted({f"{ev.device_id}:{ev.metric}" for ev in draft.evidence})
        fingerprint = hashlib.sha1("|".join(tokens).encode("utf-8")).hexdigest()[:12]
        return f"notify:{draft.target_id}:{fingerprint}"
    return f"{draft.tool_name}:{draft.target_id}"


def policy_version(
    policies_used: dict[str, PolicyValue],
    catalog_row: HardwareCatalogRow | None,
) -> str:
    """Compute a deterministic fingerprint over the active policy values and catalog row."""
    payload: dict[str, Any] = {
        "policies": {
            k: {"value": v.value, "source": v.source} for k, v in sorted(policies_used.items())
        },
        "catalog": (
            {
                "model_identifier": catalog_row.model_identifier,
                "ram_upgradable": catalog_row.ram_upgradable,
                "ram_max_gb": catalog_row.ram_max_gb,
                "storage_upgradable": catalog_row.storage_upgradable,
                "battery_serviceable": catalog_row.battery_serviceable,
                "verified_at": catalog_row.verified_at,
            }
            if catalog_row is not None
            else None
        ),
    }
    return "pol_" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def parse_capacity_gb(spec: str) -> float | None:
    """Extract numeric capacity in GB/GiB from an upgrade spec string (e.g. '32 GB', '1 TB')."""
    if not spec:
        return None
    m = _CAPACITY_RE.search(spec)
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("tb", "tib"):
        return amount * 1024.0
    return amount


def _pval(policies: dict[str, PolicyValue], key: str, default: Any) -> tuple[Any, str]:
    pv = policies.get(key)
    if pv is None:
        return default, "global"
    return pv.value, pv.source


def _device_disk_rule(facts: DeviceFacts, low_disk_pct: float) -> bool:
    is_low = facts.disk_latest_free_pct is not None and facts.disk_latest_free_pct < low_disk_pct
    soon_low = facts.disk_depleting and facts.days_to_low is not None and facts.days_to_low < 30.0
    return is_low or soon_low


def _device_has_active_finding_for_evidence(
    dev: DeviceFacts,
    ev: Evidence,
    *,
    low_disk_pct: float,
    eol_cycles: int,
) -> bool:
    m = ev.metric.lower()
    if m.startswith("disk_"):
        return _device_disk_rule(dev, low_disk_pct)
    if m.startswith("ram_"):
        return dev.ram_sustained
    if m.startswith("battery_"):
        sr = dev.battery_condition is not None and dev.battery_condition != "Normal"
        high_cyc = dev.cycles is not None and dev.cycles >= eol_cycles
        return sr or high_cyc
    if m.startswith("compliance:"):
        parts = ev.metric.split(":")
        chk = parts[1] if len(parts) > 1 else ""
        return chk in dev.failing_checks
    if m.startswith("software:"):
        return len(dev.non_baseline_apps) > 0
    return bool(dev.active_signals or dev.failing_checks or dev.non_baseline_apps)


def evaluate(
    draft: ProposalDraft,
    facts: DeviceFacts | EmployeeFacts | None,
    policies: dict[str, PolicyValue],
    catalog_row: HardwareCatalogRow | None,
    clock: Clock,
    telemetry_as_of: datetime,
    *,
    actor_role: Literal["viewer", "it_admin"] = "it_admin",
    tool_enabled: bool = True,
    actions_enabled: bool = True,
    has_active_duplicate: bool = False,
    in_cooldown: bool = False,
    valid_evidence: bool = True,
    _evaluating_alternative: bool = False,
) -> PolicyVerdict:
    """Evaluate all common and tool-specific action policy rules deterministically."""
    checks: list[PolicyCheck] = []
    reasons: list[str] = []
    warnings: list[str] = []

    dk = dedupe_key(draft, facts)
    pv_str = policy_version(policies, catalog_row)

    low_disk_pct, low_disk_src = _pval(policies, "low_disk_free_pct", 15.0)
    eol_cycles, eol_src = _pval(policies, "battery_eol_cycles", 800)
    stale_days, stale_src = _pval(policies, "stale_device_days", 7)
    max_lag_days, lag_src = _pval(policies, "max_telemetry_lag_days", None)
    rep_min_sig, rep_src = _pval(policies, "replacement_min_signals", 2)
    notify_max_chars, notify_src = _pval(policies, "notify_message_max_chars", 800)

    def _add(
        name: str, observed: Any, threshold: Any, source: str, passed: bool, reason: str
    ) -> None:
        checks.append(
            PolicyCheck(
                name=name,
                observed=observed,
                threshold=threshold,
                source=source,
                passed=passed,
            )
        )
        if not passed:
            reasons.append(reason)

    # --- Common invariants ---
    _add(
        "actor_role",
        actor_role,
        "it_admin",
        "rbac",
        actor_role == "it_admin",
        f"Role {actor_role!r} cannot stage or execute actions (requires 'it_admin').",
    )
    _add(
        "actions_enabled",
        actions_enabled,
        True,
        "env",
        actions_enabled,
        "Actions are disabled globally via ACTIONS_ENABLED=false.",
    )
    _add(
        "tool_enabled",
        tool_enabled,
        True,
        "tool_config",
        tool_enabled,
        f"Tool {draft.tool_name!r} is disabled by platform or tenant configuration.",
    )
    _add(
        "target_exists",
        facts is not None,
        True,
        "tenant",
        facts is not None,
        "not_found: target does not exist in this tenant.",
    )
    _add(
        "evidence_present_and_valid",
        valid_evidence and len(draft.evidence) > 0,
        True,
        "ledger",
        valid_evidence and len(draft.evidence) > 0,
        "At least one valid, tenant-scoped evidence record from the current ledger is required.",
    )
    _add(
        "no_active_duplicate",
        not has_active_duplicate,
        True,
        "dedupe",
        not has_active_duplicate,
        f"An active proposal already exists for {dk}.",
    )
    _add(
        "not_in_cooldown",
        not in_cooldown,
        True,
        "cooldown",
        not in_cooldown,
        f"A matching action ({dk}) was recently executed and is still in cooldown.",
    )

    if max_lag_days is not None:
        lag_days = (clock.now() - telemetry_as_of).total_seconds() / 86400.0
        _add(
            "telemetry_lag",
            round(lag_days, 2),
            float(max_lag_days),
            lag_src,
            lag_days <= float(max_lag_days),
            f"Telemetry lag ({lag_days:.1f}d) exceeds max_telemetry_lag_days ({max_lag_days}d).",
        )

    if facts is None:
        return PolicyVerdict(
            decision="refuse",
            checks=checks,
            reasons=reasons,
            alternatives=[],
            warnings=[],
            dedupe_key=dk,
            policy_version=pv_str,
        )

    # Evidence ownership & data-clock recency
    if isinstance(facts, DeviceFacts):
        owned_ids = {facts.device_id}
        latest_by_dev = {facts.device_id: facts.latest_snapshot_at}
    else:
        owned_ids = {d.device_id for d in facts.devices}
        latest_by_dev = {d.device_id: d.latest_snapshot_at for d in facts.devices}

    if draft.evidence:
        all_belong = all(ev.device_id in owned_ids for ev in draft.evidence)
        _add(
            "evidence_matches_target",
            all_belong,
            True,
            "ledger",
            all_belong,
            "Cited evidence must belong to the target device (or target employee's devices).",
        )
        # Each cited device must include at least one recent snapshot within stale_device_days
        # of its latest snapshot (so trend context handles like first/transition can be cited
        # alongside latest, while stale-only evidence is refused).
        newest_by_cited_dev: dict[str, datetime] = {}
        for ev in draft.evidence:
            prev_ts = newest_by_cited_dev.get(ev.device_id)
            if prev_ts is None or ev.collected_at > prev_ts:
                newest_by_cited_dev[ev.device_id] = ev.collected_at

        all_recent = True
        for dev_id, newest_ts in newest_by_cited_dev.items():
            ref_latest = latest_by_dev.get(dev_id)
            if ref_latest is not None:
                if newest_ts < ref_latest - timedelta(days=int(stale_days)):
                    all_recent = False
        _add(
            "evidence_recent",
            all_recent,
            int(stale_days),
            stale_src,
            all_recent,
            (
                f"Cited evidence is older than stale_device_days ({stale_days}d) "
                "before latest snapshot."
            ),
        )

    # --- Tool-specific rules ---
    candidate_alts: list[Alternative] = []

    if draft.tool_name == "create_upgrade_order":
        if not isinstance(facts, DeviceFacts):
            _add(
                "target_type",
                "employee",
                "device",
                "schema",
                False,
                "Upgrade target must be device.",
            )
        else:
            component = str(draft.arguments.get("component", "")).strip().lower()
            spec = str(draft.arguments.get("spec", "")).strip()
            _add(
                "valid_component",
                component,
                ["ram", "storage", "battery"],
                "schema",
                component in ("ram", "storage", "battery"),
                f"Invalid component {component!r}; valid: ram, storage, battery.",
            )
            _add("valid_spec", bool(spec), True, "schema", bool(spec), "Upgrade spec is required.")

            if component == "battery":
                is_laptop_bat = facts.form_factor == "laptop" and facts.ever_had_battery
                _add(
                    "laptop_with_battery",
                    {"form_factor": facts.form_factor, "ever_had_battery": facts.ever_had_battery},
                    True,
                    "telemetry",
                    is_laptop_bat,
                    (
                        f"Device {facts.device_id} ({facts.form_factor}) does not have a "
                        "serviceable laptop battery."
                    ),
                )
                bat_eol = (
                    facts.battery_condition is not None and facts.battery_condition != "Normal"
                ) or (facts.cycles is not None and facts.cycles >= int(eol_cycles))
                _add(
                    "battery_eol_evidence",
                    {"condition": facts.battery_condition, "cycles": facts.cycles},
                    {"condition_ne": "Normal", "cycles_ge": int(eol_cycles)},
                    eol_src,
                    bat_eol,
                    "No battery degradation or EOL cycle threshold evidence on this device.",
                )
                if catalog_row is None:
                    warnings.append("Hardware upgradability unknown; a human must confirm.")
                else:
                    _add(
                        "battery_serviceable",
                        catalog_row.battery_serviceable,
                        True,
                        f"catalog:{catalog_row.model_identifier}",
                        catalog_row.battery_serviceable,
                        (
                            f"Catalog row {catalog_row.model_identifier} marks battery "
                            "as non-serviceable."
                        ),
                    )

            elif component == "ram":
                _add(
                    "ram_sustained",
                    facts.ram_sustained,
                    True,
                    "analyzer:ram",
                    facts.ram_sustained,
                    "No evidence of sustained RAM pressure on this device.",
                )
                spec_gb = parse_capacity_gb(spec)
                _add(
                    "spec_capacity_parseable",
                    spec_gb,
                    ">0 GB",
                    "schema",
                    spec_gb is not None and spec_gb > 0,
                    f"Could not parse RAM capacity from spec {spec!r}.",
                )
                if spec_gb is not None and facts.ram_total_gb is not None:
                    _add(
                        "spec_gt_current_ram",
                        spec_gb,
                        f">{facts.ram_total_gb}",
                        "telemetry",
                        spec_gb > facts.ram_total_gb,
                        f"Requested RAM {spec_gb} GB must exceed current {facts.ram_total_gb} GB.",
                    )
                if catalog_row is None:
                    warnings.append("Hardware upgradability unknown; a human must confirm.")
                else:
                    _add(
                        "ram_upgradable",
                        catalog_row.ram_upgradable,
                        True,
                        f"catalog:{catalog_row.model_identifier}",
                        catalog_row.ram_upgradable,
                        (
                            f"RAM in {catalog_row.model_name} ({catalog_row.model_identifier}) "
                            "is soldered and not upgradable."
                        ),
                    )
                    if not catalog_row.ram_upgradable and facts.ram_sustained:
                        candidate_alts.append(
                            Alternative(
                                tool="flag_device_for_replacement",
                                args_hint={
                                    "device_id": facts.device_id,
                                    "reason": (
                                        f"Sustained RAM pressure on non-upgradable "
                                        f"{catalog_row.model_name} ({catalog_row.model_identifier})"
                                    ),
                                },
                            )
                        )
                        candidate_alts.append(
                            Alternative(
                                tool="open_remediation_ticket",
                                args_hint={
                                    "device_id": facts.device_id,
                                    "check_id": "memory_pressure",
                                    "note": "Investigate memory pressure on soldered-RAM model",
                                },
                            )
                        )
                    if (
                        catalog_row.ram_upgradable
                        and catalog_row.ram_max_gb is not None
                        and spec_gb is not None
                    ):
                        _add(
                            "spec_lte_ram_max",
                            spec_gb,
                            catalog_row.ram_max_gb,
                            f"catalog:{catalog_row.model_identifier}",
                            spec_gb <= catalog_row.ram_max_gb,
                            (
                                f"Requested RAM {spec_gb} GB exceeds model maximum "
                                f"{catalog_row.ram_max_gb} GB."
                            ),
                        )

            elif component == "storage":
                disk_needed = _device_disk_rule(facts, float(low_disk_pct))
                _add(
                    "disk_pressure_evidence",
                    {
                        "latest_free_pct": facts.disk_latest_free_pct,
                        "depleting": facts.disk_depleting,
                        "days_to_low": facts.days_to_low,
                    },
                    {"low_disk_free_pct": float(low_disk_pct), "days_to_low_lt": 30},
                    low_disk_src,
                    disk_needed,
                    "No low-disk or near-term disk-depletion evidence on this device.",
                )
                spec_gb = parse_capacity_gb(spec)
                _add(
                    "spec_capacity_parseable",
                    spec_gb,
                    ">0 GB",
                    "schema",
                    spec_gb is not None and spec_gb > 0,
                    f"Could not parse storage capacity from spec {spec!r}.",
                )
                if spec_gb is not None and facts.storage_total_gb is not None:
                    _add(
                        "spec_gt_current_storage",
                        spec_gb,
                        f">{facts.storage_total_gb}",
                        "telemetry",
                        spec_gb > facts.storage_total_gb,
                        (
                            f"Requested storage {spec_gb} GB must exceed current "
                            f"{facts.storage_total_gb} GB."
                        ),
                    )
                if catalog_row is None:
                    warnings.append("Hardware upgradability unknown; a human must confirm.")
                else:
                    _add(
                        "storage_upgradable",
                        catalog_row.storage_upgradable,
                        True,
                        f"catalog:{catalog_row.model_identifier}",
                        catalog_row.storage_upgradable,
                        (
                            f"Storage in {catalog_row.model_name} "
                            f"({catalog_row.model_identifier}) is soldered and not upgradable."
                        ),
                    )
                    if not catalog_row.storage_upgradable and disk_needed:
                        candidate_alts.append(
                            Alternative(
                                tool="open_remediation_ticket",
                                args_hint={
                                    "device_id": facts.device_id,
                                    "check_id": "low_disk_space",
                                    "note": "Remediate low disk on non-upgradable storage model",
                                },
                            )
                        )
                        candidate_alts.append(
                            Alternative(
                                tool="flag_device_for_replacement",
                                args_hint={
                                    "device_id": facts.device_id,
                                    "reason": (
                                        f"Low disk space on non-upgradable storage model "
                                        f"{catalog_row.model_name} ({catalog_row.model_identifier})"
                                    ),
                                },
                            )
                        )

    elif draft.tool_name == "open_remediation_ticket":
        if not isinstance(facts, DeviceFacts):
            _add(
                "target_type",
                "employee",
                "device",
                "schema",
                False,
                "Ticket target must be device.",
            )
        else:
            check_id = str(draft.arguments.get("check_id", "")).strip()
            note = str(draft.arguments.get("note", "")).strip()
            _add(
                "valid_check_id",
                bool(check_id),
                True,
                "schema",
                bool(check_id),
                "check_id is required.",
            )
            _add("valid_note", bool(note), True, "schema", bool(note), "Ticket note is required.")

            if check_id == "low_disk_space":
                ok = _device_disk_rule(facts, float(low_disk_pct))
                _add(
                    "low_disk_space_active",
                    facts.disk_latest_free_pct,
                    f"<{low_disk_pct}% or depleting",
                    low_disk_src,
                    ok,
                    "Device does not have low or depleting disk space.",
                )
            elif check_id == "memory_pressure":
                _add(
                    "memory_pressure_active",
                    facts.ram_sustained,
                    True,
                    "analyzer:ram",
                    facts.ram_sustained,
                    "Device does not have sustained memory pressure.",
                )
            elif check_id == "non_baseline_software":
                _add(
                    "non_baseline_software_active",
                    facts.non_baseline_apps,
                    "non-empty",
                    "analyzer:software",
                    len(facts.non_baseline_apps) > 0,
                    "Device has no non-baseline software on its latest snapshot.",
                )
            elif check_id:
                is_failing = check_id in facts.failing_checks
                _add(
                    "telemetry_check_failing",
                    "fail" if is_failing else "pass_or_absent",
                    "fail",
                    "telemetry",
                    is_failing,
                    f"Compliance check {check_id!r} is not currently failing on {facts.device_id}.",
                )

    elif draft.tool_name == "flag_device_for_replacement":
        if not isinstance(facts, DeviceFacts):
            _add(
                "target_type",
                "employee",
                "device",
                "schema",
                False,
                "Replacement target must be device.",
            )
        else:
            reason_arg = str(draft.arguments.get("reason", "")).strip()
            _add(
                "valid_reason",
                bool(reason_arg),
                True,
                "schema",
                bool(reason_arg),
                "reason is required.",
            )

            bat_sr = facts.battery_condition is not None and facts.battery_condition != "Normal"
            multi_sig = len(facts.active_signals) >= int(rep_min_sig)
            ram_non_up = (
                facts.ram_sustained
                and catalog_row is not None
                and catalog_row.ram_upgradable is False
            )
            disk_low_non_up = (
                facts.disk_latest_free_pct is not None
                and facts.disk_latest_free_pct < float(low_disk_pct)
                and catalog_row is not None
                and catalog_row.storage_upgradable is False
            )
            eligible = bat_sr or multi_sig or ram_non_up or disk_low_non_up
            _add(
                "replacement_criteria",
                {
                    "battery_sr": bat_sr,
                    "active_signals": len(facts.active_signals),
                    "ram_non_upgradable": ram_non_up,
                    "disk_low_non_upgradable": disk_low_non_up,
                },
                {"min_signals": int(rep_min_sig)},
                rep_src,
                eligible,
                (
                    f"Device {facts.device_id} does not meet replacement criteria "
                    f"(active_signals={len(facts.active_signals)} < {rep_min_sig}, no battery SR, "
                    "and no non-upgradable RAM/disk failure)."
                ),
            )

    elif draft.tool_name == "notify_employee":
        if not isinstance(facts, EmployeeFacts):
            _add(
                "target_type",
                "device",
                "employee",
                "schema",
                False,
                "notify_employee target must be an employee.",
            )
        else:
            msg = str(draft.arguments.get("message", "")).strip()
            _add(
                "message_non_empty_and_bounded",
                len(msg),
                int(notify_max_chars),
                notify_src,
                0 < len(msg) <= int(notify_max_chars),
                f"Notification message must be 1..{notify_max_chars} characters.",
            )

            dev_by_id = {d.device_id: d for d in facts.devices}
            # Each cited evidence item must map to an active finding on an owned device
            active_cited_devs: set[str] = set()
            all_active = bool(draft.evidence)
            for ev in draft.evidence:
                dev_fact = dev_by_id.get(ev.device_id)
                if dev_fact is None or not _device_has_active_finding_for_evidence(
                    dev_fact,
                    ev,
                    low_disk_pct=float(low_disk_pct),
                    eol_cycles=int(eol_cycles),
                ):
                    all_active = False
                else:
                    active_cited_devs.add(ev.device_id)
            _add(
                "cited_evidence_active_findings",
                all_active,
                True,
                "telemetry",
                all_active,
                "Cited evidence must correspond to active findings on the employee's devices.",
            )

            msg_lower = msg.lower()
            cited_names_match = False
            for dev_id in active_cited_devs:
                dev_fact = dev_by_id[dev_id]
                if dev_id.lower() in msg_lower:
                    cited_names_match = True
                if dev_fact.hostname and dev_fact.hostname.lower() in msg_lower:
                    cited_names_match = True
            _add(
                "message_names_cited_device",
                cited_names_match,
                True,
                "privacy",
                cited_names_match,
                "Notification message must name at least one of the employee's cited devices.",
            )

            # Check no other device ID, hostname, or employee ID is mentioned
            owned_dev_lower = {d.device_id.lower() for d in facts.devices}
            owned_host_lower = {h.lower() for h in facts.hostnames if h}
            leaked_refs: list[str] = []
            for other_dev in facts.other_device_ids:
                if other_dev.lower() in msg_lower and other_dev.lower() not in owned_dev_lower:
                    leaked_refs.append(other_dev)
            for other_host in facts.other_hostnames:
                if other_host.lower() in msg_lower and other_host.lower() not in owned_host_lower:
                    leaked_refs.append(other_host)
            for other_emp in facts.other_employee_ids:
                if (
                    other_emp.lower() in msg_lower
                    and other_emp.lower() != facts.employee_id.lower()
                ):
                    leaked_refs.append(other_emp)
            for emp_tok in _EMP_TOKEN_RE.findall(msg):
                if emp_tok.lower() != facts.employee_id.lower():
                    leaked_refs.append(emp_tok)
            for dev_tok in _DEVICE_ID_TOKEN_RE.findall(msg):
                if dev_tok.lower() not in owned_dev_lower:
                    leaked_refs.append(dev_tok)

            _add(
                "message_no_other_device_or_employee",
                leaked_refs,
                [],
                "privacy",
                len(leaked_refs) == 0,
                "Notification message mentions another device or employee not owned by target.",
            )

    if reasons:
        # Enforce the no-dead-end invariant: only emit alternatives that themselves pass evaluate().
        verified_alts: list[Alternative] = []
        if not _evaluating_alternative and candidate_alts and isinstance(facts, DeviceFacts):
            for alt in candidate_alts:
                alt_draft = ProposalDraft(
                    tool_name=alt.tool,
                    target_type="device",
                    target_id=facts.device_id,
                    arguments=dict(alt.args_hint),
                    rationale=draft.rationale,
                    evidence=draft.evidence,
                )
                alt_verdict = evaluate(
                    alt_draft,
                    facts,
                    policies,
                    catalog_row,
                    clock,
                    telemetry_as_of,
                    actor_role=actor_role,
                    tool_enabled=tool_enabled,
                    actions_enabled=actions_enabled,
                    has_active_duplicate=False,
                    in_cooldown=False,
                    valid_evidence=valid_evidence,
                    _evaluating_alternative=True,
                )
                if alt_verdict.decision in ("allow", "allow_with_warnings"):
                    verified_alts.append(alt)

        return PolicyVerdict(
            decision="refuse",
            checks=checks,
            reasons=reasons,
            alternatives=verified_alts,
            warnings=[],
            dedupe_key=dk,
            policy_version=pv_str,
        )

    decision: Literal["allow", "allow_with_warnings"] = (
        "allow_with_warnings" if warnings else "allow"
    )
    return PolicyVerdict(
        decision=decision,
        checks=checks,
        reasons=[],
        alternatives=[],
        warnings=warnings,
        dedupe_key=dk,
        policy_version=pv_str,
    )
