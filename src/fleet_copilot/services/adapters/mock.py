"""Deterministic mock adapter with idempotent receipts, outbox payloads, and test failure hooks."""

from __future__ import annotations

import hashlib
from typing import Any

from fleet_copilot.privacy import redact_for_logs
from fleet_copilot.services.adapters.base import OutboxChannel, Receipt

_TOOL_PREFIX: dict[str, str] = {
    "create_upgrade_order": "ORD-",
    "open_remediation_ticket": "TKT-",
    "flag_device_for_replacement": "RPL-",
    "notify_employee": "NTF-",
}

_TOOL_CHANNEL: dict[str, OutboxChannel] = {
    "open_remediation_ticket": "ticketing",
    "create_upgrade_order": "procurement",
    "notify_employee": "email",
    "flag_device_for_replacement": "asset_management",
}


def _render_outbound_payload(
    proposal: dict[str, Any],
    *,
    tool_name: str,
    idempotency_key: str,
) -> tuple[OutboxChannel, dict[str, Any]]:
    args = proposal.get("arguments_json") or proposal.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    target_id = str(proposal.get("target_id") or "")
    proposal_id = str(proposal.get("proposal_id") or "")
    company_id = str(proposal.get("company_id") or "")
    rationale = redact_for_logs(str(proposal.get("rationale") or ""))
    employee_id = str(proposal.get("employee_id") or args.get("employee_id") or "")

    if tool_name == "open_remediation_ticket":
        check_id = str(args.get("check_id") or "general_remediation")
        note = redact_for_logs(str(args.get("note") or rationale))
        priority = (
            "2 - High" if check_id in ("disk_encryption", "low_disk_space") else "3 - Moderate"
        )
        return (
            "ticketing",
            {
                "system": "servicenow_mock",
                "short_description": f"Remediate {check_id} on {target_id}",
                "description": note,
                "category": "endpoint_compliance",
                "priority": priority,
                "configuration_item": target_id,
                "check_id": check_id,
                "proposal_id": proposal_id,
                "idempotency_key": idempotency_key,
            },
        )

    if tool_name == "create_upgrade_order":
        component = str(args.get("component") or "hardware").lower()
        spec = redact_for_logs(str(args.get("spec") or ""))
        return (
            "procurement",
            {
                "system": "procurement_mock",
                "sku": f"HW-{component.upper()}-UPG",
                "component": component,
                "spec": spec,
                "quantity": 1,
                "device_id": target_id,
                "ship_to_employee_id": employee_id or target_id,
                "justification": rationale,
                "proposal_id": proposal_id,
                "idempotency_key": idempotency_key,
            },
        )

    if tool_name == "notify_employee":
        message = redact_for_logs(str(args.get("message") or ""))
        return (
            "email",
            {
                "system": "email_mock",
                "recipient_employee_id": target_id,
                "subject": f"Action needed on your managed device ({company_id})",
                "body_preview": message[:240],
                "proposal_id": proposal_id,
                "idempotency_key": idempotency_key,
            },
        )

    reason = redact_for_logs(str(args.get("reason") or rationale))
    return (
        "asset_management",
        {
            "system": "asset_lifecycle_mock",
            "action": "flag_for_replacement",
            "device_id": target_id,
            "assigned_employee_id": employee_id or None,
            "reason": reason,
            "priority": "high",
            "proposal_id": proposal_id,
            "idempotency_key": idempotency_key,
        },
    )


class MockAdapter:
    """Deterministic, idempotent in-memory adapter for procurement/ITSM/lifecycle/comms."""

    name: str = "mock"

    def __init__(self) -> None:
        self._fail_next: str | None = None
        self._raise_queue: list[Exception] = []
        self._receipts: dict[str, Receipt] = {}
        self.call_count: int = 0

    @property
    def executed_keys(self) -> set[str]:
        """Set of distinct idempotency keys that have succeeded."""
        return set(self._receipts.keys())

    def fail_next(self, error_code: str = "adapter_unavailable") -> None:
        """Instruct the next `execute()` call to return a failed receipt with `error_code`."""
        self._fail_next = error_code

    def raise_next(self, exc: Exception, count: int = 1) -> None:
        """Instruct the next `count` `execute()` calls to raise `exc`."""
        for _ in range(count):
            self._raise_queue.append(exc)

    def execute(self, proposal: dict[str, Any], idempotency_key: str) -> Receipt:
        """Execute a proposal idempotently, returning a deterministic receipt + outbox payload."""
        self.call_count += 1
        if self._raise_queue:
            raise self._raise_queue.pop(0)
        if self._fail_next is not None:
            err = self._fail_next
            self._fail_next = None
            tool_name = str(proposal.get("tool_name", ""))
            return Receipt(
                adapter=self.name,
                receipt_id="",
                idempotency_key=idempotency_key,
                status="failed",
                error_code=err,
                channel=_TOOL_CHANNEL.get(tool_name),
                details={"tool_name": tool_name},
            )

        existing = self._receipts.get(idempotency_key)
        if existing is not None:
            return existing

        tool_name = str(proposal.get("tool_name", ""))
        prefix = _TOOL_PREFIX.get(tool_name, "ACT-")
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:10]
        channel, payload = _render_outbound_payload(
            proposal, tool_name=tool_name, idempotency_key=idempotency_key
        )
        receipt = Receipt(
            adapter=self.name,
            receipt_id=f"{prefix}{digest}",
            idempotency_key=idempotency_key,
            status="ok",
            channel=channel,
            payload=payload,
            details={
                "tool_name": tool_name,
                "target_type": str(proposal.get("target_type", "")),
                "target_id": str(proposal.get("target_id", "")),
                "channel": channel,
            },
        )
        self._receipts[idempotency_key] = receipt
        return receipt
