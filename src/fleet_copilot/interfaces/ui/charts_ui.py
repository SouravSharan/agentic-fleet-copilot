"""Render-time Vega-Lite chart helpers (never mutates stored chart specs in DB or TurnResponse)."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from fleet_copilot.interfaces.ui.theme import CHART_PALETTE, STATUS_HEX

# Default policy threshold overlays applied purely at render time
_METRIC_THRESHOLDS: dict[str, tuple[str, float, str]] = {
    "disk_free_pct": ("low_disk_free_pct", 15.0, "Low disk threshold (15%)"),
    "ram_used_pct": ("high_ram_used_pct", 80.0, "High RAM threshold (80%)"),
    "battery_cycles": ("battery_eol_cycles", 800.0, "End-of-life threshold (800 cycles)"),
}


def _infer_metric_from_spec(
    spec: dict[str, Any], chart_meta: dict[str, Any] | None = None
) -> str | None:
    if chart_meta:
        req = chart_meta.get("request_json")
        if isinstance(req, dict) and req.get("metric"):
            return str(req["metric"])
        title_l = str(chart_meta.get("title") or "").lower()
    else:
        title_l = str(spec.get("title") or "").lower()

    raw_enc = spec.get("encoding")
    enc: dict[str, Any] = raw_enc if isinstance(raw_enc, dict) else {}
    raw_y = enc.get("y")
    y_enc: dict[str, Any] = raw_y if isinstance(raw_y, dict) else {}
    y_title = str(y_enc.get("title") or "").lower()
    combined = f"{title_l} {y_title}"

    if "disk free" in combined or "disk_free" in combined:
        return "disk_free_pct"
    if "ram used" in combined or "ram_used" in combined or "memory" in combined:
        return "ram_used_pct"
    if "cycle" in combined or "battery" in combined and "capacity" not in combined:
        return "battery_cycles"
    return None


def _threshold_for_metric(
    metric: str | None,
    policies: Mapping[str, Any] | None = None,
) -> tuple[float, str] | None:
    if not metric or metric not in _METRIC_THRESHOLDS:
        return None
    policy_key, default_val, default_label = _METRIC_THRESHOLDS[metric]
    val = default_val
    if policies and policy_key in policies:
        pv = policies[policy_key]
        raw_v = pv.value if hasattr(pv, "value") else pv
        try:
            val = float(raw_v)
        except (TypeError, ValueError):
            val = default_val
    if metric == "disk_free_pct":
        label = f"Low disk policy ({val:g}%)"
    elif metric == "ram_used_pct":
        label = f"High RAM policy ({val:g}%)"
    elif metric == "battery_cycles":
        label = f"EOL threshold ({int(round(val))} cycles)"
    else:
        label = default_label
    return val, label


def prepare_chart_spec_for_render(
    raw_spec: dict[str, Any],
    *,
    chart_meta: dict[str, Any] | None = None,
    metric: str | None = None,
    policies: Mapping[str, Any] | None = None,
    height: int = 260,
    strip_inner_title: bool = True,
) -> dict[str, Any]:
    """Return a render-ready deep copy of a stored Vega-Lite spec with polish & threshold layer.

    Never mutates `raw_spec`. Applies:
    - single title (removes duplicate inner Vega title when card renders title above)
    - full container width (`width: "container"`)
    - clean legend title (`"Device"` instead of `"series"`)
    - dashed policy threshold rule layer when metric has a known policy threshold
    """
    spec = copy.deepcopy(raw_spec)
    if strip_inner_title:
        spec.pop("title", None)
    spec["width"] = "container"
    spec["height"] = height

    # Ensure clean encoding polish on non-layered specs
    enc = spec.get("encoding")
    if isinstance(enc, dict):
        color = enc.get("color")
        if isinstance(color, dict):
            if str(color.get("field") or "").lower() in ("series", "device_id", "device"):
                color["title"] = "Device"
            elif str(color.get("title") or "").lower() == "series":
                color["title"] = "Device"
            if "legend" not in color:
                color["legend"] = {"orient": "bottom", "title": "Device"}
            elif isinstance(color["legend"], dict) and color["legend"].get("title") == "series":
                color["legend"]["title"] = "Device"
            if "scale" not in color:
                color["scale"] = {"range": list(CHART_PALETTE)}

    resolved_metric = metric or _infer_metric_from_spec(spec, chart_meta)
    threshold_info = _threshold_for_metric(resolved_metric, policies)

    # Only add a horizontal threshold rule when y is quantitative and spec is single-layer
    if (
        threshold_info is not None
        and "layer" not in spec
        and isinstance(spec.get("encoding"), dict)
        and isinstance(spec["encoding"].get("y"), dict)
        and spec["encoding"]["y"].get("type") == "quantitative"
        and "mark" in spec
    ):
        thresh_val, thresh_label = threshold_info
        base_layer = {
            "mark": spec.pop("mark"),
            "encoding": spec.pop("encoding"),
        }
        rule_layer = {
            "data": {"values": [{"threshold": thresh_val, "label": thresh_label}]},
            "mark": {
                "type": "rule",
                "strokeDash": [6, 4],
                "strokeWidth": 1.6,
                "color": STATUS_HEX["high"],
            },
            "encoding": {
                "y": {"field": "threshold", "type": "quantitative"},
                "tooltip": [{"field": "label", "type": "nominal", "title": "Policy"}],
            },
        }
        spec["layer"] = [base_layer, rule_layer]

    return spec
