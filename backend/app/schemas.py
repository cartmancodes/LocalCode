from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    provider: Literal["claude", "opencode", "fleet"]
    model: str
    cwd: str | None = None
    title: str | None = None
    # When provider == "fleet", a partial config dict (matches the YAML schema)
    # that overrides the file-level config for this session only. Anything you
    # omit inherits the file-level / built-in default.
    fleet_config_override: dict[str, Any] | None = None


class SessionOut(BaseModel):
    id: str
    title: str
    provider: str
    model: str
    cwd: str | None
    upstream_id: str | None
    fleet_config_override: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: str
    role: str
    content: list[dict[str, Any]]
    cost_usd: float | None
    duration_ms: int | None
    created_at: datetime


class CatalogModel(BaseModel):
    id: str
    provider: str
    model: str


class BudgetOut(BaseModel):
    spend_usd: float
    daily_budget_usd: float
    remaining_usd: float
    window: str = Field(description="ISO date for the spend window")


# Unified streaming event surfaced over the WebSocket. Front-end renders these.
class StreamEvent(BaseModel):
    type: Literal[
        "session.started",
        "assistant.text",
        "assistant.tool_use",
        "tool.result",
        "assistant.done",
        "error",
    ]
    data: dict[str, Any] = Field(default_factory=dict)
