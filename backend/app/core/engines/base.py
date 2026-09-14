"""The Engine contract — where LocalCode differs from pi on purpose.

pi's ``Agent`` owns the loop: it calls a raw LLM API and executes tools
itself. Here the loop belongs to a vendor's official binary — ``claude``
through the Agent SDK, ``codex`` through its app-server — because that is
what keeps a subscription usable. An *engine* wraps one such binary and
translates its native stream into pi's event vocabulary, so the session,
the extensions and the RPC layer never see anything vendor-specific.

Events an engine yields from :meth:`Engine.prompt` (pi's ``AgentEvent``):

    {"type": "agent_start"}
    {"type": "turn_start"}
    {"type": "message_start",  "message": <partial assistant message>}
    {"type": "message_update", "message": <partial>, "assistantMessageEvent": {...}}
    {"type": "message_end",    "message": <assistant message>}
    {"type": "tool_execution_start",  "toolCallId", "toolName", "args"}
    {"type": "tool_execution_update", "toolCallId", "toolName", "args", "partialResult"}
    {"type": "tool_execution_end",    "toolCallId", "toolName", "result", "isError"}
    {"type": "turn_end", "message": <assistant message>, "toolResults": [<toolResult>...]}
    {"type": "agent_end", "messages": [<every message this run produced>]}

plus a few the shell needs from an engine-owned loop:

    {"type": "engine_session", "engine": "claude", "sessionId": "..."}   # id to resume later
    {"type": "compaction", "summary": str | None, "tokensBefore": int | None}
    {"type": "rate_limit", "info": {...}}                                  # quota governor input
    {"type": "error", "message": str, "willRetry": bool}

Hooks (:class:`EngineHooks`) are the compile-down target for extension
events: the session hands the engine callbacks, the engine calls them at
the moment its binary exposes — Claude at ``PreToolUse`` / ``can_use_tool``,
Codex at ``item/commandExecution/requestApproval``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..extensions.types import ToolDefinition

EngineEvent = dict[str, Any]


class EngineError(Exception):
    pass


class NotSupportedError(EngineError):
    """The engine's binary has no equivalent for this operation."""


@dataclass(frozen=True)
class EngineCapabilities:
    steer: bool = False  # inject a message mid-run, before the next model call
    follow_up: bool = True  # queue a message for after the run
    fork: bool = False  # branch the engine's own transcript
    compact: bool = False  # ask the engine to compact its context now
    tool_input_rewrite: bool = False  # before_tool may return updated_input
    custom_tools: bool = False  # extension tools can be mounted into the engine
    thinking_levels: tuple[str, ...] = ()


@dataclass
class EngineConfig:
    cwd: str
    model: str | None = None
    thinking_level: str = "off"
    system_prompt: str | None = None  # replaces the engine's default prompt
    append_system_prompt: str | None = None
    session_id: str | None = None  # resume the engine's own session/thread
    permission_mode: str | None = None  # claude permission_mode / codex sandbox
    add_dirs: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


BeforeToolHook = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
AfterToolHook = Callable[
    [str, str, dict[str, Any], list[dict[str, Any]], bool], Awaitable[dict[str, Any] | None]
]
BeforePromptHook = Callable[[str], Awaitable[dict[str, Any] | None]]
StopHook = Callable[[], Awaitable[None]]
AskUserHook = Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any] | None]]
PermissionRequestHook = Callable[
    [str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None]
]  # (tool name, input, context) → {allow, updated_input?, message?, interrupt?}


@dataclass
class EngineHooks:
    """Callbacks the session installs; every field is optional.

    ``before_tool(name, call_id, input)`` → ``None`` to allow, or
    ``{"block": True, "reason": str}`` to refuse, or ``{"updated_input": {...}}``
    (only honoured when ``capabilities.tool_input_rewrite``).

    ``after_tool(name, call_id, input, content, is_error)`` → optional
    ``{"content": [...], "is_error": bool}`` override (only where the engine
    can rewrite tool output).

    ``before_prompt(text)`` → optional ``{"additional_context": str}``.

    ``ask_user(questions)`` → answers for an engine-initiated user-input
    request (Codex ``item/tool/requestUserInput``), or ``None`` to cancel.

    ``permission_request(name, input, context)`` → ``{"allow": bool, ...}``
    when the binary would prompt a human (Claude ``can_use_tool``, Codex
    ``item/*/requestApproval``). Absent → the engine denies with a message
    pointing at the approval extension. pi ships no permission popups in
    core either.
    """

    before_tool: BeforeToolHook | None = None
    after_tool: AfterToolHook | None = None
    before_prompt: BeforePromptHook | None = None
    on_stop: StopHook | None = None
    ask_user: AskUserHook | None = None
    permission_request: PermissionRequestHook | None = None


ToolExecutor = Callable[
    [str, str, dict[str, Any]], Awaitable[dict[str, Any]]
]  # (tool name, call id, args) → ToolResult


@runtime_checkable
class Engine(Protocol):
    name: str

    @property
    def capabilities(self) -> EngineCapabilities: ...

    @property
    def session_id(self) -> str | None:
        """The engine's own session/thread id, once known."""
        ...

    @property
    def model(self) -> str | None: ...

    async def start(self, config: EngineConfig) -> None:
        """Spawn or connect. Resumes ``config.session_id`` when given."""
        ...

    def prompt(self, message: dict[str, Any], hooks: EngineHooks) -> AsyncIterator[EngineEvent]:
        """Run one agent run to completion for a pi ``UserMessage``."""
        ...

    async def follow_up(self, message: dict[str, Any]) -> None: ...

    async def steer(self, message: dict[str, Any]) -> bool:
        """Inject mid-run. ``False`` when unsupported (the session then falls
        back to interrupt + re-prompt)."""
        ...

    async def interrupt(self) -> None: ...

    async def fork(self) -> str:
        """Branch the engine's transcript at its current point; returns the
        new engine session id and switches to it."""
        ...

    async def set_model(self, model: str) -> None: ...

    async def set_thinking_level(self, level: str) -> None: ...

    async def compact(self, custom_instructions: str | None = None) -> None: ...

    async def mount_tools(self, tools: list[ToolDefinition], executor: ToolExecutor) -> None: ...

    async def close(self) -> None: ...
