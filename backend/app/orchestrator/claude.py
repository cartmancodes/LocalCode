"""Claude Code provider — wraps `claude-agent-sdk`.

The SDK spawns the host's `claude` CLI, which reads its OAuth token from
~/.claude/ (Linux) or the macOS keychain (Darwin). The token is persisted by
`claude login` and auto-refreshes; the orchestrator never sees it.

``run()`` is a two-producer merge rather than a straight ``async for`` over
``query()``, and that is not structural taste: the permission callback
(``can_use_tool``) emits the approval card from *inside* the SDK's message
loop and then blocks there waiting for the answer. A single-iterator run()
would therefore hold the card in a frame nobody is draining — the user would
be asked a question they cannot see. The sink is drained by its own task and
merged with the translated message stream, exactly as ``orchestrator.py``
does for its dispatch tools.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from ..config import get_settings
from .approvals import EventSink, build_can_use_tool
from .base import Event, RunContext
from .permissions import normalize_permission_mode, policy_for_role, resolve_roots

logger = logging.getLogger(__name__)

# Sentinel for the merged-event queue — same shape as orchestrator.py's.
_DONE = object()


class ClaudeProvider:
    name = "claude"

    async def open_session(self, ctx: RunContext) -> str:
        # Claude Code creates the session lazily when query() runs. If LocalCode
        # already captured that id, hand it back so the runner can resume it.
        return ctx.upstream_session_id or ""

    async def close_session(self, session_id: str) -> None:
        # Nothing persistent per session yet — ``query()`` spawns and reaps a
        # CLI per turn. Task 4 replaces that with a long-lived
        # ``ClaudeSDKClient`` and this is where it gets disconnected.
        return None

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        settings = get_settings()
        # The role policy decides what this turn may touch; the mode only
        # decides how often a human is asked. An unknown mode folds to
        # "default" (ask) — never to acceptEdits, which is what the deleted
        # fallback here used to do on every headless fleet step.
        roots = resolve_roots(ctx.cwd, ctx.additional_dirs)
        policy = policy_for_role(ctx.role, roots, settings.denied_path_list())
        mode = normalize_permission_mode(
            ctx.permission_mode, allow_bypass=settings.allow_bypass_permissions
        )
        option_extras: dict[str, Any] = {}
        allowed_tools = ctx.extras.get("claude_allowed_tools")
        if isinstance(allowed_tools, list):
            option_extras["allowed_tools"] = [str(tool) for tool in allowed_tools]
        elif ctx.extras.get("claude_no_tools"):
            option_extras["allowed_tools"] = []
        if ctx.extras.get("claude_disable_settings") or ctx.extras.get("claude_no_tools"):
            option_extras["setting_sources"] = []
        if ctx.extras.get("claude_disable_skills") or ctx.extras.get("claude_no_tools"):
            option_extras["skills"] = []
        # Union, not replacement: the ad-hoc extras list (Task 9 removes it)
        # and the role policy's denials are both real, and dropping either one
        # re-grants a tool someone deliberately took away.
        disallowed_tools = list(
            dict.fromkeys(
                [
                    *(str(t) for t in (ctx.extras.get("claude_disallowed_tools") or [])),
                    *policy.deny_tools,
                ]
            )
        )

        # The callback's card and the turn's messages are two producers; the
        # sink is the callback's half. See the module docstring.
        sink = EventSink()
        can_use_tool = build_can_use_tool(
            policy=policy,
            mode=mode,
            sink=sink,
            approval_channel=ctx.approval_channel,
            timeout_s=settings.tool_approval_timeout_s,
        )

        options = ClaudeAgentOptions(
            model=ctx.model,
            cwd=ctx.cwd,
            # Extra paths the spawned `claude` CLI may read/write. The SDK
            # restricts tools to `cwd` by default; this opens up sibling repos.
            add_dirs=list(ctx.additional_dirs or []),
            system_prompt=ctx.system_prompt,
            permission_mode=mode,
            can_use_tool=can_use_tool,
            resume=ctx.upstream_session_id,
            disallowed_tools=disallowed_tools,
            include_partial_messages=True,  # surface token-level deltas to the UI
            **option_extras,
        )

        merged: asyncio.Queue[Event | object] = asyncio.Queue()

        async def _pump_messages() -> None:
            """Drain the SDK's message iterator → translate → merged queue.

            Task 4 swaps ``query()`` here for a persistent ``ClaudeSDKClient``;
            nothing outside this coroutine knows where messages come from.
            """
            try:
                async for message in query(prompt=ctx.prompt, options=options):
                    async for ev in _translate(message):
                        await merged.put(ev)
            except Exception as exc:  # surface to UI rather than crashing the WS
                logger.exception("claude provider pump raised")
                await merged.put(
                    Event(
                        type="error",
                        data={"message": str(exc) or repr(exc), "provider": self.name},
                    )
                )
            finally:
                # A permission callback can still be mid-flight when the
                # message stream ends (a denied tool, then the final
                # ResultMessage): close the sink so its pump drains the tail
                # and exits instead of waiting forever.
                await sink.close()

        async def _pump_sink() -> None:
            """Drain the approval EventSink → merged queue."""
            while True:
                ev = await sink.get()
                if ev is None:
                    break
                await merged.put(ev)

        msg_task = asyncio.create_task(_pump_messages())
        sink_task = asyncio.create_task(_pump_sink())

        async def _seal_when_drained() -> None:
            await asyncio.gather(msg_task, sink_task)
            await merged.put(_DONE)

        seal_task = asyncio.create_task(_seal_when_drained())

        try:
            while True:
                ev = await merged.get()
                if ev is _DONE:
                    break
                yield ev  # type: ignore[misc]
        finally:
            # On consumer exit (WS close, cancellation, an exception in the
            # caller) cancel the producers so neither the CLI subprocess nor a
            # callback blocked on an approval outlives the turn.
            for t in (msg_task, sink_task, seal_task):
                if not t.done():
                    t.cancel()
            for t in (msg_task, sink_task, seal_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

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
                "upstream_session_id": getattr(message, "session_id", None),
            },
        )
    elif isinstance(message, SystemMessage):
        # System init/notice messages — optional to surface; skip for now.
        return
