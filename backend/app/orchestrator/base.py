from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


EventType = Literal[
    "session.started",
    "assistant.text",
    "assistant.tool_use",
    "tool.result",
    "assistant.done",
    "error",
]


@dataclass
class Event:
    """Provider-agnostic streaming event consumed by the WebSocket layer."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data}


@dataclass
class RunContext:
    """Inputs shared by every provider on each turn."""

    model: str
    prompt: str
    cwd: str | None = None
    upstream_session_id: str | None = None
    system_prompt: str | None = None


class Provider(Protocol):
    """A backend that can run an agent turn and stream unified Events.

    Implementations:
      - ClaudeProvider: spawns claude-agent-sdk with ANTHROPIC_BASE_URL pointed at LiteLLM.
      - OpenCodeProvider: talks to `opencode serve` HTTP API (also fronted by LiteLLM).
    """

    name: str

    async def open_session(self, ctx: RunContext) -> str:
        """Create or reuse an upstream session and return its id."""
        ...

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        """Stream a single user turn as Events."""
        ...

    async def aclose(self) -> None:
        """Release any persistent resources."""
        ...
