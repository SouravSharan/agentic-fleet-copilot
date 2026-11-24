"""Nightly maintenance job: expire proposals, recover stuck executions, then reset demo tenants."""

from __future__ import annotations

import uuid
from typing import Any

from fleet_copilot.clock import Clock, SystemClock
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.ops.demo_reset import discover_companies, reset_all
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.policy import PolicyService


def run_nightly(
    *,
    admin_url: str | None = None,
    min_age_minutes: int = 5,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run expire_proposals + recover_executing across all tenants, then demo_reset --all.

    Safe on an empty database (0 companies). Never touches `copilot.spend_ledger`.
    """
    settings = get_settings()
    used_clock: Clock = clock if clock is not None else SystemClock()
    companies = discover_companies(admin_url=admin_url)

    expired_total = 0
    recovered_total = 0

    if companies:
        with DatabaseManager(
            telemetry_ro_dsn=settings.telemetry_ro_database_url,
            copilot_dsn=settings.copilot_database_url,
            min_size=1,
            max_size=4,
        ) as db:
            repo = PgTelemetryRepository(db, settings=settings)
            store = PgCopilotStore(db)
            svc = ActionService(
                db,
                repo,
                store,
                PolicyService(store),
                AuditService(db),
                MockAdapter(),
                settings=settings,
            )
            for cid in companies:
                ctx = TenantContext(
                    company_id=cid,
                    actor_id="ops_nightly",
                    role="it_admin",
                    trace_id=f"ops-nightly-{uuid.uuid4().hex[:12]}",
                )
                expired_total += len(svc.expire_due(ctx, used_clock))
                recovered_total += len(
                    svc.recover_executing(ctx, used_clock, min_age_minutes=min_age_minutes)
                )

    reset_summaries = reset_all(reason="nightly_maintenance", admin_url=admin_url)
    return {
        "companies": companies,
        "expired_count": expired_total,
        "recovered_count": recovered_total,
        "resets": reset_summaries,
    }
