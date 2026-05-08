"""Claude Code provider — wraps `claude-agent-sdk` and routes through LiteLLM.

The SDK reads ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY from the env it spawns the
underlying CLI with, so we set those to point at the LiteLLM proxy. This means
every Claude turn flows through LiteLLM and is counted against the same budget
as OpenCode turns.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..config import get_settings
from .base import Event, RunContext


class ClaudeProvider:
    name = "claude"

    def __init__(self) -> None:
        self._settings = get_settings()

    async def open_session(self, ctx: RunContext) -> str:
        # claude-agent-sdk's `query()` is stateless per call — there is no upstream
        # session id to preserve. We treat each WebSocket turn as a fresh query and
        # rebuild context from message history when we extend to multi-turn.
        return ctx.upstream_session_id or ""

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        # Native-auth mode: pass an empty env override — the spawned `claude`
        # CLI inherits the process's env, picking up the OAuth token from
        # `claude login` (stored in ~/.claude/ on Linux, the macOS keychain on
        # Darwin).
        # Proxied mode: route every call through LiteLLM so spend hits the
        # budget bar; the SDK's virtual key authenticates the proxy.
        env: dict[str, str] = (
            {}
            if self._settings.claude_use_native_auth
            else {
                "ANTHROPIC_BASE_URL": self._settings.litellm_api_base,
                "ANTHROPIC_API_KEY": self._settings.effective_litellm_key,
            }
        )
        options = ClaudeAgentOptions(
            model=ctx.model,
            cwd=ctx.cwd,
            system_prompt=ctx.system_prompt,
            permission_mode="acceptEdits",
            include_partial_messages=True,  # surface token-level deltas to the UI
            env=env,
        )

        try:
            async for message in query(prompt=ctx.prompt, options=options):
                async for ev in _translate(message):
                    yield ev
        except Exception as exc:  # surface to UI rather than crashing the WS
            yield Event(type="error", data={"message": str(exc), "provider": self.name})

    async def aclose(self) -> None:
        return None


async def _translate(message: Any) -> AsyncIterator[Event]:
    """Map claude-agent-sdk message objects to our unified Event stream.

    With ``include_partial_messages=True`` the SDK emits raw Anthropic streaming
    events as ``StreamEvent`` objects *and* a final ``AssistantMessage`` with
    the consolidated content. We surface deltas from ``StreamEvent`` (so the UI
    streams live) and emit only ``ToolUse`` / ``ToolResult`` blocks from the
    final ``AssistantMessage`` — its ``TextBlock``s would otherwise double up
    on top of the deltas we already streamed.
    """
    if isinstance(message, StreamEvent):
        ev = message.event or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {}) or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    yield Event(type="assistant.text", data={"text": text})
        # Tool-use blocks (and their input_json_delta accumulations) are surfaced
        # from the final AssistantMessage where the input is fully formed.
        return

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                # Already streamed via StreamEvent; skip to avoid duplication.
                continue
            elif isinstance(block, ToolUseBlock):
                yield Event(
                    type="assistant.tool_use",
                    data={"id": block.id, "name": block.name, "input": block.input},
                )
            elif isinstance(block, ToolResultBlock):
                yield Event(
                    type="tool.result",
                    data={
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": getattr(block, "is_error", False),
                    },
                )
    elif isinstance(message, UserMessage):
        # Tool results sometimes arrive as UserMessage with ToolResultBlock content.
        for block in getattr(message, "content", []) or []:
            if isinstance(block, ToolResultBlock):
                yield Event(
                    type="tool.result",
                    data={
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": getattr(block, "is_error", False),
                    },
                )
    elif isinstance(message, ResultMessage):
        yield Event(
            type="assistant.done",
            data={
                "cost_usd": getattr(message, "total_cost_usd", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "num_turns": getattr(message, "num_turns", None),
            },
        )
    elif isinstance(message, SystemMessage):
        # System init/notice messages — optional to surface; skip for now.
        return
