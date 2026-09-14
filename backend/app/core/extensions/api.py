"""ExtensionAPI — the object handed to an extension's ``setup(api)``.

One instance per extension so registrations are attributed to their file
(pi builds one API per extension for the same reason). Registration methods
work at load time; session-facing methods (``send_message``,
``append_entry``, ``set_model`` …) need a live session and raise until the
:class:`AgentSession` has bound itself to the runner.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from .runner import EventBus, ExtensionRunner, LoadedExtension, SessionBridge
from .types import (
    SUPPORTED_EVENTS,
    UNSUPPORTED_EVENTS,
    ExtensionFlag,
    ExtensionShortcut,
    Handler,
    RegisteredCommand,
    SlashCommandInfo,
    ToolDefinition,
    ToolInfo,
)


class ExecResult(dict):
    """``{"stdout", "stderr", "code", "killed"}`` — pi's ExecResult."""


class ExtensionAPI:
    def __init__(self, ext: LoadedExtension, runner: ExtensionRunner) -> None:
        self._ext = ext
        self._runner = runner

    # ── events ─────────────────────────────────────────────────────────

    def on(self, event: str, handler: Handler) -> None:
        if event in UNSUPPORTED_EVENTS:
            raise NotImplementedError(f"'{event}' is not available: {UNSUPPORTED_EVENTS[event]}")
        if event not in SUPPORTED_EVENTS:
            raise ValueError(f"unknown extension event {event!r}")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._ext.handlers.setdefault(event, []).append(handler)

    @property
    def events(self) -> EventBus:
        return self._runner.event_bus

    # ── registrations ──────────────────────────────────────────────────

    def register_tool(self, tool: ToolDefinition | dict[str, Any]) -> None:
        if isinstance(tool, dict):
            tool = ToolDefinition(**tool)
        if not inspect.iscoroutinefunction(tool.execute):
            raise TypeError(f"tool {tool.name!r}: execute must be an async function")
        self._ext.tools[tool.name] = tool

    def register_command(
        self,
        name: str,
        handler: Callable[[str, Any], Awaitable[None] | None] | None = None,
        *,
        description: str = "",
        get_argument_completions: Callable[[str], list[str]] | None = None,
    ) -> None:
        if handler is None:
            raise TypeError("register_command requires a handler")
        self._ext.commands[name] = RegisteredCommand(
            name=name,
            handler=handler,
            description=description,
            source_info={"extensionPath": self._ext.path, "name": self._ext.name},
            get_argument_completions=get_argument_completions,
        )

    def register_shortcut(
        self,
        shortcut: str,
        handler: Callable[[Any], Awaitable[None] | None],
        *,
        description: str = "",
    ) -> None:
        self._ext.shortcuts.append(ExtensionShortcut(shortcut, handler, description))

    def register_flag(
        self,
        name: str,
        *,
        type: Literal["boolean", "string"],  # noqa: A002 — mirrors pi's option name
        description: str = "",
        default: bool | str | None = None,
    ) -> None:
        if default is not None:
            ok = isinstance(default, bool) if type == "boolean" else isinstance(default, str)
            if not ok:
                raise TypeError(f"invalid default for flag {name!r}: expected {type}")
        self._ext.flags[name] = ExtensionFlag(name, type, description, default)
        if default is not None and name not in self._runner.flag_values:
            self._runner.flag_values[name] = default

    def get_flag(self, name: str) -> bool | str | None:
        return self._runner.get_flag(name)

    def register_message_renderer(self, custom_type: str, renderer: Any) -> None:
        self._ext.message_renderers[custom_type] = renderer

    def register_entry_renderer(self, custom_type: str, renderer: Any) -> None:
        self._ext.entry_renderers[custom_type] = renderer

    def register_markdown_transformer(self, transformer: Any) -> None:
        self._ext.markdown_transformers.append(transformer)

    def register_provider(self, name: str, factory: Any) -> None:
        """Register an engine factory (pi: custom provider). ``factory(**opts)``
        must return an object satisfying ``engines.base.Engine``."""
        self._ext.providers[name] = factory
        self._runner.engine_factories[name] = factory

    def unregister_provider(self, name: str) -> None:
        self._ext.providers.pop(name, None)
        self._runner.engine_factories.pop(name, None)

    # ── session-facing ─────────────────────────────────────────────────

    def _bridge(self) -> SessionBridge:
        if self._runner.bridge is None:
            raise RuntimeError(
                "no live session yet: call this from an event handler or command, not from setup()"
            )
        return self._runner.bridge

    def send_message(
        self,
        message: dict[str, Any],
        *,
        trigger_turn: bool = False,
        deliver_as: Literal["steer", "followUp", "nextTurn"] | None = None,
    ) -> None:
        self._bridge().send_custom_message(
            message, trigger_turn=trigger_turn, deliver_as=deliver_as
        )

    def send_user_message(
        self,
        content: Any,
        *,
        deliver_as: Literal["steer", "followUp"] | None = None,
        expand_prompt_templates: bool = True,
    ) -> None:
        self._bridge().send_user_message(
            content, deliver_as=deliver_as, expand_prompt_templates=expand_prompt_templates
        )

    def append_entry(self, custom_type: str, data: Any = None) -> str:
        return self._bridge().append_entry(custom_type, data)

    def set_session_name(self, name: str) -> None:
        self._bridge().set_session_name(name)

    def get_session_name(self) -> str | None:
        return self._bridge().get_session_name()

    def set_label(self, entry_id: str, label: str | None) -> None:
        self._bridge().set_label(entry_id, label)

    def get_active_tools(self) -> list[str]:
        return self._bridge().get_active_tools()

    def get_all_tools(self) -> list[ToolInfo]:
        return self._bridge().get_all_tools()

    def set_active_tools(self, names: list[str]) -> None:
        self._bridge().set_active_tools(names)

    def get_commands(self) -> list[SlashCommandInfo]:
        return self._bridge().get_commands()

    async def set_model(self, model: dict[str, str] | str) -> bool:
        return await self._bridge().set_model(model)

    def get_thinking_level(self) -> str:
        return self._bridge().get_thinking_level()

    def set_thinking_level(self, level: str) -> None:
        self._bridge().set_thinking_level(level)

    # ── utilities ──────────────────────────────────────────────────────

    async def exec(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        killed = False
        try:
            if timeout_s is None:
                out, err = await proc.communicate()
            else:
                async with asyncio.timeout(timeout_s):
                    out, err = await proc.communicate()
        except TimeoutError:
            killed = True
            proc.kill()
            out, err = await proc.communicate()
        return ExecResult(
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            code=proc.returncode,
            killed=killed,
        )
