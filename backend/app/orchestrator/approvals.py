"""One approval bus: the shared path from "a tool wants to run" to a human yes.

LocalCode drives two vendor CLIs, and each of them has its own permission
callback shape (claude-agent-sdk's ``can_use_tool``, Codex's
``execCommandApproval``). If each provider grew its own gate, the user would
learn two approval UIs and we would maintain two places where a deny can be
forgotten. So this module is split in two deliberately:

  * :func:`evaluate_tool_request` — **provider-neutral**. It takes a tool
    name, a plain mapping of arguments, a :class:`ToolPolicy`, a mode and the
    approval channel, and returns a :class:`Decision` whose outcome is only
    ever ``allow`` or ``deny``: it resolves ``ask`` itself by putting a card
    on the event sink and waiting for the answer. No vendor SDK type appears
    in its signature or its body, so Task 10's Codex approval handler calls
    it directly and the user sees the very same card.
  * :func:`build_can_use_tool` — a **thin Claude adapter** that translates
    that decision into the SDK's ``PermissionResultAllow`` /
    ``PermissionResultDeny``. It holds no policy logic; a new branch here
    would be a branch Codex does not get, which is the whole failure this
    split prevents.

``EventSink`` and :func:`await_approval` moved here from ``dispatch.py``
(which still re-exports them) because they are no longer the plan gate's
private machinery — the tool gate uses the same queue. Answers are routed to
the gate that asked by ``_ApprovalRouter``, since a turn can now have several
gates open at once and a waiter that reads the channel itself destroys the
other gate's answer.

The headless answer to ``ask`` is **deny**, not allow. The code this replaces
escalated an unknown permission mode to ``acceptEdits`` so a run with no
human attached would not hang on a prompt nobody could answer; that traded a
hang for a silent grant of filesystem writes on every fleet step. A refusal
the model can read ("nobody is attached to approve this") costs one wasted
tool call and grants nothing.

The one exception is exec (``Bash``/``BashOutput``/``KillBash``): when no
channel is attached AND the role's own policy already grants exec, the
headless branch allows rather than denies — a fleet step is *always*
headless, so without this every ``pytest``/``git diff``/``rg`` a fleet role
runs would refuse. This lives here, keyed on "no channel attached", rather
than as a mode branch in ``permissions.decide``: ``ctx.role`` is unset for
every interactive session too, so a mode-keyed version of this concession
would auto-approve shell commands in a plain chat session under the UI's
default (``acceptEdits``) mode with no card ever shown.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from weakref import WeakKeyDictionary

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .base import Event
from .permissions import EXEC_TOOLS, Decision, ToolPolicy, decide

logger = logging.getLogger(__name__)


# Approval-id prefixes. The id is what an inbound decision is routed by, so it
# must be unique per gate: two gates in one turn sharing an id is exactly how a
# stale click on the first card satisfies the second.
APPROVAL_ID_PREFIX = "approval.tool"
PLAN_APPROVAL_ID_PREFIX = "approval.plan"

# Process-wide monotonic counter behind ``next_approval_id``. Uniqueness is
# all that is asked of it — the ids are never persisted or compared across
# processes — and ``itertools.count.__next__`` is atomic, so the fleet's
# worker threads can share it.
_approval_seq = itertools.count(1)

# Input-preview budgets for the approval card.
#
# Keys whose *whole* value is the thing the user is approving: truncating a
# path or a shell command in the middle turns the card into a guess ("allow
# `rm -rf /Users/me/pro…`?").
WHOLE_VALUE_KEYS = ("file_path", "path", "notebook_path", "filePath", "command", "pattern")
# A file body is never read off an approval card; 200 chars is enough to
# recognize what is being written.
PREVIEW_CHARS = 200
# Headroom reserved for the "… (<n> more chars)" marker so adding it cannot
# push a preview past the caller's cap.
_MARKER_BUDGET = 40


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


class _ApprovalRouter:
    """One reader per approval channel, dispatching each answer by its id.

    Every waiter used to read the channel itself and ``continue`` past any
    message whose id was not its own — which *discarded* it. That was harmless
    while a turn had at most one gate, and wrong the moment a turn can have
    several: claude-agent-sdk handles each permission request in its own task
    (``_internal/query.py``, ``_spawn_control_request_handler``), so two
    parallel tool calls open two gates on the same channel, and gate B would
    eat gate A's answer and leave A waiting out its full timeout for a decision
    the user had already made.

    So exactly one task reads the channel and hands each message to the gate
    that asked for it. An answer for no open gate is dropped *deliberately* and
    logged, instead of being left in the queue to satisfy the next gate.
    """

    __slots__ = ("_waiters", "_reader")

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None

    def register(
        self, channel: asyncio.Queue[dict[str, Any]], approval_id: str
    ) -> asyncio.Future[dict[str, Any]]:
        if approval_id in self._waiters:
            # ``next_approval_id`` makes this impossible; if it ever happens,
            # failing here is better than silently orphaning the first gate.
            raise ValueError(f"approval id {approval_id!r} is already open")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[approval_id] = fut
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read(channel))
        return fut

    def release(self, approval_id: str, fut: asyncio.Future[dict[str, Any]]) -> None:
        if self._waiters.get(approval_id) is fut:
            del self._waiters[approval_id]
        if not self._waiters and self._reader is not None:
            # Nothing is open: stop reading rather than leave a task parked on
            # a finished turn's queue for the life of the process. Cancelling a
            # pending ``Queue.get()`` leaves the item in the queue, so no
            # answer is lost by stopping — and the next gate starts a reader
            # again *before* its card is published.
            if not self._reader.done():
                self._reader.cancel()
            self._reader = None

    async def _read(self, channel: asyncio.Queue[dict[str, Any]]) -> None:
        while self._waiters:
            self._dispatch(await channel.get())

    def _dispatch(self, msg: dict[str, Any]) -> None:
        approval_id = str(msg.get("id") or "")
        fut = self._waiters.get(approval_id)
        if fut is None and not approval_id:
            # An older client can send a decision with no id at all. It is
            # attributable only when exactly one gate is open; with two open
            # there is no way to tell which one the user clicked, and guessing
            # would answer the wrong question.
            if len(self._waiters) == 1:
                fut = next(iter(self._waiters.values()))
        if fut is None or fut.done():
            logger.info(
                "approval decision %r matches no open gate (open: %s) — dropped",
                approval_id or "<no id>",
                sorted(self._waiters) or "none",
            )
            return
        fut.set_result(msg)


# One router per approval channel. Weak-keyed so a finished turn's queue (and
# its router) is collected with the turn; the router deliberately holds no
# reference to the channel itself, which would otherwise keep its own key
# alive forever.
_routers: WeakKeyDictionary[asyncio.Queue[dict[str, Any]], _ApprovalRouter] = (
    WeakKeyDictionary()
)


class ApprovalGate:
    """One open question, registered on the channel before its card is shown.

    Registration happens in ``open_approval_gate`` rather than inside
    ``answer()`` on purpose: the card is published to the UI between the two,
    and an answer that arrives before the gate is registered would be dropped
    as unattributable.
    """

    __slots__ = ("approval_id", "_router", "_future")

    def __init__(
        self, approval_id: str, router: _ApprovalRouter, fut: asyncio.Future[dict[str, Any]]
    ) -> None:
        self.approval_id = approval_id
        self._router = router
        self._future = fut

    async def answer(self, timeout_s: float) -> dict[str, Any]:
        """Wait for this gate's decision. ``value`` is "yes", "no" or "timeout"."""
        try:
            msg = await asyncio.wait_for(self._future, timeout=timeout_s)
        except TimeoutError:
            # Only this gate gives up; every other open gate keeps waiting.
            return {"id": self.approval_id, "value": "timeout", "feedback": None}
        value = "yes" if msg.get("value") == "yes" else "no"
        return {"id": self.approval_id, "value": value, "feedback": msg.get("feedback")}

    def close(self) -> None:
        """Stop waiting. Idempotent; safe to call from a ``finally``."""
        self._router.release(self.approval_id, self._future)


def open_approval_gate(
    channel: asyncio.Queue[dict[str, Any]], approval_id: str
) -> ApprovalGate:
    """Register ``approval_id`` on ``channel`` so an answer can be routed to it.

    Call this *before* emitting the card, then ``await gate.answer(timeout)``,
    then ``gate.close()`` in a ``finally``.

    The future and the reader task this creates are bound to the *calling*
    event loop (``asyncio.get_running_loop()`` / ``asyncio.create_task``), not
    to whatever loop the channel's queue was constructed under. That is fine
    for the fleet today because a fleet step's `RunContext` carries no
    `approval_channel` at all (see `fleet/collect.py`) — but a fleet step
    itself runs on an *isolated thread with its own event loop*
    (`collect.py`'s module docstring), so if Task 9/10 ever thread an approval
    channel into a fleet step, a gate opened from inside that thread would
    build its future on the child loop while `submit_approval` feeds the
    channel from the main loop's WS handler. `asyncio.Queue` is not
    thread-safe across loops, and neither is the future this returns —
    wiring a fleet step's approvals through this function needs a
    loop-crossing bridge (e.g. `run_coroutine_threadsafe`), not a bare call.
    """
    router = _routers.get(channel)
    if router is None:
        router = _ApprovalRouter()
        _routers[channel] = router
    return ApprovalGate(approval_id, router, router.register(channel, approval_id))


async def await_approval(
    channel: asyncio.Queue[dict[str, Any]],
    approval_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Open a gate, wait for its decision, and release it.

    Kept as the one-call form for callers that have nothing to do between
    publishing a card and waiting (and because ``dispatch`` has imported this
    name since the plan gate was written). Callers that publish a card should
    prefer ``open_approval_gate`` so the gate is registered first.
    """
    gate = open_approval_gate(channel, approval_id)
    try:
        return await gate.answer(timeout_s)
    finally:
        gate.close()


def next_approval_id(prefix: str) -> str:
    """A unique id for one approval gate, e.g. ``approval.tool.7``."""
    return f"{prefix}.{next(_approval_seq)}"


def _clip(value: str, *, soft_limit: int, hard_limit: int) -> str:
    if len(value) <= soft_limit:
        return value[:hard_limit]
    clipped = f"{value[:soft_limit]}… ({len(value) - soft_limit} more chars)"
    # Belt and braces: the marker is short, but the contract callers rely on
    # is "no value longer than max_chars", not "usually shorter".
    return clipped[:hard_limit]


def summarize_tool_input(
    tool_input: Mapping[str, Any], *, max_chars: int = 600
) -> dict[str, Any]:
    """Render a tool's arguments as a display-safe preview for the card.

    The raw input is untrusted, unbounded text: a ``Write`` of a 2 MB file
    would otherwise be pushed through the WebSocket to every viewer and
    rendered in a ``<pre>``. Paths and commands survive whole (see
    ``WHOLE_VALUE_KEYS``) because they *are* the decision; bodies are cut to
    ``PREVIEW_CHARS`` with a marker saying how much was dropped, and nothing
    returned exceeds ``max_chars``.
    """
    whole = max(1, max_chars - _MARKER_BUDGET)
    preview_limit = min(PREVIEW_CHARS, whole)
    out: dict[str, Any] = {}
    for key, value in tool_input.items():
        name = str(key)
        soft = whole if name in WHOLE_VALUE_KEYS else preview_limit
        if isinstance(value, str):
            out[name] = _clip(value, soft_limit=soft, hard_limit=max_chars)
        elif value is None or isinstance(value, bool | int | float):
            out[name] = value
        else:
            # Lists and nested dicts (MultiEdit's ``edits``, Bash's env) get
            # the same cap via their repr — the card is a summary, and a
            # structure-preserving walk would just reinvent the cap per level.
            out[name] = _clip(str(value), soft_limit=soft, hard_limit=max_chars)
    return out


def _deny(tool_name: str, policy: ToolPolicy, reason: str) -> Decision:
    """Every refusal funnels through here so the audit trail is complete.

    "Why did my agent refuse to edit that file" is unanswerable without this
    line — the model only reports the refusal in prose, and the card is gone
    once the turn ends.
    """
    logger.info("permission denied: tool=%s role=%s reason=%s", tool_name, policy.name, reason)
    return Decision("deny", reason)


async def evaluate_tool_request(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    policy: ToolPolicy,
    mode: str,
    sink: EventSink | None,
    approval_channel: asyncio.Queue[dict[str, Any]] | None,
    timeout_s: float,
) -> Decision:
    """Resolve one tool request to ``allow`` or ``deny`` — never ``ask``.

    Provider-neutral on purpose: ``tool_name``/``tool_input`` are a string
    and a mapping, the answer is our own :class:`Decision`, and the only
    I/O is the event sink and the approval queue the WebSocket layer already
    carries. Codex's approval callback (Task 10) calls this with its own
    command mapping and maps the result to its own reply shape.
    """
    try:
        result = decide(tool_name, tool_input, policy, mode=mode)
        if result.outcome == "allow":
            return result
        if result.outcome == "deny":
            return _deny(tool_name, policy, result.reason)

        if approval_channel is None or sink is None:
            # Headless: no human is attached, so there is nobody to say yes.
            # Deny rather than escalate — see the module docstring. The one
            # exception is exec: a fleet step runs in a child process with no
            # approval channel by construction (this is the fact that makes
            # the branch headless, not a mode), so an unanswerable `ask` for
            # `Bash`/`BashOutput`/`KillBash` is allowed when the role's own
            # policy already grants exec. This concession belongs HERE, not
            # in `decide`'s acceptEdits branch: `ctx.role` is set nowhere in
            # the tree, so every interactive session also reaches that branch
            # with the permissive default policy, and the UI's default mode
            # IS acceptEdits — widening it there would auto-approve every
            # shell command in a plain chat session with no card. Here, the
            # absence of `approval_channel`/`sink` is the actual, load-bearing
            # signal that no human is attached.
            if tool_name in EXEC_TOOLS and policy.exec_allowed:
                return Decision(
                    "allow",
                    f"{tool_name} pre-approved for role {policy.name}: no operator is "
                    f"attached to this headless run to ask ({result.reason})",
                )
            return _deny(
                tool_name,
                policy,
                f"{tool_name} needs approval ({result.reason}) but no operator is "
                f"attached to this run, so it could not be approved",
            )

        approval_id = next_approval_id(APPROVAL_ID_PREFIX)
        # Registered before the card is published: an answer that arrives
        # between the two would otherwise belong to no open gate and be
        # dropped. Several gates can be open at once (parallel tool calls), and
        # the router is what keeps each one's answer its own.
        gate = open_approval_gate(approval_channel, approval_id)
        try:
            await sink.put(
                Event(
                    type="pipeline.awaiting_approval",
                    data={
                        "id": approval_id,
                        "kind": "tool",
                        "tool": tool_name,
                        "input": summarize_tool_input(tool_input),
                        "reason": result.reason,
                        "timeout_s": timeout_s,
                    },
                )
            )
            answer = await gate.answer(timeout_s)
        finally:
            gate.close()
        # Emitted on every path (yes / no / timeout) because the UI clears the
        # card on it and the runner clears its pending-approval record on it.
        await sink.put(Event(type="pipeline.approval_received", data=answer))

        if answer["value"] == "yes":
            return Decision("allow", f"approved by the user ({result.reason})")
        if answer["value"] == "timeout":
            return _deny(
                tool_name,
                policy,
                f"no response to the {tool_name} approval request arrived within "
                f"{timeout_s:.0f}s (timeout)",
            )
        feedback = (answer.get("feedback") or "").strip()
        reason = f"the user denied {tool_name}"
        if feedback:
            reason = f"{reason}: {feedback}"
        return _deny(tool_name, policy, reason)
    except asyncio.CancelledError:
        # The turn is being torn down; a deny would be read as a policy
        # decision by the model. Let cancellation propagate.
        raise
    except Exception as exc:  # noqa: BLE001
        # A permission callback that raises takes the whole turn down with it
        # (the SDK has no tool result to report), so the failure is reported
        # as a refusal the model can react to instead.
        logger.exception("permission check for %s raised", tool_name)
        return _deny(
            tool_name,
            policy,
            f"the permission check for {tool_name} failed: {exc or exc!r}",
        )


CanUseToolFn = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


def build_can_use_tool(
    *,
    policy: ToolPolicy,
    mode: str,
    sink: EventSink | None,
    approval_channel: asyncio.Queue[dict[str, Any]] | None,
    timeout_s: float,
) -> CanUseToolFn:
    """Adapt :func:`evaluate_tool_request` to claude-agent-sdk's ``CanUseTool``.

    Translation only: outcome → result type, reason → the message the model
    reads. Any branch that looks like policy belongs in
    ``evaluate_tool_request``, where Codex gets it too.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        decision = await evaluate_tool_request(
            tool_name,
            tool_input,
            policy=policy,
            mode=mode,
            sink=sink,
            approval_channel=approval_channel,
            timeout_s=timeout_s,
        )
        if decision.outcome == "allow":
            return PermissionResultAllow()
        # One sentence, naming the tool and the reason: this text is all the
        # model sees, and "permission denied" alone sends it straight into a
        # retry loop on the same refused call.
        return PermissionResultDeny(
            message=f"LocalCode denied {tool_name}: {decision.reason}."
        )

    return can_use_tool
