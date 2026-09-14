"""ExtensionRunner — dispatches events to loaded extensions with pi's merge
semantics (``core/extensions/runner.ts``).

Handlers run in load order. A handler that raises is recorded as an
:class:`ExtensionError` (and surfaced as an ``extension_error`` event by the
session) and never breaks the others.

Merge rules, per event kind:

- ``tool_call``: the first ``{"block": True}`` wins and stops dispatch;
  in-place edits to ``event["input"]`` persist; any ``terminate`` is kept.
- ``tool_result``: chained — each handler sees the previous handler's
  ``content`` / ``details`` / ``isError``.
- ``input``: ``handled`` stops dispatch; ``transform`` rewrites the text and
  continues to the next handler.
- ``before_agent_start``: messages from every handler are collected; the
  last ``systemPrompt`` wins.
- ``session_before_*``: the first ``{"cancel": True}`` wins.
- ``project_trust``: the first decision that is not ``undecided`` wins.
- ``resources_discover``: path lists are concatenated.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import (
    ExtensionError,
    ExtensionFlag,
    ExtensionShortcut,
    ExtensionUIContext,
    Handler,
    NoUIBridge,
    RegisteredCommand,
    SlashCommandInfo,
    ToolDefinition,
    ToolInfo,
)


class EventBus:
    """pi's ``api.events`` — inter-extension pub/sub, nothing more."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[..., Any]]] = {}

    def on(self, name: str, fn: Callable[..., Any]) -> Callable[[], None]:
        self._listeners.setdefault(name, []).append(fn)

        def off() -> None:
            self.off(name, fn)

        return off

    def off(self, name: str, fn: Callable[..., Any]) -> None:
        listeners = self._listeners.get(name)
        if listeners and fn in listeners:
            listeners.remove(fn)

    def emit(self, name: str, *args: Any) -> None:
        for fn in list(self._listeners.get(name, [])):
            fn(*args)


@dataclass
class LoadedExtension:
    path: str
    name: str
    handlers: dict[str, list[Handler]] = field(default_factory=dict)
    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    flags: dict[str, ExtensionFlag] = field(default_factory=dict)
    shortcuts: list[ExtensionShortcut] = field(default_factory=list)
    message_renderers: dict[str, Any] = field(default_factory=dict)
    entry_renderers: dict[str, Any] = field(default_factory=dict)
    markdown_transformers: list[Any] = field(default_factory=list)
    providers: dict[str, Any] = field(default_factory=dict)


class SessionBridge(Protocol):
    """What :class:`ExtensionAPI` needs from the live session. The
    :class:`AgentSession` implements this and binds itself on start."""

    def send_custom_message(
        self, message: dict[str, Any], *, trigger_turn: bool, deliver_as: str | None
    ) -> None: ...
    def send_user_message(
        self, content: Any, *, deliver_as: str | None, expand_prompt_templates: bool
    ) -> None: ...
    def append_entry(self, custom_type: str, data: Any) -> str: ...
    def set_session_name(self, name: str) -> None: ...
    def get_session_name(self) -> str | None: ...
    def set_label(self, entry_id: str, label: str | None) -> None: ...
    def get_active_tools(self) -> list[str]: ...
    def set_active_tools(self, names: list[str]) -> None: ...
    def get_all_tools(self) -> list[ToolInfo]: ...
    def get_commands(self) -> list[SlashCommandInfo]: ...
    async def set_model(self, model: dict[str, str] | str) -> bool: ...
    def get_thinking_level(self) -> str: ...
    def set_thinking_level(self, level: str) -> None: ...


class SimpleExtensionContext:
    """Minimal concrete :class:`ExtensionContext` — used by print/json mode
    and tests. :class:`AgentSession` builds the real one on top of it."""

    def __init__(
        self,
        *,
        cwd: str,
        mode: str = "print",
        ui_bridge: Any = None,
        session_manager: Any = None,
        model: dict[str, str] | None = None,
        thinking_level: str = "off",
        project_trusted: bool = False,
    ) -> None:
        bridge = ui_bridge if ui_bridge is not None else NoUIBridge()
        self.ui = ExtensionUIContext(bridge)
        self.mode = mode
        self.has_ui = bool(getattr(bridge, "has_ui", False))
        self.cwd = cwd
        self.session_manager = session_manager
        self.model = model
        self.thinking_level = thinking_level
        self.signal: asyncio.Event | None = None
        self._project_trusted = project_trusted

    def is_idle(self) -> bool:
        return True

    def is_project_trusted(self) -> bool:
        return self._project_trusted

    def abort(self) -> None:
        return None

    def has_pending_messages(self) -> bool:
        return False

    def shutdown(self) -> None:
        return None

    def get_context_usage(self) -> dict[str, Any] | None:
        return None

    def compact(self, custom_instructions: str | None = None) -> None:
        return None

    def get_system_prompt(self) -> str:
        return ""


class ExtensionRunner:
    def __init__(self, on_error: Callable[[ExtensionError], None] | None = None) -> None:
        self.extensions: list[LoadedExtension] = []
        self.errors: list[ExtensionError] = []
        self.flag_values: dict[str, bool | str] = {}
        self.engine_factories: dict[str, Any] = {}
        self.event_bus = EventBus()
        self.bridge: SessionBridge | None = None
        self.on_error = on_error

    # ── wiring ─────────────────────────────────────────────────────────

    def bind(self, bridge: SessionBridge | None) -> None:
        self.bridge = bridge

    def add(self, ext: LoadedExtension) -> None:
        self.extensions.append(ext)
        for name, flag in ext.flags.items():
            if flag.default is not None and name not in self.flag_values:
                self.flag_values[name] = flag.default
        for name, factory in ext.providers.items():
            self.engine_factories[name] = factory

    def record_error(self, path: str, event: str, error: BaseException | str) -> ExtensionError:
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        err = ExtensionError(extension_path=path, event=event, error=text)
        self.errors.append(err)
        if self.on_error is not None:
            try:
                self.on_error(err)
            except Exception:  # noqa: BLE001 — an error reporter must not raise
                pass
        return err

    # ── generic dispatch ───────────────────────────────────────────────

    def has_handlers(self, event_name: str) -> bool:
        return any(ext.handlers.get(event_name) for ext in self.extensions)

    async def _dispatch(
        self, event_name: str, event: dict[str, Any], ctx: Any
    ) -> AsyncIterator[tuple[LoadedExtension, dict[str, Any] | None]]:
        for ext in list(self.extensions):
            for handler in list(ext.handlers.get(event_name, [])):
                try:
                    result = handler(event, ctx)
                    if inspect.isawaitable(result):
                        result = await result
                except Exception as exc:  # noqa: BLE001 — isolate extension failures
                    self.record_error(ext.path, event_name, exc)
                    continue
                yield ext, (result if isinstance(result, dict) else None)

    async def emit(self, event: dict[str, Any], ctx: Any) -> list[dict[str, Any]]:
        """Fire-and-collect for notification-style events."""
        results: list[dict[str, Any]] = []
        async for _ext, result in self._dispatch(event["type"], event, ctx):
            if result is not None:
                results.append(result)
        return results

    # ── typed emitters with merge semantics ────────────────────────────

    async def emit_tool_call(
        self, tool_name: str, tool_call_id: str, tool_input: dict[str, Any], ctx: Any
    ) -> dict[str, Any] | None:
        event = {
            "type": "tool_call",
            "toolName": tool_name,
            "toolCallId": tool_call_id,
            "input": tool_input,
        }
        terminate = False
        async for _ext, result in self._dispatch("tool_call", event, ctx):
            if not result:
                continue
            if result.get("terminate"):
                terminate = True
            if result.get("block"):
                out: dict[str, Any] = {"block": True, "reason": result.get("reason") or "blocked"}
                if terminate:
                    out["terminate"] = True
                return out
        return {"terminate": True} if terminate else None

    async def emit_tool_result(
        self,
        tool_name: str,
        tool_call_id: str,
        tool_input: dict[str, Any],
        content: list[dict[str, Any]],
        details: Any,
        is_error: bool,
        ctx: Any,
    ) -> dict[str, Any] | None:
        event: dict[str, Any] = {
            "type": "tool_result",
            "toolName": tool_name,
            "toolCallId": tool_call_id,
            "input": tool_input,
            "content": content,
            "details": details,
            "isError": is_error,
        }
        changed = False
        usage: dict[str, Any] | None = None
        async for _ext, result in self._dispatch("tool_result", event, ctx):
            if not result:
                continue
            for key in ("content", "details", "isError"):
                if key in result:
                    event[key] = result[key]
                    changed = True
            if "usage" in result:
                usage = result["usage"]
                changed = True
        if not changed:
            return None
        out = {
            "content": event["content"],
            "details": event["details"],
            "isError": event["isError"],
        }
        if usage is not None:
            out["usage"] = usage
        return out

    async def emit_before_agent_start(
        self,
        prompt: str,
        images: list[dict[str, Any]] | None,
        system_prompt: str,
        ctx: Any,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "before_agent_start",
            "prompt": prompt,
            "systemPrompt": system_prompt,
        }
        if images:
            event["images"] = images
        messages: list[dict[str, Any]] = []
        new_prompt: str | None = None
        async for _ext, result in self._dispatch("before_agent_start", event, ctx):
            if not result:
                continue
            if result.get("message"):
                messages.append(result["message"])
            if isinstance(result.get("systemPrompt"), str):
                new_prompt = result["systemPrompt"]
        return {"messages": messages, "systemPrompt": new_prompt}

    async def emit_input(
        self,
        text: str,
        images: list[dict[str, Any]] | None,
        source: str,
        streaming_behavior: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {"type": "input", "text": text, "source": source}
        if images:
            event["images"] = images
        if streaming_behavior:
            event["streamingBehavior"] = streaming_behavior
        current_text, current_images = text, images
        async for _ext, result in self._dispatch("input", event, ctx):
            if not result:
                continue
            action = result.get("action")
            if action == "handled":
                return {"action": "handled"}
            if action == "transform":
                current_text = result.get("text", current_text)
                if "images" in result:
                    current_images = result["images"]
                event["text"] = current_text
                if current_images:
                    event["images"] = current_images
        return {"action": "continue", "text": current_text, "images": current_images}

    async def emit_project_trust(self, cwd: str, ctx: Any) -> dict[str, Any] | None:
        event = {"type": "project_trust", "cwd": cwd}
        async for _ext, result in self._dispatch("project_trust", event, ctx):
            if result and result.get("trusted") in ("yes", "no"):
                return result
        return None

    async def emit_resources_discover(
        self, cwd: str, reason: str, ctx: Any
    ) -> dict[str, list[str]]:
        event = {"type": "resources_discover", "cwd": cwd, "reason": reason}
        merged: dict[str, list[str]] = {"skillPaths": [], "promptPaths": [], "themePaths": []}
        async for _ext, result in self._dispatch("resources_discover", event, ctx):
            if not result:
                continue
            for key in merged:
                merged[key].extend(result.get(key) or [])
        return merged

    async def emit_cancellable(self, event: dict[str, Any], ctx: Any) -> bool:
        """``session_before_switch`` / ``_fork`` / ``_tree``: True if cancelled."""
        async for _ext, result in self._dispatch(event["type"], event, ctx):
            if result and result.get("cancel"):
                return True
        return False

    async def emit_session_before_compact(
        self, event: dict[str, Any], ctx: Any
    ) -> dict[str, Any] | None:
        async for _ext, result in self._dispatch("session_before_compact", event, ctx):
            if not result:
                continue
            if result.get("cancel"):
                return {"cancel": True}
            if result.get("compaction"):
                return {"compaction": result["compaction"]}
        return None

    # ── registries ─────────────────────────────────────────────────────

    def all_tools(self) -> list[ToolDefinition]:
        return [tool for ext in self.extensions for tool in ext.tools.values()]

    def get_tool(self, name: str) -> tuple[LoadedExtension, ToolDefinition] | None:
        for ext in self.extensions:
            if name in ext.tools:
                return ext, ext.tools[name]
        return None

    def all_commands(self) -> list[RegisteredCommand]:
        return [cmd for ext in self.extensions for cmd in ext.commands.values()]

    def get_command(self, name: str) -> RegisteredCommand | None:
        for ext in self.extensions:
            if name in ext.commands:
                return ext.commands[name]
        return None

    def command_infos(self) -> list[SlashCommandInfo]:
        return [
            {
                "name": cmd.name,
                "description": cmd.description,
                "source": "extension",
                "sourceInfo": cmd.source_info,
            }
            for cmd in self.all_commands()
        ]

    def get_flag(self, name: str) -> bool | str | None:
        return self.flag_values.get(name)
