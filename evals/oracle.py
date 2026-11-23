"""Independent ground-truth oracle over the raw NDJSON telemetry dataset.

MUST NOT import `fleet_copilot` (or any application module) so integration tests and
evaluation suites cross-check the application against an independent pure-Python reference.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "device-telemetry-dataset.ndjson"

_LAPTOP_RE = re.compile(
    r"\b(macbook|laptop|thinkpad|xps|chromebook|elitebook|latitude|zenbook)\b",
    re.IGNORECASE,
)
_DESKTOP_RE = re.compile(
    r"\b(mac mini|imac|mac studio|desktop|tower|workstation)\b",
    re.IGNORECASE,
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


@lru_cache(maxsize=1)
def _load_rows() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    with DATASET_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return tuple(rows)


def _company_rows(company: str) -> list[dict[str, Any]]:
    return [r for r in _load_rows() if r["company_id"] == company]


def _by_device(company: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _company_rows(company):
        grouped[row["device_id"]].append(row)
    for snaps in grouped.values():
        snaps.sort(key=lambda r: _parse_ts(r["collected_at"]))
    return dict(grouped)


def _form_factor(snaps: list[dict[str, Any]]) -> str:
    ever_battery = any(
        isinstance(s.get("battery"), dict) and bool(s["battery"].get("battery_present"))
        for s in snaps
    )
    if ever_battery:
        return "laptop"
    model = ""
    for s in reversed(snaps):
        ident = s.get("device_identity")
        if isinstance(ident, dict) and ident.get("model_name"):
            model = str(ident["model_name"])
            break
    if _LAPTOP_RE.search(model):
        return "laptop"
    if _DESKTOP_RE.search(model):
        return "desktop"
    return "unknown"


def _primary_disk_free_pct(snap: dict[str, Any]) -> float | None:
    vols = snap.get("disk_volumes")
    if not isinstance(vols, list) or not vols:
        return None
    valid = [
        v
        for v in vols
        if isinstance(v, dict)
        and isinstance(v.get("size_bytes"), (int, float))
        and v["size_bytes"] > 0
        and isinstance(v.get("available_bytes"), (int, float))
        and v["available_bytes"] >= 0
    ]
    if not valid:
        return None
    primary = max(valid, key=lambda v: v["size_bytes"])
    return round((primary["available_bytes"] / primary["size_bytes"]) * 100.0, 2)


def _ram_used_pct(snap: dict[str, Any]) -> float | None:
    mem = snap.get("memory")
    if not isinstance(mem, dict):
        return None
    total = mem.get("total_memory_bytes")
    used = mem.get("used_memory_bytes")
    if (
        isinstance(total, (int, float))
        and total > 0
        and isinstance(used, (int, float))
        and used >= 0
    ):
        return round((used / total) * 100.0, 2)
    return None


def _ols_pure(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    if ss_xx <= 0.0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / ss_xx
    intercept = y_mean - slope * x_mean
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    if ss_tot <= 0.0:
        return slope, 0.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = max(0.0, min(1.0, 1.0 - ss_res / ss_tot))
    return slope, r2


def as_of(company: str) -> datetime:
    """Return max(collected_at) UTC across all snapshots for `company`."""
    rows = _company_rows(company)
    if not rows:
        raise ValueError(f"No rows for company {company!r}")
    return max(_parse_ts(r["collected_at"]) for r in rows)


def latest_by_device(company: str) -> dict[str, dict[str, Any]]:
    """Return the latest raw snapshot dict per `device_id` for `company`."""
    return {dev_id: snaps[-1] for dev_id, snaps in _by_device(company).items()}


def low_disk(company: str, threshold: float = 15.0) -> set[str]:
    """Return device IDs whose latest primary disk free % is strictly below `threshold`."""
    out: set[str] = set()
    for dev_id, snap in latest_by_device(company).items():
        fp = _primary_disk_free_pct(snap)
        if fp is not None and fp < threshold:
            out.add(dev_id)
    return out


def laptops_failing(company: str, severity: str) -> set[str]:
    """Return laptop device IDs with any failing compliance check of `severity` on latest snap."""
    out: set[str] = set()
    for dev_id, snaps in _by_device(company).items():
        if _form_factor(snaps) != "laptop":
            continue
        latest = snaps[-1]
        checks = latest.get("compliance_results") or []
        if any(
            isinstance(c, dict) and c.get("severity") == severity and c.get("status") == "fail"
            for c in checks
        ):
            out.add(dev_id)
    return out


def macos_older_than(company: str, major: int = 15) -> set[str]:
    """Return macOS device IDs whose latest OS version is strictly older than `major`."""
    out: set[str] = set()
    for dev_id, snap in latest_by_device(company).items():
        os_info = snap.get("os") or {}
        if str(os_info.get("product_name", "")).lower() != "macos":
            continue
        ver = str(os_info.get("product_version", ""))
        parts = ver.split(".")
        if parts and parts[0].isdigit():
            parsed_major = int(parts[0])
            if parsed_major < major:
                out.add(dev_id)
    return out


def battery_eol(company: str, cycles: int = 800, drop_pct: float = 3.0) -> set[str]:
    """Return laptop device IDs showing battery EOL (condition, cycles, or capacity drop)."""
    out: set[str] = set()
    for dev_id, snaps in _by_device(company).items():
        if _form_factor(snaps) != "laptop":
            continue
        bats = [
            s["battery"]
            for s in snaps
            if isinstance(s.get("battery"), dict) and s["battery"].get("battery_present")
        ]
        if not bats:
            continue
        latest_cond = bats[-1].get("condition")
        cycs = [int(b["cycle_count"]) for b in bats if b.get("cycle_count") is not None]
        caps = [
            int(b["full_charge_capacity"])
            for b in bats
            if b.get("full_charge_capacity") is not None
        ]
        cap_drop = ((caps[0] - caps[-1]) / caps[0] * 100.0) if caps and caps[0] > 0 else 0.0
        if (
            (latest_cond is not None and latest_cond != "Normal")
            or (cycs and cycs[-1] >= cycles)
            or cap_drop >= drop_pct
        ):
            out.add(dev_id)
    return out


def ram_sustained(company: str, t: float = 80.0, r: float = 0.7) -> set[str]:
    """Return device IDs where RAM used % is >= `t` on at least share `r` of snapshots."""
    out: set[str] = set()
    for dev_id, snaps in _by_device(company).items():
        vals = [v for s in snaps if (v := _ram_used_pct(s)) is not None]
        if not vals:
            continue
        share = sum(1 for v in vals if v >= t) / len(vals)
        if share >= r:
            out.add(dev_id)
    return out


def disk_depleting(company: str, slope: float = 0.1, r2: float = 0.8) -> set[str]:
    """Return device IDs whose disk free % OLS slope < -`slope`/day with R^2 >= `r2`."""
    out: set[str] = set()
    for dev_id, snaps in _by_device(company).items():
        pts: list[tuple[datetime, float]] = []
        for s in snaps:
            fp = _primary_disk_free_pct(s)
            if fp is not None:
                pts.append((_parse_ts(s["collected_at"]), fp))
        if len(pts) < 3:
            continue
        t0 = pts[0][0]
        xs = [(ts - t0).total_seconds() / 86400.0 for ts, _ in pts]
        ys = [fp for _, fp in pts]
        fit = _ols_pure(xs, ys)
        if fit is not None and fit[0] < -slope and fit[1] >= r2:
            out.add(dev_id)
    return out


def screen_lock_drift(company: str) -> dict[str, str]:
    """Return `{device_id: first_fail_date}` for devices drifting pass -> fail on screen_lock."""
    out: dict[str, str] = {}
    for dev_id, snaps in _by_device(company).items():
        seen_pass = False
        for s in snaps:
            for chk in s.get("compliance_results") or []:
                if not isinstance(chk, dict) or chk.get("check_id") != "screen_lock":
                    continue
                status = chk.get("status")
                if status == "pass":
                    seen_pass = True
                elif status == "fail" and seen_pass and dev_id not in out:
                    out[dev_id] = _parse_ts(s["collected_at"]).date().isoformat()
    return out


def non_baseline_software(company: str, share: float = 0.8) -> dict[str, dict[str, str]]:
    """Return `{device_id: {software_name: first_seen_date}}` for non-baseline installs."""
    by_dev = _by_device(company)
    if not by_dev:
        return {}
    n_devices = len(by_dev)
    first_counts: Counter[str] = Counter()
    for snaps in by_dev.values():
        first_names = {
            str(sw["name"])
            for sw in (snaps[0].get("installed_software") or [])
            if isinstance(sw, dict) and sw.get("name")
        }
        for name in first_names:
            first_counts[name] += 1

    baseline = {name for name, count in first_counts.items() if (count / n_devices) >= share}

    result: dict[str, dict[str, str]] = {}
    for dev_id, snaps in sorted(by_dev.items()):
        found: dict[str, str] = {}
        for s in snaps:
            dt_str = _parse_ts(s["collected_at"]).date().isoformat()
            for sw in s.get("installed_software") or []:
                if not isinstance(sw, dict) or not sw.get("name"):
                    continue
                name = str(sw["name"])
                if name not in baseline and name not in found:
                    found[name] = dt_str
        if found:
            result[dev_id] = found
    return result


def employee_devices(company: str, employee_id: str) -> set[str]:
    """Return all device IDs owned by `employee_id` in `company`."""
    return {
        dev_id
        for dev_id, snap in latest_by_device(company).items()
        if snap.get("employee_id") == employee_id
    }


def overview(company: str) -> dict[str, Any]:
    """Return fleet summary counts by form factor, OS, model, and latest compliance."""
    by_dev = _by_device(company)
    ff_counts: Counter[str] = Counter()
    os_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    comp_counts: dict[str, dict[str, Any]] = {}

    for snaps in by_dev.values():
        ff_counts[_form_factor(snaps)] += 1
        latest = snaps[-1]
        os_info = latest.get("os") or {}
        os_label = (
            f"{os_info.get('product_name', 'unknown')} {os_info.get('product_version', '')}".strip()
        )
        os_counts[os_label] += 1
        ident = latest.get("device_identity") or {}
        model_counts[str(ident.get("model_name") or "unknown")] += 1

        for chk in latest.get("compliance_results") or []:
            if not isinstance(chk, dict) or not chk.get("check_id"):
                continue
            cid = str(chk["check_id"])
            sev = str(chk.get("severity") or "unknown")
            st = str(chk.get("status") or "unknown")
            bucket = comp_counts.setdefault(cid, {"severity": sev, "pass": 0, "fail": 0})
            if st in ("pass", "fail"):
                bucket[st] += 1

    return {
        "total_devices": len(by_dev),
        "by_form_factor": dict(sorted(ff_counts.items())),
        "by_os": dict(sorted(os_counts.items())),
        "by_model": dict(sorted(model_counts.items())),
        "compliance": dict(sorted(comp_counts.items())),
        "telemetry_as_of": as_of(company).isoformat(),
    }
