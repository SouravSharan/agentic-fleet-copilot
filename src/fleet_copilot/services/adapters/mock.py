"""Deterministic mock adapter with idempotent receipts and programmable test failures."""

from __future__ import annotations

import hashlib
from typing import Any

from fleet_copilot.services.adapters.base import Receipt

_TOOL_PREFIX: dict[str, str] = {
    "create_upgrade_order": "ORD-",
    "open_remediation_ticket": "TKT-",
    "flag_device_for_replacement": "RPL-",
    "notify_employee": "NTF-",
}


class MockAdapter:
    """Deterministic, idempotent in-memory adapter for procurement/ITSM/lifecycle/comms."""

    name: str = "mock"

    def __init__(self) -> None:
        self._fail_next: str | None = None
        self._receipts: dict[str, Receipt] = {}
        self.call_count: int = 0

    @property
    def executed_keys(self) -> set[str]:
        """Set of distinct idempotency keys that have succeeded."""
        return set(self._receipts.keys())

    def fail_next(self, error_code: str = "adapter_unavailable") -> None:
        """Instruct the next `execute()` call to fail with `error_code`."""
        self._fail_next = error_code

    def execute(self, proposal: dict[str, Any], idempotency_key: str) -> Receipt:
        """Execute a proposal idempotently, returning a deterministic prefixed receipt ID."""
        self.call_count += 1
        if self._fail_next is not None:
            err = self._fail_next
            self._fail_next = None
            return Receipt(
                adapter=self.name,
                receipt_id="",
                idempotency_key=idempotency_key,
                status="failed",
                error_code=err,
                details={"tool_name": str(proposal.get("tool_name", ""))},
            )

        existing = self._receipts.get(idempotency_key)
        if existing is not None:
            return existing

        tool_name = str(proposal.get("tool_name", ""))
        prefix = _TOOL_PREFIX.get(tool_name, "ACT-")
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:10]
        receipt = Receipt(
            adapter=self.name,
            receipt_id=f"{prefix}{digest}",
            idempotency_key=idempotency_key,
            status="ok",
            details={
                "tool_name": tool_name,
                "target_type": str(proposal.get("target_type", "")),
                "target_id": str(proposal.get("target_id", "")),
            },
        )
        self._receipts[idempotency_key] = receipt
        return receipt
