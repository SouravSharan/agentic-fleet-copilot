"""Minimal FastAPI integration seam over `GuardedCopilot` and Fleet Copilot services."""

from fleet_copilot.interfaces.api.app import ApiContainer, create_app

__all__ = ["ApiContainer", "create_app"]
