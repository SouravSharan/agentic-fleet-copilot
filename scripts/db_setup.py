"""Idempotent PostgreSQL bootstrapper for roles, telemetry schema, and copilot schema.

Uses `ADMIN_DATABASE_URL` from `.env` (no `psql` binary required).
"""

from pathlib import Path
from typing import Annotated

import psycopg
import typer
from psycopg import sql

from fleet_copilot.config import get_settings

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


def run_setup(admin_url: str | None = None) -> None:
    """Execute 00_roles.sql, sync role passwords, then apply 01_telemetry.sql & 02_copilot.sql."""
    settings = get_settings()
    dsn = admin_url or settings.admin_database_url

    migration_files = [
        DB_DIR / "00_roles.sql",
        DB_DIR / "01_telemetry.sql",
        DB_DIR / "02_copilot.sql",
    ]
    for path in migration_files:
        if not path.exists():
            raise FileNotFoundError(f"Missing migration file: {path}")

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute(migration_files[0].read_text(encoding="utf-8"))
            _sync_role_passwords(
                conn,
                {
                    "telemetry_writer": settings.telemetry_writer_password,
                    "telemetry_ro": settings.telemetry_ro_password,
                    "copilot_app": settings.copilot_app_password,
                },
            )
            conn.execute(migration_files[1].read_text(encoding="utf-8"))
            conn.execute(migration_files[2].read_text(encoding="utf-8"))

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
) -> None:
    """Create roles, schemas, RLS policies, views, and global defaults idempotently."""
    run_setup(admin_url=admin_url)


if __name__ == "__main__":
    app()
