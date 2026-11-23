"""Tenant-scoped read-only telemetry repository over PostgreSQL live views.

Privacy invariant: never SELECTs IP addresses, MAC addresses, `hardware_uuid`, or
`serial_number`. `serial_number` is referenced only in a `WHERE` predicate inside
`resolve_device()`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from psycopg import sql

from fleet_copilot.clock import telemetry_as_of
from fleet_copilot.config import Settings
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import (
    AmbiguousDevice,
    ComplianceRecord,
    DeviceState,
    SnapshotMetricPoint,
    SoftwareRecord,
)

_GIB = float(1024**3)


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    return float(val)


def _bytes_to_gb(val: Any) -> float | None:
    if val is None:
        return None
    num = int(val)
    if num <= 0:
        return None
    return round(num / _GIB, 2)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _row_to_device_state(row: dict[str, Any]) -> DeviceState:
    return DeviceState(
        snapshot_id=str(row["snapshot_id"]),
        company_id=str(row["company_id"]),
        device_id=str(row["device_id"]),
        employee_id=str(row["employee_id"]),
        collected_at=_to_utc(row["collected_at"]),
        agent_version=str(row["agent_version"]),
        os_platform=row["os_platform"],
        os_product_name=row["os_product_name"],
        os_product_version=row["os_product_version"],
        os_build_version=row["os_build_version"],
        os_architecture=row["os_architecture"],
        os_hostname=row["os_hostname"],
        model_name=row["model_name"],
        model_identifier=row["model_identifier"],
        processor=row["processor"],
        total_memory_label=row["total_memory_label"],
        ram_total_bytes=row["ram_total_bytes"],
        ram_used_bytes=row["ram_used_bytes"],
        ram_free_bytes=row["ram_free_bytes"],
        ram_total_gb=_bytes_to_gb(row["ram_total_bytes"]),
        ram_used_pct=_to_float(row["ram_used_pct"]),
        disk_free_pct=_to_float(row["disk_free_pct"]),
        disk_total_gb=_bytes_to_gb(row["disk_size_bytes"]),
        has_battery_block=bool(row["has_battery_block"]),
        battery_present=row["battery_present"],
        battery_charging_status=row["battery_charging_status"],
        battery_percentage=row["battery_percentage"],
        battery_condition=row["battery_condition"],
        battery_cycle_count=row["battery_cycle_count"],
        battery_full_charge_capacity=row["battery_full_charge_capacity"],
        first_seen_at=_to_utc(row["first_seen_at"]),
        last_seen_at=_to_utc(row["last_seen_at"]),
        snapshot_count=int(row["snapshot_count"]),
        ever_had_battery=bool(row["ever_had_battery"]),
        form_factor=row["form_factor"],
    )


_LATEST_STATE_SQL_BASE = """
WITH primary_disk AS (
    SELECT DISTINCT ON (d.snapshot_id)
        d.snapshot_id,
        d.size_bytes AS disk_size_bytes,
        d.free_pct AS disk_free_pct
    FROM telemetry.v_disk_volumes AS d
    ORDER BY d.snapshot_id, d.size_bytes DESC NULLS LAST, d.volume_name ASC
)
SELECT
    l.snapshot_id,
    l.company_id,
    l.device_id,
    l.employee_id,
    l.collected_at,
    l.agent_version,
    l.os_platform,
    l.os_product_name,
    l.os_product_version,
    l.os_build_version,
    l.os_architecture,
    l.os_hostname,
    l.model_name,
    l.model_identifier,
    l.processor,
    l.total_memory_label,
    l.ram_total_bytes,
    l.ram_used_bytes,
    l.ram_free_bytes,
    l.ram_used_pct,
    pd.disk_free_pct,
    pd.disk_size_bytes,
    l.has_battery_block,
    l.battery_present,
    l.battery_charging_status,
    l.battery_percentage,
    l.battery_condition,
    l.battery_cycle_count,
    l.battery_full_charge_capacity,
    l.first_seen_at,
    l.last_seen_at,
    l.snapshot_count,
    l.ever_had_battery,
    l.form_factor
FROM telemetry.v_latest_device_state AS l
LEFT JOIN primary_disk AS pd
  ON pd.snapshot_id = l.snapshot_id
"""


class TelemetryRepository(Protocol):
    """Read-only tenant-scoped telemetry access protocol."""

    def tenant_as_of(self, company_id: str) -> datetime: ...

    def latest_states(self, company_id: str) -> list[DeviceState]: ...

    def series(
        self,
        company_id: str,
        since: datetime,
        device_ids: Sequence[str] | None = None,
    ) -> list[SnapshotMetricPoint]: ...

    def compliance(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[ComplianceRecord]: ...

    def software(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[SoftwareRecord]: ...

    def resolve_device(self, company_id: str, identifier: str) -> DeviceState | None: ...

    def employee_devices(self, company_id: str, employee_id: str) -> list[DeviceState]: ...

    def existing_snapshot_ids(self, company_id: str, ids: Sequence[str]) -> set[str]: ...


class PgTelemetryRepository:
    """PostgreSQL implementation of `TelemetryRepository` using `telemetry_ro_cursor`."""

    def __init__(self, db: DatabaseManager, settings: Settings | None = None) -> None:
        self._db = db
        self._settings = settings

    def tenant_as_of(self, company_id: str) -> datetime:
        """Return the tenant's data clock (`FLEET_AS_OF` override or `max(collected_at)`)."""
        with self._db.telemetry_ro_cursor(company_id) as cur:
            return telemetry_as_of(cur, self._settings)

    def latest_states(self, company_id: str) -> list[DeviceState]:
        """Return sanitized latest state for every device in `company_id`, sorted by device_id."""
        with self._db.telemetry_ro_cursor(company_id) as cur:
            cur.execute(_LATEST_STATE_SQL_BASE + " ORDER BY l.device_id ASC, l.collected_at ASC")
            return [_row_to_device_state(dict(r)) for r in cur.fetchall()]

    def series(
        self,
        company_id: str,
        since: datetime,
        device_ids: Sequence[str] | None = None,
    ) -> list[SnapshotMetricPoint]:
        """Return deduplicated time-series metric points since `since` (inclusive)."""
        since_utc = _to_utc(since)
        where_clauses: list[sql.SQL] = [sql.SQL("s.collected_at >= %s")]
        params: list[Any] = [since_utc]
        if device_ids is not None:
            if not device_ids:
                return []
            where_clauses.append(sql.SQL("s.device_id = ANY(%s)"))
            params.append(list(device_ids))

        query = sql.SQL(
            """
            WITH primary_disk AS (
                SELECT DISTINCT ON (d.snapshot_id)
                    d.snapshot_id,
                    d.size_bytes AS disk_size_bytes,
                    d.free_pct AS disk_free_pct
                FROM telemetry.v_disk_volumes AS d
                ORDER BY d.snapshot_id, d.size_bytes DESC NULLS LAST, d.volume_name ASC
            )
            SELECT
                s.snapshot_id,
                s.device_id,
                s.employee_id,
                s.os_hostname,
                s.collected_at,
                s.ram_used_pct,
                pd.disk_free_pct,
                pd.disk_size_bytes,
                s.has_battery_block,
                s.battery_present,
                s.battery_condition,
                s.battery_cycle_count,
                s.battery_full_charge_capacity,
                s.battery_percentage
            FROM telemetry.v_snapshots_deduped AS s
            LEFT JOIN primary_disk AS pd
              ON pd.snapshot_id = s.snapshot_id
            WHERE {where}
            ORDER BY s.device_id ASC, s.collected_at ASC
            """
        ).format(where=sql.SQL(" AND ").join(where_clauses))

        with self._db.telemetry_ro_cursor(company_id) as cur:
            cur.execute(query, params)
            return [
                SnapshotMetricPoint(
                    snapshot_id=str(r["snapshot_id"]),
                    device_id=str(r["device_id"]),
                    employee_id=str(r["employee_id"]),
                    hostname=r["os_hostname"],
                    collected_at=_to_utc(r["collected_at"]),
                    ram_used_pct=_to_float(r["ram_used_pct"]),
                    disk_free_pct=_to_float(r["disk_free_pct"]),
                    disk_total_gb=_bytes_to_gb(r["disk_size_bytes"]),
                    has_battery_block=bool(r["has_battery_block"]),
                    battery_present=r["battery_present"],
                    battery_condition=r["battery_condition"],
                    battery_cycle_count=r["battery_cycle_count"],
                    battery_full_charge_capacity=r["battery_full_charge_capacity"],
                    battery_percentage=r["battery_percentage"],
                )
                for r in cur.fetchall()
            ]

    def compliance(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[ComplianceRecord]:
        """Return compliance check rows (latest snapshot per device or time window)."""
        where_clauses: list[sql.SQL] = [sql.SQL("c.check_id IS NOT NULL")]
        params: list[Any] = []
        if since is not None:
            where_clauses.append(sql.SQL("c.collected_at >= %s"))
            params.append(_to_utc(since))
        if device_ids is not None:
            if not device_ids:
                return []
            where_clauses.append(sql.SQL("c.device_id = ANY(%s)"))
            params.append(list(device_ids))

        join_sql = (
            sql.SQL(
                """
                FROM telemetry.v_compliance_results AS c
                JOIN telemetry.v_latest_device_state AS s
                  ON s.snapshot_id = c.snapshot_id
                """
            )
            if latest_only
            else sql.SQL(
                """
                FROM telemetry.v_compliance_results AS c
                JOIN telemetry.v_snapshots_deduped AS s
                  ON s.snapshot_id = c.snapshot_id
                """
            )
        )

        query = sql.SQL(
            """
            SELECT
                c.snapshot_id,
                c.device_id,
                c.employee_id,
                s.os_hostname,
                c.collected_at,
                c.check_id,
                c.status,
                c.severity
            {join_sql}
            WHERE {where}
            ORDER BY c.device_id ASC, c.collected_at ASC, c.check_id ASC
            """
        ).format(join_sql=join_sql, where=sql.SQL(" AND ").join(where_clauses))

        with self._db.telemetry_ro_cursor(company_id) as cur:
            cur.execute(query, params)
            return [
                ComplianceRecord(
                    snapshot_id=str(r["snapshot_id"]),
                    device_id=str(r["device_id"]),
                    employee_id=str(r["employee_id"]),
                    hostname=r["os_hostname"],
                    collected_at=_to_utc(r["collected_at"]),
                    check_id=str(r["check_id"]),
                    status=str(r["status"]),
                    severity=str(r["severity"]),
                )
                for r in cur.fetchall()
            ]

    def software(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[SoftwareRecord]:
        """Return installed software rows (latest snapshot per device or time window)."""
        where_clauses: list[sql.SQL] = [sql.SQL("sw.software_name IS NOT NULL")]
        params: list[Any] = []
        if since is not None:
            where_clauses.append(sql.SQL("sw.collected_at >= %s"))
            params.append(_to_utc(since))
        if device_ids is not None:
            if not device_ids:
                return []
            where_clauses.append(sql.SQL("sw.device_id = ANY(%s)"))
            params.append(list(device_ids))

        join_sql = (
            sql.SQL(
                """
                FROM telemetry.v_installed_software AS sw
                JOIN telemetry.v_latest_device_state AS s
                  ON s.snapshot_id = sw.snapshot_id
                """
            )
            if latest_only
            else sql.SQL(
                """
                FROM telemetry.v_installed_software AS sw
                JOIN telemetry.v_snapshots_deduped AS s
                  ON s.snapshot_id = sw.snapshot_id
                """
            )
        )

        query = sql.SQL(
            """
            SELECT
                sw.snapshot_id,
                sw.device_id,
                sw.employee_id,
                s.os_hostname,
                sw.collected_at,
                sw.software_name,
                sw.version,
                sw.publisher
            {join_sql}
            WHERE {where}
            ORDER BY sw.device_id ASC, sw.collected_at ASC, sw.software_name ASC
            """
        ).format(join_sql=join_sql, where=sql.SQL(" AND ").join(where_clauses))

        with self._db.telemetry_ro_cursor(company_id) as cur:
            cur.execute(query, params)
            return [
                SoftwareRecord(
                    snapshot_id=str(r["snapshot_id"]),
                    device_id=str(r["device_id"]),
                    employee_id=str(r["employee_id"]),
                    hostname=r["os_hostname"],
                    collected_at=_to_utc(r["collected_at"]),
                    software_name=str(r["software_name"]),
                    version=r["version"],
                    publisher=r["publisher"],
                )
                for r in cur.fetchall()
            ]

    def resolve_device(self, company_id: str, identifier: str) -> DeviceState | None:
        """Resolve a device by exact `device_id`, case-insensitive hostname, or serial.

        Raises `AmbiguousDevice` if hostname or serial matches multiple devices.
        Returns `None` if not found within `company_id`.
        """
        ident = identifier.strip()
        if not ident:
            return None

        predicates = (
            "WHERE l.device_id = %s",
            "WHERE LOWER(l.os_hostname) = LOWER(%s)",
            "WHERE l.serial_number = %s",
        )
        with self._db.telemetry_ro_cursor(company_id) as cur:
            for where_sql in predicates:
                cur.execute(
                    _LATEST_STATE_SQL_BASE + " " + where_sql + " ORDER BY l.device_id ASC",
                    (ident,),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    return _row_to_device_state(dict(rows[0]))
                if len(rows) > 1:
                    raise AmbiguousDevice(ident, [str(r["device_id"]) for r in rows])
        return None

    def employee_devices(self, company_id: str, employee_id: str) -> list[DeviceState]:
        """Return latest state for all devices owned by `employee_id` in `company_id`."""
        with self._db.telemetry_ro_cursor(company_id) as cur:
            cur.execute(
                _LATEST_STATE_SQL_BASE
                + " WHERE l.employee_id = %s ORDER BY l.device_id ASC, l.collected_at ASC",
                (employee_id,),
            )
            return [_row_to_device_state(dict(r)) for r in cur.fetchall()]

    def existing_snapshot_ids(self, company_id: str, ids: Sequence[str]) -> set[str]:
        """Return the subset of `ids` that exist in `telemetry.v_snapshots_deduped` for tenant."""
        unique_ids = list(dict.fromkeys(ids))
        if not unique_ids:
            return set()
        with self._db.telemetry_ro_cursor(company_id) as cur:
            cur.execute(
                """
                SELECT snapshot_id
                FROM telemetry.v_snapshots_deduped
                WHERE snapshot_id = ANY(%s)
                """,
                (unique_ids,),
            )
            return {str(r["snapshot_id"]) for r in cur.fetchall()}
