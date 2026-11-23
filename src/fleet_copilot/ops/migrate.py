"""Unified database + LangGraph checkpointer migration entrypoint (`ops.migrate`)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from fleet_copilot.config import get_settings, with_database
from fleet_copilot.db.postgres import DatabaseManager, make_checkpointer

app = typer.Typer(add_completion=False, help="Run SQL migrations and PostgresSaver setup.")

_REPO_ROOT = Path(__file__).resolve().parents[3]


def run_migrations(*, admin_url: str | None = None, copilot_url: str | None = None) -> None:
    """Run numbered SQL migrations + hardware catalog upsert, then `checkpointer.setup()`."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from scripts.db_setup import run_setup

    settings = get_settings()
    run_setup(admin_url=admin_url or settings.admin_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=copilot_url or settings.copilot_database_url,
        min_size=1,
        max_size=2,
    ) as db:
        saver = make_checkpointer(db, settings=settings)
        saver.setup()
    typer.echo("LangGraph PostgresSaver checkpoint tables ready in schema copilot.")


@app.command()
def main(
    test_db: Annotated[
        bool,
        typer.Option("--test-db", help="Migrate and setup checkpoints on TEST_DB_NAME."),
    ] = False,
) -> None:
    """Run full database and checkpointer setup."""
    settings = get_settings()
    if test_db:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from scripts.db_setup import ensure_database_exists

        ensure_database_exists(settings.admin_database_url, settings.test_db_name)
        run_migrations(
            admin_url=with_database(settings.admin_database_url, settings.test_db_name),
            copilot_url=with_database(settings.copilot_database_url, settings.test_db_name),
        )
    else:
        run_migrations()


if __name__ == "__main__":
    app()
