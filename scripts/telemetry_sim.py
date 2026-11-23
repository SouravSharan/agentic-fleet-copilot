"""Standalone external telemetry ingestion & live-feed simulator.

Intentionally lives outside `src/fleet_copilot/` and connects exclusively with
`TELEMETRY_WRITER_DATABASE_URL` so the Copilot service itself never holds write
credentials to the `telemetry` schema.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
from psycopg.types.json import Jsonb

from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import validate_company_id

app = typer.Typer(
    add_completion=False,
    help="External telemetry seeder and live-feed edge-case simulator.",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_PATH = REPO_ROOT / "data" / "device-telemetry-dataset.ndjson"

REQUIRED_FIELDS = (
    "company_id",
    "device_id",
    "employee_id",
    "collected_at",
    "agent_version",
)

VALID_SCENARIOS = (
    "partial-payload",
    "duplicate-and-late",
    "toctou-resolved",
    "high-sev-alert",
)

COMPANY_DISPLAY_NAMES = {
    "acme-001": "Acme",
    "globex-002": "Globex",
    "initech-003": "Initech",
}


def validate_snapshot_record(record: Any) -> None:
    """Ensure `record` is a JSON object with valid, non-empty strings for all 5 core fields."""
    if not isinstance(record, dict):
        raise ValueError(f"Snapshot must be a JSON object, got {type(record).__name__}")

    missing_or_invalid: list[str] = []
    for field in REQUIRED_FIELDS:
        val = record.get(field)
        if not isinstance(val, str) or not val.strip():
            missing_or_invalid.append(field)
    if missing_or_invalid:
        raise ValueError(
            f"Snapshot missing/invalid required core fields: {', '.join(missing_or_invalid)}"
        )

    validate_company_id(record["company_id"])


def make_snapshot_id(record: dict[str, Any], *, salt: str = "") -> str:
    """Create a deterministic, human-citable snapshot_id anchor."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{canonical}|{salt}".encode()).hexdigest()[:16]
    return f"snap_{digest}"


def _display_name_for(company_id: str) -> str:
    return COMPANY_DISPLAY_NAMES.get(company_id, company_id.split("-")[0].title())


def _load_ndjson(data_path: Path) -> list[dict[str, Any]]:
    if not data_path.exists():
        raise FileNotFoundError(
            f"Dataset not found at {data_path}. Download it per README instructions."
        )
    records = [
        json.loads(line)
        for line in data_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for record in records:
        validate_snapshot_record(record)
    return records


def _insert_records_in_cursor(
    cur: psycopg.Cursor[Any],
    records: list[dict[str, Any]],
) -> int:
    companies = sorted({r["company_id"] for r in records})
    for cid in companies:
        cur.execute(
            """
            INSERT INTO telemetry.companies (company_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (company_id) DO UPDATE
                SET display_name = EXCLUDED.display_name
            """,
            (cid, _display_name_for(cid)),
        )

    inserted = 0
    for record in records:
        snap_id = make_snapshot_id(record)
        cur.execute(
            """
            INSERT INTO telemetry.telemetry_snapshots (
                snapshot_id,
                company_id,
                device_id,
                employee_id,
                collected_at,
                agent_version,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            (
                snap_id,
                record["company_id"],
                record["device_id"],
                record["employee_id"],
                record["collected_at"],
                record["agent_version"],
                Jsonb(record),
            ),
        )
        inserted += cur.rowcount
    return inserted


def ingest_snapshot(
    record: dict[str, Any],
    *,
    writer_url: str | None = None,
    snapshot_id: str | None = None,
    salt: str = "",
) -> str:
    """Insert a single raw telemetry JSON snapshot into `telemetry.telemetry_snapshots`."""
    validate_snapshot_record(record)
    dsn = writer_url or get_settings().telemetry_writer_database_url
    snap_id = snapshot_id or make_snapshot_id(record, salt=salt)
    company_id = record["company_id"]

    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO telemetry.companies (company_id, display_name)
                    VALUES (%s, %s)
                    ON CONFLICT (company_id) DO NOTHING
                    """,
                    (company_id, _display_name_for(company_id)),
                )
                cur.execute(
                    """
                    INSERT INTO telemetry.telemetry_snapshots (
                        snapshot_id,
                        company_id,
                        device_id,
                        employee_id,
                        collected_at,
                        agent_version,
                        payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (snapshot_id) DO NOTHING
                    """,
                    (
                        snap_id,
                        company_id,
                        record["device_id"],
                        record["employee_id"],
                        record["collected_at"],
                        record["agent_version"],
                        Jsonb(record),
                    ),
                )
    return snap_id


def seed_dataset(
    data_path: Path = DEFAULT_DATA_PATH,
    *,
    writer_url: str | None = None,
) -> tuple[int, int]:
    """Idempotently load all NDJSON snapshots from `data_path` into `telemetry`."""
    records = _load_ndjson(data_path)
    dsn = writer_url or get_settings().telemetry_writer_database_url
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                inserted = _insert_records_in_cursor(cur, records)
    return inserted, len(records)


def reset_dataset(
    data_path: Path = DEFAULT_DATA_PATH,
    *,
    writer_url: str | None = None,
) -> tuple[int, int]:
    """Atomically validate NDJSON, clear raw snapshots/companies, and re-seed baseline."""
    records = _load_ndjson(data_path)
    dsn = writer_url or get_settings().telemetry_writer_database_url
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE telemetry.telemetry_snapshots, telemetry.companies")
                inserted = _insert_records_in_cursor(cur, records)
    return inserted, len(records)


def _fetch_latest_payload(
    dsn: str,
    company_id: str,
    device_id: str,
    *,
    require_compliance: bool = False,
) -> dict[str, Any]:
    where_extra = (
        "AND jsonb_typeof(payload->'compliance_results') = 'array'" if require_compliance else ""
    )
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT payload
                FROM telemetry.telemetry_snapshots
                WHERE company_id = %s AND device_id = %s {where_extra}
                ORDER BY collected_at DESC, ingested_at DESC, id DESC
                LIMIT 1
                """,
                (company_id, device_id),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"No matching snapshot found for {company_id}/{device_id}. Run `seed` first."
        )
    return dict(row[0])


def _upsert_compliance_check(
    payload: dict[str, Any],
    *,
    check_id: str,
    status: str,
    severity: str,
) -> None:
    checks = payload.get("compliance_results")
    if not isinstance(checks, list):
        checks = []
        payload["compliance_results"] = checks
    for chk in checks:
        if isinstance(chk, dict) and chk.get("check_id") == check_id:
            chk["status"] = status
            chk["severity"] = severity
            return
    checks.append({"check_id": check_id, "status": status, "severity": severity})


@app.command()
def seed(
    data_path: Annotated[
        Path, typer.Option(help="Path to NDJSON telemetry dataset.")
    ] = DEFAULT_DATA_PATH,
) -> None:
    """Idempotently seed all telemetry snapshots from NDJSON."""
    inserted, total = seed_dataset(data_path=data_path)
    typer.echo(f"Seeded telemetry: inserted={inserted}, skipped={total - inserted}, total={total}")


@app.command()
def reset(
    data_path: Annotated[
        Path, typer.Option(help="Path to NDJSON telemetry dataset.")
    ] = DEFAULT_DATA_PATH,
) -> None:
    """Atomically truncate telemetry snapshots/companies and re-seed the baseline dataset."""
    inserted, total = reset_dataset(data_path=data_path)
    typer.echo(f"Reset telemetry baseline: inserted={inserted}/{total}")


@app.command()
def push(
    json_file: Annotated[Path, typer.Argument(help="Path to a single snapshot JSON file.")],
) -> None:
    """Ingest a single raw JSON snapshot."""
    record = json.loads(json_file.read_text(encoding="utf-8"))
    snap_id = ingest_snapshot(record)
    typer.echo(f"Ingested snapshot_id={snap_id}")


@app.command()
def scenario(
    name: Annotated[
        str,
        typer.Argument(
            help="Scenario: partial-payload | duplicate-and-late | toctou-resolved | high-sev-alert"
        ),
    ],
    device_id: Annotated[str, typer.Option(help="Target Acme device_id.")] = "1LYSSFD074BB",
) -> None:
    """Inject a reproducible live-feed edge-case scenario."""
    if name not in VALID_SCENARIOS:
        raise typer.BadParameter(
            f"Unknown scenario {name!r}. Choose from: {', '.join(VALID_SCENARIOS)}"
        )

    dsn = get_settings().telemetry_writer_database_url
    company_id = "acme-001"

    if name == "partial-payload":
        latest = _fetch_latest_payload(dsn, company_id, device_id)
        minimal = {
            "company_id": company_id,
            "device_id": device_id,
            "employee_id": latest["employee_id"],
            "collected_at": "2026-06-13T10:00:00Z",
            "agent_version": "43.0.0-minimal",
        }
        snap_id = ingest_snapshot(minimal, writer_url=dsn)
        typer.echo(f"[partial-payload] Ingested minimal 5-field snapshot_id={snap_id}")

    elif name == "duplicate-and-late":
        latest = _fetch_latest_payload(dsn, company_id, device_id)
        retry = dict(latest)
        retry["agent_version"] = f"{latest['agent_version']}-retry"
        retry_id = ingest_snapshot(retry, writer_url=dsn, salt="retry")

        late = dict(latest)
        late["collected_at"] = "2026-05-01T00:00:00Z"
        late["agent_version"] = "41.0.0-late"
        late_id = ingest_snapshot(late, writer_url=dsn)
        typer.echo(f"[duplicate-and-late] Ingested retry={retry_id} and backdated={late_id}")

    elif name == "toctou-resolved":
        latest = _fetch_latest_payload(dsn, company_id, device_id, require_compliance=True)
        resolved = json.loads(json.dumps(latest))
        resolved["collected_at"] = "2026-06-13T12:00:00Z"
        _upsert_compliance_check(resolved, check_id="screen_lock", status="pass", severity="medium")
        snap_id = ingest_snapshot(resolved, writer_url=dsn)
        typer.echo(f"[toctou-resolved] Ingested screen_lock=pass snapshot_id={snap_id}")

    elif name == "high-sev-alert":
        latest = _fetch_latest_payload(dsn, company_id, device_id, require_compliance=True)
        alert = json.loads(json.dumps(latest))
        alert["collected_at"] = "2026-06-13T12:05:00Z"
        _upsert_compliance_check(alert, check_id="disk_encryption", status="fail", severity="high")
        snap_id = ingest_snapshot(alert, writer_url=dsn)
        typer.echo(f"[high-sev-alert] Ingested disk_encryption=fail(high) snapshot_id={snap_id}")


if __name__ == "__main__":
    app()
