"""Dispatch — the provider-agnostic ``Task`` equivalent + sibling agent tools.

The Orchestrator agent (a claude-agent-sdk session) drives the workflow via
a small set of MCP tools defined here:

  - ``dispatch_subagent(name, prompt)`` — the core ``Task`` equivalent. Runs
    the named subagent (claude- or opencode-backed) in its own context and
    returns a text summary.
  - ``request_plan_approval(plan_summary)`` — HITL gate. Pauses the workflow,
    surfaces an Approve/Reject card to the WS client, awaits the decision,
    returns it to the orchestrator so it can continue or abort.

A per-turn ``EventSink`` is shared by both tools: any tool_use / tool_result /
heartbeat / approval-card events that should be visible in the chat get
pushed onto the sink while the tool is running. The OrchestratorAgent drains
the sink concurrently with the model loop and forwards events to the WS in
real time — so the user sees per-role cards during a dispatch instead of one
giant blocking call.

Why custom MCP rather than the SDK's native ``Task`` + ``AgentDefinition``:
``AgentDefinition`` only knows how to dispatch claude-agent-sdk subagents.
We need to dispatch *opencode-backed* subagents too (cheaper coder model
running on a ChatGPT subscription via opencode). Routing inside our own
tool gives us the unified provider-agnostic dispatch.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .agent_def import AgentDef
from .base import Event, RunContext

logger = logging.getLogger(__name__)


# How long the plan-approval gate blocks before treating silence as a timeout.
# 5 min is long enough for the user to read the plan, short enough that a
# tab left open overnight doesn't pin a session forever.
APPROVAL_TIMEOUT_S = 300.0


# Sentinel pushed onto an EventSink to signal "no more events". Distinct
# object so we never confuse it with a real Event.
_SINK_DONE = object()


class EventSink:
    """A bounded asyncio.Queue with sentinel-based shutdown.

    The dispatch / approval MCP tools push events here while they run; the
    OrchestratorAgent drains it concurrently with its model loop and
    forwards the events to the WS. ``close()`` lets the consumer know
    no more events will arrive.
    """

    __slots__ = ("_q",)

    def __init__(self, maxsize: int = 256) -> None:
        # Bounded so a runaway subagent producing thousands of token deltas
        # can't grow the queue without limit.
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


# Type alias — ``FleetProvider._run_step_with_role``-shaped callable.
RunStepFn = Callable[..., Any]


def build_dispatch_mcp(
    *,
    registry: dict[str, AgentDef],
    ctx: RunContext,
    sink: EventSink,
    run_step_fn: RunStepFn,
) -> tuple[Any, list[str]]:
    """Build the in-process MCP server exposing ``dispatch_subagent`` and
    ``request_plan_approval``.

    Returns ``(mcp_server_config, allowed_tool_names)``. Caller wires both
    into ``ClaudeAgentOptions``.

    A fresh MCP server is built per-turn — its closures capture this turn's
    registry / ctx / sink — so concurrent sessions can't leak events into
    each other.
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
            "agent's final output as text. When name='planner', the plan "
            "is also persisted under <cwd>/.localcode/plans/<timestamp>-"
            "<slug>.md and the path is appended to the returned text."
        ),
        {"name": str, "prompt": str},
    )
    async def _dispatch_subagent(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        prompt = str(args.get("prompt", "")).strip()

        if not name:
            return _err(f"missing 'name' argument; available: {sorted(registry)}")
        if not prompt:
            return _err("missing 'prompt' argument — give the agent something to do")
        if name not in registry:
            return _err(
                f"unknown subagent {name!r}. Available: "
                f"{', '.join(sorted(registry.keys())) or '(none)'}"
            )

        agent = registry[name]
        step_id = _next_step_id(name)
        role_cfg = RoleConfig(
            provider=agent.provider,
            model=agent.model,
            system_prompt=agent.system_prompt,
        )
        step = Step(id=step_id, role=agent.name, prompt=prompt)
        outputs: dict[str, str] = {}

        # Stream every per-step event onto the sink so the WS shows the
        # agent's tool_use / tool_result / heartbeat cards while the
        # dispatch is in flight. Tool body returns only the final text.
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
                f"for its tool_result card; then try a more focused prompt."
            )

        # Side-effect for the planner: persist the markdown plan to disk
        # so it's an inspectable artifact and downstream agents can `cat`
        # it from the path. The orchestrator's narrative also gets the
        # path appended so it can include it in its summary.
        if agent.name == "planner":
            try:
                plan_path = save_plan(result, ctx.cwd)
                result = f"{result}\n\n---\n_Plan saved to_ `{plan_path}`"
            except OSError as exc:
                logger.warning("failed to save plan to disk: %s", exc)

        return {"content": [{"type": "text", "text": result}]}

    @tool(
        "request_plan_approval",
        (
            "Pause the workflow and ask the user to approve the plan before "
            "dispatching downstream agents. Surfaces an Approve / Reject "
            "card to the chat with the plan summary you provide. Returns "
            "the user's decision as text — one of 'yes', 'no', or 'timeout' "
            "— with any feedback they wrote. Call this AFTER the planner "
            "and BEFORE dispatching the coder when the workflow requires "
            "human-in-the-loop approval."
        ),
        {"plan_summary": str},
    )
    async def _request_plan_approval(args: dict[str, Any]) -> dict[str, Any]:
        summary = str(args.get("plan_summary", "")).strip()
        if not summary:
            return _err("missing 'plan_summary' argument")

        # Headless / no-WS path: auto-approve so unit tests and direct
        # provider usage don't deadlock waiting for input that will never
        # come.
        if ctx.approval_channel is None:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "auto-approved (no approval channel wired — "
                            "running in headless mode)"
                        ),
                    }
                ]
            }

        approval_id = "approval.plan"
        await sink.put(
            Event(
                type="pipeline.awaiting_approval",
                data={
                    "id": approval_id,
                    "kind": "plan",
                    "plan": summary,
                    "message": (
                        "Approve this plan to run the worker steps, or "
                        "reject with feedback to abort the turn."
                    ),
                    "timeout_s": APPROVAL_TIMEOUT_S,
                },
            )
        )
        decision = await await_approval(
            ctx.approval_channel, approval_id, APPROVAL_TIMEOUT_S
        )
        await sink.put(Event(type="pipeline.approval_received", data=decision))

        # Return a text describing the outcome that the orchestrator can
        # reason about directly. We include the value AND the feedback so
        # the orchestrator can echo concrete user feedback back to them.
        if decision["value"] == "yes":
            return {
                "content": [
                    {"type": "text", "text": "User approved. Continue with the workflow."}
                ]
            }
        if decision["value"] == "timeout":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Approval timed out. Halt the workflow and "
                            "explain to the user that no decision arrived "
                            "within the timeout."
                        ),
                    }
                ]
            }
        # Rejected.
        feedback = decision.get("feedback") or ""
        body = "User rejected the plan."
        if feedback:
            body += f"\nFeedback: {feedback}"
        body += "\nHalt the workflow and explain why."
        return {"content": [{"type": "text", "text": body}]}

    server_name = "fleet_dispatch"
    mcp_server = create_sdk_mcp_server(
        name=server_name,
        version="1.0.0",
        tools=[_dispatch_subagent, _request_plan_approval],
    )
    allowed = [
        f"mcp__{server_name}__dispatch_subagent",
        f"mcp__{server_name}__request_plan_approval",
    ]
    return mcp_server, allowed


# ─────────────────────────────────────────────────────────────────────────────
# Plan-on-disk helpers (used by the planner branch of dispatch_subagent)
# ─────────────────────────────────────────────────────────────────────────────


def slugify_plan_title(plan_text: str) -> str:
    """Pull the first H1 heading from a markdown plan and turn it into a
    filename-safe slug. Falls back to ``"plan"`` when there's no heading."""
    m = re.search(r"^\s*#\s+(.+?)\s*$", plan_text, re.MULTILINE)
    title = m.group(1) if m else "plan"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "plan")[:60]


def save_plan(plan_text: str, cwd: str | None) -> Path:
    """Persist the planner's markdown output under
    ``<cwd>/.localcode/plans/YYYYMMDD-HHMMSS-<slug>.md``."""
    base = Path(cwd) if cwd else Path.cwd()
    plans_dir = base / ".localcode" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = plans_dir / f"{timestamp}-{slugify_plan_title(plan_text)}.md"
    path.write_text(plan_text, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# HITL approval helper
# ─────────────────────────────────────────────────────────────────────────────


async def await_approval(
    channel: asyncio.Queue[dict[str, Any]],
    approval_id: str,
    timeout: float,
) -> dict[str, Any]:
    """Block until the user accepts/rejects this approval, or timeout fires.

    Stale messages (different ``id``) are dropped — this is what lets a
    second approval gate in the same turn ignore a late click on the
    previous gate's button.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"id": approval_id, "value": "timeout", "feedback": None}
        try:
            msg = await asyncio.wait_for(channel.get(), timeout=remaining)
        except TimeoutError:
            return {"id": approval_id, "value": "timeout", "feedback": None}
        msg_id = msg.get("id")
        if msg_id and msg_id != approval_id:
            continue
        value = "yes" if msg.get("value") == "yes" else "no"
        return {"id": approval_id, "value": value, "feedback": msg.get("feedback")}


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


# Module-level counter is fine: each turn builds a fresh MCP server with
# its own closure but step ids are namespaced by agent name AND counter
# so collisions don't matter across turns.
_step_counters: dict[str, int] = {}


def _next_step_id(agent_name: str) -> str:
    n = _step_counters.get(agent_name, 0) + 1
    _step_counters[agent_name] = n
    return f"orch.{agent_name}.{n}"


def reset_step_counters() -> None:
    """Reset per-agent step counters. Call between independent test runs."""
    _step_counters.clear()


def _err(message: str) -> dict[str, Any]:
    """Standard error shape for an MCP tool — orchestrator sees this as a
    tool result with is_error=True and can decide to retry or escalate."""
    return {"content": [{"type": "text", "text": message}], "is_error": True}
