from __future__ import annotations

import asyncio
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
    # HITL — fleet pauses after planner; UI shows Approve/Reject buttons.
    # `pipeline.approval_received` records the user's decision (or a timeout)
    # so the chat history reflects what happened.
    "pipeline.awaiting_approval",
    "pipeline.approval_received",
    # Synthesized per subscriber by the EventBus, never by a provider: "you
    # missed `dropped` events after `resume_from` because your queue filled".
    # The UI refetches `/messages` rather than rendering a hole.
    "stream.gap",
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
    # The LocalCode session this turn belongs to. Providers key persistent
    # per-session state (an SDK client, a quota window) on it. None in
    # headless/unit use.
    session_id: str | None = None
    # The fleet role this context is running as ("planner", "coder", ...) or
    # None for a plain single-agent session. Selects the ToolPolicy.
    role: str | None = None
    # Extra absolute paths the agent's tools may operate on beyond `cwd`.
    # Supported by ClaudeProvider via `add_dirs`; OpenCode currently has no
    # equivalent so they're informational there.
    additional_dirs: list[str] = field(default_factory=list)
    upstream_session_id: str | None = None
    system_prompt: str | None = None
    # Permission/auto mode forwarded to the upstream agent. For Claude this
    # maps to claude-agent-sdk's ``permission_mode`` (acceptEdits / default /
    # plan / bypassPermissions). None → the provider's safe default. Chosen
    # per-session in the UI; threaded through to sub-providers in the fleet.
    permission_mode: str | None = None
    # Provider-specific extras. Currently only used by the fleet provider
    # (a per-session partial config dict that overrides the file-level YAML).
    extras: dict[str, Any] = field(default_factory=dict)
    # Back-channel for HITL: when set, the WebSocket handler routes inbound
    # approval messages into this queue. Providers that implement an approval
    # gate `await` on it; providers that don't can ignore it. Each message is
    # a dict like {"id": "approval.tool.3", "value": "yes"|"no", "feedback": "..."}
    # where the id is the one the gate published (see `next_approval_id`).
    approval_channel: asyncio.Queue[dict[str, Any]] | None = None


class Provider(Protocol):
    """A backend that can run an agent turn and stream unified Events.

    Implementations:
      - ClaudeProvider: spawns claude-agent-sdk; the CLI authenticates via the
        host's `claude login` OAuth token.
      - OpenCodeProvider: talks to `opencode serve` HTTP API; OpenCode reads
        its own OAuth credentials from ~/.local/share/opencode/auth.json.
    """

    name: str

    async def open_session(self, ctx: RunContext) -> str:
        """Create or reuse an upstream session and return its id."""
        ...

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        """Stream a single user turn as Events."""
        ...

    async def close_session(self, session_id: str) -> None:
        """Release any state held for one session. Default: nothing.

        A provider that keeps a live per-session handle (Task 4 gives Claude a
        persistent ``ClaudeSDKClient``) has to be told when a session goes
        away, or the handle — and the CLI process behind it — outlives the
        session that owned it. Providers holding nothing implement this as a
        no-op rather than leaving the protocol unsatisfied.
        """
        ...

    async def aclose(self) -> None:
        """Release any persistent resources."""
        ...
