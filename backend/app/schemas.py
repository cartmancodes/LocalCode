from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    provider: Literal["claude", "opencode", "fleet"]
    model: str
    cwd: str | None = None
    # Extra absolute directory paths the agent's tools may read/write under,
    # beyond the primary `cwd`. Useful when one chat needs access to sibling
    # repos. Validated identically to `cwd` (must satisfy the allowlist if one
    # is configured; otherwise permissive).
    additional_dirs: list[str] | None = None
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
    additional_dirs: list[str] | None = None
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

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _decimal_to_float(cls, v: Any) -> Any:
        # Storage is Numeric(12,6) (Decimal); the wire format stays float so
        # the frontend doesn't have to special-case Decimal serialization.
        if isinstance(v, Decimal):
            return float(v)
        return v


class MessagesPage(BaseModel):
    """Paginated /messages response. `next_before` is the timestamp to pass on
    the next request to load older messages (or null if exhausted)."""

    messages: list[MessageOut]
    next_before: datetime | None = None
    has_more: bool = False


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
