"""PostgreSQL database access layer with fail-closed tenant scoping."""

from fleet_copilot.db.postgres import (
    DatabaseManager,
    InvalidTenantError,
    get_db_manager,
    validate_company_id,
)

__all__ = [
    "DatabaseManager",
    "InvalidTenantError",
    "get_db_manager",
    "validate_company_id",
]
