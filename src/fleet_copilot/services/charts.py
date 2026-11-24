"""Deterministic Vega-Lite v5 chart service, spec validator, and personal saved charts."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fleet_copilot.domain.models import (
    ComplianceRecord,
    DeviceFilters,
    DeviceState,
    Insight,
    SnapshotMetricPoint,
    TenantContext,
)
from fleet_copilot.domain.os_version import parse_os
from fleet_copilot.domain.stats import percentile
from fleet_copilot.repositories.telemetry import TelemetryRepository
from fleet_copilot.services.audit import (
    ChartCreatedPayload,
    ChartDeletedPayload,
    ChartPinnedPayload,
    ChartRefreshedPayload,
)
from fleet_copilot.services.fleet import find_devices as svc_find_devices
from fleet_copilot.services.policy import PolicyService

VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

# Unified status / series palette (10 §9)
CHART_PALETTE: tuple[str, ...] = (
    "#2563EB",  # accent blue
    "#D97706",  # amber
    "#DC2626",  # red
    "#059669",  # green
    "#7C3AED",  # violet
    "#0891B2",  # cyan
    "#4B5563",  # slate
)

_FORBIDDEN_SPEC_KEYS: frozenset[str] = frozenset(
    {
        "url",
        "signals",
        "expr",
        "calculate",
        "lookup",
        "from",
        "params",
    }
)

_VALID_TYPES_BY_KIND: dict[str, frozenset[str]] = {
    "metric_over_time": frozenset({"line", "bar", "scatter", "table"}),
    "aggregate_by_group": frozenset({"bar", "stacked_bar", "table"}),
    "from_query": frozenset({"line", "bar", "stacked_bar", "scatter", "table"}),
}

ChartKind = Literal["metric_over_time", "aggregate_by_group", "from_query"]
ChartMetric = Literal[
    "disk_free_pct",
    "ram_used_pct",
    "battery_cycles",
    "battery_capacity_mah",
    "compliance_pass_rate",
]
ChartStat = Literal["avg", "min", "max", "p95", "count"]
ChartGroupBy = Literal[
    "model",
    "os_version",
    "os_family",
    "form_factor",
    "employee_id",
    "check_id",
    "severity",
]
ChartType = Literal["line", "bar", "stacked_bar", "scatter", "table"]


class UnsafeChartSpecError(ValueError):
    """Raised when a Vega-Lite spec contains forbidden remote URLs, signals, or expressions."""


def _walk_validate(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            if key in _FORBIDDEN_SPEC_KEYS:
                raise UnsafeChartSpecError(f"Forbidden Vega-Lite key {key!r} at {path or 'root'}")
            if key == "data" and isinstance(v, dict):
                if "url" in v or ("name" in v and "values" not in v):
                    raise UnsafeChartSpecError("Chart data must use inline `values` only")
            _walk_validate(v, f"{path}.{key}" if path else key)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _walk_validate(item, f"{path}[{idx}]")


def validate_vega_lite_spec(spec: dict[str, Any]) -> None:
    """Validate that a Vega-Lite spec uses our exact v5 schema, inline values, and safe keys."""
    if not isinstance(spec, dict):
        raise UnsafeChartSpecError("Spec must be a JSON object")
    if spec.get("$schema") != VEGA_LITE_V5_SCHEMA:
        raise UnsafeChartSpecError(f"Unsupported $schema: {spec.get('$schema')!r}")
    data = spec.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("values"), list):
        raise UnsafeChartSpecError("Spec must include inline `data.values` list")
    _walk_validate(spec)


class ChartRequest(BaseModel):
    """Validated flat chart specification requested by the LLM or saved-chart refresh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    kind: ChartKind
    metric: ChartMetric | None = None
    device_ids: list[str] | None = None
    form_factor: Literal["laptop", "desktop", "unknown"] | None = None
    os_family: Literal["macos", "windows"] | None = None
    os_version_lt: str | None = None
    model: str | None = None
    employee_id: str | None = None
    low_disk_only: bool = False
    max_disk_free_pct: float | None = None
    min_ram_used_pct: float | None = None
    battery_condition: str | None = None
    software_name: str | None = None
    stat: ChartStat | None = None
    group_by: ChartGroupBy | None = None
    window_days: int = Field(default=30, ge=1, le=30)
    query_handle: str | None = None
    x: str | None = None
    y: str | None = None
    series: str | None = None
    chart_type: ChartType = "line"

    @model_validator(mode="after")
    def _validate_kind_contract(self) -> ChartRequest:
        allowed_types = _VALID_TYPES_BY_KIND[self.kind]
        if self.chart_type not in allowed_types:
            raise ValueError(
                f"chart_type {self.chart_type!r} is not allowed for kind {self.kind!r} "
                f"(allowed: {sorted(allowed_types)})"
            )
        if self.kind == "metric_over_time":
            if self.metric is None:
                raise ValueError("metric is required for kind='metric_over_time'")
        elif self.kind == "aggregate_by_group":
            if self.metric is None:
                raise ValueError("metric is required for kind='aggregate_by_group'")
            if self.group_by is None:
                raise ValueError("group_by is required for kind='aggregate_by_group'")
        elif self.kind == "from_query":
            if not self.query_handle or not self.query_handle.startswith("Q"):
                raise ValueError("from_query requires a `Q<n>` query_handle from run_readonly_sql")
            if not self.x or not self.y:
                raise ValueError("from_query requires both `x` and `y` column names")
        return self


class ChartArtifact(BaseModel):
    """Computed, validated Vega-Lite chart artifact anchored to snapshot evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chart_id: str
    handle: str | None = None
    title: str
    request_json: dict[str, Any]
    vega_lite_spec: dict[str, Any]
    data_rows: list[dict[str, Any]]
    evidence_snapshot_ids: list[str]
    telemetry_as_of: datetime
    row_count: int


def _metric_label(metric: str) -> str:
    return {
        "disk_free_pct": "Disk Free (%)",
        "ram_used_pct": "RAM Used (%)",
        "battery_cycles": "Battery Cycle Count",
        "battery_capacity_mah": "Battery Full-Charge Capacity (mAh)",
        "compliance_pass_rate": "Compliance Pass Rate (%)",
    }.get(metric, metric)


def _compute_stat(values: list[float], stat: ChartStat) -> float:
    if not values:
        return 0.0
    if stat == "count":
        return float(len(values))
    if stat == "min":
        return round(min(values), 2)
    if stat == "max":
        return round(max(values), 2)
    if stat == "p95":
        p = percentile(values, 95)
        return round(p if p is not None else max(values), 2)
    return round(sum(values) / len(values), 2)


def _build_vega_lite_spec(
    *,
    title: str,
    chart_type: ChartType,
    data_rows: list[dict[str, Any]],
    x_field: str,
    x_type: Literal["temporal", "nominal", "ordinal", "quantitative"],
    x_title: str,
    y_field: str,
    y_title: str,
    series_field: str | None = None,
) -> dict[str, Any]:
    mark: Any
    if chart_type == "line":
        mark = {"type": "line", "point": True}
    elif chart_type == "scatter":
        mark = {"type": "point", "filled": True}
    else:
        mark = {"type": "bar"}

    encoding: dict[str, Any] = {
        "x": {"field": x_field, "type": x_type, "title": x_title},
        "y": {"field": y_field, "type": "quantitative", "title": y_title},
    }
    if series_field:
        encoding["color"] = {
            "field": series_field,
            "type": "nominal",
            "scale": {"range": list(CHART_PALETTE)},
        }
        if chart_type == "stacked_bar":
            encoding["y"]["stack"] = "zero"
    else:
        encoding["color"] = {"value": CHART_PALETTE[0]}

    spec: dict[str, Any] = {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": title[:80],
        "width": "container",
        "height": 260,
        "data": {"values": data_rows},
        "mark": mark,
        "encoding": encoding,
    }
    validate_vega_lite_spec(spec)
    return spec


def _group_key_for_device(dev: DeviceState, group_by: ChartGroupBy) -> str:
    if group_by == "model":
        return dev.model_name or "unknown"
    if group_by == "os_version":
        return f"{dev.os_product_name or 'unknown'} {dev.os_product_version or ''}".strip()
    if group_by == "os_family":
        parsed = parse_os(dev.os_product_name, dev.os_product_version)
        return parsed.family if parsed else (dev.os_platform or "unknown")
    if group_by == "form_factor":
        return dev.form_factor
    if group_by == "employee_id":
        return dev.employee_id
    return "unknown"


def _device_metric_value(dev: DeviceState, metric: ChartMetric) -> float | None:
    if metric == "disk_free_pct":
        return dev.disk_free_pct
    if metric == "ram_used_pct":
        return dev.ram_used_pct
    if metric == "battery_cycles":
        if dev.battery_present and dev.battery_cycle_count is not None:
            return float(dev.battery_cycle_count)
        return None
    if metric == "battery_capacity_mah":
        if dev.battery_present and dev.battery_full_charge_capacity is not None:
            return float(dev.battery_full_charge_capacity)
        return None
    return None


def _point_metric_value(pt: SnapshotMetricPoint, metric: ChartMetric) -> float | None:
    if metric == "disk_free_pct":
        return pt.disk_free_pct
    if metric == "ram_used_pct":
        return pt.ram_used_pct
    if metric == "battery_cycles":
        if pt.battery_present and pt.battery_cycle_count is not None:
            return float(pt.battery_cycle_count)
        return None
    if metric == "battery_capacity_mah":
        if pt.battery_present and pt.battery_full_charge_capacity is not None:
            return float(pt.battery_full_charge_capacity)
        return None
    return None


def build_insight_chart(
    insight: Insight,
    *,
    device_series: Sequence[SnapshotMetricPoint],
    device_compliance: Sequence[ComplianceRecord],
    as_of: datetime,
) -> dict[str, Any] | None:
    """Build a compact validated Vega-Lite chart dictionary directly from analyzer series."""
    cat = insight.category
    rows: list[dict[str, Any]] = []
    snap_ids: set[str] = set()
    y_title = "Value"
    title = f"{insight.device_id} · {cat.replace('_', ' ').title()}"

    if cat == "disk":
        y_title = "Disk Free (%)"
        for pt in sorted(device_series, key=lambda p: p.collected_at):
            if pt.disk_free_pct is not None:
                rows.append(
                    {
                        "date": pt.collected_at.date().isoformat(),
                        "value": round(float(pt.disk_free_pct), 2),
                        "device_id": pt.device_id,
                        "snapshot_id": pt.snapshot_id,
                    }
                )
                snap_ids.add(pt.snapshot_id)
    elif cat == "ram":
        y_title = "RAM Used (%)"
        for pt in sorted(device_series, key=lambda p: p.collected_at):
            if pt.ram_used_pct is not None:
                rows.append(
                    {
                        "date": pt.collected_at.date().isoformat(),
                        "value": round(float(pt.ram_used_pct), 2),
                        "device_id": pt.device_id,
                        "snapshot_id": pt.snapshot_id,
                    }
                )
                snap_ids.add(pt.snapshot_id)
    elif cat == "battery":
        y_title = "Battery Cycle Count"
        for pt in sorted(device_series, key=lambda p: p.collected_at):
            if pt.battery_present and pt.battery_cycle_count is not None:
                rows.append(
                    {
                        "date": pt.collected_at.date().isoformat(),
                        "value": float(pt.battery_cycle_count),
                        "device_id": pt.device_id,
                        "snapshot_id": pt.snapshot_id,
                    }
                )
                snap_ids.add(pt.snapshot_id)
    elif cat == "compliance_drift":
        y_title = "Pass (1) / Fail (0)"
        check_id = str(insight.stats.get("check_id") or "")
        for rec in sorted(device_compliance, key=lambda r: r.collected_at):
            if check_id and rec.check_id != check_id:
                continue
            rows.append(
                {
                    "date": rec.collected_at.date().isoformat(),
                    "value": 1.0 if rec.status == "pass" else 0.0,
                    "device_id": rec.device_id,
                    "snapshot_id": rec.snapshot_id,
                }
            )
            snap_ids.add(rec.snapshot_id)
    else:
        # Software / generic insight: plot evidence values or observation count by date
        y_title = "Observed"
        for idx, ev in enumerate(insight.evidence, start=1):
            rows.append(
                {
                    "date": ev.collected_at.date().isoformat(),
                    "value": float(ev.value) if isinstance(ev.value, (int, float)) else float(idx),
                    "device_id": ev.device_id,
                    "snapshot_id": ev.snapshot_id,
                    "label": ev.label[:60],
                }
            )
            snap_ids.add(ev.snapshot_id)

    if not rows:
        return None

    spec = _build_vega_lite_spec(
        title=title[:80],
        chart_type="bar" if cat == "software" else "line",
        data_rows=rows,
        x_field="date",
        x_type="temporal",
        x_title="Date (UTC)",
        y_field="value",
        y_title=y_title,
    )
    return {
        "title": title[:80],
        "vega_lite_spec": spec,
        "data_rows": rows,
        "evidence_snapshot_ids": sorted(snap_ids),
        "telemetry_as_of": as_of.isoformat(),
    }


class ChartService:
    """Computes validated charts from live telemetry or SQL results and manages saved charts."""

    def __init__(
        self,
        telemetry_repo: TelemetryRepository,
        policy_svc: PolicyService,
        *,
        store: Any | None = None,
        audit_svc: Any | None = None,
    ) -> None:
        self._repo = telemetry_repo
        self._policy_svc = policy_svc
        self._store = store
        self._audit = audit_svc

    def _resolve_filtered_devices(
        self,
        ctx: TenantContext,
        req: ChartRequest,
    ) -> list[DeviceState]:
        has_filters = any(
            [
                req.form_factor is not None,
                req.os_family is not None,
                req.os_version_lt is not None,
                req.model is not None,
                req.employee_id is not None,
                req.low_disk_only,
                req.max_disk_free_pct is not None,
                req.min_ram_used_pct is not None,
                req.battery_condition is not None,
                req.software_name is not None,
            ]
        )
        all_devices = self._repo.latest_states(ctx.company_id)
        if req.device_ids:
            wanted = set(req.device_ids)
            return [d for d in all_devices if d.device_id in wanted]
        if has_filters:
            filters = DeviceFilters(
                form_factor=req.form_factor,
                os_family=req.os_family,
                os_version_lt=req.os_version_lt,
                model=req.model,
                employee_id=req.employee_id,
                low_disk_only=req.low_disk_only,
                max_disk_free_pct=req.max_disk_free_pct,
                min_ram_used_pct=req.min_ram_used_pct,
                battery_condition=req.battery_condition,
                software_name=req.software_name,
                limit=50,
            )
            res = svc_find_devices(ctx, filters, self._repo, self._policy_svc)
            matched_ids = {str(r["device_id"]) for r in res.data}
            return [d for d in all_devices if d.device_id in matched_ids]
        return all_devices

    def _compute(
        self,
        ctx: TenantContext,
        req: ChartRequest,
        *,
        query_lookup: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], datetime]:
        as_of = self._repo.tenant_as_of(ctx.company_id)
        since = as_of - timedelta(days=req.window_days)

        if req.kind == "from_query":
            assert req.query_handle and req.x and req.y
            q_entry = (query_lookup or {}).get(req.query_handle)
            if q_entry is None:
                raise ValueError(f"Unknown query handle {req.query_handle!r} in current turn")
            raw_rows = list(q_entry.get("rows") or [])
            data_rows: list[dict[str, Any]] = []
            for row in raw_rows[:200]:
                if req.x not in row or req.y not in row:
                    continue
                item: dict[str, Any] = {req.x: row[req.x], req.y: row[req.y]}
                if req.series and req.series in row:
                    item[req.series] = row[req.series]
                if "snapshot_id" in row and row["snapshot_id"]:
                    item["snapshot_id"] = row["snapshot_id"]
                if "device_id" in row and row["device_id"]:
                    item["device_id"] = row["device_id"]
                data_rows.append(item)
            snap_ids = sorted(
                {str(s) for s in (q_entry.get("snapshot_ids") or []) if s}
                | {str(r["snapshot_id"]) for r in data_rows if r.get("snapshot_id")}
            )
            x_type: Literal["temporal", "nominal", "ordinal", "quantitative"] = (
                "temporal" if "date" in req.x.lower() or "day" in req.x.lower() else "nominal"
            )
            spec = _build_vega_lite_spec(
                title=req.title,
                chart_type=req.chart_type,
                data_rows=data_rows,
                x_field=req.x,
                x_type=x_type,
                x_title=req.x,
                y_field=req.y,
                y_title=req.y,
                series_field=req.series,
            )
            return spec, data_rows, snap_ids, as_of

        if req.kind == "metric_over_time":
            assert req.metric is not None
            devices = self._resolve_filtered_devices(ctx, req)
            dev_ids = [d.device_id for d in devices]
            rows: list[dict[str, Any]] = []
            snaps: set[str] = set()
            if req.metric == "compliance_pass_rate":
                recs = self._repo.compliance(
                    ctx.company_id, since=since, latest_only=False, device_ids=dev_ids or None
                )
                by_day: dict[str, list[ComplianceRecord]] = defaultdict(list)
                for r in recs:
                    by_day[r.collected_at.date().isoformat()].append(r)
                    snaps.add(r.snapshot_id)
                for day in sorted(by_day.keys()):
                    day_recs = by_day[day]
                    pass_n = sum(1 for r in day_recs if r.status == "pass")
                    pct = round((pass_n / len(day_recs)) * 100.0, 2) if day_recs else 0.0
                    rows.append({"date": day, "value": pct, "series": "fleet_pass_rate"})
            else:
                pts = self._repo.series(ctx.company_id, since=since, device_ids=dev_ids or None)
                for pt in sorted(pts, key=lambda p: (p.device_id, p.collected_at)):
                    val = _point_metric_value(pt, req.metric)
                    if val is None:
                        continue
                    rows.append(
                        {
                            "date": pt.collected_at.date().isoformat(),
                            "value": round(float(val), 2),
                            "device_id": pt.device_id,
                            "series": pt.hostname or pt.device_id,
                            "snapshot_id": pt.snapshot_id,
                        }
                    )
                    snaps.add(pt.snapshot_id)

            spec = _build_vega_lite_spec(
                title=req.title,
                chart_type=req.chart_type,
                data_rows=rows,
                x_field="date",
                x_type="temporal",
                x_title="Date (UTC)",
                y_field="value",
                y_title=_metric_label(req.metric),
                series_field="series" if rows else None,
            )
            return spec, rows, sorted(snaps), as_of

        # aggregate_by_group
        assert req.metric is not None and req.group_by is not None
        stat: ChartStat = req.stat or "avg"
        rows = []
        snaps = set()
        if req.metric == "compliance_pass_rate" or req.group_by in ("check_id", "severity"):
            recs = self._repo.compliance(ctx.company_id, latest_only=True)
            grouped_comp: dict[str, list[ComplianceRecord]] = defaultdict(list)
            for rec in recs:
                gkey = rec.check_id if req.group_by == "check_id" else rec.severity
                grouped_comp[gkey].append(rec)
                snaps.add(rec.snapshot_id)
            for grp in sorted(grouped_comp.keys()):
                g_recs = grouped_comp[grp]
                if stat == "count":
                    val = float(len(g_recs))
                else:
                    pass_n = sum(1 for r in g_recs if r.status == "pass")
                    val = round((pass_n / len(g_recs)) * 100.0, 2) if g_recs else 0.0
                rows.append({"group": grp, "value": val, "count": len(g_recs)})
        else:
            devices = self._resolve_filtered_devices(ctx, req)
            grouped_vals: dict[str, list[float]] = defaultdict(list)
            for dev in devices:
                val = _device_metric_value(dev, req.metric)
                if val is None:
                    continue
                grouped_vals[_group_key_for_device(dev, req.group_by)].append(val)
                snaps.add(dev.snapshot_id)
            for grp in sorted(grouped_vals.keys()):
                vals = grouped_vals[grp]
                rows.append(
                    {
                        "group": grp,
                        "value": _compute_stat(vals, stat),
                        "count": len(vals),
                    }
                )

        spec = _build_vega_lite_spec(
            title=req.title,
            chart_type=req.chart_type,
            data_rows=rows,
            x_field="group",
            x_type="nominal",
            x_title=req.group_by.replace("_", " ").title(),
            y_field="value",
            y_title=f"{stat.upper()} · {_metric_label(req.metric)}",
        )
        return spec, rows, sorted(snaps), as_of

    def create(
        self,
        ctx: TenantContext,
        req: ChartRequest,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        query_lookup: dict[str, dict[str, Any]] | None = None,
        persist: bool = True,
    ) -> ChartArtifact:
        """Compute, validate, optionally persist to `copilot.charts`, and audit a chart."""
        spec, data_rows, snap_ids, as_of = self._compute(ctx, req, query_lookup=query_lookup)
        chart_id = f"chart_{uuid.uuid4().hex[:12]}"
        req_dump = req.model_dump(mode="json")

        if persist and self._store is not None and hasattr(self._store, "insert_chart"):
            self._store.insert_chart(
                ctx.company_id,
                chart_id=chart_id,
                owner_actor_id=ctx.actor_id,
                thread_id=thread_id,
                turn_id=turn_id,
                title=req.title,
                request_json=req_dump,
                spec_json=spec,
                data_json=data_rows,
                evidence_snapshot_ids=snap_ids,
                telemetry_as_of=as_of,
                pinned=False,
            )

        if self._audit is not None:
            self._audit.record_standalone(
                ctx,
                "chart_created",
                ChartCreatedPayload(chart_id=chart_id, kind=req.kind, row_count=len(data_rows)),
                thread_id=thread_id,
            )

        return ChartArtifact(
            chart_id=chart_id,
            title=req.title,
            request_json=req_dump,
            vega_lite_spec=spec,
            data_rows=data_rows,
            evidence_snapshot_ids=snap_ids,
            telemetry_as_of=as_of,
            row_count=len(data_rows),
        )

    def list_charts(
        self,
        ctx: TenantContext,
        *,
        pinned_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if self._store is None or not hasattr(self._store, "list_charts"):
            return []
        return list(
            self._store.list_charts(
                ctx.company_id,
                owner_actor_id=ctx.actor_id,
                pinned_only=pinned_only,
                limit=limit,
            )
        )

    def pin_chart(
        self,
        ctx: TenantContext,
        chart_id: str,
        *,
        pinned: bool = True,
    ) -> dict[str, Any] | None:
        if self._store is None or not hasattr(self._store, "pin_chart"):
            return None
        row = self._store.pin_chart(
            ctx.company_id, chart_id, owner_actor_id=ctx.actor_id, pinned=pinned
        )
        if row is not None and self._audit is not None:
            self._audit.record_standalone(
                ctx,
                "chart_pinned",
                ChartPinnedPayload(chart_id=chart_id, pinned=pinned),
                thread_id=row.get("thread_id"),
            )
        return row

    def refresh_chart(self, ctx: TenantContext, chart_id: str) -> ChartArtifact | None:
        """Re-run a saved personal chart's `request_json` on latest telemetry in place."""
        if self._store is None or not hasattr(self._store, "get_chart"):
            return None
        existing = self._store.get_chart(ctx.company_id, chart_id, owner_actor_id=ctx.actor_id)
        if existing is None:
            return None
        req = ChartRequest.model_validate(existing["request_json"])
        if req.kind == "from_query":
            raise ValueError("Charts built from ad-hoc query handles cannot be refreshed directly")
        spec, data_rows, snap_ids, as_of = self._compute(ctx, req)
        self._store.update_chart_in_place(
            ctx.company_id,
            chart_id,
            owner_actor_id=ctx.actor_id,
            spec_json=spec,
            data_json=data_rows,
            evidence_snapshot_ids=snap_ids,
            telemetry_as_of=as_of,
        )
        if self._audit is not None:
            self._audit.record_standalone(
                ctx,
                "chart_refreshed",
                ChartRefreshedPayload(chart_id=chart_id, row_count=len(data_rows)),
                thread_id=existing.get("thread_id"),
            )
        return ChartArtifact(
            chart_id=chart_id,
            title=str(existing["title"]),
            request_json=req.model_dump(mode="json"),
            vega_lite_spec=spec,
            data_rows=data_rows,
            evidence_snapshot_ids=snap_ids,
            telemetry_as_of=as_of,
            row_count=len(data_rows),
        )

    def delete_chart(self, ctx: TenantContext, chart_id: str) -> bool:
        if self._store is None or not hasattr(self._store, "delete_chart"):
            return False
        deleted = bool(
            self._store.delete_chart(ctx.company_id, chart_id, owner_actor_id=ctx.actor_id)
        )
        if deleted and self._audit is not None:
            self._audit.record_standalone(
                ctx,
                "chart_deleted",
                ChartDeletedPayload(chart_id=chart_id),
            )
        return deleted
