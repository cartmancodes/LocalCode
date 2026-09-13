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

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..artifacts import ArtifactStore
from ..config import get_settings
from .agent_def import AgentDef

if TYPE_CHECKING:
    # Type-only: importing the fleet package at module load time is the
    # circular import the lazy imports below exist to avoid.
    from .fleet.envelope import StepResult

# The approval machinery lives in approvals.py now — the tool gate uses the
# same queue, and answers are routed to the gate that asked. Re-exported from
# here so every existing importer (orchestrator.py, tests) keeps working.
from .approvals import (
    PLAN_APPROVAL_ID_PREFIX,
    EventSink,
    next_approval_id,
    open_approval_gate,
)

# Re-exported, not used here: the plan gate now registers before publishing its
# card (``open_approval_gate``), but this name has been importable from this
# module since the gate was written and outside callers may still use it.
from .approvals import await_approval as await_approval
from .base import Event, RunContext

logger = logging.getLogger(__name__)


# How long the plan-approval gate blocks before treating silence as a timeout.
# 5 min is long enough for the user to read the plan, short enough that a
# tab left open overnight doesn't pin a session forever.
APPROVAL_TIMEOUT_S = 300.0


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
    from .fleet import (
        DISPATCH_HARD_FAIL_CAP,
        RoleConfig,
        Step,
        StepNotAttemptedError,
        StepTimeoutError,
    )

    # Per-TURN hard-failure ledger (this closure is rebuilt every turn). A
    # role that times out / reports an unresponsive backend lands here; once
    # it hits the cap we refuse further dispatches of it and tell the
    # orchestrator to abort — killing the unbounded silent retry loop where
    # a wedged planner is re-dispatched forever.
    hard_fail: dict[str, int] = {}
    role_outputs: dict[str, str] = {}
    # Per-TURN step ids, for the same reason: see StepIdSequence.
    step_ids = StepIdSequence()
    # Per-TURN spend ledger, for the same reason again. See TurnBudget for what
    # each arm bounds and why an unbounded turn was reachable without it.
    budget = TurnBudget(
        max_dispatches=max(8, 2 * len(registry)),
        token_budget=int(getattr(get_settings(), "fleet_turn_token_budget", 0)),
    )

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

        if hard_fail.get(name, 0) >= DISPATCH_HARD_FAIL_CAP:
            return _err(
                f"REFUSING to dispatch {name!r}: it has already hard-failed "
                f"{hard_fail[name]} time(s) this turn (unresponsive backend). "
                f"Do NOT retry {name}. ABORT the workflow now and tell the "
                f"user the {name} backend is unavailable — name it explicitly "
                f"and stop. Re-dispatching will only hang again."
            )

        over_budget = budget.refusal()
        if over_budget is not None:
            return _err(over_budget)

        agent = registry[name]
        prompt = _effective_prompt(name, prompt, ctx.prompt, role_outputs)
        step_id = step_ids.next_id(name)
        role_cfg = RoleConfig(
            provider=agent.provider,
            model=agent.model,
            system_prompt=agent.system_prompt,
        )
        step = Step(id=step_id, role=agent.name, prompt=prompt)
        # Envelopes, not text: this tool needs two different views of one step
        # — the BOUNDED ``context_text()`` it returns to the orchestrator, and
        # (for the planner) the step's COMPLETE output to write to disk. Only
        # the ``StepResult`` can give both, and deriving the bounded view from
        # the envelope is safe where reconstructing the full text from the
        # bounded view is impossible.
        outputs: dict[str, StepResult] = {}

        # Counted before the step runs, not after it succeeds: a turn whose
        # dispatches all FAIL still consumed wall clock and a session lock, and
        # the retry loop this bounds is made of failures.
        budget.dispatches += 1

        # Stream every per-step event onto the sink so the WS shows the
        # agent's tool_use / tool_result / heartbeat cards while the
        # dispatch is in flight. Tool body returns only the final text.
        try:
            async for ev in run_step_fn(step, role_cfg, ctx, outputs):
                await sink.put(ev)
        except StepNotAttemptedError as exc:
            # NOT counted against the cap, and deliberately so. This step never
            # reached a sub-provider — it was queued behind another step on the
            # same worker and dropped — so it demonstrated nothing about the
            # backend. Charging it would refuse a role for a failure it was
            # never given the chance to have, which is the same class of bug as
            # a cap that counts nothing at all.
            logger.info("dispatch_subagent: %s was not attempted: %s", name, exc)
            return _err(
                f"subagent {name!r} did not run: {exc}. This is NOT a backend "
                f"failure and is not held against {name}. Re-dispatch it when "
                f"the other step on its worker has finished."
            )
        except StepTimeoutError as exc:
            # Backend unresponsive / wedged. Count it; escalate to a hard
            # ABORT instruction once we hit the cap so the orchestrator can't
            # spin on it. Even the first time, tell it not to blindly retry.
            n = hard_fail.get(name, 0) + 1
            hard_fail[name] = n
            logger.warning("dispatch_subagent: %s hard-failed (%dx): %s", name, n, exc)
            if n >= DISPATCH_HARD_FAIL_CAP:
                return _err(
                    f"subagent {name!r} hard-failed {n}x — the "
                    f"{role_cfg.provider} backend is unresponsive. STOP. Do "
                    f"NOT dispatch {name} again. Abort the workflow and tell "
                    f"the user the {name} backend ({role_cfg.provider}) is "
                    f"unavailable. Detail: {exc}"
                )
            return _err(
                f"subagent {name!r} did not respond (backend likely "
                f"unresponsive): {exc}. Do NOT immediately re-dispatch the "
                f"same agent — if you have nothing else productive to do, "
                f"abort and report the backend problem to the user."
            )
        except Exception as exc:
            # This path counts against the cap too. It used to not, and that
            # was the whole of D7.1's second half: an oversize worker result
            # raised here, never incremented ``hard_fail``, so the cap never
            # tripped — and the orchestrator re-dispatched the planner, which
            # deterministically reproduced the same oversize output, for up to
            # 30 turns of 600 s each while holding the session lock. A cap that
            # bounds one of five failure classes is not a cap.
            n = hard_fail.get(name, 0) + 1
            hard_fail[name] = n
            logger.exception("dispatch_subagent: %s raised (%dx)", name, n)
            failure = f"{type(exc).__name__}: {exc}"
            if n >= DISPATCH_HARD_FAIL_CAP:
                return _err(
                    f"subagent {name!r} failed {n}x with the SAME kind of "
                    f"error — this is deterministic, not bad luck. STOP. Do "
                    f"NOT dispatch {name} again. Abort the workflow and tell "
                    f"the user that {name} ({role_cfg.provider}) is failing. "
                    f"Detail: {failure}"
                )
            return _err(
                f"subagent {name!r} failed: {failure}. Do NOT re-dispatch it "
                f"with the same prompt — if the failure is deterministic the "
                f"retry reproduces it. Change the approach or abort and report "
                f"this to the user."
            )

        # ``run_step_fn`` records the step's envelope here. What this tool
        # RETURNS is the orchestrator's context, so it returns the bounded
        # ``context_text()`` (summary + capped tool digest + an artifact
        # pointer when the output was evicted) and never the raw transcript —
        # an unbounded value here is the context-runaway defect itself.
        envelope = outputs.get(step_id)
        # Spend the step's tokens against the turn's ceiling. Done here, from
        # the envelope, because this is the only place the turn can see what a
        # step actually cost — the orchestrator's own usage covers its model
        # loop, not its sub-agents'.
        if envelope is not None:
            budget.spend(envelope.usage)
        result = envelope.context_text() if envelope is not None else ""
        if not result:
            return _err(
                f"subagent {name!r} produced no output. Inspect the chat "
                f"for its tool_result card; then try a more focused prompt."
            )

        # Side-effect for the planner: persist the markdown plan to disk
        # so it's an inspectable artifact and downstream agents can `cat`
        # it from the path. The orchestrator's narrative also gets the
        # path appended so it can include it in its summary.
        if agent.name == "planner" and envelope is not None:
            try:
                # The WHOLE plan, not the bounded view. This file is a
                # user-facing artifact — the README and the planner's own
                # description promise the full plan is committed here, and
                # people open it. Writing the summary instead left a
                # head/tail excerpt plus a pointer in a file that is supposed
                # to BE the document, which also broke the reasoning that
                # makes the bounded context safe: the coder is told to read
                # this file, so the file has to be complete.
                plan_path = save_plan(_full_output(envelope), ctx.cwd)
                result = f"{result}\n\n---\n_Plan saved to_ `{plan_path}`"
            except OSError as exc:
                logger.warning("failed to save plan to disk: %s", exc)

        role_outputs[name] = result

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

        # Unique per gate, not a fixed string: a decision is routed to a gate
        # by id, so two gates in one turn sharing an id is how a stale click on
        # the first card satisfies the second.
        approval_id = next_approval_id(PLAN_APPROVAL_ID_PREFIX)
        # Registered before the card goes out, so a fast answer cannot land
        # before anything is waiting for it (see ``open_approval_gate``).
        gate = open_approval_gate(ctx.approval_channel, approval_id)
        try:
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
            decision = await gate.answer(APPROVAL_TIMEOUT_S)
        finally:
            gate.close()
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


def _full_output(envelope: StepResult) -> str:
    """A step's COMPLETE output, fetching it back from the artifact store when
    the envelope only carries a bounded summary.

    When nothing was evicted, ``summary`` IS the full output and is returned
    unchanged — the common case costs no I/O. When something was evicted, the
    bounded summary is not a substitute for anything written to disk as a
    document, so we read the artifact. A missing artifact (someone pruned the
    store between the step and this call) degrades to the summary with a
    warning rather than failing the dispatch: a truncated plan file is bad, a
    lost plan step is worse.
    """
    if envelope.artifact_id:
        text = ArtifactStore().get_text(envelope.artifact_id)
        if text is not None:
            return text
        logger.warning(
            "artifact %s is missing from the store; writing the bounded "
            "summary instead of the full output",
            envelope.artifact_id,
        )
    return envelope.summary


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
# Internals
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TurnBudget:
    """What ONE turn is allowed to spend on sub-agent dispatches.

    Per-turn, like ``hard_fail`` and ``StepIdSequence``, and for the same
    reason: a ledger that outlives its turn either leaks keys forever or
    refuses a fresh turn for a previous one's spending.

    Two arms, bounding two different runaways that the hard-fail cap does not
    reach — because neither of them involves a *failure*:

    * **dispatches** — an orchestrator that keeps delegating instead of
      answering. Every dispatch succeeds, so nothing counts against
      ``DISPATCH_HARD_FAIL_CAP``; only ``max_turns`` stops it, at up to the
      step budget each. ``max(8, 2 * len(registry))`` is deliberately loose:
      every role twice (one review retry) plus headroom, so a legitimate
      workflow never meets it.
    * **tokens** — the same loop measured in money rather than calls, summed
      from each step's reported ``usage``. Off by default (``0``), because the
      right ceiling depends on the user's plan, not on us.

    ``usage`` of ``None`` contributes nothing: a provider that reports no token
    counts must not be charged a guess, and must not be refused either.
    """

    max_dispatches: int
    token_budget: int
    dispatches: int = 0
    tokens: int = 0

    def spend(self, usage: dict[str, int] | None) -> None:
        if usage:
            self.tokens += sum(usage.values())

    def refusal(self) -> str | None:
        """The message to refuse the next dispatch with, or ``None``.

        Shaped like the ``DISPATCH_HARD_FAIL_CAP`` refusal on purpose: the
        orchestrator already knows how to read "stop and tell the user", and a
        second vocabulary for the same instruction is a second thing it can
        misread.
        """
        if self.dispatches >= self.max_dispatches:
            return (
                f"REFUSING to dispatch: this turn has already run "
                f"{self.dispatches} sub-agent dispatches, the per-turn cap. "
                f"Do NOT dispatch anything else. STOP now, summarize what the "
                f"agents produced so far, and tell the user the turn hit its "
                f"dispatch cap so they can continue with a new prompt."
            )
        if self.token_budget > 0 and self.tokens >= self.token_budget:
            return (
                f"REFUSING to dispatch: this turn's sub-agents have used "
                f"{self.tokens} tokens, over the per-turn budget of "
                f"{self.token_budget}. Do NOT dispatch anything else. STOP "
                f"now, summarize what the agents produced so far, and tell "
                f"the user the turn hit its token budget."
            )
        return None


class StepIdSequence:
    """Per-agent step-id counters for ONE turn.

    Scoped to the turn, like ``hard_fail``, and that scope is the point: the
    module-level dict this replaces kept a counter per role name for the life
    of the process, so a long-running backend never gave those keys back. Step
    ids only have to be unique within a turn — the accumulator pairs
    ``tool_use``/``tool_result`` inside a single message — so nothing needs the
    counter to survive the turn that produced it.

    Not shared between turns either, which matters: a process-global counter
    that some turn boundary resets can hand two steps of a *concurrent* turn
    the same id, and then one step's result is paired with the other's call.
    """

    __slots__ = ("_counts",)

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def next_id(self, agent_name: str) -> str:
        n = self._counts.get(agent_name, 0) + 1
        self._counts[agent_name] = n
        return f"orch.{agent_name}.{n}"


def _err(message: str) -> dict[str, Any]:
    """Standard error shape for an MCP tool — orchestrator sees this as a
    tool result with is_error=True and can decide to retry or escalate."""
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _effective_prompt(
    name: str,
    prompt: str,
    user_prompt: str,
    role_outputs: dict[str, str],
) -> str:
    """Stitch prior-step context into one role's prompt.

    ``role_outputs`` holds bounded step envelopes (see ``dispatch_subagent``),
    which is what makes this stitching safe to keep doing verbatim. A plan
    under ``artifact_inline_max_bytes`` is inlined unchanged, exactly as
    before. A plan over it arrives as a head/tail summary carrying the path of
    the artifact that holds the whole document — so the "Full Planner Artifact"
    section below is still the full plan when the plan is small, and a pointer
    the Coder can ``Read`` when it is not. That is the trade this task accepts:
    previously the full plan was inlined at any size, and a single large plan
    consumed the context the Coder needed to execute it.
    """
    if name == "planner":
        return user_prompt
    if name == "coder" and role_outputs.get("planner"):
        return (
            f"{prompt}\n\n"
            "You MUST execute the FULL planner artifact below task-by-task. "
            "Do not rely on the abbreviated prompt above.\n\n"
            "# Full Planner Artifact\n\n"
            f"{role_outputs['planner']}"
        )
    if name == "reviewer":
        sections = [prompt]
        if role_outputs.get("planner"):
            sections.append("# Full Planner Artifact\n\n" + role_outputs["planner"])
        if role_outputs.get("coder"):
            sections.append("# Coder Result\n\n" + role_outputs["coder"])
        sections.append(
            "Review against the full planner artifact and coder result above, "
            "not just the final file state. If any planned task or verification "
            "was skipped, end with `NACK: <reason>`."
        )
        return "\n\n".join(sections)
    return prompt
