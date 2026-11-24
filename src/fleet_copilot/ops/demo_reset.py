"""Per-company demo reset via least-privilege `demo_ops` SECURITY DEFINER function.

Never touches `copilot.spend_ledger` or other companies, and never rewrites baseline telemetry
rows (deletes only snapshots absent from `ops.seed_snapshot_ids`).
"""

from __future__ import annotations

from typing import Annotated, Any

import psycopg
import typer

from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import validate_company_id

app = typer.Typer(
    add_completion=False,
    help="Reset one company's (or all companies') demo operational state without touching spend.",
)


def reset_company(
    company_id: str,
    *,
    reason: str = "manual_demo_reset",
    demo_ops_url: str | None = None,
    use_admin: bool = False,
    admin_url: str | None = None,
) -> dict[str, Any]:
    """Reset one tenant's operational tables and non-seed rows via `ops.demo_reset_company`.

    Uses `DEMO_OPS_DATABASE_URL` by default (zero admin credentials in the web app).
    CLI / nightly jobs may set `use_admin=True`.
    """
    validate_company_id(company_id)
    settings = get_settings()
    if use_admin:
        dsn = admin_url or settings.admin_database_url
    else:
        dsn = demo_ops_url or settings.demo_ops_database_url

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ops.demo_reset_company(%s, %s)",
                    (company_id, reason),
                )
                row = cur.fetchone()
    if row is None or not isinstance(row[0], dict):
        raise RuntimeError(f"ops.demo_reset_company({company_id!r}) returned unexpected result")
    return dict(row[0])


def discover_companies(*, admin_url: str | None = None) -> list[str]:
    """List all tenant `company_id`s from `telemetry.companies` (admin connection)."""
    dsn = admin_url or get_settings().admin_database_url
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        rows = conn.execute(
            "SELECT company_id FROM telemetry.companies ORDER BY company_id ASC"
        ).fetchall()
    return [str(r[0]) for r in rows]


def reset_all(
    *,
    reason: str = "nightly_demo_reset",
    admin_url: str | None = None,
) -> list[dict[str, Any]]:
    """Reset every discovered company via `ops.demo_reset_company` (preserves spend_ledger)."""
    companies = discover_companies(admin_url=admin_url)
    results: list[dict[str, Any]] = []
    for cid in companies:
        results.append(reset_company(cid, reason=reason, use_admin=True, admin_url=admin_url))
    return results


@app.command()
def main(
    company: Annotated[
        str | None,
        typer.Option("--company", help="Single company_id to reset (e.g. acme-001)."),
    ] = None,
    all_companies: Annotated[
        bool,
        typer.Option("--all", help="Reset all companies."),
    ] = False,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Short audit reason recorded in demo_reset event."),
    ] = "manual_demo_reset",
) -> None:
    """Reset one company or all companies without touching `copilot.spend_ledger`."""
    if not all_companies and not company:
        raise typer.BadParameter("Provide either --company <id> or --all.")
    if all_companies:
        summaries = reset_all(reason=reason)
        for s in summaries:
            typer.echo(
                f"[{s['company_id']}] reset complete: threads={s['deleted_threads']}, "
                f"proposals={s['deleted_proposals']}, scenarios={s['deleted_telemetry_scenarios']}"
            )
        typer.echo(f"Done. Reset {len(summaries)} company(s).")
        return

    assert company is not None
    s = reset_company(company, reason=reason)
    typer.echo(
        f"[{s['company_id']}] reset complete: threads={s['deleted_threads']}, "
        f"proposals={s['deleted_proposals']}, scenarios={s['deleted_telemetry_scenarios']}"
    )


if __name__ == "__main__":
    app()
