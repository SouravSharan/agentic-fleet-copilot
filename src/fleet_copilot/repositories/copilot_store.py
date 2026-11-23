"""Tenant-scoped copilot store repository (read-only policies & tool configs in Step 2)."""

from __future__ import annotations

from typing import Protocol

from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import PolicyRow, ToolConfigRow


class CopilotStore(Protocol):
    """Protocol for accessing tenant policies and tool configs."""

    def policies(self, company_id: str) -> list[PolicyRow]: ...

    def tool_configs(self, company_id: str) -> list[ToolConfigRow]: ...


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
