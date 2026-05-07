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
        # Native-auth mode: don't pass env overrides — the spawned `claude` CLI
        # will use the OAuth token from `claude login` stored under ~/.claude/.
        # Proxied mode: route every call through LiteLLM so spend hits the budget bar.
        env = (
            None
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
    """Map claude-agent-sdk message objects to our unified Event stream."""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                yield Event(type="assistant.text", data={"text": block.text})
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
