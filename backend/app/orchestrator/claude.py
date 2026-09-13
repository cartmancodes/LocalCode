"""Claude Code provider — wraps `claude-agent-sdk`.

The SDK spawns the host's `claude` CLI, which reads its OAuth token from
~/.claude/ (Linux) or the macOS keychain (Darwin). The token is persisted by
`claude login` and auto-refreshes; the orchestrator never sees it.

**One client per session, held across turns.** The one-shot ``query()`` this
replaced spawned a fresh `claude` CLI for every message: the CLI paid its
startup cost again on each turn, the server-side prompt cache was cold every
time (a long system prompt plus a growing transcript re-read at full price),
and there was no process left alive to ``interrupt()``. A ``ClaudeSDKClient``
is connected once per LocalCode session and reused, keyed on
``ctx.session_id``.

Two rules keep that reuse honest.

``_signature``: a client is rebuilt — not mutated — when the model, cwd, extra
dirs, system prompt, permission mode or tool set change. Mutating a live
client's prompt prefix or tool list is what *invalidates* the cache this whole
design exists to keep, so there is no setter call anywhere in here.

**Only a turn whose ``ResultMessage`` went past leaves a reusable client.**
Every turn's messages arrive on one per-connection stream, so a turn that ended
early (Stop, a closed socket, a failed stream) leaves an unread tail that would
be delivered to the *next* turn — see :meth:`ClaudeProvider._discard`. And since
``receive_response()`` returns at stream EOF as well as on the result, "the loop
ended" is not the test; the result message itself is. Anything else and the
client is dropped and the next turn connects a fresh one. The exact boundary of
that guarantee is spelled out where the flag is declared in ``run()``.

``run()`` is a two-producer merge rather than a straight ``async for`` over
the message stream, and that is not structural taste: the permission callback
(``can_use_tool``) emits the approval card from *inside* the SDK's message
loop and then blocks there waiting for the answer. A single-iterator run()
would therefore hold the card in a frame nobody is draining — the user would
be asked a question they cannot see. The sink is drained by its own task and
merged with the translated message stream, exactly as ``orchestrator.py``
does for its dispatch tools.

The subtle part of making the client outlive the turn is
:class:`_TurnBinding`; read its docstring before changing how the callback is
built.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..config import get_settings
from ..usage import TurnUsage, UsageLog, parse_claude_usage, usage_log_from_settings
from .approvals import CanUseToolFn, EventSink, build_can_use_tool
from .base import Event, RunContext
from .permissions import (
    EXEC_TOOLS,
    WRITE_TOOLS,
    ToolPolicy,
    normalize_permission_mode,
    policy_for_role,
    resolve_roots,
)

logger = logging.getLogger(__name__)

# Sentinel for the merged-event queue — same shape as orchestrator.py's.
_DONE = object()

# Test seam. A fake client class is injected here (or straight onto a
# provider's ``_factory``) so no test needs the real `claude` CLI on PATH, and
# so nothing has to monkeypatch the SDK module itself.
_client_factory: Any = ClaudeSDKClient


@dataclass
class _TurnBinding:
    """The halves of the permission callback that are reborn every turn.

    THE HAZARD this type exists to prevent: a persistent client is built once,
    with one ``can_use_tool`` function, but two of that function's inputs are
    per-turn objects — ``run()`` creates a fresh :class:`EventSink` on every
    turn and ``SessionRunner`` a fresh approval queue. A callback that closed
    over turn 1's pair would, on turn 2, publish the approval card onto a sink
    that is already closed (so the user never sees the card) and then wait for
    an answer on a queue nobody writes to — every approval after the first
    turn would silently time out and the tool would be denied.

    So the callback closes over this mutable holder instead and reads it at
    call time; ``run()`` rebinds the fields at the top of each turn, while it
    holds the handle's lock and therefore before any tool call of that turn can
    arrive. For the same reason ``sink`` and ``approval_channel`` are
    deliberately absent from :func:`_signature`: they differ on every turn by
    design and must never trigger a rebuild.
    """

    policy: ToolPolicy
    mode: str
    timeout_s: float
    sink: EventSink | None = None
    approval_channel: asyncio.Queue[dict[str, Any]] | None = None


@dataclass
class _ClientHandle:
    """A connected ``ClaudeSDKClient`` plus what we need to reuse it safely."""

    client: Any
    # What the client was built with. Differs → rebuild, never mutate.
    signature: tuple[Any, ...]
    # Serialises turns on this client: one CLI cannot stream two turns at once.
    lock: asyncio.Lock
    binding: _TurnBinding
    last_used: float = field(default=0.0)
    # Several paths can reach the same handle (a cancelled turn discards it
    # while a session delete closes it). Disconnecting twice is harmless but
    # noisy; this makes teardown idempotent per handle.
    closed: bool = field(default=False)


@dataclass
class _Builder:
    """Serialises *building* one key's client, outside the dict lock.

    ``connect()`` spawns the `claude` CLI: ~a second when healthy, unbounded
    when not. Holding the provider-wide ``_clients_lock`` across it would queue
    every other session's first turn, plus ``close_session`` (a ``DELETE``) and
    ``aclose`` (shutdown) behind one spawn — and ``session_runner/registry.py``
    promises the opposite ("cancel_turn is bounded, so DELETE cannot hang").
    So the dict lock is only ever held for dict mutation, and this per-key lock
    is what stops two turns on one key from each building a client and leaking
    the loser's CLI.

    ``waiting`` is a refcount rather than ``lock.locked()``: a waiter that has
    been woken but not yet resumed leaves ``locked()`` False, and dropping the
    builder then would let a third caller build concurrently.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0


def _frozen(value: Any) -> Any:
    """Make an options list hashable/comparable for the signature, preserving
    the difference between absent (``None``) and empty (``()``)."""
    return tuple(value) if isinstance(value, list) else value


def _allow_entry_tool(entry: str) -> str:
    """The tool an ``allowed_tools`` entry names, scoped-rule syntax and all.

    The SDK's own resolver (``claude_agent_sdk.types._whole_tool_allowed``)
    splits at the first ``(``: ``"Write"``, ``"Write()"`` and ``"Write(*)"`` all
    allow the whole ``Write`` tool, and ``"Bash(git diff:*)"`` allows a subset of
    ``Bash`` invocations. Matching on the raw string instead of this prefix is
    how a sanitizer that filters ``"Write"`` lets ``"Write(*)"`` straight
    through. We bar on the prefix, which is *stricter* than the SDK's rule —
    a scoped entry auto-approves the invocations it matches, and for a write or
    exec tool that is still the path check not running.
    """
    return entry.split("(", 1)[0].strip()


def _narrowed_allowed_tools(
    policy: ToolPolicy, extras: Mapping[str, Any]
) -> list[str] | None:
    """The SDK ``allowed_tools`` for this turn: the role's allow-list narrowed
    by ``ctx.extras``, never widened by it.

    ``None`` means "send no ``allowed_tools`` at all" — the vendor's default
    set, with ``can_use_tool`` consulted for every call. That is the *safe*
    answer, not the permissive one, which is why it is also what an extras-only
    request gets (rule 1 below).

    Three rules, in order:

    1. The policy decides whether there is an allow-list at all. When
       ``policy.allow_tools`` is ``None`` the role has no allow-list, and extras
       may not create one: an entry here auto-approves before the callback runs,
       so extras that "only add ``Read``" would take ``Read`` out of the gate's
       hands — including ``decide``'s ``denied_cwd_paths`` check. Creating an
       allow-list is a widening however small the list, so it is refused.
       (An extras list of ``[]`` loses nothing by this: the SDK's own default
       for ``allowed_tools`` is ``[]``, so "empty" and "absent" are the same
       instruction to the CLI.)
    2. Intersection, not replacement. When the role DOES have an allow-list,
       a tool the extras ask for that the list does not contain is dropped.
       Before Task 9 the extras simply overwrote the list, so
       ``claude_allowed_tools=["Write"]`` on a reviewer granted it ``Write``.
    3. No write or exec tool is EVER named here, whatever the role and whatever
       the extras, and matched by tool name so scoped syntax cannot slip past
       (see :func:`_allow_entry_tool`). A whole-tool entry in ``allowed_tools``
       auto-approves that tool before ``can_use_tool`` is consulted at all (see
       ``claude_agent_sdk.types._warn_if_can_use_tool_shadowed``), so a coder
       with ``Write`` on this list would skip the path-in-roots check and be
       able to write anywhere on the disk. Narrowing goes through
       ``disallowed_tools`` plus the callback instead.

    **Known limitation.** A read-only role's own allow-list (the planner's
    ``Read``/``Glob``/``Grep``/``LS``) does shadow the callback for those tools,
    so ``decide``'s path check does not run for a planner's ``Read`` and it can
    read outside its roots or under ``denied_cwd_paths``. That is accepted here
    only because it preserves the exact grants the hand-written extras this
    replaced already gave; it is a real gap, not a proof that read containment
    is impossible. Whether the CLI would auto-approve reads anyway in ``default``
    mode is UNVERIFIED — nothing in the installed SDK package says so, and the
    only auto-approvals visible in it are ``bypassPermissions`` and allow-list
    entries. Closing the gap means dropping the allow-list entirely and letting
    ``disallowed_tools`` plus the callback do all the work.
    """
    requested: list[str] | None = None
    raw = extras.get("claude_allowed_tools")
    if isinstance(raw, list):
        requested = [str(tool) for tool in raw]
    elif extras.get("claude_no_tools"):
        requested = []

    if policy.allow_tools is None:
        return None  # rule 1: extras cannot create an allow-list
    base = list(policy.allow_tools)
    if requested is not None:
        wanted = set(requested)
        base = [tool for tool in base if tool in wanted]

    barred = set(policy.deny_tools) | WRITE_TOOLS | EXEC_TOOLS
    return [
        entry for entry in dict.fromkeys(base) if _allow_entry_tool(entry) not in barred
    ]


def _policy_disallowed_tools(
    policy: ToolPolicy, extras: Mapping[str, Any]
) -> list[str]:
    """The SDK ``disallowed_tools`` for this turn.

    Union, not replacement: a caller's extra denials and the role policy's
    denials are both real, and dropping either one re-grants a tool someone
    deliberately took away.

    The write/exec families are derived from ``policy.writable`` /
    ``policy.exec_allowed`` rather than trusted to be spelled out in
    ``deny_tools``. They are, in every row of today's table — but under
    ``bypassPermissions`` the callback is never invoked, so this list is the
    SOLE enforcement, and a future role row with ``writable=False`` and an
    incomplete ``deny_tools`` would lose its read-only guarantee silently, with
    no test failing. Deriving it means the structural fields of a policy and the
    tools the CLI is offered cannot drift apart.
    """
    denied: list[str] = [str(t) for t in (extras.get("claude_disallowed_tools") or [])]
    denied.extend(policy.deny_tools)
    if not policy.writable:
        denied.extend(sorted(WRITE_TOOLS))
    if not policy.exec_allowed:
        denied.extend(sorted(EXEC_TOOLS))
    return list(dict.fromkeys(denied))


def _signature(
    ctx: RunContext,
    *,
    mode: str,
    option_extras: dict[str, Any],
    disallowed_tools: list[str],
) -> tuple[Any, ...]:
    """Everything a change in which must produce a *new* client.

    ``resume`` (``ctx.upstream_session_id``) is pointedly NOT here: it is empty
    on the first turn and set from then on, so including it would rebuild the
    client on turn 2 and throw away the very cache reuse is for. Neither are
    the sink and the approval channel — see :class:`_TurnBinding`.

    ``setting_sources`` and ``skills`` ARE here even though nothing varies them
    independently of ``allowed_tools`` today: once a role can (Task 9), a
    reused client would keep serving the previous role's settings sources and
    skills, which is a permissions difference the user cannot see.
    """
    return (
        ctx.model,
        ctx.cwd,
        tuple(ctx.additional_dirs or []),
        ctx.system_prompt,
        mode,
        # None ("no restriction") and () ("no tools at all") are different
        # clients, so the absent case cannot collapse to an empty tuple.
        _frozen(option_extras.get("allowed_tools")),
        tuple(disallowed_tools),
        _frozen(option_extras.get("setting_sources")),
        _frozen(option_extras.get("skills")),
    )


def _rebindable_can_use_tool(binding: _TurnBinding) -> CanUseToolFn:
    """Adapt the shared approval gate to a callback that outlives the turn.

    The gate itself (``build_can_use_tool``) takes its sink and channel by
    value, which is right for a one-shot run; here they are resolved from
    ``binding`` at call time instead, so the card from turn N lands on turn N's
    sink. Rebuilding the tiny adapter per tool call costs nothing next to the
    tool call itself.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        gate = build_can_use_tool(
            policy=binding.policy,
            mode=binding.mode,
            sink=binding.sink,
            approval_channel=binding.approval_channel,
            timeout_s=binding.timeout_s,
        )
        return await gate(tool_name, tool_input, context)

    return can_use_tool


async def _disconnect(client: Any) -> None:
    """Best-effort teardown. A client that is already gone is not an error —
    and a raise here would fail a session delete or an app shutdown."""
    try:
        await client.disconnect()
    except Exception:
        logger.debug("disconnecting a claude client failed", exc_info=True)


# How long the end-of-turn drain check waits for ONE frame that is already on
# the connection to surface. It is a SCHEDULING window, not an I/O one: the
# turn has reached its result, so anything the CLI has already emitted is
# sitting in the SDK's reader queue and needs only a pass or two of the loop to
# appear. Paid once per turn, after the answer has been delivered to the
# viewer, so it costs the user nothing they can see. It cannot be zero:
# `wait_for(..., 0)` never lets the read run at all and would report "drained"
# for every connection.
_TAIL_DRAIN_PEEK_S = 0.05

# Ceiling on the WHOLE check, however many benign frames it walks past. The
# per-frame window above bounds one read; without this, a connection that
# trickles benign frames faster than that window restarts it forever and the
# turn never completes — and the deferred-agent path this check exists for is
# exactly the one that emits a stream of `system` task-lifecycle frames
# (`task_started` / `task_updated` / `task_notification`, see
# `_internal/query.py`).
#
# It is a RECONNECT threshold, not a safety threshold, and the difference is
# what makes the number easy to choose. Expiry does not mean "probably fine" —
# it means the check never reached the end of the trickle, so the frames that
# decide reuse (a second ``ResultMessage``, the deferred agent's text) may be
# behind it, unexamined. The client is therefore dropped on expiry, and all
# this constant buys is HOW LONG a healthy connection is willing to wait before
# paying for a reconnect it did not need. Five per-frame windows: a healthy
# connection answers inside the first, a burst of post-turn lifecycle frames
# clears well inside five, and anything still arriving after a quarter of a
# second is a connection whose state is better re-established than guessed.
_TAIL_CHECK_DEADLINE_S = 0.25

# Ceiling on the ``interrupt()`` a cancelled turn sends. The SDK delivers it as
# a control request and waits for the CLI's ack up to its own default timeout —
# 60 s (``claude_agent_sdk._internal.query.Query._send_control_request``) — and
# that await happens while this turn still holds the per-session handle lock.
# ``SessionRunner._CANCEL_GRACE_S`` is 5 s, so an unacked interrupt means every
# cancelled turn on that session is *detached* rather than reaped, and the next
# turn queues behind the handle lock meanwhile: a Stop that costs a minute of
# apparent wedge. Three seconds is inside that grace window with room for the
# discard that follows. A timeout is treated exactly as a failed interrupt —
# ``completed`` is already False by then, so the client is dropped rather than
# handed to the next turn — which is why bounding it is safe: the worst case is
# a CLI we stopped waiting for, on a connection nobody will reuse.
_INTERRUPT_TIMEOUT_S = 3.0

# How many times a turn will take a handle and find it retired under it before
# giving up. See ``_locked_handle_for``: each pass costs only a dict lookup and
# a lock acquire, and the loop can only spin while OTHER turns are departing.
_HANDLE_ACQUIRE_TRIES = 3


@dataclass(frozen=True)
class _TailCheck:
    """What the end-of-turn drain check found on the connection.

    ``blocking`` is prose describing why this client cannot serve another turn,
    or None when it is at a clean message boundary. ``rate_limits`` carries any
    trailing :class:`RateLimitEvent` the check consumed: those are benign for
    the reuse decision (see :func:`_unread_tail`) but are NOT discardable — the
    CLI emits one only when the rate-limit status TRANSITIONS, so a dropped one
    is a measurement nobody gets again until the next transition. The caller
    translates them through the normal path.
    """

    blocking: str | None = None
    rate_limits: tuple[Any, ...] = ()


async def _unread_tail(client: Any) -> _TailCheck:
    """Is ``client`` at a clean message boundary after its turn's result?

    ``receive_response()`` returns at the ``ResultMessage``, but a result is
    not always the run's last word: when the CLI backgrounds delegated agent
    work (``claude_agent_sdk._internal.query.DEFERRING_TASK_TYPES``) the
    follow-up frames and a SECOND result arrive after it, on the same
    per-connection stream. Whatever is found here therefore belongs to the turn
    that just ended, and handing it to the next turn is the misattribution
    :meth:`ClaudeProvider._discard` exists to prevent.

    Two frame types are walked past rather than counted against reuse:

    * ``SystemMessage`` — the SDK forwards post-turn ``session_state_changed``
      and task-lifecycle frames on perfectly healthy connections, and
      ``_translate`` drops them anyway. Charging a reconnect (and a cold prompt
      cache) for one would cost more than it protects.
    * ``RateLimitEvent`` — likewise irrelevant to whether the next turn can be
      served, but it is the only measurement of the user's remaining plan
      headroom anyone gets, so it is carried back to the caller instead of
      being dropped on the floor.

    A failure of the check ITSELF counts as not drained, and so does running
    out of time. The question it asks is "is this connection in a state I
    understand?", and both an exception and an expiry are the answer "no":
    keeping the client would hand the next turn a connection nobody has
    finished reading. A reconnect is cheap by comparison — it costs a CLI spawn
    and a cold prompt cache for one turn, against a turn that silently answers
    with someone else's words.
    """
    deadline = time.monotonic() + _TAIL_CHECK_DEADLINE_S
    rate_limits: list[Any] = []

    def expired() -> _TailCheck:
        """Out of time with frames still arriving.

        The verdict turns on what was NOT seen, not on what was: the frames
        walked past were benign, but the check never reached the end of the
        trickle, and the ones that decide reuse — a second ``ResultMessage``,
        the deferred agent's own text — may be sitting behind it. "Everything I
        looked at was harmless" is not "there is nothing left", and treating it
        as such would reintroduce a bounded version of the misattribution this
        whole check exists to prevent.
        """
        logger.info(
            "claude: the tail check ran out of time after %.2fs with frames "
            "still arriving; dropping the client rather than assuming the "
            "connection is drained",
            _TAIL_CHECK_DEADLINE_S,
        )
        return _TailCheck(
            blocking="the tail check ran out of time with frames still arriving",
            rate_limits=tuple(rate_limits),
        )

    while True:
        # ONE place consults the deadline, and it yields both the next read's
        # budget and the meaning of that read timing out. A FULL window that
        # expires says the connection has gone quiet — nothing is coming, so
        # it is drained. A window the DEADLINE truncated says only that time
        # ran out, which is not the same claim at all: reading the second as
        # the first is how "discard on expiry" quietly becomes "reuse on
        # expiry", because a trickle that outlasts the budget always ends on a
        # truncated window. That is what the budget running out looks like from
        # inside this loop.
        window = min(_TAIL_DRAIN_PEEK_S, deadline - time.monotonic())
        if window <= 0:
            # The deadline landed exactly between two reads. Same verdict as a
            # truncated window below, so a reader never has to work out which
            # of the two fired.
            return expired()
        stream = client.receive_response()
        try:
            message = await asyncio.wait_for(stream.__anext__(), timeout=window)
        except StopAsyncIteration:
            # EOF. There is genuinely nothing left to misattribute.
            return _TailCheck(rate_limits=tuple(rate_limits))
        except TimeoutError:
            return (
                expired()
                if window < _TAIL_DRAIN_PEEK_S
                else _TailCheck(rate_limits=tuple(rate_limits))
            )
        except Exception as exc:
            logger.debug("checking for an unread tail failed", exc_info=True)
            return _TailCheck(
                blocking=f"the drain check failed ({type(exc).__name__})",
                rate_limits=tuple(rate_limits),
            )
        finally:
            try:
                await stream.aclose()
            except Exception:
                logger.debug("closing the tail-check stream failed", exc_info=True)
        if isinstance(message, RateLimitEvent):
            rate_limits.append(message)
            continue
        if isinstance(message, SystemMessage):
            continue
        return _TailCheck(
            blocking=f"an unread {type(message).__name__} after this turn's result",
            rate_limits=tuple(rate_limits),
        )


class ClaudeProvider:
    name = "claude"

    def __init__(self) -> None:
        # One live client per LocalCode session id (plus short-lived anonymous
        # keys for headless/unit runs, closed at the end of their run).
        self._clients: dict[str, _ClientHandle] = {}
        # Guards the dict and nothing else: never held across connect() or
        # disconnect(). See _Builder for why that matters.
        # Created eagerly: asyncio.Lock binds to an event loop on first *use*,
        # not on construction, so building the provider outside a loop (the
        # orchestrator registry's warm_up) is safe.
        self._clients_lock: asyncio.Lock = asyncio.Lock()
        self._builders: dict[str, _Builder] = {}
        self._factory: Any = _client_factory
        # Built lazily on first use, not here: constructing it in __init__
        # would resolve UsageLog's default ``~/.localcode/usage.jsonl`` at
        # provider-construction time, which for a test can be before HOME is
        # redirected. See usage.default_usage_log_path.
        self._usage_log: UsageLog | None = None

    def _get_usage_log(self) -> UsageLog:
        if self._usage_log is None:
            # Shared with GET /api/system/usage (routes/system.py) so the
            # writer and the reader can never resolve to two different
            # files if Settings.usage_log_path is ever overridden. See
            # usage.usage_log_from_settings.
            self._usage_log = usage_log_from_settings()
        return self._usage_log

    async def open_session(self, ctx: RunContext) -> str:
        # Claude Code creates the session lazily when the first turn runs. If
        # LocalCode already captured that id, hand it back so the runner can
        # resume it.
        return ctx.upstream_session_id or ""

    async def close_session(self, session_id: str) -> None:
        """Drop the live client for one session.

        Without this the client — and the `claude` CLI process behind it —
        outlives the session that owned it, holding a subprocess for a session
        the user deleted.
        """
        async with self._clients_lock:
            handle = self._clients.pop(session_id, None)
        if handle is not None:
            await self._close_handle(handle)

    async def aclose(self) -> None:
        async with self._clients_lock:
            handles = list(self._clients.values())
            self._clients.clear()
        for handle in handles:
            await self._close_handle(handle)

    async def _close_handle(self, handle: _ClientHandle) -> None:
        """Disconnect a handle's client once, whichever path got here first.

        Always called with ``_clients_lock`` released: the disconnect talks to a
        subprocess, and a delete or a shutdown must not queue behind it.
        """
        if handle.closed:
            return
        handle.closed = True
        await _disconnect(handle.client)

    async def _discard(self, key: str, handle: _ClientHandle) -> None:
        """Forget a client that cannot safely serve another turn.

        Two cases, and the second is the subtle one:

          * its stream failed, and reusing a wedged client would turn one
            broken turn into every later turn on that session failing the same
            way;
          * **the turn ended before its ``ResultMessage``** — an interrupt, a
            closed WebSocket, a consumer that walked away. Every message of
            every turn arrives on one per-connection stream, and
            ``receive_response()`` stops at the result, so whatever this turn
            left unread (trailing text deltas, and the result the CLI may still
            emit for an interrupted turn) would be handed to the *next* turn:
            the next answer prefixed with the previous turn's text, or
            terminated by the previous turn's result so its ``assistant.done``
            reports the wrong outcome and the real answer slides into the turn
            after. Draining to the result would be the optimisation; dropping
            the client is the correctness.
        """
        async with self._clients_lock:
            if self._clients.get(key) is handle:
                del self._clients[key]
        await self._close_handle(handle)

    async def _acquire_builder(self, key: str) -> _Builder:
        async with self._clients_lock:
            builder = self._builders.get(key)
            if builder is None:
                builder = _Builder()
                self._builders[key] = builder
            builder.waiting += 1
            return builder

    async def _release_builder(self, key: str, builder: _Builder) -> None:
        async with self._clients_lock:
            builder.waiting -= 1
            # Dropped as soon as nobody else wants it: anonymous keys are
            # unique per run, so a builder left behind per key would be a slow
            # leak in the fleet's worker processes.
            if builder.waiting <= 0 and self._builders.get(key) is builder:
                del self._builders[key]

    async def _handle_for(
        self,
        key: str,
        *,
        ctx: RunContext,
        signature: tuple[Any, ...],
        policy: ToolPolicy,
        mode: str,
        timeout_s: float,
        option_extras: dict[str, Any],
        disallowed_tools: list[str],
    ) -> _ClientHandle:
        """Get-or-build the connected client for ``key``, then evict the excess.

        ``_clients_lock`` is taken in short bursts around the dict only. The
        slow parts — ``connect()``, ``disconnect()`` — happen outside it, under
        this key's :class:`_Builder` instead.
        """
        builder = await self._acquire_builder(key)
        try:
            async with builder.lock:
                async with self._clients_lock:
                    handle = self._clients.get(key)
                    stale = handle is not None and handle.signature != signature
                    if stale:
                        # Prompt-prefix discipline: a changed model / cwd /
                        # add_dirs / system prompt / mode / tool set gets a
                        # brand-new client. The alternative — calling a setter
                        # on the live one — is exactly what silently
                        # invalidates the prompt cache and, for the tool set,
                        # would let a turn run under the previous turn's
                        # permissions.
                        del self._clients[key]
                if stale and handle is not None:
                    await self._close_handle(handle)
                    handle = None

                if handle is None:
                    handle = await self._build(
                        ctx=ctx,
                        signature=signature,
                        policy=policy,
                        mode=mode,
                        timeout_s=timeout_s,
                        option_extras=option_extras,
                        disallowed_tools=disallowed_tools,
                    )
                    async with self._clients_lock:
                        self._clients[key] = handle

                async with self._clients_lock:
                    handle.last_used = time.monotonic()
                    evicted = [
                        self._clients.pop(victim)
                        for victim in self._evictions_locked(keep=key)
                    ]
                # Popped under the dict lock, disconnected outside it — the
                # disconnect is subprocess I/O and nothing else may queue on it.
                for victim_handle in evicted:
                    logger.debug("evicting a claude client (LRU)")
                    await self._close_handle(victim_handle)
                return handle
        finally:
            await self._release_builder(key, builder)

    async def _locked_handle_for(self, key: str, **kwargs: Any) -> _ClientHandle:
        """A handle for ``key`` whose turn lock we hold and whose client is
        still connected.

        The two steps cannot be one, and the gap between them is a real race
        rather than a theoretical one. The handle comes out of a dict guarded
        by the provider lock, but its own lock is held by the turn in front of
        us for as long as that turn runs — and a turn that ends without
        reaching a clean message boundary discards its handle, disconnecting
        the CLI, from inside that lock (see ``_discard``). So the handle we
        were given a moment ago can be dead by the time we get in, and using it
        writes to a closed transport: the queued turn fails for no reason of
        its own, because the turn before it was Stopped.

        Re-checking after the acquire, and taking a fresh handle when the one
        we hold has been retired, is the whole of it. The retry is bounded
        because the only thing that can retire a handle is a turn holding this
        same lock, so each pass either finds a live client or waits behind one
        fewer departing turn.
        """
        for _ in range(_HANDLE_ACQUIRE_TRIES):
            handle = await self._handle_for(key, **kwargs)
            await handle.lock.acquire()
            if not handle.closed:
                return handle
            handle.lock.release()
        raise RuntimeError(
            "every claude client built for this session was retired before its "
            "turn could start"
        )

    async def _build(
        self,
        *,
        ctx: RunContext,
        signature: tuple[Any, ...],
        policy: ToolPolicy,
        mode: str,
        timeout_s: float,
        option_extras: dict[str, Any],
        disallowed_tools: list[str],
    ) -> _ClientHandle:
        binding = _TurnBinding(policy=policy, mode=mode, timeout_s=timeout_s)
        options = ClaudeAgentOptions(
            model=ctx.model,
            cwd=ctx.cwd,
            # Extra paths the spawned `claude` CLI may read/write. The SDK
            # restricts tools to `cwd` by default; this opens up sibling repos.
            add_dirs=list(ctx.additional_dirs or []),
            system_prompt=ctx.system_prompt,
            permission_mode=mode,
            can_use_tool=_rebindable_can_use_tool(binding),
            # Build-time only, and that is the whole point of holding the
            # client: resume replays a transcript into a *new* CLI. The second
            # turn of a live client needs nothing here — the conversation is
            # already in the process we are talking to.
            resume=ctx.upstream_session_id,
            disallowed_tools=disallowed_tools,
            include_partial_messages=True,  # token-level deltas for the UI
            **option_extras,
        )
        client = self._factory(options=options)
        try:
            await client.connect()
        except BaseException:
            # connect() may have spawned the CLI before failing (or been
            # cancelled mid-spawn). Nothing else holds this client, so without
            # this the process is orphaned — the leak class Task 14 fought.
            await _disconnect(client)
            raise
        return _ClientHandle(
            client=client,
            signature=signature,
            lock=asyncio.Lock(),
            binding=binding,
            last_used=time.monotonic(),
        )

    def _evictions_locked(self, *, keep: str) -> list[str]:
        """Which keys are over the cap, least recently used first.

        Caller holds ``self._clients_lock``; this only reads the dict, so the
        disconnects happen after the caller has let the lock go. A handle whose
        own lock is held is skipped: that client is streaming a turn right now,
        and disconnecting it would kill that turn mid-sentence. The cap is
        therefore a target, not a hard ceiling — overshooting by the number of
        concurrent turns is the correct failure.
        """
        cap = max(1, get_settings().claude_max_live_clients)
        over = len(self._clients) - cap
        if over <= 0:
            return []
        candidates = sorted(
            (k for k, h in self._clients.items() if k != keep and not h.lock.locked()),
            key=lambda k: self._clients[k].last_used,
        )
        return candidates[:over]

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
        # ctx.extras may only NARROW this policy, never widen it. Allowed tools
        # are INTERSECTED with the role's allow-list (and cannot create one
        # where the role has none), disallowed tools are UNIONED with the role's
        # denials, and settings/skills can only be switched off. Before Task 9
        # the extras simply replaced the allowed list, so any caller that put a
        # tool in `claude_allowed_tools` handed itself that tool no matter what
        # the role said — which makes the role table decoration, not a limit.
        option_extras: dict[str, Any] = {}
        allowed_tools = _narrowed_allowed_tools(policy, ctx.extras)
        if allowed_tools is not None:
            option_extras["allowed_tools"] = allowed_tools
        # A read-only role's `setting_sources=[]` is load-bearing, not
        # belt-and-braces: allow rules in a user's or project's settings file
        # shadow `can_use_tool` the same way an allow-list entry does, so a
        # reviewer that loaded the developer's own settings would be handed
        # back the tools this policy took away. Derived from the policy as well
        # as the extras so a context that carries no extras still gets it.
        if (
            not policy.writable
            or ctx.extras.get("claude_disable_settings")
            or ctx.extras.get("claude_no_tools")
        ):
            option_extras["setting_sources"] = []
        if (
            not policy.writable
            or ctx.extras.get("claude_disable_skills")
            or ctx.extras.get("claude_no_tools")
        ):
            option_extras["skills"] = []
        # This is the half of the reviewer's read-only guarantee that stops the
        # tool being *offered*; `can_use_tool` below refuses it if it is offered
        # anyway. Under `bypassPermissions` the callback is never invoked, so
        # this half is the only one left — see `_policy_disallowed_tools` for
        # why the write/exec families are derived rather than trusted to the
        # table.
        disallowed_tools = _policy_disallowed_tools(policy, ctx.extras)

        # The callback's card and the turn's messages are two producers; the
        # sink is the callback's half. See the module docstring.
        sink = EventSink()

        # Anonymous keys exist so the fleet's worker processes and unit tests —
        # which run with session_id=None — do not accumulate one live CLI per
        # run. They are closed in the finally below.
        key = ctx.session_id or f"anon:{id(ctx)}"
        anonymous = ctx.session_id is None

        try:
            handle = await self._locked_handle_for(
                key,
                ctx=ctx,
                signature=_signature(
                    ctx,
                    mode=mode,
                    option_extras=option_extras,
                    disallowed_tools=disallowed_tools,
                ),
                policy=policy,
                mode=mode,
                timeout_s=settings.tool_approval_timeout_s,
                option_extras=option_extras,
                disallowed_tools=disallowed_tools,
            )
        except Exception as exc:
            # Connecting spawns the CLI; a failure there is something the user
            # should see (not logged in, binary missing), not a traceback
            # thrown through the WebSocket layer.
            logger.exception("connecting a claude client failed")
            yield Event(
                type="error",
                data={"message": str(exc) or repr(exc), "provider": self.name},
            )
            return

        try:
            # One turn at a time per client: the CLI cannot interleave two. The
            # lock is already held — ``_locked_handle_for`` takes it, because
            # the handle has to be re-checked after the acquire — so it is
            # released here rather than by a `with`.
            try:
                # Rebind the per-turn halves of the permission callback before
                # anything can call it. See _TurnBinding — this is what keeps
                # turn N's approval card on turn N's sink.
                handle.binding.policy = policy
                handle.binding.mode = mode
                handle.binding.timeout_s = settings.tool_approval_timeout_s
                handle.binding.sink = sink
                handle.binding.approval_channel = ctx.approval_channel

                merged: asyncio.Queue[Event | object] = asyncio.Queue()
                client = handle.client
                # "This turn's ResultMessage went past", i.e. the client is back
                # at a clean message boundary and is safe to reuse. Anything
                # else and the finally below drops it — see _discard.
                #
                # It is set from the message itself, never from falling out of
                # the loop: ``receive_response()`` also returns at stream EOF
                # (the SDK's reader queues its `end` sentinel in a `finally`,
                # so a clean CLI exit ends the loop with no result and no
                # error). Treating that as completion would mark a client whose
                # CLI is *gone* as reusable — the dangerous direction.
                #
                # "Saw the result" is still not "the connection is
                # drained", so the flag is confirmed once more below: for
                # backgrounded agent work (the SDK's deferring task types) a
                # result can arrive with tasks still in flight, and the
                # follow-up frames plus a SECOND result then land on this same
                # connection. ``_unread_tail`` looks for exactly that after the
                # drain loop ends and clears this flag when it finds it, which
                # is what stops the tail from being served to the next turn as
                # its own output. Frames that are harmless for reuse (a
                # post-turn ``SystemMessage``, a ``RateLimitEvent``) do not
                # clear it — see that function, and see the note further down
                # for the residual it cannot close.
                #
                # And because that check runs AFTER the result, the flag is
                # reset by the cancellation handler too: a Stop landing inside
                # the check is a turn that never confirmed its boundary.
                completed = False

                async def _pump_messages() -> None:
                    """Drain one turn off the persistent client → translate →
                    merged queue."""
                    nonlocal completed
                    try:
                        await client.query(ctx.prompt)
                        async for message in client.receive_response():
                            usage: TurnUsage | None = None
                            if isinstance(message, ResultMessage):
                                completed = True
                                usage = parse_claude_usage(
                                    message,
                                    provider=self.name,
                                    model=ctx.model,
                                    session_id=ctx.session_id,
                                )
                                # File I/O off the event loop — same house
                                # style as storage/sessions.py's asyncio.to_thread
                                # wrapping of its own sync writes.
                                await asyncio.to_thread(self._get_usage_log().append, usage)
                            async for ev in _translate(message, usage=usage):
                                await merged.put(ev)
                        if completed:
                            # A tail left on this connection would be read by
                            # the NEXT turn as its own output, and that turn
                            # would terminate on this turn's second result:
                            # silent misattribution, which is worse than the
                            # reconnect that dropping the client costs. The SDK
                            # cannot fix this from below (it needs a
                            # run-boundary signal the CLI does not send), so the
                            # provider refuses to reuse a connection it can see
                            # is not at a clean message boundary.
                            #
                            # THE RESIDUAL, in two parts, both recorded in
                            # docs/harness.md: a tail the CLI emits LATER —
                            # after this check and before the next turn — is
                            # undetectable; and a frame delivered at the exact
                            # moment the per-frame window expires can be
                            # consumed and lost (anyio assigns the item to the
                            # receiver before waking it, and the cancellation
                            # `wait_for` then raises drops it). The second is
                            # the same class as the first, one frame wide.
                            #
                            # Neither is what happens when the check runs out
                            # of time: that is not a risk taken, it is a
                            # reconnect paid. An expiry means the trickle never
                            # ended inside the budget, so the client is dropped
                            # and the next turn starts a fresh CLI.
                            check = await _unread_tail(client)
                            # Trailing quota measurements are emitted whatever
                            # the verdict: the CLI reports its rate-limit window
                            # only when the status TRANSITIONS, so a dropped one
                            # is a number nobody gets again. They arrive after
                            # this turn's `assistant.done`, which costs the
                            # meter nothing and the transcript nothing.
                            for event in check.rate_limits:
                                await merged.put(rate_limit_event(event))
                            if check.blocking is not None:
                                logger.warning(
                                    "claude session %s: %s — dropping the "
                                    "client rather than serving its tail to "
                                    "the next turn",
                                    ctx.session_id,
                                    check.blocking,
                                )
                                completed = False
                    except asyncio.CancelledError:
                        # THE interrupt the roadmap asks for: the CLI outlives
                        # the turn now, so a cancelled turn has to tell it to
                        # stop working. Without this it keeps burning tokens on
                        # a turn nobody is reading, and the next turn queues
                        # behind it.
                        #
                        # The client is NOT kept afterwards: the interrupted
                        # turn's unread tail would otherwise be delivered to the
                        # next turn on this connection.
                        #
                        # RESET, not "stays False". A cancel can land anywhere
                        # in this pump, and one of those places is the
                        # end-of-turn tail check — which runs 50-250 ms AFTER
                        # `completed` was set at the ResultMessage, on every
                        # single turn. A Stop inside that window used to leave
                        # the flag True, so the finally below skipped the
                        # discard and kept a connection whose tail nobody had
                        # finished reading; the next turn on that session then
                        # read the previous turn's leftovers as its own answer.
                        # The flag means "this turn ended at a clean message
                        # boundary", and a cancellation is the proof that it
                        # did not.
                        completed = False
                        try:
                            # Bounded: see _INTERRUPT_TIMEOUT_S. A timeout is a
                            # failed interrupt, and a failed interrupt is a
                            # discard — which the line above has already
                            # guaranteed.
                            await asyncio.wait_for(client.interrupt(), _INTERRUPT_TIMEOUT_S)
                        except BaseException:
                            logger.debug("interrupt after cancellation failed", exc_info=True)
                        raise
                    except Exception as exc:  # surface to UI rather than crashing the WS
                        logger.exception("claude provider pump raised")
                        # A client whose stream blew up may be wedged; drop it
                        # so the next turn connects a fresh one instead of
                        # failing identically forever.
                        await self._discard(key, handle)
                        await merged.put(
                            Event(
                                type="error",
                                data={"message": str(exc) or repr(exc), "provider": self.name},
                            )
                        )
                    finally:
                        # A permission callback can still be mid-flight when the
                        # message stream ends (a denied tool, then the final
                        # ResultMessage): close the sink so its pump drains the
                        # tail and exits instead of waiting forever.
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
                    # On consumer exit (WS close, cancellation, an exception in
                    # the caller) cancel the producers so neither the turn on
                    # the CLI nor a callback blocked on an approval outlives
                    # this turn.
                    for t in (msg_task, sink_task, seal_task):
                        if not t.done():
                            t.cancel()
                    for t in (msg_task, sink_task, seal_task):
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    if not completed:
                        # The turn did not reach its ResultMessage (Stop, a
                        # closed socket, a consumer that walked away, a failed
                        # stream), so this client's connection is mid-turn and
                        # must not serve another one. _discard explains what
                        # reuse would do to the next turn.
                        await self._discard(key, handle)
            finally:
                handle.lock.release()
        finally:
            # Headless and unit runs get no session id, so nothing would ever
            # come back to close their client.
            if anonymous:
                await self.close_session(key)


def rate_limit_event(message: Any) -> Event:
    """One SDK ``RateLimitEvent`` as our ``quota.limit`` Event.

    The only place Claude's remaining plan headroom is ever MEASURED. The CLI
    emits this as its own message type when the rate-limit status TRANSITIONS —
    not on every turn, and not on the result — so a dropped one is a
    measurement nobody gets again until the next transition, which is why
    ``quota.py`` persists what it learns here and treats absence as "no
    change".

    An EVENT, not a governor call: this same code runs inside a fleet worker
    PROCESS, and ``quota.json`` is a read-modify-write file with exactly one
    writer. The main process records it — see ``session_runner/turn.py``.

    Shared with ``orchestrator.py``'s translator rather than copied into it:
    the orchestrator's own model loop is a claude-agent-sdk session too, and a
    user who works only in fleet sessions would otherwise have Claude's
    headroom never measured at all. Two copies of the ``getattr`` chain is two
    places to fix when the SDK's shape moves.

    Every field is read through ``getattr`` with a default: the info object can
    be absent or ``None``, and a future SDK can rename everything under it.
    Taking a good turn down over a telemetry field would be a far worse failure
    than a stale meter, so this function has no path that raises.
    """
    info = getattr(message, "rate_limit_info", None)
    return Event(
        type="quota.limit",
        data={
            "provider": ClaudeProvider.name,
            "status": getattr(info, "status", None),
            "resets_at": getattr(info, "resets_at", None),
            "rate_limit_type": getattr(info, "rate_limit_type", None),
            "utilization": getattr(info, "utilization", None),
        },
    )


async def _translate(message: Any, *, usage: TurnUsage | None = None) -> AsyncIterator[Event]:
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
        data: dict[str, Any] = {
            "cost_usd": getattr(message, "total_cost_usd", None),
            "duration_ms": getattr(message, "duration_ms", None),
            "num_turns": getattr(message, "num_turns", None),
            "upstream_session_id": getattr(message, "session_id", None),
        }
        if usage is not None:
            # Cache hit rate is only computable once this reaches the log;
            # cost_usd stays a top-level field too — Task 11 changes what the
            # UI emphasises, not what is recorded here.
            data["usage"] = asdict(usage)
        yield Event(type="assistant.done", data=data)
    elif isinstance(message, RateLimitEvent):
        yield rate_limit_event(message)
    elif isinstance(message, SystemMessage):
        # System init/notice messages — optional to surface; skip for now.
        return
