"""Per-device reporting staleness helpers shared by fleet and trend read services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from fleet_copilot.domain.models import DeviceState, PolicyValue

DEFAULT_STALE_DEVICE_DAYS = 7


def resolve_stale_device_days(policies: Mapping[str, PolicyValue]) -> int:
    """Resolve `stale_device_days` from active policies, falling back to the 7-day default."""
    pv = policies.get("stale_device_days")
    if pv is None or pv.value is None:
        return DEFAULT_STALE_DEVICE_DAYS
    return int(pv.value)


def stale_days_ago(
    device: DeviceState,
    as_of: datetime,
    stale_days: int,
) -> int | None:
    """Return whole days since `device.collected_at` iff older than `stale_days` before `as_of`."""
    delta = as_of - device.collected_at
    if delta <= timedelta(days=stale_days):
        return None
    return int(delta.total_seconds() // 86400)


def stale_devices_note(
    devices: Sequence[DeviceState],
    as_of: datetime,
    stale_days: int,
) -> str | None:
    """Return a coverage note listing stale devices, or `None` when 0 devices are stale."""
    stale = [
        d
        for d in sorted(devices, key=lambda x: (x.device_id, x.collected_at))
        if stale_days_ago(d, as_of, stale_days) is not None
    ]
    if not stale:
        return None
    entries = ", ".join(
        f"{d.os_hostname or d.device_id} (last seen {d.collected_at.date().isoformat()})"
        for d in stale
    )
    return f"{len(stale)} device(s) not seen for more than {stale_days} days: {entries}"
