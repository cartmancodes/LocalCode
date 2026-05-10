"""OrchestratorAgent — the LLM-driven dispatcher at the top of the fleet.

This is the Tier-4 layer. The orchestrator is itself a claude-agent-sdk
session running its own ReAct loop. Its only tool is ``dispatch_subagent``
(see ``dispatch.py``). It analyses the user's request, decides which
subagents to dispatch in what order, reasons about their results, and
re-dispatches as needed — exactly mirroring how Claude Code's main session
uses ``Task`` to delegate to specialised subagents.

Streaming model: two event sources flow into one outgoing channel:

  1. Orchestrator's own narrative text (its reasoning, decisions, final
     summary) — read straight from the SDK message stream.
  2. Subagent events emitted from inside the ``dispatch_subagent`` tool's
     body — pushed to a per-turn ``EventSink`` and drained concurrently.

We merge both into a single ``asyncio.Queue`` so the WS sees a clean
interleaved transcript: orchestrator says "Let me start with the planner"
→ planner card (tool_use → heartbeats → tool_result) → orchestrator
reads the plan and says "Now dispatching the coder" → coder card → ...
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

from .agent_def import AgentDef, render_registry_for_prompt
from .base import Event, RunContext
from .dispatch import EventSink, build_dispatch_mcp


logger = logging.getLogger(__name__)


# Default model / bound for the orchestrator. The orchestrator does
# meta-reasoning (which agent runs when) — sonnet is plenty smart for that
# and ~5x cheaper than opus per turn. The actual heavy lifting (planning,
# coding) is done by dispatched subagents which can independently use opus
# or whatever model their AgentDef pins.
DEFAULT_ORCHESTRATOR_MODEL = "claude-sonnet-4-6"

# Turn budget for the orchestrator's own ReAct loop. One "turn" = one
# round-trip with the model. A typical workflow needs ~6 turns: dispatch
# planner, read plan, dispatch coder, read result, dispatch reviewer, read
# verdict, dispatch tester, read verdict, summarise. 30 leaves headroom for
# NACK retry chains.
DEFAULT_ORCHESTRATOR_MAX_TURNS = 30


# Sentinel for the merged-event queue.
_DONE = object()


ORCHESTRATOR_SYSTEM = """\
You are the Orchestrator in a multi-agent coding fleet. You DO NOT write
code, edit files, or run commands yourself. Your sole job is to delegate
to specialised subagents via the `dispatch_subagent` tool and synthesise
their results.

# Available subagents

{registry}

You call them by passing `name` (one of the keys above) and `prompt` (a
focused, self-contained task description for that agent).

# Workflow rules

For a non-trivial task:

  1. Dispatch `planner` first. The planner produces a Markdown plan
     committed to disk under `.localcode/plans/`. Pass the user's
     original request as the prompt.
  2. Dispatch `coder` with the FULL plan text in the prompt + a clear
     instruction to execute it task-by-task with file/bash tools.
  3. If `reviewer` is registered, dispatch it after the coder. Read its
     last line:
        - `LGTM` → proceed to the tester (if registered).
        - `NACK: <reason>` → re-dispatch the coder with the reviewer
          feedback prepended and a "fix the issue and continue" preface.
          Then re-dispatch the reviewer. Bound: at most 3 NACK retries.
  4. If `tester` is registered, dispatch it after the reviewer LGTM (or
     after the coder if no reviewer). Read its last line:
        - `LGTM` → workflow ships; emit a final summary.
        - `NACK_CODE: <reason>` → re-dispatch coder + reviewer + tester.
        - `NACK_TESTS: <reason>` → re-dispatch tester only with the
          feedback prepended.

For a trivial 1-line task (e.g. "rename foo to bar"): skip the planner;
dispatch the coder directly.

# Verifying that the coder actually did the work

When a coder returns, check its tool result for ACTUAL changes:
  - It should mention specific files written and commands run.
  - If it returns only narrative ("Starting with the scaffold…"), that
    is a stall. Re-dispatch with: "Your previous response did not call
    file-edit or bash tools. You MUST use the available tools to make
    real changes. Execute the plan now."

# Output budget

You have a hard cap of {max_turns} turns. If you hit it, emit a final
summary describing the state of the work — don't try to start new
dispatches.

# Style

- Keep your own narrative terse. Most useful output comes from the
  subagents.
- Don't restate plans or code; the user can see the agent cards.
- When all gates are green, end with a one-paragraph summary of what
  was built and where to find it.
"""


class OrchestratorAgent:
    """Top-level fleet entry. Replaces the legacy fixed pipeline when a
    fleet config opts into ``orchestrator_mode``.
    """

    def __init__(
        self,
        *,
        registry: dict[str, AgentDef],
        run_step_fn: Any,  # FleetProvider._run_step_with_role bound method
        model: str = DEFAULT_ORCHESTRATOR_MODEL,
        max_turns: int = DEFAULT_ORCHESTRATOR_MAX_TURNS,
    ) -> None:
        self.registry = registry
        self._run_step_fn = run_step_fn
        self.model = model
        self.max_turns = max_turns

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        """Drive one user-prompt → assistant-response turn through the
        orchestrator + dispatched subagents.

        Yields a unified Event stream merging the orchestrator's own text
        with per-subagent tool_use / tool_result / heartbeat events.
        """
        sink = EventSink()
        mcp_server, allowed_tool = build_dispatch_mcp(
            registry=self.registry,
            ctx=ctx,
            sink=sink,
            run_step_fn=self._run_step_fn,
        )

        system_prompt = ORCHESTRATOR_SYSTEM.format(
            registry=render_registry_for_prompt(self.registry),
            max_turns=self.max_turns,
        )

        options = ClaudeAgentOptions(
            model=self.model,
            cwd=ctx.cwd,
            add_dirs=list(ctx.additional_dirs or []),
            system_prompt=system_prompt,
            mcp_servers={"fleet_dispatch": mcp_server},
            allowed_tools=[allowed_tool],
            permission_mode="acceptEdits",
            max_turns=self.max_turns,
            include_partial_messages=True,
        )

        # Merged channel: orchestrator's own events + dispatch sink events.
        merged: asyncio.Queue[Event | object] = asyncio.Queue()

        async def _pump_orchestrator() -> None:
            """Drain the SDK's message iterator → translate → merged queue."""
            try:
                async for message in query(prompt=ctx.prompt, options=options):
                    async for ev in _translate_orchestrator_message(
                        message, dispatch_tool_name=allowed_tool
                    ):
                        await merged.put(ev)
            except Exception as exc:  # noqa: BLE001
                logger.exception("orchestrator pump raised")
                await merged.put(
                    Event(type="error", data={"message": str(exc) or repr(exc)})
                )
            finally:
                # The dispatch tool may still be in flight when we hit the
                # final ResultMessage — close the sink so its pump can drain
                # any tail events and exit cleanly.
                await sink.close()

        async def _pump_sink() -> None:
            """Drain the EventSink → merged queue."""
            while True:
                ev = await sink.get()
                if ev is None:
                    break
                await merged.put(ev)

        # Two producers, one consumer (this generator).
        orch_task = asyncio.create_task(_pump_orchestrator())
        sink_task = asyncio.create_task(_pump_sink())

        # When BOTH producers finish, push the sentinel so the consumer
        # loop knows to exit. We wrap this in its own task so the consumer
        # isn't blocked behind it.
        async def _seal_when_drained() -> None:
            await asyncio.gather(orch_task, sink_task)
            await merged.put(_DONE)

        seal_task = asyncio.create_task(_seal_when_drained())

        try:
            while True:
                ev = await merged.get()
                if ev is _DONE:
                    break
                yield ev  # type: ignore[misc]
        finally:
            # On consumer exit (WS close, exception), cancel everything so
            # tasks don't leak. The dispatch tool's _run_step_with_role
            # cancellation path handles its own cleanup.
            for t in (orch_task, sink_task, seal_task):
                if not t.done():
                    t.cancel()
            for t in (orch_task, sink_task, seal_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def _translate_orchestrator_message(
    message: Any,
    *,
    dispatch_tool_name: str,
) -> AsyncIterator[Event]:
    """Like ``claude.py:_translate`` but suppresses the dispatch tool's own
    tool_use / tool_result events.

    The orchestrator's call to ``dispatch_subagent`` is an implementation
    detail — what the user wants to see is the per-role tool_use cards
    that the dispatch tool's body emits via the EventSink. If we forwarded
    the raw ``mcp__fleet_dispatch__dispatch_subagent`` tool_use, the chat
    would show *both* cards for the same call.
    """
    if isinstance(message, StreamEvent):
        ev = message.event or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {}) or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    yield Event(type="assistant.text", data={"text": text})
        return

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                # Already streamed via StreamEvent.
                continue
            if isinstance(block, ToolUseBlock):
                if block.name == dispatch_tool_name:
                    # Hide the dispatch shim — sub-agent events from the
                    # sink already render the user-visible card.
                    continue
                yield Event(
                    type="assistant.tool_use",
                    data={"id": block.id, "name": block.name, "input": block.input},
                )
            elif isinstance(block, ToolResultBlock):
                # Same suppression for the matching tool result. We can't
                # easily check the tool name from the result block, but
                # since the only tool the orchestrator can call is the
                # dispatch shim (allowed_tools whitelist), all tool results
                # at this layer are dispatch results. Suppress them.
                continue
    elif isinstance(message, UserMessage):
        # User messages here are tool-results being fed back into the
        # orchestrator — same suppression as above.
        return
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
        return
