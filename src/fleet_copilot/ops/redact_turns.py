"""Retention redaction job for `copilot.turns` covering all current and future text/JSON columns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import psycopg
import typer
from psycopg import sql
from psycopg.rows import DictRow

from fleet_copilot.clock import SystemClock
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import DatabaseManager, get_db_manager

app = typer.Typer(
    add_completion=False,
    help="Redact free-text and JSON payload columns on expired copilot.turns rows.",
)

# Non-sensitive structural, hash, and aggregate-metric columns preserved after retention redaction.
# Any OTHER text/varchar/json/jsonb column on `copilot.turns` (including future columns such as
# `response_json` or chart payloads) is automatically discovered and cleared.
RETAINED_TURN_COLUMNS: frozenset[str] = frozenset(
    {
        "turn_id",
        "trace_id",
        "thread_id",
        "company_id",
        "actor_id",
        "status",
        "llm_model",
        "model_id",
        "prompt_version",
        "input_hash",
        "answer_hash",
        "input_tokens",
        "output_tokens",
        "cost_usd_est",
        "latency_ms",
        "feedback",
        "created_at",
        "completed_at",
        "redacted_at",
    }
)

_REDACTABLE_DATA_TYPES: frozenset[str] = frozenset({"text", "character varying", "json", "jsonb"})


def _discover_tenants() -> list[str]:
    settings = get_settings()
    with psycopg.connect(settings.admin_database_url, prepare_threshold=None) as conn:
        rows = conn.execute(
            "SELECT company_id FROM telemetry.companies ORDER BY company_id ASC"
        ).fetchall()
    return [str(r[0]) for r in rows]


def _discover_redactable_columns(cur: psycopg.Cursor[DictRow]) -> list[dict[str, Any]]:
    """Inspect `information_schema.columns` for every non-retained text/JSON column on turns."""
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'copilot'
          AND table_name = 'turns'
        ORDER BY ordinal_position ASC
        """
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        col_name = str(row["column_name"])
        data_type = str(row["data_type"]).lower()
        if col_name in RETAINED_TURN_COLUMNS:
            continue
        if data_type in _REDACTABLE_DATA_TYPES:
            out.append(dict(row))
    return out


def _redacted_assignment(col: dict[str, Any]) -> sql.Composable:
    """Build a safe SQL `col = <redacted value>` expression respecting NULLability and type."""
    ident = sql.Identifier(str(col["column_name"]))
    is_nullable = str(col["is_nullable"]).upper() == "YES"
    data_type = str(col["data_type"]).lower()
    default = str(col.get("column_default") or "")

    if is_nullable:
        return sql.SQL("{} = NULL").format(ident)
    if data_type in ("json", "jsonb"):
        empty_literal = "[]" if "[]" in default else "{}"
        return sql.SQL("{} = {}::{}").format(
            ident,
            sql.Literal(empty_literal),
            sql.SQL(data_type),
        )
    return sql.SQL("{} = ''").format(ident)


def redact_expired_turns(
    db: DatabaseManager,
    *,
    cutoff: datetime,
    now: datetime | None = None,
    company_id: str | None = None,
) -> int:
    """Clear all non-retained text and JSON/JSONB columns on turns older than `cutoff`.

    Preserves `input_hash`, `answer_hash`, token/cost/latency metrics, and stamps `redacted_at`.
    Automatically discovers future JSON/text columns (e.g. `response_json`) from catalog metadata.
    """
    ts_now = now if now is not None else datetime.now(UTC)
    tenants = [company_id] if company_id else _discover_tenants()
    total = 0

    for cid in tenants:
        with db.copilot_cursor(cid) as cur:
            cols = _discover_redactable_columns(cur)
            assignments = [_redacted_assignment(c) for c in cols]
            assignments.append(sql.SQL("redacted_at = %s"))
            query = sql.SQL(
                """
                UPDATE copilot.turns
                SET {sets}
                WHERE created_at < %s
                  AND redacted_at IS NULL
                """
            ).format(sets=sql.SQL(", ").join(assignments))
            cur.execute(query, (ts_now, cutoff))
            total += max(0, cur.rowcount or 0)
    return total


def scan_redacted_turns(
    db: DatabaseManager,
    *,
    company_id: str | None = None,
) -> list[dict[str, Any]]:
    """Verify every redacted turn row has zero residual content in any text/JSON payload column."""
    tenants = [company_id] if company_id else _discover_tenants()
    violations: list[dict[str, Any]] = []

    for cid in tenants:
        with db.copilot_cursor(cid) as cur:
            cols = _discover_redactable_columns(cur)
            col_names = [str(c["column_name"]) for c in cols]
            select_cols = [sql.Identifier("turn_id"), *(sql.Identifier(c) for c in col_names)]
            query = sql.SQL(
                """
                SELECT {cols}
                FROM copilot.turns
                WHERE redacted_at IS NOT NULL
                ORDER BY turn_id ASC
                """
            ).format(cols=sql.SQL(", ").join(select_cols))
            cur.execute(query)
            for row in cur.fetchall():
                for col_name in col_names:
                    val = row[col_name]
                    if val is None or val == "" or val == [] or val == {}:
                        continue
                    violations.append(
                        {
                            "company_id": cid,
                            "turn_id": str(row["turn_id"]),
                            "column": col_name,
                        }
                    )
    return violations


@app.command()
def main(
    days: Annotated[
        int | None,
        typer.Option(help="Retention window in days (defaults to TURN_RETENTION_DAYS)."),
    ] = None,
    company: Annotated[
        str | None,
        typer.Option(help="Limit redaction to a single company_id (default: all tenants)."),
    ] = None,
) -> None:
    """Redact expired turns while preserving hashes and aggregate operational metrics."""
    settings = get_settings()
    retention_days = days if days is not None else settings.turn_retention_days
    now = SystemClock().now()
    cutoff = now - timedelta(days=retention_days)
    db = get_db_manager()
    count = redact_expired_turns(db, cutoff=cutoff, now=now, company_id=company)
    typer.echo(f"Redacted {count} turn(s) created before {cutoff.isoformat()}.")


if __name__ == "__main__":
    app()
