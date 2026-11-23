"""External action adapter protocol and deterministic mock implementation."""

from fleet_copilot.services.adapters.base import ActionAdapter, Receipt
from fleet_copilot.services.adapters.mock import MockAdapter

__all__ = ["ActionAdapter", "MockAdapter", "Receipt"]
