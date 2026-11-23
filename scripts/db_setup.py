"""Idempotent PostgreSQL bootstrapper for roles, telemetry schema, and copilot schema.

Uses `ADMIN_DATABASE_URL` from `.env` (no `psql` binary required).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, LiteralString, cast

import psycopg
import typer
from psycopg import sql

from fleet_copilot.config import get_settings, with_database

app = typer.Typer(add_completion=False, help="Bootstrap PostgreSQL roles and schemas.")

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = REPO_ROOT / "db"


def _sync_role_passwords(conn: psycopg.Connection, passwords: dict[str, str]) -> None:
    """Apply non-empty role passwords safely via psycopg SQL composition."""
    with conn.cursor() as cur:
        for role_name, password in passwords.items():
            if not password or not password.strip():
                raise ValueError(f"Empty password configured for role {role_name!r}")
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                    sql.Identifier(role_name),
                    sql.Literal(password),
                )
            )


def ensure_database_exists(admin_url: str, dbname: str) -> None:
    """Create `dbname` on the server pointed to by `admin_url` if it does not exist."""
    maintenance_dsn = with_database(admin_url, "postgres")
    with psycopg.connect(maintenance_dsn, autocommit=True, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def run_setup(admin_url: str | None = None) -> None:
    """Execute 00_roles.sql, sync role passwords, then apply remaining numbered migrations."""
    settings = get_settings()
    dsn = admin_url or settings.admin_database_url

    migration_files = sorted(DB_DIR.glob("[0-9][0-9]_*.sql"))
    if not migration_files or migration_files[0].name != "00_roles.sql":
        raise FileNotFoundError(f"Expected db/00_roles.sql in {DB_DIR}")

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute(cast(LiteralString, migration_files[0].read_text(encoding="utf-8")))
            _sync_role_passwords(
                conn,
                {
                    "telemetry_writer": settings.telemetry_writer_password,
                    "telemetry_ro": settings.telemetry_ro_password,
                    "copilot_app": settings.copilot_app_password,
                },
            )
            for path in migration_files[1:]:
                conn.execute(cast(LiteralString, path.read_text(encoding="utf-8")))

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
                    FROM pg_roles
                    WHERE rolname IN ('telemetry_writer', 'telemetry_ro', 'copilot_app')
                    ORDER BY rolname
                    """
                )
                roles = cur.fetchall()
                cur.execute(
                    """
                    SELECT table_schema, table_name, table_type
                    FROM information_schema.tables
                    WHERE table_schema IN ('telemetry', 'copilot')
                    ORDER BY table_schema, table_type, table_name
                    """
                )
                objects = cur.fetchall()

    typer.echo("Roles configured:")
    for rolname, rolsuper, rolbypassrls, rolcanlogin in roles:
        typer.echo(
            f"  - {rolname}: login={rolcanlogin}, superuser={rolsuper}, bypassrls={rolbypassrls}"
        )
    typer.echo(f"Schema objects ready ({len(objects)} tables/views across telemetry & copilot).")


@app.command()
def main(
    admin_url: Annotated[
        str | None,
        typer.Option(help="Override ADMIN_DATABASE_URL from .env."),
    ] = None,
    test_db: Annotated[
        bool,
        typer.Option("--test-db", help="Create (if needed) and migrate TEST_DB_NAME."),
    ] = False,
) -> None:
    """Create roles, schemas, RLS policies, views, and global defaults idempotently."""
    settings = get_settings()
    base_admin = admin_url or settings.admin_database_url
    if test_db:
        ensure_database_exists(base_admin, settings.test_db_name)
        target_dsn = with_database(base_admin, settings.test_db_name)
        typer.echo(f"Bootstrapping test database: {settings.test_db_name}")
        run_setup(admin_url=target_dsn)
    else:
        run_setup(admin_url=base_admin)


if __name__ == "__main__":
    app()
