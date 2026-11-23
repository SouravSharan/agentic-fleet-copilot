"""Tenant policy resolution service (global '*' < tenant < device)."""

from __future__ import annotations

from typing import Protocol

from fleet_copilot.domain.models import PolicyRow, PolicyValue, TenantContext


class _PolicyReader(Protocol):
    def policies(self, company_id: str) -> list[PolicyRow]: ...


class PolicyService:
    """Resolves typed thresholds in strict precedence order: global < tenant < device."""

    def __init__(self, store: _PolicyReader) -> None:
        self._store = store

    def resolve(
        self,
        ctx: TenantContext,
        device_id: str | None = None,
    ) -> dict[str, PolicyValue]:
        """Resolve all policy keys for `ctx.company_id` (and optional `device_id`)."""
        rows = self._store.policies(ctx.company_id)
        resolved: dict[str, PolicyValue] = {}

        # Layer 1: global '*'
        for row in rows:
            if row.company_id == "*" and row.scope_type == "global":
                resolved[row.metric_key] = PolicyValue(value=row.value, source="global")

        # Layer 2: tenant override
        for row in rows:
            if row.company_id == ctx.company_id and row.scope_type == "tenant":
                resolved[row.metric_key] = PolicyValue(value=row.value, source="tenant")

        # Layer 3: device override
        if device_id is not None:
            for row in rows:
                if (
                    row.company_id == ctx.company_id
                    and row.scope_type == "device"
                    and row.scope_value == device_id
                ):
                    resolved[row.metric_key] = PolicyValue(value=row.value, source="device")

        return resolved

    def get(
        self,
        ctx: TenantContext,
        metric_key: str,
        device_id: str | None = None,
    ) -> PolicyValue:
        """Return the resolved `PolicyValue` for `metric_key`, raising `KeyError` if unknown."""
        resolved = self.resolve(ctx, device_id=device_id)
        if metric_key not in resolved:
            raise KeyError(f"Unknown policy key: {metric_key!r}")
        return resolved[metric_key]
