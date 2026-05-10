"""Dispatch — the provider-agnostic ``Task`` equivalent.

The Orchestrator agent (a claude-agent-sdk session) dispatches subagents
through ONE custom MCP tool: ``dispatch_subagent(name, prompt)``. This file
defines that tool and the small infrastructure it needs:

  - An ``EventSink`` (asyncio.Queue wrapper) that the tool pushes per-step
    events into so the OrchestratorAgent can stream them to the WS while
    the dispatch is in flight (rather than buffering until completion).
  - A ``build_dispatch_mcp`` factory that produces a fresh MCP server bound
    to a specific registry + RunContext + sink. Per-turn instances stay
    isolated.

The tool's body delegates to ``FleetProvider._run_step_with_role`` (passed
in as a callable to avoid a circular import), so it inherits all the
hard-won behaviour we already shipped — heartbeats, per-step timeout,
clean cancellation, gate classifier, and fail-safe NACK detection.

Why custom MCP rather than the SDK's native ``Task`` + ``AgentDefinition``:
``AgentDefinition`` only knows how to dispatch claude-agent-sdk subagents.
We need to dispatch *opencode-backed* subagents too (cheaper coder model
running on a ChatGPT subscription via opencode). Routing inside our own
tool gives us the unified provider-agnostic dispatch.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .agent_def import AgentDef
from .base import Event, RunContext


logger = logging.getLogger(__name__)


# Sentinel pushed onto an EventSink to signal "no more events". Distinct
# object so we never confuse it with a real Event that happens to compare
# equal.
_SINK_DONE = object()


class EventSink:
    """A bounded asyncio.Queue with sentinel-based shutdown.

    The dispatch MCP tool pushes events here while a subagent runs; the
    OrchestratorAgent drains it concurrently with its model loop and
    forwards the events to the WS. ``close()`` lets the consumer know
    no more events will arrive.
    """

    __slots__ = ("_q",)

    def __init__(self, maxsize: int = 256) -> None:
        # Bounded so a runaway subagent producing thousands of token deltas
        # can't grow the queue without limit. 256 events is ~80 KB of text
        # in the worst case — small.
        self._q: asyncio.Queue[Event | object] = asyncio.Queue(maxsize=maxsize)

    async def put(self, ev: Event) -> None:
        await self._q.put(ev)

    async def close(self) -> None:
        await self._q.put(_SINK_DONE)

    async def get(self) -> Event | None:
        """Returns the next event, or ``None`` when the sink is closed."""
        item = await self._q.get()
        if item is _SINK_DONE:
            return None
        return item  # type: ignore[return-value]


# Type alias — a callable that runs one step of a sub-provider, yielding
# the same Event shape the rest of the orchestrator uses. We accept this
# as a parameter rather than importing ``FleetProvider._run_step_with_role``
# directly, both to avoid the circular import and to make the dispatch
# layer testable in isolation.
RunStepFn = Callable[..., Any]  # really an AsyncIterator[Event]; loosened to keep mypy quiet


def build_dispatch_mcp(
    *,
    registry: dict[str, AgentDef],
    ctx: RunContext,
    sink: EventSink,
    run_step_fn: RunStepFn,
) -> tuple[Any, str]:
    """Create an in-process MCP server exposing ``dispatch_subagent``.

    Returns ``(mcp_server_config, allowed_tool_name)``. The caller passes
    the config into ``ClaudeAgentOptions.mcp_servers`` and the allowed
    tool name into ``ClaudeAgentOptions.allowed_tools`` so the orchestrator
    can call it but nothing else.

    A fresh MCP server is built per-turn (closures capture this turn's
    registry/ctx/sink) — keeps state strictly per-turn so concurrent
    sessions can't leak events into each other.
    """

    # Importing here to avoid a circular import at module load time.
    from .fleet import RoleConfig, Step

    @tool(
        "dispatch_subagent",
        (
            "Dispatch a named subagent to handle a focused task. The "
            "subagent runs in its own context window with its own tools "
            "and returns a final text summary. Use this to delegate "
            "planning, coding, reviewing, or testing work to specialists. "
            "Input: name (one of the registered agents), prompt (the "
            "specific task description for that agent). Returns the "
            "agent's final output as text."
        ),
        {"name": str, "prompt": str},
    )
    async def _dispatch_subagent(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        prompt = str(args.get("prompt", "")).strip()

        if not name:
            return _err(f"missing 'name' argument; available: {list(registry)}")
        if not prompt:
            return _err("missing 'prompt' argument — give the agent something to do")
        if name not in registry:
            return _err(
                f"unknown subagent {name!r}. Available: "
                f"{', '.join(sorted(registry.keys())) or '(none)'}"
            )

        agent = registry[name]
        # Build the legacy RoleConfig + Step pair that _run_step_with_role
        # consumes. Note: step.id includes the agent name AND a per-turn
        # counter so the UI's tool_use cards stay distinct when the
        # orchestrator dispatches the same agent twice in one turn.
        step_id = _next_step_id(name)
        role_cfg = RoleConfig(
            provider=agent.provider,
            model=agent.model,
            system_prompt=agent.system_prompt,
        )
        step = Step(id=step_id, role=agent.name, prompt=prompt)
        outputs: dict[str, str] = {}

        # Drive the inner step loop, fanning every event out to the sink so
        # the WS sees per-role tool_use / tool_result / heartbeat cards in
        # real time. The dispatch tool itself returns only the final text.
        try:
            async for ev in run_step_fn(step, role_cfg, ctx, outputs):
                await sink.put(ev)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("dispatch_subagent: %s raised", name)
            return _err(f"subagent {name!r} raised: {exc}")

        result = outputs.get(step_id, "")
        if not result:
            return _err(
                f"subagent {name!r} produced no output. Inspect the chat "
                f"for the agent's tool_result card to see what happened, "
                f"then try a more focused prompt or a different agent."
            )
        return {"content": [{"type": "text", "text": result}]}

    server_name = "fleet_dispatch"
    mcp_server = create_sdk_mcp_server(
        name=server_name, version="1.0.0", tools=[_dispatch_subagent]
    )
    # The SDK exposes tools as ``mcp__<server>__<tool>`` to the model — we
    # allowlist the exact wire name so the orchestrator can call it but
    # can't reach any other MCP tool the user might have configured.
    allowed = f"mcp__{server_name}__dispatch_subagent"
    return mcp_server, allowed


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Module-level counter is fine: each turn builds a fresh MCP server with
# its own closure but step ids are namespaced by agent name AND counter
# so collisions don't matter across turns. The counter is incremented
# under the asyncio event loop so it's effectively serialised.
_step_counters: dict[str, int] = {}


def _next_step_id(agent_name: str) -> str:
    n = _step_counters.get(agent_name, 0) + 1
    _step_counters[agent_name] = n
    return f"orch.{agent_name}.{n}"


def reset_step_counters() -> None:
    """Reset per-agent step counters. Call between independent test runs."""
    _step_counters.clear()


def _err(message: str) -> dict[str, Any]:
    """Standard error shape for the MCP tool — orchestrator sees this as a
    tool result with is_error=True and can decide to retry or escalate."""
    return {"content": [{"type": "text", "text": message}], "is_error": True}
