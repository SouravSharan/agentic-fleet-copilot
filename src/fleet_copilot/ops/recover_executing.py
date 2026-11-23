"""CLI job to recover proposals stuck in `executing` state longer than 5 minutes."""

from __future__ import annotations

import uuid
from typing import Annotated

import psycopg
import typer

from fleet_copilot.clock import SystemClock
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import get_db_manager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.policy import PolicyService

app = typer.Typer(add_completion=False, help="Recover proposals stuck in executing state.")


def _discover_tenants() -> list[str]:
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        rows = conn.execute(
            "SELECT company_id FROM telemetry.companies ORDER BY company_id ASC"
        ).fetchall()
    return [str(r[0]) for r in rows]


@app.command()
def main(
    company: Annotated[
        str | None,
        typer.Option(help="Limit recovery to a single company_id (default: all tenants)."),
    ] = None,
    min_age_minutes: Annotated[
        int,
        typer.Option(help="Minimum minutes in `executing` before recovery retries."),
    ] = 5,
) -> None:
    """Retry stuck `executing` proposals idempotently using their stored idempotency keys."""
    db = get_db_manager()
    repo = PgTelemetryRepository(db)
    store = PgCopilotStore(db)
    svc = ActionService(db, repo, store, PolicyService(store), AuditService(db), MockAdapter())
    clock = SystemClock()
    tenants = [company] if company else _discover_tenants()

    total = 0
    for company_id in tenants:
        ctx = TenantContext(
            company_id=company_id,
            actor_id="ops_recover_executing",
            role="it_admin",
            trace_id=f"ops-recover-{uuid.uuid4().hex[:12]}",
        )
        recovered = svc.recover_executing(ctx, clock, min_age_minutes=min_age_minutes)
        total += len(recovered)
        if recovered:
            typer.echo(f"[{company_id}] recovered {len(recovered)} proposal(s)")
    typer.echo(f"Done. Total recovered: {total}")


if __name__ == "__main__":
    app()
