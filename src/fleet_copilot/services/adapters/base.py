"""ActionAdapter protocol and structured Receipt contract."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

OutboxChannel = Literal["ticketing", "procurement", "email", "asset_management"]


class Receipt(BaseModel):
    """Structured execution receipt returned by an external action adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    receipt_id: str
    idempotency_key: str
    status: Literal["ok", "failed"] = "ok"
    error_code: str | None = None
    channel: OutboxChannel | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class ActionAdapter(Protocol):
    """Protocol for idempotent external action execution (procurement, ITSM, lifecycle, comms)."""

    name: str

    def execute(self, proposal: dict[str, Any], idempotency_key: str) -> Receipt: ...
