"""Governed read-only SQL validator (sqlglot) and tenant-locked executor."""

from __future__ import annotations

import re
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

# Positive per-view column allowlist: every view column EXCEPT serial_number, hardware_uuid,
# payload, and id. Verified against live information_schema in integration tests.
_SNAPSHOT_BASE_COLS: frozenset[str] = frozenset(
    {
        "snapshot_id",
        "company_id",
        "device_id",
        "employee_id",
        "collected_at",
        "agent_version",
        "ingested_at",
        "os_platform",
        "os_product_name",
        "os_product_version",
        "os_build_version",
        "os_architecture",
        "os_hostname",
        "model_name",
        "model_identifier",
        "processor",
        "total_memory_label",
        "ram_total_bytes",
        "ram_used_bytes",
        "ram_free_bytes",
        "ram_used_pct",
        "has_battery_block",
        "battery_present",
        "battery_charging_status",
        "battery_percentage",
        "battery_condition",
        "battery_cycle_count",
        "battery_full_charge_capacity",
        "has_network_block",
    }
)

SQL_ALLOWED_COLUMNS: dict[str, frozenset[str]] = {
    "v_snapshots_deduped": _SNAPSHOT_BASE_COLS,
    "v_latest_device_state": _SNAPSHOT_BASE_COLS
    | frozenset(
        {
            "first_seen_at",
            "last_seen_at",
            "snapshot_count",
            "ever_had_battery",
            "form_factor",
        }
    ),
    "v_disk_volumes": frozenset(
        {
            "snapshot_id",
            "company_id",
            "device_id",
            "employee_id",
            "collected_at",
            "volume_name",
            "file_system",
            "mount_point",
            "size_bytes",
            "available_bytes",
            "free_pct",
            "encrypted",
        }
    ),
    "v_compliance_results": frozenset(
        {
            "snapshot_id",
            "company_id",
            "device_id",
            "employee_id",
            "collected_at",
            "check_id",
            "status",
            "severity",
        }
    ),
    "v_installed_software": frozenset(
        {
            "snapshot_id",
            "company_id",
            "device_id",
            "employee_id",
            "collected_at",
            "software_name",
            "version",
            "publisher",
        }
    ),
}

# Defense-in-depth sensitive column blocklist (second layer on top of SQL_ALLOWED_COLUMNS).
FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "hardware_uuid",
        "serial_number",
        "payload",
        "id",
        "secret",
        "mac",
        "address",
        "ip_address",
    }
)

# Result-side PII guard patterns (same MAC / IPv4 / IPv6 / UUID patterns as must_not_match_pii).
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![0-9a-fA-F:])(?:"
    r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"
    r"|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}"
    r"|[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}"
    r"|:(?::[0-9a-fA-F]{1,4}){1,7}"
    r"|fe80:(?::[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]+"
    r")(?![0-9a-fA-F:])",
    re.IGNORECASE,
)
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_PII_PATTERNS: tuple[re.Pattern[str], ...] = (_IPV4_RE, _IPV6_RE, _MAC_RE, _UUID_RE)

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


class SqlPiiInResultError(ValueError):
    """Raised when executed SQL rows match restricted PII patterns (MAC / IPv4 / IPv6 / UUID)."""


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


def contains_pii_value(obj: Any) -> bool:
    """Return True if any string anywhere inside `obj` matches MAC, IPv4, IPv6, or UUID."""
    if isinstance(obj, str):
        return any(pat.search(obj) is not None for pat in _PII_PATTERNS)
    if isinstance(obj, dict):
        return any(contains_pii_value(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_pii_value(item) for item in obj)
    return False


def _normalize_func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").strip().lower()
    sql_name = node.sql_name()
    if sql_name:
        return sql_name.strip().lower()
    return type(node).__name__.lower()


def _resolve_column(
    col: exp.Column,
    *,
    sources: dict[str, frozenset[str]],
    outer_scopes: list[dict[str, frozenset[str]]],
    scope_entity_names: frozenset[str],
    output_aliases: frozenset[str],
    using_cols: frozenset[str],
    allow_output_alias: bool,
) -> None:
    """Verify `col` is not a whole-row reference and resolves to an allowed column."""
    if isinstance(col.this, exp.Star):
        raise SqlValidationError("Wildcard `alias.*` is not allowed; specify explicit columns.")

    tbl_qual = (col.table or "").strip().lower()
    col_name = (col.name or "").strip().lower()
    if not col_name:
        raise SqlValidationError("Empty column reference is not permitted.")

    # Second-layer blocklist
    if col_name in FORBIDDEN_COLUMNS:
        raise SqlValidationError(f"Column {col_name!r} is restricted and cannot be queried.")

    # Explicit whole-row reference block: bare column matching any table, alias, or CTE in scope
    if not tbl_qual and col_name in scope_entity_names:
        raise SqlValidationError(
            f"Whole-row reference {col_name!r} (matches table/alias/CTE in scope) is not permitted."
        )

    if tbl_qual:
        allowed_cols: frozenset[str] | None = sources.get(tbl_qual)
        if allowed_cols is None:
            for scope in reversed(outer_scopes):
                if tbl_qual in scope:
                    allowed_cols = scope[tbl_qual]
                    break
        if allowed_cols is None:
            raise SqlValidationError(f"Unknown table or alias qualifier {tbl_qual!r}.")
        if col_name not in allowed_cols:
            raise SqlValidationError(
                f"Column '{tbl_qual}.{col_name}' is not in the allowed column list."
            )
        return

    # Unqualified column: must resolve unambiguously to an allowed column in current scope
    matches = [q for q, cols in sources.items() if col_name in cols]
    if len(matches) == 1:
        return
    if len(matches) > 1:
        if col_name in using_cols:
            return
        raise SqlValidationError(
            f"Ambiguous unqualified column {col_name!r}; qualify it with its table alias."
        )

    # Output aliases are allowed ONLY in GROUP BY / ORDER BY
    if allow_output_alias and col_name in output_aliases:
        return

    # Correlated subquery fallback: check enclosing scopes unambiguously
    for scope in reversed(outer_scopes):
        outer_matches = [q for q, cols in scope.items() if col_name in cols]
        if len(outer_matches) == 1:
            return
        if len(outer_matches) > 1:
            raise SqlValidationError(
                f"Ambiguous outer column {col_name!r}; qualify it with its table alias."
            )

    raise SqlValidationError(
        f"Column {col_name!r} is not in the allowed column list for the referenced view(s)."
    )


def _walk_expr_columns(
    node: exp.Expr | None,
    *,
    sources: dict[str, frozenset[str]],
    cte_columns: dict[str, frozenset[str]],
    outer_scopes: list[dict[str, frozenset[str]]],
    scope_entity_names: frozenset[str],
    output_aliases: frozenset[str],
    using_cols: frozenset[str],
    allow_output_alias: bool,
    real_table_counter: list[int],
) -> None:
    """Recursively validate all columns in `node`, recursing cleanly into nested SELECT scopes."""
    if node is None:
        return
    if isinstance(node, exp.Select):
        _validate_select_scope(
            node,
            cte_columns=cte_columns,
            outer_scopes=[*outer_scopes, sources],
            outer_entity_names=scope_entity_names,
            real_table_counter=real_table_counter,
        )
        return
    if isinstance(node, exp.Subquery):
        if not isinstance(node.this, exp.Select):
            raise SqlValidationError("Only SELECT subqueries are permitted.")
        _validate_select_scope(
            node.this,
            cte_columns=cte_columns,
            outer_scopes=[*outer_scopes, sources],
            outer_entity_names=scope_entity_names,
            real_table_counter=real_table_counter,
        )
        return
    if isinstance(node, exp.Column):
        _resolve_column(
            node,
            sources=sources,
            outer_scopes=outer_scopes,
            scope_entity_names=scope_entity_names,
            output_aliases=output_aliases,
            using_cols=using_cols,
            allow_output_alias=allow_output_alias,
        )
        return
    for child in node.iter_expressions():
        _walk_expr_columns(
            child,
            sources=sources,
            cte_columns=cte_columns,
            outer_scopes=outer_scopes,
            scope_entity_names=scope_entity_names,
            output_aliases=output_aliases,
            using_cols=using_cols,
            allow_output_alias=allow_output_alias,
            real_table_counter=real_table_counter,
        )


def _validate_select_scope(
    select_node: exp.Select,
    *,
    cte_columns: dict[str, frozenset[str]],
    outer_scopes: list[dict[str, frozenset[str]]],
    outer_entity_names: frozenset[str],
    real_table_counter: list[int],
) -> frozenset[str]:
    """Validate one SELECT scope (CTEs, FROM/JOIN sources, columns) and return exported columns."""
    visible_ctes = dict(cte_columns)
    with_clause = select_node.args.get("with_")
    if with_clause is not None:
        if with_clause.args.get("recursive"):
            raise SqlValidationError("Recursive CTEs are not permitted.")
        for cte in with_clause.expressions:
            if not isinstance(cte, exp.CTE) or not isinstance(cte.this, exp.Select):
                raise SqlValidationError("Only non-recursive SELECT CTEs are permitted.")
            cte_name = (cte.alias_or_name or "").strip().lower()
            if not cte_name:
                raise SqlValidationError("CTE must have an explicit name.")
            inferred = _validate_select_scope(
                cte.this,
                cte_columns=visible_ctes,
                outer_scopes=outer_scopes,
                outer_entity_names=outer_entity_names | frozenset(visible_ctes.keys()),
                real_table_counter=real_table_counter,
            )
            alias_node = cte.args.get("alias")
            explicit_cols: list[str] = []
            if isinstance(alias_node, exp.TableAlias):
                for col_ident in alias_node.args.get("columns") or []:
                    cname = str(getattr(col_ident, "name", "") or "").strip().lower()
                    if cname in FORBIDDEN_COLUMNS:
                        raise SqlValidationError(f"CTE column alias {cname!r} is restricted.")
                    if cname:
                        explicit_cols.append(cname)
            visible_ctes[cte_name] = frozenset(explicit_cols) if explicit_cols else inferred

    # Validate FROM and JOIN sources for this SELECT only
    sources: dict[str, frozenset[str]] = {}
    entity_names: set[str] = set(outer_entity_names) | set(visible_ctes.keys())
    using_cols: set[str] = set()

    def _register_source(src: Any) -> None:
        if isinstance(src, exp.Subquery):
            if not isinstance(src.this, exp.Select):
                raise SqlValidationError("Only SELECT subqueries are permitted in FROM/JOIN.")
            sub_cols = _validate_select_scope(
                src.this,
                cte_columns=visible_ctes,
                outer_scopes=outer_scopes,
                outer_entity_names=frozenset(entity_names),
                real_table_counter=real_table_counter,
            )
            sub_alias = (src.alias or "").strip().lower()
            if not sub_alias:
                raise SqlValidationError("Subqueries in FROM/JOIN must have an alias.")
            entity_names.add(sub_alias)
            sources[sub_alias] = sub_cols
            return
        if isinstance(src, exp.Table):
            schema_name = (src.db or "").strip().lower()
            table_name = (src.name or "").strip().lower()
            alias_name = (src.alias or "").strip().lower()
            if not schema_name and table_name in visible_ctes:
                entity_names.add(table_name)
                if alias_name:
                    entity_names.add(alias_name)
                sources[alias_name or table_name] = visible_ctes[table_name]
                return
            if schema_name == "telemetry" and table_name in SQL_ALLOWED_COLUMNS:
                real_table_counter[0] += 1
                entity_names.add(table_name)
                if alias_name:
                    entity_names.add(alias_name)
                sources[alias_name or table_name] = SQL_ALLOWED_COLUMNS[table_name]
                return
            raise SqlValidationError(
                f"Table {src.sql(dialect='postgres')!r} is not permitted; "
                f"only telemetry.v_* views ({', '.join(sorted(ALLOWED_TELEMETRY_VIEWS))}) "
                "are allowed."
            )
        raise SqlValidationError(f"Unsupported FROM/JOIN source: {type(src).__name__}")

    from_clause = select_node.args.get("from_")
    if from_clause is not None and from_clause.this is not None:
        _register_source(from_clause.this)
    joins = list(select_node.args.get("joins") or [])
    for join in joins:
        if join.this is not None:
            _register_source(join.this)
        for u_ident in join.args.get("using") or []:
            u_name = str(getattr(u_ident, "name", "") or "").strip().lower()
            if not u_name or u_name in FORBIDDEN_COLUMNS:
                raise SqlValidationError(f"JOIN USING column {u_name!r} is restricted.")
            if not all(u_name in cols for cols in sources.values() if u_name in cols) or not any(
                u_name in cols for cols in sources.values()
            ):
                raise SqlValidationError(f"JOIN USING column {u_name!r} is not allowed.")
            using_cols.add(u_name)

    # Collect output aliases and exported column names for this SELECT
    output_aliases: set[str] = set()
    exported_cols: set[str] = set()
    for proj in select_node.expressions:
        if isinstance(proj, exp.Alias):
            alias_name = (proj.alias or "").strip().lower()
            if alias_name in FORBIDDEN_COLUMNS:
                raise SqlValidationError(f"Output alias {alias_name!r} is restricted.")
            if alias_name:
                output_aliases.add(alias_name)
                exported_cols.add(alias_name)
        elif isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
            cname = (proj.name or "").strip().lower()
            if cname:
                exported_cols.add(cname)

    frozen_entities = frozenset(entity_names)
    frozen_aliases = frozenset(output_aliases)
    frozen_using = frozenset(using_cols)

    # Validate projection, JOIN ON, DISTINCT, WHERE, HAVING, QUALIFY (no output aliases)
    for proj in select_node.expressions:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        _walk_expr_columns(
            target,
            sources=sources,
            cte_columns=visible_ctes,
            outer_scopes=outer_scopes,
            scope_entity_names=frozen_entities,
            output_aliases=frozen_aliases,
            using_cols=frozen_using,
            allow_output_alias=False,
            real_table_counter=real_table_counter,
        )

    for join in joins:
        _walk_expr_columns(
            join.args.get("on"),
            sources=sources,
            cte_columns=visible_ctes,
            outer_scopes=outer_scopes,
            scope_entity_names=frozen_entities,
            output_aliases=frozen_aliases,
            using_cols=frozen_using,
            allow_output_alias=False,
            real_table_counter=real_table_counter,
        )

    for clause_key in ("distinct", "where", "having", "qualify", "windows"):
        clause = select_node.args.get(clause_key)
        if isinstance(clause, list):
            for item in clause:
                _walk_expr_columns(
                    item,
                    sources=sources,
                    cte_columns=visible_ctes,
                    outer_scopes=outer_scopes,
                    scope_entity_names=frozen_entities,
                    output_aliases=frozen_aliases,
                    using_cols=frozen_using,
                    allow_output_alias=False,
                    real_table_counter=real_table_counter,
                )
        elif isinstance(clause, exp.Expr):
            _walk_expr_columns(
                clause,
                sources=sources,
                cte_columns=visible_ctes,
                outer_scopes=outer_scopes,
                scope_entity_names=frozen_entities,
                output_aliases=frozen_aliases,
                using_cols=frozen_using,
                allow_output_alias=False,
                real_table_counter=real_table_counter,
            )

    # Validate GROUP BY and ORDER BY (output aliases defined in this SELECT are permitted here)
    for clause_key in ("group", "order"):
        clause = select_node.args.get(clause_key)
        if isinstance(clause, exp.Expr):
            _walk_expr_columns(
                clause,
                sources=sources,
                cte_columns=visible_ctes,
                outer_scopes=outer_scopes,
                scope_entity_names=frozen_entities,
                output_aliases=frozen_aliases,
                using_cols=frozen_using,
                allow_output_alias=True,
                real_table_counter=real_table_counter,
            )

    return frozenset(exported_cols)


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

    # 1. Forbid `SELECT *` / `alias.*` (allow `COUNT(*)` only) and any `exp.Dot` access.
    for star in stmt.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise SqlValidationError(
                "Wildcard `SELECT *` or `alias.*` is not allowed; specify explicit columns."
            )

    for _dot in stmt.find_all(exp.Dot):
        raise SqlValidationError(
            "Dotted composite access and schema-qualified function calls are not permitted."
        )

    # 2. Enforce strict function allowlist across the entire AST.
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

    # 3. Scope-aware table, positive column allowlist, whole-row reference, and blocklist check.
    real_table_counter = [0]
    _validate_select_scope(
        stmt,
        cte_columns={},
        outer_scopes=[],
        outer_entity_names=frozenset(),
        real_table_counter=real_table_counter,
    )
    if real_table_counter[0] == 0:
        raise SqlValidationError("Query must select from at least one allowed telemetry.v_* view.")

    # 4. Enforce LIMIT <= max_limit (inject LIMIT 200 if absent; clamp if > max_limit).
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

    # Result-side PII guard: drop entire result if any value matches PII patterns.
    if contains_pii_value(rows):
        raise SqlPiiInResultError(
            "Query result contained restricted PII patterns (IP/MAC/UUID) and was blocked."
        )

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
