"""Extension contracts — pi's ``core/extensions/types.ts`` in Python.

Events are plain dicts with a ``type`` key (the same shape they take on the
RPC wire); the TypedDicts below document them. Handlers return either
``None`` or a result dict whose keys are listed per event.

What is deliberately *not* here, compared with pi:

- ``context`` — pi lets an extension rewrite the message array before every
  LLM call. Our loop lives inside the vendor's binary, which never exposes
  that. Registering for it raises, so nobody ships a hook that silently
  does nothing.
- ``before_provider_request`` / ``before_provider_headers`` /
  ``after_provider_response`` — same reason: the engines own HTTP.

Everything else maps onto something both engines provide (see
``engines/base.py`` for the compile-down).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict

ExtensionMode = Literal["tui", "rpc", "json", "print"]
InputSource = Literal["interactive", "rpc", "extension"]
StreamingBehavior = Literal["steer", "followUp"]

# Events we dispatch, in pi's names. Kept as a tuple so ``on()`` can reject
# typos and the unsupported ones with a clear message.
SUPPORTED_EVENTS: tuple[str, ...] = (
    "project_trust",
    "resources_discover",
    "session_start",
    "session_info_changed",
    "session_before_switch",
    "session_before_fork",
    "session_before_compact",
    "session_compact",
    "session_compact_failed",
    "session_shutdown",
    "session_before_tree",
    "session_tree",
    "before_agent_start",
    "agent_start",
    "agent_end",
    "agent_settled",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "model_select",
    "thinking_level_select",
    "tool_call",
    "tool_result",
    "user_bash",
    "input",
)

UNSUPPORTED_EVENTS: dict[str, str] = {
    "context": (
        "the 'context' hook rewrites messages before each LLM call; the agent loop "
        "lives inside the official claude/codex binary and never exposes that"
    ),
    "before_provider_request": "engines own their HTTP layer",
    "before_provider_headers": "engines own their HTTP layer",
    "after_provider_response": "engines own their HTTP layer",
}


# ── event payloads ─────────────────────────────────────────────────────────


class ToolCallEvent(TypedDict):
    type: Literal["tool_call"]
    toolName: str
    toolCallId: str
    input: dict[str, Any]  # mutable — handlers may edit in place


class ToolCallEventResult(TypedDict, total=False):
    block: bool
    reason: str
    terminate: bool


class ToolResultEvent(TypedDict):
    type: Literal["tool_result"]
    toolName: str
    toolCallId: str
    input: dict[str, Any]
    content: list[dict[str, Any]]
    details: Any
    isError: bool


class ToolResultEventResult(TypedDict, total=False):
    content: list[dict[str, Any]]
    details: Any
    isError: bool
    usage: dict[str, Any]


class BeforeAgentStartEvent(TypedDict):
    type: Literal["before_agent_start"]
    prompt: str
    images: NotRequired[list[dict[str, Any]]]
    systemPrompt: str


class BeforeAgentStartEventResult(TypedDict, total=False):
    message: dict[str, Any]  # {customType, content, display, details?}
    systemPrompt: str


class InputEvent(TypedDict):
    type: Literal["input"]
    text: str
    images: NotRequired[list[dict[str, Any]]]
    source: InputSource
    streamingBehavior: NotRequired[StreamingBehavior]


class InputEventResult(TypedDict, total=False):
    action: Literal["continue", "transform", "handled"]
    text: str
    images: list[dict[str, Any]]


class SessionStartEvent(TypedDict):
    type: Literal["session_start"]
    reason: Literal["startup", "reload", "new", "resume", "fork"]
    previousSessionFile: NotRequired[str]


class SessionShutdownEvent(TypedDict):
    type: Literal["session_shutdown"]
    reason: Literal["quit", "reload", "new", "resume", "fork"]
    targetSessionFile: NotRequired[str]


class ProjectTrustEventResult(TypedDict, total=False):
    trusted: Literal["yes", "no", "undecided"]
    remember: bool


class ResourcesDiscoverResult(TypedDict, total=False):
    skillPaths: list[str]
    promptPaths: list[str]
    themePaths: list[str]


Event = dict[str, Any]
EventResult = dict[str, Any] | None
Handler = Callable[[Event, "ExtensionContext"], EventResult | Awaitable[EventResult]]


# ── tools ──────────────────────────────────────────────────────────────────


class ToolResult(TypedDict, total=False):
    content: list[dict[str, Any]]  # [{type:"text", text}] | [{type:"image", data, mimeType}]
    details: Any
    usage: dict[str, Any]
    terminate: bool


ToolUpdate = Callable[[ToolResult], None]
ToolExecute = Callable[
    [str, dict[str, Any], "asyncio.Event | None", "ToolUpdate | None", "ExtensionContext"],
    Awaitable[ToolResult],
]


@dataclass
class ToolDefinition:
    """A tool an extension adds. The engine mounts it (Claude: in-process
    MCP server; Codex: dynamic tools) and routes calls back to ``execute``."""

    name: str
    description: str
    execute: ToolExecute
    label: str = ""
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": True}
    )
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name


class ToolInfo(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]
    active: bool
    source: str  # "engine" | "extension:<path>"


# ── commands, flags, shortcuts ─────────────────────────────────────────────

CommandHandler = Callable[[str, "ExtensionCommandContext"], Awaitable[None] | None]


@dataclass
class RegisteredCommand:
    name: str
    handler: CommandHandler
    description: str = ""
    source_info: dict[str, Any] = field(default_factory=dict)
    get_argument_completions: Callable[[str], list[str]] | None = None


@dataclass
class ExtensionFlag:
    name: str
    type: Literal["boolean", "string"]
    description: str = ""
    default: bool | str | None = None


@dataclass
class ExtensionShortcut:
    shortcut: str
    handler: Callable[[ExtensionContext], Awaitable[None] | None]
    description: str = ""


class SlashCommandInfo(TypedDict):
    name: str
    description: str
    source: Literal["extension", "prompt", "skill"]
    sourceInfo: dict[str, Any]


# ── UI bridge ──────────────────────────────────────────────────────────────


class UIBridge(Protocol):
    """How ``ctx.ui`` reaches a human. RPC mode emits ``extension_ui_request``
    frames and waits for ``extension_ui_response``; print/json mode resolves
    dialogs with their defaults; a WebSocket bridge does the same as RPC."""

    has_ui: bool

    async def dialog(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Blocking request. Returns ``{"value": …}``, ``{"confirmed": bool}``
        or ``{"cancelled": True}``."""
        ...

    def notify(self, method: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget (notify, setStatus, setWidget, setTitle, set_editor_text)."""
        ...


# pi passes dialog options as one object (``ExtensionUIDialogOptions``);
# ``timeout`` is in seconds here and milliseconds on the wire.
DialogOptions = dict[str, Any]


def _with_timeout(payload: dict[str, Any], opts: DialogOptions | None) -> dict[str, Any]:
    if opts and opts.get("timeout") is not None:
        payload["timeout"] = int(float(opts["timeout"]) * 1000)
    return payload


class ExtensionUIContext:
    """``ctx.ui`` — pi's dialog and status methods over a :class:`UIBridge`."""

    def __init__(self, bridge: UIBridge) -> None:
        self._bridge = bridge

    async def select(
        self, title: str, options: list[str], opts: DialogOptions | None = None
    ) -> str | None:
        payload = _with_timeout({"title": title, "options": options}, opts)
        res = await self._bridge.dialog("select", payload)
        return None if res.get("cancelled") else res.get("value")

    async def confirm(self, title: str, message: str, opts: DialogOptions | None = None) -> bool:
        payload = _with_timeout({"title": title, "message": message}, opts)
        res = await self._bridge.dialog("confirm", payload)
        return bool(res.get("confirmed")) and not res.get("cancelled")

    async def input(
        self, title: str, placeholder: str | None = None, opts: DialogOptions | None = None
    ) -> str | None:
        payload: dict[str, Any] = {"title": title}
        if placeholder is not None:
            payload["placeholder"] = placeholder
        res = await self._bridge.dialog("input", _with_timeout(payload, opts))
        return None if res.get("cancelled") else res.get("value")

    async def editor(self, title: str, prefill: str | None = None) -> str | None:
        payload: dict[str, Any] = {"title": title}
        if prefill is not None:
            payload["prefill"] = prefill
        res = await self._bridge.dialog("editor", payload)
        return None if res.get("cancelled") else res.get("value")

    def notify(
        self, message: str, notify_type: Literal["info", "warning", "error"] = "info"
    ) -> None:
        self._bridge.notify("notify", {"message": message, "notifyType": notify_type})

    def set_status(self, key: str, text: str | None) -> None:
        self._bridge.notify("setStatus", {"statusKey": key, "statusText": text})

    def set_widget(
        self,
        key: str,
        lines: list[str] | None,
        placement: Literal["aboveEditor", "belowEditor"] = "aboveEditor",
    ) -> None:
        self._bridge.notify(
            "setWidget", {"widgetKey": key, "widgetLines": lines, "widgetPlacement": placement}
        )

    def set_title(self, title: str) -> None:
        self._bridge.notify("setTitle", {"title": title})

    def set_editor_text(self, text: str) -> None:
        self._bridge.notify("set_editor_text", {"text": text})


class NoUIBridge:
    """print/json mode: no human is attached, dialogs resolve to defaults."""

    has_ui = False

    async def dialog(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "confirm":
            return {"confirmed": False}
        if method == "select":
            options = payload.get("options") or []
            return {"value": options[0]} if options else {"cancelled": True}
        return {"cancelled": True}

    def notify(self, method: str, payload: dict[str, Any]) -> None:
        return None


# ── context ────────────────────────────────────────────────────────────────


class ExtensionContext(Protocol):
    """What every handler receives. Mirrors pi's ``ExtensionContext``.

    ``session_manager`` is read-only by convention; mutate the session through
    the :class:`ExtensionAPI` methods so events fire and the file stays
    consistent.
    """

    ui: ExtensionUIContext
    mode: ExtensionMode
    has_ui: bool
    cwd: str
    session_manager: Any  # ReadonlySessionManager
    model: dict[str, str] | None  # {"provider": …, "modelId": …}
    thinking_level: str
    signal: asyncio.Event | None  # set when the current run is being aborted

    def is_idle(self) -> bool: ...
    def is_project_trusted(self) -> bool: ...
    def abort(self) -> None: ...
    def has_pending_messages(self) -> bool: ...
    def shutdown(self) -> None: ...
    def get_context_usage(self) -> dict[str, Any] | None: ...
    def compact(self, custom_instructions: str | None = None) -> None: ...
    def get_system_prompt(self) -> str: ...


class ExtensionCommandContext(ExtensionContext, Protocol):
    async def wait_for_idle(self) -> None: ...
    async def new_session(self, parent_session: str | None = None) -> dict[str, Any]: ...
    async def fork(
        self, entry_id: str, position: Literal["before", "at"] = "before"
    ) -> dict[str, Any]: ...
    async def navigate_tree(self, target_id: str, summarize: bool = True) -> dict[str, Any]: ...
    async def switch_session(self, session_path: str) -> dict[str, Any]: ...
    async def reload(self) -> None: ...


@dataclass
class ExtensionError:
    extension_path: str
    event: str
    error: str
