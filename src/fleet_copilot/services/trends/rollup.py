"""Multi-signal device rollup and replacement-candidate ranking."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fleet_copilot.domain.models import DeviceState, Insight, PolicyValue
from fleet_copilot.services.trends.base import resolve_policy_int


def build_device_rollup(
    devices: list[DeviceState],
    insights: list[Insight],
    policies: dict[str, PolicyValue],
    catalog_by_model: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank devices by count of active signal categories and compute replacement-candidate flag.

    Active categories counted per device:
    - `battery`
    - `ram`
    - `disk`
    - `compliance` (compliance drift or chronic high-severity failure)
    - `software` (non-baseline software)

    Replacement candidate is True if:
    - battery condition is non-Normal (e.g., Service Recommended), OR
    - active signal count >= `replacement_min_signals`, OR
    - (when `catalog_by_model` is provided) sustained RAM on a non-ram-upgradable model, OR
      low disk on a non-storage-upgradable model.
    """
    min_signals, _ = resolve_policy_int(policies, "replacement_min_signals", 2)

    signals_by_dev: dict[str, set[str]] = defaultdict(set)
    disk_low_by_dev: set[str] = set()
    ram_hit_by_dev: set[str] = set()

    for ins in insights:
        if ins.category == "battery":
            signals_by_dev[ins.device_id].add("battery")
        elif ins.category == "ram":
            signals_by_dev[ins.device_id].add("ram")
            ram_hit_by_dev.add(ins.device_id)
        elif ins.category == "disk":
            signals_by_dev[ins.device_id].add("disk")
            if ins.stats.get("is_low"):
                disk_low_by_dev.add(ins.device_id)
        elif ins.category == "compliance_drift":
            if not ins.stats.get("recovered", False):
                signals_by_dev[ins.device_id].add("compliance")
        elif ins.category == "compliance_chronic":
            if ins.stats.get("check_severity") == "high":
                signals_by_dev[ins.device_id].add("compliance")
        elif ins.category == "software":
            signals_by_dev[ins.device_id].add("software")

    rows: list[dict[str, Any]] = []
    for dev in devices:
        active = sorted(signals_by_dev.get(dev.device_id, set()))
        battery_sr = dev.battery_condition is not None and dev.battery_condition != "Normal"
        reasons: list[str] = []
        if battery_sr:
            reasons.append(f"battery_condition={dev.battery_condition}")
        if len(active) >= min_signals:
            reasons.append(f"active_signals={len(active)}>={min_signals}")

        if catalog_by_model is not None and dev.model_identifier:
            cat_row = catalog_by_model.get(dev.model_identifier)
            if cat_row is not None:
                ram_up = (
                    cat_row.get("ram_upgradable")
                    if isinstance(cat_row, dict)
                    else getattr(cat_row, "ram_upgradable", None)
                )
                storage_up = (
                    cat_row.get("storage_upgradable")
                    if isinstance(cat_row, dict)
                    else getattr(cat_row, "storage_upgradable", None)
                )
                if dev.device_id in ram_hit_by_dev and ram_up is False:
                    reasons.append("sustained_ram_non_upgradable")
                if dev.device_id in disk_low_by_dev and storage_up is False:
                    reasons.append("low_disk_non_upgradable")

        rows.append(
            {
                "device_id": dev.device_id,
                "hostname": dev.os_hostname,
                "employee_id": dev.employee_id,
                "model_identifier": dev.model_identifier,
                "form_factor": dev.form_factor,
                "active_signals": active,
                "signal_count": len(active),
                "replacement_candidate": bool(reasons),
                "replacement_reasons": reasons,
            }
        )

    rows.sort(key=lambda r: (-int(r["signal_count"]), str(r["device_id"])))
    return rows
