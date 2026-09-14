"""FakeEngine — a scripted engine for tests and for driving the session and
RPC layers without a vendor binary.

A script is a list of *runs*; a run is a list of *turns*; a turn is a dict:

    {"text": "…", "thinking": "…", "tools": [{"name", "args", "result", "is_error"}]}

Each ``prompt()`` consumes the next run (or echoes the prompt when the
script is exhausted) and emits exactly the event sequence a real engine
does, calling ``hooks.before_tool`` / ``hooks.after_tool`` around every
tool so hook wiring can be tested end to end.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..extensions.types import ToolDefinition
from ..messages import now_ms
from .base import (
    EngineCapabilities,
    EngineConfig,
    EngineEvent,
    EngineHooks,
    NotSupportedError,
    ToolExecutor,
)
from .stream import AssistantMessageBuilder, usage_from_counts

Turn = dict[str, Any]
Run = list[Turn]


class FakeEngine:
    name = "fake"

    def __init__(
        self,
        runs: list[Run] | None = None,
        *,
        model: str = "fake-1",
        capabilities: EngineCapabilities | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.runs: list[Run] = list(runs or [])
        self._model = model
        self._capabilities = capabilities or EngineCapabilities(
            steer=True,
            follow_up=True,
            fork=True,
            compact=True,
            tool_input_rewrite=True,
            custom_tools=True,
            thinking_levels=("off", "low", "medium", "high"),
        )
        self._delay = delay_s
        self.config: EngineConfig | None = None
        self._session_id: str | None = None
        self._interrupt = asyncio.Event()
        self.prompts: list[dict[str, Any]] = []
        self.steers: list[dict[str, Any]] = []
        self.follow_ups: list[dict[str, Any]] = []
        self.compactions: list[str | None] = []
        self.mounted_tools: list[ToolDefinition] = []
        self.executor: ToolExecutor | None = None
        self.thinking_level = "off"
        self.closed = False
        # Set to a vendor rate-limit payload to exercise the quota path.
        self.rate_limit: dict[str, Any] | None = None

    # ── protocol ───────────────────────────────────────────────────────

    @property
    def capabilities(self) -> EngineCapabilities:
        return self._capabilities

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._model

    async def start(self, config: EngineConfig) -> None:
        self.config = config
        self._model = config.model or self._model
        self.thinking_level = config.thinking_level
        self._session_id = config.session_id or f"fake-{uuid.uuid4().hex[:8]}"

    async def prompt(
        self, message: dict[str, Any], hooks: EngineHooks
    ) -> AsyncIterator[EngineEvent]:
        self.prompts.append(message)
        self._interrupt.clear()
        if hooks.before_prompt is not None:
            await hooks.before_prompt(_text_of(message))
        run = self.runs.pop(0) if self.runs else [{"text": f"echo: {_text_of(message)}"}]
        produced: list[dict[str, Any]] = []
        yield {"type": "engine_session", "engine": self.name, "sessionId": self._session_id}
        if self.rate_limit is not None:
            yield {"type": "rate_limit", "info": self.rate_limit}
        yield {"type": "agent_start"}
        aborted = False
        for turn in run:
            if self._interrupt.is_set():
                aborted = True
                break
            yield {"type": "turn_start"}
            builder = AssistantMessageBuilder(api="fake", provider="fake", model=self._model or "")
            yield {"type": "message_start", "message": builder.message}
            for delta in _chunks(turn.get("thinking", "")):
                for ev in builder.thinking_delta(delta):
                    yield {
                        "type": "message_update",
                        "message": builder.message,
                        "assistantMessageEvent": ev,
                    }
            for delta in _chunks(turn.get("text", "")):
                if self._delay:
                    await asyncio.sleep(self._delay)
                if self._interrupt.is_set():
                    break
                for ev in builder.text_delta(delta):
                    yield {
                        "type": "message_update",
                        "message": builder.message,
                        "assistantMessageEvent": ev,
                    }
            tools = list(turn.get("tools", []))
            if self._interrupt.is_set():
                for ev in builder.finish("aborted"):
                    yield {
                        "type": "message_update",
                        "message": builder.message,
                        "assistantMessageEvent": ev,
                    }
                yield {"type": "message_end", "message": builder.message}
                produced.append(builder.message)
                yield {"type": "turn_end", "message": builder.message, "toolResults": []}
                aborted = True
                break
            for i, tool in enumerate(tools):
                call_id = tool.get("id") or f"call_{uuid.uuid4().hex[:6]}_{i}"
                tool["id"] = call_id
                for ev in builder.tool_call(call_id, tool["name"], dict(tool.get("args", {}))):
                    yield {
                        "type": "message_update",
                        "message": builder.message,
                        "assistantMessageEvent": ev,
                    }
            usage = usage_from_counts(
                input_tokens=10, output_tokens=len(turn.get("text", "")) // 4 + 1
            )
            for ev in builder.finish("toolUse" if tools else "stop", usage):
                yield {
                    "type": "message_update",
                    "message": builder.message,
                    "assistantMessageEvent": ev,
                }
            yield {"type": "message_end", "message": builder.message}
            produced.append(builder.message)

            tool_results: list[dict[str, Any]] = []
            for tool in tools:
                call_id, name, args = tool["id"], tool["name"], dict(tool.get("args", {}))
                decision = None
                if hooks.before_tool is not None:
                    decision = await hooks.before_tool(name, call_id, args)
                if (
                    decision
                    and decision.get("updated_input")
                    and self._capabilities.tool_input_rewrite
                ):
                    args = dict(decision["updated_input"])
                yield {
                    "type": "tool_execution_start",
                    "toolCallId": call_id,
                    "toolName": name,
                    "args": args,
                }
                if decision and decision.get("block"):
                    content = [{"type": "text", "text": f"blocked: {decision.get('reason', '')}"}]
                    is_error = True
                elif self.executor is not None and any(t.name == name for t in self.mounted_tools):
                    result = await self.executor(name, call_id, args)
                    content, is_error = list(result.get("content", [])), bool(result.get("isError"))
                else:
                    content = [{"type": "text", "text": str(tool.get("result", ""))}]
                    is_error = bool(tool.get("is_error", False))
                if hooks.after_tool is not None and not (decision and decision.get("block")):
                    override = await hooks.after_tool(name, call_id, args, content, is_error)
                    if override:
                        content = override.get("content", content)
                        is_error = bool(override.get("is_error", is_error))
                yield {
                    "type": "tool_execution_end",
                    "toolCallId": call_id,
                    "toolName": name,
                    "result": {"content": content, "details": {}},
                    "isError": is_error,
                }
                tr = {
                    "role": "toolResult",
                    "toolCallId": call_id,
                    "toolName": name,
                    "content": content,
                    "isError": is_error,
                    "timestamp": now_ms(),
                }
                tool_results.append(tr)
                produced.append(tr)
            yield {"type": "turn_end", "message": builder.message, "toolResults": tool_results}
            # A steer arrives before the next model call, as in pi.
            while self.steers:
                produced.append(self.steers.pop(0))
        if hooks.on_stop is not None:
            await hooks.on_stop()
        yield {"type": "agent_end", "messages": produced, "aborted": aborted}

    async def follow_up(self, message: dict[str, Any]) -> None:
        self.follow_ups.append(message)

    async def steer(self, message: dict[str, Any]) -> bool:
        if not self._capabilities.steer:
            return False
        self.steers.append(message)
        return True

    async def interrupt(self) -> None:
        self._interrupt.set()

    async def fork(self) -> str:
        if not self._capabilities.fork:
            raise NotSupportedError("fake engine configured without fork")
        self._session_id = f"fake-{uuid.uuid4().hex[:8]}"
        return self._session_id

    async def set_model(self, model: str) -> None:
        self._model = model

    async def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    async def compact(self, custom_instructions: str | None = None) -> None:
        if not self._capabilities.compact:
            raise NotSupportedError("fake engine configured without compact")
        self.compactions.append(custom_instructions)

    async def mount_tools(self, tools: list[ToolDefinition], executor: ToolExecutor) -> None:
        self.mounted_tools = list(tools)
        self.executor = executor

    async def close(self) -> None:
        self.closed = True


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


def _chunks(text: str, size: int = 5) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
