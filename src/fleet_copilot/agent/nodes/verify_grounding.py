"""`verify_grounding` node: full-answer deterministic citation, device, and numeric verifier."""

from __future__ import annotations

import logging
import re
from typing import Any

from langgraph.runtime import Runtime

from fleet_copilot.agent.schemas import Answer, Claim
from fleet_copilot.agent.state import CopilotState
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.audit import GroundingViolationPayload

logger = logging.getLogger(__name__)

# Patterns stripped before extracting standalone numeric values from text
_ISO_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_SHORT_DATE_RE = re.compile(r"\b\d{2}-\d{2}\b")
_VERSION_CONTEXT_RE = re.compile(
    r"(?:\b(?:macos|windows|os|version|ver|gen|xps|m|v)\s*\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+\b|\b\d{2}H[12]\b)",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"\b[ECQ]\d+\b")
_STANDALONE_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.\-:])\d+(?:\.\d+)?(?![A-Za-z0-9_.\-:])")

# 12-char alphanumeric device ID token containing >=1 uppercase letter and >=1 digit
_DEVICE_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?=[A-Z0-9]{12}(?![A-Za-z0-9_-]))(?=[A-Z0-9]*[0-9])(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12}"
)
# Generic fleet hostname pattern catching unknown/cross-tenant hostnames (e.g. globex-dell-7)
_ORG_HOSTNAME_RE = re.compile(
    r"\b[a-z][a-z0-9]{1,15}-(?:macbook|thinkpad|dell|mac|laptop|desktop|imac|xps)-\d+\b",
    re.IGNORECASE,
)


def _extract_prompt_numbers(text: str) -> set[float]:
    nums: set[float] = set()
    for match in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            nums.add(float(match))
        except ValueError:
            pass
    return nums


def _extract_claim_text_numbers(text: str) -> list[float]:
    cleaned = _ISO_DATE_RE.sub(" ", text)
    cleaned = _SHORT_DATE_RE.sub(" ", cleaned)
    cleaned = _VERSION_CONTEXT_RE.sub(" ", cleaned)
    cleaned = _HANDLE_RE.sub(" ", cleaned)
    nums: list[float] = []
    for m in _STANDALONE_NUM_RE.findall(cleaned):
        try:
            nums.append(float(m))
        except ValueError:
            pass
    return nums


def _extract_devices_and_hostnames(
    text: str,
    hostname_to_device: dict[str, str],
) -> tuple[set[str], set[str]]:
    """Extract resolved device IDs and unresolved hostname tokens from arbitrary text."""
    devices: set[str] = set(_DEVICE_ID_RE.findall(text))
    unknown_hostnames: set[str] = set()
    lower = text.lower()

    for host_lower, dev_id in hostname_to_device.items():
        if re.search(rf"(?<![a-z0-9_-]){re.escape(host_lower)}(?![a-z0-9_-])", lower):
            devices.add(dev_id)

    for match in _ORG_HOSTNAME_RE.findall(lower):
        mapped = hostname_to_device.get(match)
        if mapped:
            devices.add(mapped)
        else:
            unknown_hostnames.add(match)

    return devices, unknown_hostnames


def _collect_verifiable_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        fval = float(obj)
        out.add(fval)
        if 0.0 < fval <= 1.0:
            out.add(round(fval * 100.0, 2))
            out.add(round(fval * 100.0, 1))
            out.add(float(round(fval * 100.0)))
    elif isinstance(obj, str):
        for part in re.findall(r"\d+(?:\.\d+)?", obj):
            if len(part) <= 6:
                try:
                    out.add(float(part))
                except ValueError:
                    pass
    elif isinstance(obj, list):
        for item in obj:
            _collect_verifiable_numbers(item, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_verifiable_numbers(v, out)


def _is_number_verifiable(num: float, pool: set[float], exempt: set[float]) -> bool:
    if any(abs(num - ex) <= 1e-6 for ex in exempt):
        return True
    is_int = abs(num - round(num)) < 1e-9
    for candidate in pool:
        if is_int and abs(candidate - round(candidate)) < 1e-9:
            if int(round(num)) == int(round(candidate)):
                return True
        if abs(num - candidate) <= 0.2 + 1e-9:
            return True
    return False


def _has_not_found_output(tool_outputs: list[dict[str, Any]]) -> bool:
    for item in tool_outputs:
        out = item.get("output")
        if isinstance(out, dict) and out.get("error") == "not_found":
            return True
    return False


def verify_answer_grounding(
    answer: Answer,
    *,
    evidence_map: dict[str, dict[str, Any]],
    tool_outputs: list[dict[str, Any]],
    resolved_policies: dict[str, dict[str, Any]],
    insights: list[dict[str, Any]],
    user_message: str,
    telemetry_as_of: str,
    tenant_hostnames: dict[str, str] | None = None,
    charts: list[dict[str, Any]] | None = None,
    queries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run hard citation/device checks across all Answer fields plus soft numeric checks."""
    hard_violations: list[dict[str, Any]] = []
    soft_violations: list[dict[str, Any]] = []

    hostname_to_device: dict[str, str] = {k.lower(): v for k, v in (tenant_hostnames or {}).items()}
    ledger_devices: set[str] = set()
    handle_devices: dict[str, set[str]] = {}
    for h, ev in evidence_map.items():
        dev = ev.get("device_id")
        if dev and str(dev) != "fleet":
            ledger_devices.add(str(dev))
            handle_devices.setdefault(h, set()).add(str(dev))
            if ev.get("hostname"):
                hostname_to_device[str(ev["hostname"]).lower()] = str(dev)

    for chart in charts or []:
        h = str(chart.get("handle") or "")
        for row in chart.get("data_rows") or []:
            if isinstance(row, dict) and row.get("device_id"):
                dev_s = str(row["device_id"])
                ledger_devices.add(dev_s)
                if h:
                    handle_devices.setdefault(h, set()).add(dev_s)
                if row.get("hostname"):
                    hostname_to_device[str(row["hostname"]).lower()] = dev_s

    for query in queries or []:
        h = str(query.get("handle") or "")
        for row in query.get("rows") or []:
            if isinstance(row, dict) and row.get("device_id"):
                dev_s = str(row["device_id"])
                ledger_devices.add(dev_s)
                if h:
                    handle_devices.setdefault(h, set()).add(dev_s)
                host = row.get("hostname") or row.get("os_hostname")
                if host:
                    hostname_to_device[str(host).lower()] = dev_s

    # Prompt-echo allowlist strictly when a lookup returned `not_found`
    prompt_not_found_tokens: set[str] = set()
    if _has_not_found_output(tool_outputs):
        prompt_not_found_tokens.update(_DEVICE_ID_RE.findall(user_message))
        prompt_not_found_tokens.update(m.lower() for m in _ORG_HOSTNAME_RE.findall(user_message))

    # Build verifiable numeric pool and exempt numbers (prompt, as_of date parts, version tokens)
    pool: set[float] = {0.0, float(len(evidence_map)), float(len(insights))}
    _collect_verifiable_numbers(evidence_map, pool)
    _collect_verifiable_numbers(tool_outputs, pool)
    _collect_verifiable_numbers(resolved_policies, pool)
    _collect_verifiable_numbers(insights, pool)
    for chart in charts or []:
        _collect_verifiable_numbers(chart.get("data_rows") or [], pool)
    for query in queries or []:
        _collect_verifiable_numbers(query.get("rows") or [], pool)

    exempt: set[float] = _extract_prompt_numbers(user_message)
    _collect_verifiable_numbers(telemetry_as_of, exempt)
    for ev in evidence_map.values():
        val = ev.get("value")
        if isinstance(val, str):
            for tok in re.findall(r"\d+(?:\.\d+)?", val):
                try:
                    exempt.add(float(tok))
                except ValueError:
                    pass

    all_cited_devices: set[str] = set()
    all_claim_numbers: set[float] = set()

    # 1. Verify claims (citations + self-reported & regex-extracted devices/hostnames + numbers)
    for idx, claim in enumerate(answer.claims):
        all_claim_numbers.update(claim.numbers)
        if not claim.citations:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "uncited_claim",
                    "claim_index": idx,
                    "detail": "Factual claim has no citations.",
                }
            )
            continue

        cited_devices: set[str] = set()
        missing_handles: list[str] = []
        for h in claim.citations:
            ev_rec = evidence_map.get(h)
            if ev_rec is None:
                missing_handles.append(h)
            else:
                devs_for_h = handle_devices.get(h, set())
                cited_devices.update(devs_for_h)
                all_cited_devices.update(devs_for_h)

        if missing_handles:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "unknown_citation",
                    "claim_index": idx,
                    "detail": f"Unknown citation handle(s): {', '.join(missing_handles)}",
                }
            )

        extracted_devs, unknown_hosts = _extract_devices_and_hostnames(
            claim.text, hostname_to_device
        )
        all_claim_devs = set(claim.device_ids) | extracted_devs

        for bad_host in sorted(unknown_hosts):
            if bad_host in prompt_not_found_tokens:
                continue
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "unknown_device_in_claim",
                    "claim_index": idx,
                    "detail": f"Unknown hostname {bad_host!r} in claim text is not in ledger.",
                }
            )

        for dev_id in sorted(all_claim_devs):
            if dev_id not in ledger_devices:
                if dev_id in prompt_not_found_tokens:
                    continue
                hard_violations.append(
                    {
                        "tier": "hard",
                        "code": "unknown_device_in_claim",
                        "claim_index": idx,
                        "detail": f"Device {dev_id} in claim is not present in the turn ledger.",
                    }
                )
            elif dev_id not in cited_devices:
                hard_violations.append(
                    {
                        "tier": "hard",
                        "code": "device_not_in_cited_evidence",
                        "claim_index": idx,
                        "detail": (
                            f"Device {dev_id} is not present in cited handles {claim.citations}."
                        ),
                    }
                )

        # Soft check: numbers in claim.numbers and standalone numbers in claim.text
        claim_pool = pool | {float(len(all_claim_devs)), float(len(claim.citations))}
        numbers_to_check = list(claim.numbers) + _extract_claim_text_numbers(claim.text)
        bad_nums: list[float] = []
        for num in numbers_to_check:
            if not _is_number_verifiable(num, claim_pool, exempt):
                if num not in bad_nums:
                    bad_nums.append(num)
        if bad_nums:
            soft_violations.append(
                {
                    "tier": "soft",
                    "code": "unverified_number",
                    "claim_index": idx,
                    "detail": f"Numbers not matched in tool outputs: {bad_nums}",
                }
            )

    # 2. Verify Answer.result_device_ids (H2): every entry must be in cited evidence (HARD)
    for dev_id in answer.result_device_ids:
        if dev_id not in all_cited_devices:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "result_device_not_cited",
                    "device_id": dev_id,
                    "detail": f"result_device_ids entry {dev_id} is not covered by cited evidence.",
                }
            )

    # 3. Verify Answer.summary (H1): devices/hostnames in ledger (HARD); numbers matched (SOFT)
    sum_devs, sum_unknown_hosts = _extract_devices_and_hostnames(answer.summary, hostname_to_device)
    for dev_id in sorted(sum_devs):
        if dev_id not in ledger_devices and dev_id not in prompt_not_found_tokens:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "unknown_device_in_summary",
                    "detail": f"Device {dev_id} in summary is not present in the turn ledger.",
                }
            )
    for bad_host in sorted(sum_unknown_hosts):
        if bad_host not in prompt_not_found_tokens:
            hard_violations.append(
                {
                    "tier": "hard",
                    "code": "unknown_device_in_summary",
                    "detail": f"Unknown hostname {bad_host!r} in summary is not in ledger.",
                }
            )

    summary_pool = pool | all_claim_numbers | {float(len(answer.result_device_ids))}
    bad_summary_nums: list[float] = []
    for num in _extract_claim_text_numbers(answer.summary):
        if not _is_number_verifiable(num, summary_pool, exempt):
            if num not in bad_summary_nums:
                bad_summary_nums.append(num)
    if bad_summary_nums:
        soft_violations.append(
            {
                "tier": "soft",
                "code": "unverified_number_in_summary",
                "detail": f"Summary numbers not matched in claims/tool outputs: {bad_summary_nums}",
            }
        )

    # 4. Verify Answer.caveats (H1): devices/hostnames must be in turn ledger (HARD)
    for c_idx, caveat in enumerate(answer.caveats):
        cav_devs, cav_unknown_hosts = _extract_devices_and_hostnames(caveat, hostname_to_device)
        for dev_id in sorted(cav_devs):
            if dev_id not in ledger_devices and dev_id not in prompt_not_found_tokens:
                hard_violations.append(
                    {
                        "tier": "hard",
                        "code": "unknown_device_in_caveat",
                        "caveat_index": c_idx,
                        "detail": f"Device {dev_id} in caveat[{c_idx}] is not in the turn ledger.",
                    }
                )
        for bad_host in sorted(cav_unknown_hosts):
            if bad_host not in prompt_not_found_tokens:
                hard_violations.append(
                    {
                        "tier": "hard",
                        "code": "unknown_device_in_caveat",
                        "caveat_index": c_idx,
                        "detail": (
                            f"Unknown hostname {bad_host!r} in caveat[{c_idx}] is not in ledger."
                        ),
                    }
                )

    return hard_violations, soft_violations


def make_verify_grounding_node(deps: Any) -> Any:
    """Build the `verify_grounding` node enforcing retry-once + strip(hard)/warn(soft) + audit."""

    def verify_grounding(state: CopilotState, runtime: Runtime[TenantContext]) -> dict[str, Any]:
        ctx = runtime.context
        raw_answer = state.get("answer")
        if not raw_answer:
            return {"verifier_violations": []}

        answer = Answer.model_validate(raw_answer)
        evidence_map = state.get("evidence") or {}
        tool_outputs = state.get("tool_outputs") or []
        resolved_policies = state.get("resolved_policies") or {}
        insights = state.get("insights") or []
        user_message = state.get("user_message") or ""
        telemetry_as_of = state.get("telemetry_as_of") or ""
        tenant_hostnames = state.get("tenant_hostnames") or {}

        hard_v, soft_v = verify_answer_grounding(
            answer,
            evidence_map=evidence_map,
            tool_outputs=tool_outputs,
            resolved_policies=resolved_policies,
            insights=insights,
            charts=state.get("charts") or [],
            queries=state.get("queries") or [],
            user_message=user_message,
            telemetry_as_of=telemetry_as_of,
            tenant_hostnames=tenant_hostnames,
        )
        all_v = hard_v + soft_v
        if not all_v:
            return {"verifier_violations": []}

        retries = int(state.get("verifier_retries", 0))
        max_retries = deps.runtime_cfg.budgets.max_verifier_retries
        codes = sorted({v["code"] for v in all_v})
        counts = {"hard": len(hard_v), "soft": len(soft_v)}
        turn_id = state.get("turn_id", "")
        thread_id = state.get("thread_id", "")

        if retries < max_retries:
            deps.audit_svc.record_standalone(
                ctx,
                "grounding_violation",
                GroundingViolationPayload(
                    turn_id=turn_id,
                    codes=codes,
                    counts=counts,
                    outcome="retried",
                ),
                thread_id=thread_id,
            )
            return {
                "verifier_violations": all_v,
                "verifier_retries": retries + 1,
            }

        # Retries exhausted: strip hard violations across answer fields; warn on soft numbers.
        warnings: list[str] = []
        updated_answer = answer
        if hard_v:
            bad_claim_indices = {int(v["claim_index"]) for v in hard_v if "claim_index" in v}
            kept_claims: list[Claim] = [
                c for i, c in enumerate(answer.claims) if i not in bad_claim_indices
            ]
            kept_cited_devs = {
                str(evidence_map[h]["device_id"])
                for c in kept_claims
                for h in c.citations
                if h in evidence_map and evidence_map[h].get("device_id")
            }
            kept_result_ids = [d for d in answer.result_device_ids if d in kept_cited_devs]

            bad_caveat_indices = {int(v["caveat_index"]) for v in hard_v if "caveat_index" in v}
            kept_caveats = [c for i, c in enumerate(answer.caveats) if i not in bad_caveat_indices]
            kept_caveats.append(
                f"Removed unverified grounding references ({len(hard_v)} hard violation(s))."
            )

            summary = answer.summary
            if any(v["code"] == "unknown_device_in_summary" for v in hard_v):
                summary = (
                    "Verified findings are listed below; ungrounded device references were "
                    "removed from the summary."
                )

            updated_answer = answer.model_copy(
                update={
                    "summary": summary,
                    "result_device_ids": kept_result_ids,
                    "claims": kept_claims,
                    "caveats": kept_caveats,
                }
            )
            warnings.append(
                f"Grounding verifier stripped {len(bad_claim_indices)} claim(s) and "
                f"sanitized {len(hard_v)} hard grounding violation(s)."
            )
            deps.audit_svc.record_standalone(
                ctx,
                "grounding_violation",
                GroundingViolationPayload(
                    turn_id=turn_id,
                    codes=codes,
                    counts=counts,
                    outcome="stripped",
                ),
                thread_id=thread_id,
            )
        else:
            warnings.append(
                "Numeric grounding check flagged one or more numbers not matched in tool outputs."
            )
            deps.audit_svc.record_standalone(
                ctx,
                "grounding_violation",
                GroundingViolationPayload(
                    turn_id=turn_id,
                    codes=codes,
                    counts=counts,
                    outcome="warned",
                ),
                thread_id=thread_id,
            )

        return {
            "answer": updated_answer.model_dump(mode="json"),
            "verifier_violations": [],
            "warnings": warnings,
        }

    return verify_grounding
