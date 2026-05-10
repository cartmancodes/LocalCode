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
    # All None-able fields default to None so older meta.json files
    # missing optional keys still validate cleanly.
    cwd: str | None = None
    additional_dirs: list[str] | None = None
    upstream_id: str | None = None
    fleet_config_override: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: str
    role: str
    content: list[dict[str, Any]]
    # ``cost_usd`` / ``duration_ms`` only exist on assistant messages
    # (sourced from the provider's ``assistant.done`` event). User messages
    # don't have them, so they default to None — without this Pydantic
    # would 500 the /messages endpoint when loading user-only history.
    cost_usd: float | None = None
    duration_ms: int | None = None
    created_at: datetime

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _decimal_to_float(cls, v: Any) -> Any:
        # Legacy: when the store was SQLAlchemy-backed, cost_usd came back
        # as a Decimal. Filesystem store hands us plain floats, but the
        # validator stays as defensive deserialization.
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


# Unified streaming event surfaced over the WebSocket. Front-end renders these.
class StreamEvent(BaseModel):
    type: Literal[
        "session.started",
        "assistant.text",
        "assistant.tool_use",
        "tool.result",
        "assistant.done",
        "error",
        "pipeline.awaiting_approval",
        "pipeline.approval_received",
    ]
    data: dict[str, Any] = Field(default_factory=dict)
