"""Thin CLI wrapper around `fleet_copilot.ops.scenarios` for external telemetry simulation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from fleet_copilot.ops.scenarios import (
    COMPANY_DISPLAY_NAMES,
    DEFAULT_DATA_PATH,
    REQUIRED_FIELDS,
    VALID_SCENARIOS,
    ingest_snapshot,
    make_snapshot_id,
    reset_dataset,
    run_scenario,
    seed_dataset,
    validate_snapshot_record,
)

app = typer.Typer(
    add_completion=False,
    help="External telemetry seeder and live-feed edge-case simulator.",
)

__all__ = [
    "COMPANY_DISPLAY_NAMES",
    "DEFAULT_DATA_PATH",
    "REQUIRED_FIELDS",
    "VALID_SCENARIOS",
    "app",
    "ingest_snapshot",
    "make_snapshot_id",
    "reset_dataset",
    "run_scenario",
    "seed_dataset",
    "validate_snapshot_record",
]


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
            help=(
                "Scenario: partial-payload | duplicate-and-late | toctou-resolved | "
                "high-sev-alert | prompt-injection-software"
            )
        ),
    ],
    device_id: Annotated[str, typer.Option(help="Target device_id.")] = "1LYSSFD074BB",
    company_id: Annotated[str, typer.Option(help="Target company_id.")] = "acme-001",
) -> None:
    """Inject a reproducible live-feed edge-case scenario."""
    if name not in VALID_SCENARIOS:
        raise typer.BadParameter(
            f"Unknown scenario {name!r}. Choose from: {', '.join(VALID_SCENARIOS)}"
        )
    typer.echo(run_scenario(name, device_id=device_id, company_id=company_id))


if __name__ == "__main__":
    app()
