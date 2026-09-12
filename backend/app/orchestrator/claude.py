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

What keeps that reuse honest is ``_signature``: a client is rebuilt — not
mutated — when the model, cwd, extra dirs, system prompt, permission mode or
tool set change. Mutating a live client's prompt prefix or tool list is what
*invalidates* the cache this whole design exists to keep, so there is no
setter call anywhere in here.

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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..config import get_settings
from .approvals import CanUseToolFn, EventSink, build_can_use_tool
from .base import Event, RunContext
from .permissions import (
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
    """
    allowed = option_extras.get("allowed_tools")
    return (
        ctx.model,
        ctx.cwd,
        tuple(ctx.additional_dirs or []),
        ctx.system_prompt,
        mode,
        # None ("no restriction") and () ("no tools at all") are different
        # clients, so the absent case cannot collapse to an empty tuple.
        tuple(allowed) if allowed is not None else None,
        tuple(disallowed_tools),
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


class ClaudeProvider:
    name = "claude"

    def __init__(self) -> None:
        # One live client per LocalCode session id (plus short-lived anonymous
        # keys for headless/unit runs, closed at the end of their run).
        self._clients: dict[str, _ClientHandle] = {}
        # Created eagerly: asyncio.Lock binds to an event loop on first *use*,
        # not on construction, so building the provider outside a loop (the
        # orchestrator registry's warm_up) is safe.
        self._clients_lock: asyncio.Lock = asyncio.Lock()
        self._factory: Any = _client_factory

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
            await _disconnect(handle.client)

    async def aclose(self) -> None:
        async with self._clients_lock:
            handles = list(self._clients.values())
            self._clients.clear()
        for handle in handles:
            await _disconnect(handle.client)

    async def _discard(self, key: str, handle: _ClientHandle) -> None:
        """Forget a client whose stream failed, so the next turn rebuilds.

        Reusing a wedged client would turn one broken turn into every
        subsequent turn on that session failing the same way.
        """
        async with self._clients_lock:
            if self._clients.get(key) is handle:
                del self._clients[key]
        await _disconnect(handle.client)

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
        """Get-or-build the connected client for ``key``, then evict the excess."""
        async with self._clients_lock:
            handle = self._clients.get(key)
            if handle is not None and handle.signature != signature:
                # Prompt-prefix discipline: a changed model / cwd / add_dirs /
                # system prompt / mode / tool set gets a brand-new client. The
                # alternative — calling a setter on the live one — is exactly
                # what silently invalidates the prompt cache and, for the tool
                # set, would let a turn run under the previous turn's
                # permissions.
                del self._clients[key]
                await _disconnect(handle.client)
                handle = None
            if handle is None:
                binding = _TurnBinding(policy=policy, mode=mode, timeout_s=timeout_s)
                options = ClaudeAgentOptions(
                    model=ctx.model,
                    cwd=ctx.cwd,
                    # Extra paths the spawned `claude` CLI may read/write. The
                    # SDK restricts tools to `cwd` by default; this opens up
                    # sibling repos.
                    add_dirs=list(ctx.additional_dirs or []),
                    system_prompt=ctx.system_prompt,
                    permission_mode=mode,
                    can_use_tool=_rebindable_can_use_tool(binding),
                    # Build-time only, and that is the whole point of holding
                    # the client: resume replays a transcript into a *new* CLI.
                    # The second turn of a live client needs nothing here — the
                    # conversation is already in the process we are talking to.
                    resume=ctx.upstream_session_id,
                    disallowed_tools=disallowed_tools,
                    include_partial_messages=True,  # token-level deltas for the UI
                    **option_extras,
                )
                client = self._factory(options=options)
                await client.connect()
                handle = _ClientHandle(
                    client=client,
                    signature=signature,
                    lock=asyncio.Lock(),
                    binding=binding,
                    last_used=time.monotonic(),
                )
                self._clients[key] = handle
            handle.last_used = time.monotonic()
            await self._evict_locked(keep=key)
            return handle

    async def _evict_locked(self, *, keep: str) -> None:
        """Close the least recently used clients above the cap.

        Caller holds ``self._clients_lock``. A handle whose own lock is held is
        skipped: that client is streaming a turn right now, and disconnecting
        it would kill that turn mid-sentence. The cap is therefore a target,
        not a hard ceiling — overshooting by the number of concurrent turns is
        the correct failure.
        """
        cap = max(1, get_settings().claude_max_live_clients)
        if len(self._clients) <= cap:
            return
        victims = sorted(
            (k for k, h in self._clients.items() if k != keep and not h.lock.locked()),
            key=lambda k: self._clients[k].last_used,
        )
        for victim in victims:
            if len(self._clients) <= cap:
                break
            handle = self._clients.pop(victim)
            logger.debug("evicting the claude client for %s (LRU)", victim)
            await _disconnect(handle.client)

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

        # Anonymous keys exist so the fleet's worker processes and unit tests —
        # which run with session_id=None — do not accumulate one live CLI per
        # run. They are closed in the finally below.
        key = ctx.session_id or f"anon:{id(ctx)}"
        anonymous = ctx.session_id is None

        try:
            handle = await self._handle_for(
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
            # One turn at a time per client: the CLI cannot interleave two.
            async with handle.lock:
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

                async def _pump_messages() -> None:
                    """Drain one turn off the persistent client → translate →
                    merged queue."""
                    try:
                        await client.query(ctx.prompt)
                        async for message in client.receive_response():
                            async for ev in _translate(message):
                                await merged.put(ev)
                    except asyncio.CancelledError:
                        # THE interrupt the roadmap asks for: the CLI outlives
                        # the turn now, so a cancelled turn has to tell it to
                        # stop working. Without this it keeps burning tokens on
                        # a turn nobody is reading, and the next turn queues
                        # behind it.
                        try:
                            await client.interrupt()
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
        finally:
            # Headless and unit runs get no session id, so nothing would ever
            # come back to close their client.
            if anonymous:
                await self.close_session(key)


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
