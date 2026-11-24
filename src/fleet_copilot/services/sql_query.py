"""Governed read-only SQL validator (sqlglot) and tenant-locked executor (10 §6, 11 §2 B3)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from psycopg import sql as pg_sql
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext

MAX_SQL_LIMIT = 200
DEFAULT_STATEMENT_TIMEOUT_MS = 3000

# Strictly the 5 non-PII live telemetry views (excludes raw snapshots, network_interfaces,
# tenant_lock_secret, copilot.*, pg_catalog.*, and information_schema.*).
ALLOWED_TELEMETRY_VIEWS: frozenset[str] = frozenset(
    {
        "v_latest_device_state",
        "v_snapshots_deduped",
        "v_disk_volumes",
        "v_compliance_results",
        "v_installed_software",
    }
)

# Defense-in-depth sensitive column blocklist (never selectable or returned via ad-hoc SQL).
FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "hardware_uuid",
        "serial_number",
        "payload",
        "secret",
        "mac",
        "address",
        "ip_address",
    }
)

# Strict function allowlist for analytical queries (no set_config, pg_*, lock_tenant, version...).
ALLOWED_SQL_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Aggregates
        "count",
        "avg",
        "sum",
        "min",
        "max",
        "percentile_cont",
        "percentile_disc",
        "bool_or",
        "bool_and",
        "logicalor",
        "logicaland",
        "string_agg",
        "group_concat",
        "array_agg",
        # Math & null handling
        "round",
        "floor",
        "ceil",
        "ceiling",
        "abs",
        "coalesce",
        "nullif",
        "greatest",
        "least",
        # Strings
        "lower",
        "upper",
        "trim",
        "btrim",
        "ltrim",
        "rtrim",
        "length",
        "char_length",
        "substr",
        "substring",
        "split_part",
        "left",
        "right",
        "concat",
        "concat_ws",
        # Dates / timestamps
        "date_trunc",
        "timestamp_trunc",
        "date_part",
        "extract",
        "to_char",
        "time_to_str",
        "timetostr",
        "current_date",
        "current_timestamp",
        "now",
        "age",
        "date",
        # Window functions
        "row_number",
        "rank",
        "dense_rank",
        "lag",
        "lead",
        "first_value",
        "last_value",
        # Postgres cast/formatting helpers emitted by sqlglot
        "cast",
        "try_cast",
        "if",
    }
)

# Safe SQL infix operators that inherit from exp.Func in sqlglot v30+.
_SAFE_BINARY_OPS: tuple[type[Any], ...] = (
    exp.Connector,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.ILike,
    exp.Is,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.IntDiv,
    exp.Mod,
    exp.DPipe,
)

# Disallowed AST node types (DML, DDL, utility, locking, commands).
_FORBIDDEN_NODE_TYPES: tuple[type[Any], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Grant,
    exp.Revoke,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Lock,
    exp.Into,
    exp.Union,
)


class SqlValidationError(ValueError):
    """Raised when an ad-hoc SQL query fails AST, table, column, function, or limit validation."""


class SqlQueryResult(BaseModel):
    """Structured result of an executed read-only SQL query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sql: str
    purpose: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    snapshot_ids: list[str] = Field(default_factory=list)
    device_ids: list[str] = Field(default_factory=list)
    telemetry_as_of: datetime | None = None


def _normalize_func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").strip().lower()
    sql_name = node.sql_name()
    if sql_name:
        return sql_name.strip().lower()
    return type(node).__name__.lower()


def validate_readonly_sql(raw_sql: str, *, max_limit: int = MAX_SQL_LIMIT) -> str:
    """Validate and canonicalize a single read-only PostgreSQL SELECT against telemetry views."""
    if not isinstance(raw_sql, str) or not raw_sql.strip():
        raise SqlValidationError("SQL query must be a non-empty string.")

    cleaned = raw_sql.strip()
    if "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
        raise SqlValidationError("SQL comments are not permitted.")

    # Allow at most one trailing semicolon; forbid interior semicolons (multi-statement guard).
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise SqlValidationError("Only a single SELECT statement is permitted.")
    if not cleaned:
        raise SqlValidationError("SQL query must be a non-empty string.")

    try:
        parsed_list = sqlglot.parse(cleaned, read="postgres")
    except Exception as exc:  # noqa: BLE001
        raise SqlValidationError(f"Failed to parse SQL: {type(exc).__name__}") from exc

    if len(parsed_list) != 1 or parsed_list[0] is None:
        raise SqlValidationError("Exactly one SELECT statement is required.")

    stmt = parsed_list[0]
    if not isinstance(stmt, exp.Select):
        raise SqlValidationError(f"Only SELECT queries are allowed (got {type(stmt).__name__}).")

    # Reject any forbidden AST nodes anywhere in the tree (CTEs, subqueries, locking clauses).
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            raise SqlValidationError(f"Disallowed SQL construct: {type(node).__name__}")

    if stmt.args.get("locks"):
        raise SqlValidationError("Row-locking clauses (FOR UPDATE / FOR SHARE) are not allowed.")

    # Collect CTE aliases defined in this statement so we don't confuse them with physical tables.
    cte_names: set[str] = set()
    for cte in stmt.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            cte_names.add(alias.lower())

    # 1. Validate every referenced table is strictly `telemetry.<allowed_view>` (or a local CTE).
    tables = list(stmt.find_all(exp.Table))
    real_table_count = 0
    for tbl in tables:
        schema_name = (tbl.db or "").strip().lower()
        table_name = (tbl.name or "").strip().lower()
        if not schema_name and table_name in cte_names:
            continue
        if schema_name != "telemetry" or table_name not in ALLOWED_TELEMETRY_VIEWS:
            raise SqlValidationError(
                f"Table {tbl.sql(dialect='postgres')!r} is not permitted; "
                f"only telemetry.v_* views ({', '.join(sorted(ALLOWED_TELEMETRY_VIEWS))}) "
                "are allowed."
            )
        real_table_count += 1

    if real_table_count == 0:
        raise SqlValidationError("Query must select from at least one allowed telemetry.v_* view.")

    # 2. Forbid `SELECT *` / `table.*` (allow `COUNT(*)` only) and forbid sensitive columns.
    for star in stmt.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise SqlValidationError(
                "Wildcard `SELECT *` is not allowed; specify explicit columns."
            )

    for col in stmt.find_all(exp.Column):
        col_name = (col.name or "").strip().lower()
        if col_name in FORBIDDEN_COLUMNS:
            raise SqlValidationError(f"Column {col_name!r} is restricted and cannot be queried.")

    # 3. Reject schema-qualified function calls (e.g. pg_catalog.set_config, telemetry.lock_tenant)
    for dot in stmt.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            raise SqlValidationError("Schema-qualified function calls are not permitted.")

    # 4. Enforce strict function allowlist across the entire AST.
    for fn in stmt.find_all(exp.Func):
        if isinstance(fn, _SAFE_BINARY_OPS):
            continue
        if isinstance(fn, exp.Anonymous):
            fname = str(fn.name or "").strip().lower()
            if not fname or fname not in ALLOWED_SQL_FUNCTIONS:
                raise SqlValidationError(f"SQL function {fname!r} is not in the allowlist.")
        else:
            fname = _normalize_func_name(fn)
            if fname not in ALLOWED_SQL_FUNCTIONS:
                raise SqlValidationError(f"SQL function {fname!r} is not in the allowlist.")

    # 5. Enforce LIMIT <= max_limit (inject LIMIT 200 if absent; clamp if > max_limit).
    limit_node = stmt.args.get("limit")
    if limit_node is None:
        stmt = stmt.limit(max_limit)
    else:
        lim_expr = limit_node.expression
        if isinstance(lim_expr, exp.Literal) and lim_expr.is_int:
            val = int(lim_expr.this)
            if val < 1 or val > max_limit:
                stmt = stmt.limit(max_limit)
        else:
            raise SqlValidationError("LIMIT must be a positive integer literal.")

    return stmt.sql(dialect="postgres")


def _to_json_value(val: Any) -> Any:
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, (list, tuple)):
        return [_to_json_value(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _to_json_value(v) for k, v in val.items()}
    return val


def execute_readonly_sql(
    ctx: TenantContext,
    db: DatabaseManager,
    raw_sql: str,
    *,
    purpose: str = "",
    max_limit: int = MAX_SQL_LIMIT,
    timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    bypass_validator: bool = False,
) -> SqlQueryResult:
    """Validate and execute a read-only SQL query under `telemetry_ro` + `lock_tenant` + timeout."""
    sql_to_run = (
        raw_sql if bypass_validator else validate_readonly_sql(raw_sql, max_limit=max_limit)
    )
    bounded_timeout = max(50, min(int(timeout_ms), DEFAULT_STATEMENT_TIMEOUT_MS))

    with db.telemetry_ro_cursor(ctx.company_id) as cur:
        cur.execute(
            pg_sql.SQL("SET LOCAL statement_timeout = {}").format(
                pg_sql.Literal(f"{bounded_timeout}ms")
            )
        )
        cur.execute(sql_to_run)  # type: ignore[arg-type]
        fetched = cur.fetchall() if cur.description is not None else []
        col_names = [d.name for d in (cur.description or [])]

        # Also read tenant_as_of inside the same read-only transaction when feasible
        cur.execute("SELECT MAX(collected_at) AS as_of FROM telemetry.v_snapshots_deduped")
        as_of_row = cur.fetchone()
        as_of = as_of_row["as_of"] if as_of_row is not None else None

    safe_cols = [c for c in col_names if c.lower() not in FORBIDDEN_COLUMNS]
    rows: list[dict[str, Any]] = []
    snap_ids: set[str] = set()
    dev_ids: set[str] = set()
    for raw_row in fetched[:max_limit]:
        row_dict: dict[str, Any] = {}
        for col in safe_cols:
            row_dict[col] = _to_json_value(raw_row.get(col))
        if row_dict.get("snapshot_id"):
            snap_ids.add(str(row_dict["snapshot_id"]))
        if row_dict.get("device_id"):
            dev_ids.add(str(row_dict["device_id"]))
        rows.append(row_dict)

    return SqlQueryResult(
        sql=sql_to_run,
        purpose=purpose,
        columns=safe_cols,
        rows=rows,
        row_count=len(rows),
        truncated=len(fetched) >= max_limit,
        snapshot_ids=sorted(snap_ids),
        device_ids=sorted(dev_ids),
        telemetry_as_of=as_of,
    )
