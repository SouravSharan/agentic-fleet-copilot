"""Cooperative monotonic turn deadline enforcement."""

from __future__ import annotations

import time
from typing import Any


class TurnTimeoutError(TimeoutError):
    """Raised cooperatively when a turn exceeds its configured `turn_timeout_s` deadline."""


def check_turn_deadline(config: Any) -> None:
    """Raise `TurnTimeoutError` if `_turn_deadline` in `config['configurable']` has passed."""
    if not isinstance(config, dict):
        return
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return
    deadline = configurable.get("_turn_deadline")
    if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
        raise TurnTimeoutError("Turn exceeded cooperative turn_timeout_s deadline")
