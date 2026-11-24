"""Pydantic request/response schemas for the minimal Fleet Copilot HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoLoginRequest(_ApiModel):
    """Credentials submitted to `POST /v1/auth/demo-login`."""

    code: str = Field(min_length=1, max_length=256)
    persona: str = Field(min_length=1, max_length=64)


class DemoLoginResponse(_ApiModel):
    """JWT bearer token and verified demo session metadata."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in_seconds: int
    actor_id: str
    company_id: str
    role: Literal["viewer", "it_admin"]
    rev_id: str
    persona: str


class PostMessageRequest(_ApiModel):
    """User message body for `POST /v1/threads/{thread_id}/messages`."""

    message: str = Field(min_length=1, max_length=4000)


class ProposalDecisionRequest(_ApiModel):
    """Decision payload for `POST /v1/proposals/{proposal_id}/decision`."""

    decision: Literal["approve", "reject", "defer"]
    note: str | None = Field(default=None, max_length=1000)
