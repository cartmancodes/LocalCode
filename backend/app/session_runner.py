"""Per-session turn runner — decoupled from any client connection.

The WS handler used to own turn execution: opening a WS, taking a per-session
asyncio.Lock, awaiting `provider.run()`, and forwarding events to the socket.
That meant a turn died the moment the WS closed (session switch, tab close,
network blip), even if the user wanted the pipeline to keep running.

This module flips the ownership: each session has a `SessionRunner` whose
lifecycle is tied to the session, not to a connection. A turn runs as an
independent `asyncio.Task` owned by the runner, broadcasting events to any
number of subscribers. WS handlers become viewers — they `subscribe()` for
live events on connect and `unsubscribe()` on disconnect; the turn keeps
running across reconnects, and a second tab attaching mid-turn streams the
live tail.

Robustness affordances:

  - **Replay buffer**: every broadcast event is stamped with a monotonic
    `_id` and stashed in a bounded ring. A reconnecting client passes
    `?since=<id>` and gets the tail it missed without refetching the whole
    message log.
  - **Bounded subscriber queues**: a slow consumer can't pin memory; once
    its queue fills we drop further events for that subscriber (the client
    can recover via `loadMessages` on its next reconnect).
  - **Synthetic tool_result on dangling tool_use**: same fix that lived in
    `_run_one_turn` — guarantees persisted messages have well-formed
    tool_use/tool_result pairs even when a turn ends via cancellation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .orchestrator.base import Event, Provider, RunContext
from .storage.sessions import store as session_store

logger = logging.getLogger(__name__)


# Bounded ring buffer for replay-on-reconnect. ~256 events covers a typical
# 4-step fleet pipeline (roughly 30-80 events) with margin for chatty turns.
# Larger means more memory per idle session; smaller means a slow reconnect
# can't fully resume and has to refetch via /messages.
_REPLAY_BUFFER_SIZE = 256

# Per-subscriber queue cap. A turn that emits faster than the WS can drain
# blocks on `put_nowait` — we drop instead so the producer can't be held
# hostage by one slow viewer. The dropped subscriber will re-sync on its
# next reconnect via the replay buffer.
_SUBSCRIBER_QUEUE_MAX = 512


class SessionRunner:
    """Owns a single session's turn execution and event fan-out.

    One runner per session, created lazily by `get_runner()`. The runner
    outlives any individual WS — it's only torn down when the session is
    deleted.
    """

    __slots__ = (
        "session_id",
        "_lock",
        "_subs_lock",
        "_subscribers",
        "_recent",
        "_next_event_id",
        "_turn_task",
        "_approval_q",
    )

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Serializes turn execution within a session. Held by `_execute_turn`
        # so a second prompt arriving mid-turn waits or is rejected (see
        # `start_turn`).
        self._lock = asyncio.Lock()
        # Guards the subscribers list so concurrent subscribe/unsubscribe
        # from different WS handlers don't race.
        self._subs_lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        # Ring buffer keyed by monotonic `_id` (also stamped onto each event).
        self._recent: list[dict[str, Any]] = []
        self._next_event_id = 0
        self._turn_task: asyncio.Task[None] | None = None
        # Refreshed per turn so a stale approval click from a previous turn
        # can't satisfy the next turn's gate. Routed in via `submit_approval`.
        self._approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # ─────────────────────────────────────────────────────────────────────
    # Subscription API — used by the WS handler
    # ─────────────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        t = self._turn_task
        return t is not None and not t.done()

    @property
    def last_event_id(self) -> int:
        return self._next_event_id

    async def subscribe(
        self, since_id: int | None = None
    ) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Register a subscriber and return its queue plus any replay events.

        `since_id` is the highest `_id` the caller already received; events
        with id > since_id from the recent buffer are returned for replay.
        Pass `None` on first connect to skip replay.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_MAX
        )
        async with self._subs_lock:
            self._subscribers.append(q)
            if since_id is None:
                replay: list[dict[str, Any]] = []
            else:
                replay = [ev for ev in self._recent if ev["_id"] > since_id]
        return q, replay

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._subs_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    async def submit_approval(self, msg: dict[str, Any]) -> None:
        """Forward a `{type:"approval"}` frame from the WS into the current
        turn's approval channel. No-op if no turn is waiting on it (the
        provider just won't see this message)."""
        await self._approval_q.put(msg)

    # ─────────────────────────────────────────────────────────────────────
    # Turn execution
    # ─────────────────────────────────────────────────────────────────────

    def start_turn(
        self,
        *,
        provider: Provider,
        provider_name: str,
        model: str,
        cwd: str | None,
        additional_dirs: list[str],
        upstream_id: str | None,
        fleet_override: dict[str, Any] | None,
        prompt: str,
    ) -> bool:
        """Kick off a turn as a background task.

        Returns False if a turn is already running on this session — the
        caller should surface that to the user (don't queue silently; a
        queued prompt arriving later is surprising). Returns True if the
        task was scheduled.
        """
        if self.is_running:
            return False
        self._turn_task = asyncio.create_task(
            self._execute_turn(
                provider=provider,
                provider_name=provider_name,
                model=model,
                cwd=cwd,
                additional_dirs=additional_dirs,
                upstream_id=upstream_id,
                fleet_override=fleet_override,
                prompt=prompt,
            )
        )
        return True

    async def cancel_turn(self) -> None:
        """Cancel the running turn (used on session delete or shutdown)."""
        t = self._turn_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _execute_turn(
        self,
        *,
        provider: Provider,
        provider_name: str,
        model: str,
        cwd: str | None,
        additional_dirs: list[str],
        upstream_id: str | None,
        fleet_override: dict[str, Any] | None,
        prompt: str,
    ) -> None:
        async with self._lock:
            # Fresh approval queue per turn so a click on the previous
            # turn's gate can't satisfy this turn's gate.
            self._approval_q = asyncio.Queue()

            # Persist the user message first so a failure further down still
            # leaves a coherent prompt visible in the chat history.
            await session_store.append_message(
                self.session_id,
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            )
            await self._broadcast(
                {
                    "type": "session.started",
                    "data": {"provider": provider_name, "model": model},
                }
            )

            ctx = RunContext(
                model=model,
                prompt=prompt,
                cwd=cwd,
                additional_dirs=additional_dirs,
                upstream_session_id=upstream_id,
                extras=(
                    {"fleet_config_override": fleet_override}
                    if fleet_override
                    else {}
                ),
                approval_channel=self._approval_q,
            )

            assistant_blocks: list[dict[str, Any]] = []
            text_buf: list[str] = []
            cost_usd: float | None = None
            duration_ms: int | None = None
            assistant_message_id: str | None = None

            async def _checkpoint() -> None:
                nonlocal assistant_message_id
                flushed = list(assistant_blocks)
                if text_buf:
                    flushed.append({"type": "text", "text": "".join(text_buf)})
                if not flushed:
                    return
                try:
                    payload: dict[str, Any] = {
                        "role": "assistant",
                        "content": flushed,
                        "cost_usd": cost_usd,
                        "duration_ms": duration_ms,
                    }
                    if assistant_message_id is not None:
                        payload["id"] = assistant_message_id
                    stored = await session_store.append_message(
                        self.session_id, payload
                    )
                    if assistant_message_id is None:
                        assistant_message_id = stored["id"]
                except FileNotFoundError:
                    # Session was deleted while the turn was still draining
                    # (drop_runner cancelled us). Persistence is moot at this
                    # point — skip silently rather than spewing a traceback.
                    pass
                except Exception:
                    logger.exception(
                        "checkpoint persist failed for %s", self.session_id
                    )

            saw_done = False
            try:
                async for ev in provider.run(ctx):
                    if ev.type == "assistant.text":
                        # Heartbeats are live-UI chrome only; skip persistence.
                        if not ev.data.get("heartbeat"):
                            text_buf.append(ev.data.get("text", ""))
                    elif ev.type == "assistant.tool_use":
                        if text_buf:
                            assistant_blocks.append(
                                {"type": "text", "text": "".join(text_buf)}
                            )
                            text_buf = []
                        assistant_blocks.append({"type": "tool_use", **ev.data})
                        await _checkpoint()
                    elif ev.type == "tool.result":
                        assistant_blocks.append({"type": "tool_result", **ev.data})
                        await _checkpoint()
                    elif ev.type == "assistant.done":
                        saw_done = True
                        cost_usd = ev.data.get("cost_usd")
                        duration_ms = ev.data.get("duration_ms")
                    await self._broadcast(ev.to_json())
            except asyncio.CancelledError:
                # Backend shutdown / session delete. Finally still runs and
                # writes the synthetic tool_result so persisted state stays
                # consistent.
                await self._broadcast(
                    {
                        "type": "error",
                        "data": {"message": "turn cancelled (backend shutdown)"},
                    }
                )
                raise
            except Exception as exc:
                logger.exception("turn raised for %s", self.session_id)
                await self._broadcast(
                    {
                        "type": "error",
                        "data": {"message": str(exc) or repr(exc)},
                    }
                )
            finally:
                if text_buf:
                    assistant_blocks.append(
                        {"type": "text", "text": "".join(text_buf)}
                    )
                    text_buf = []
                # Synthesize tool_results for any tool_use that ended without
                # one — see the docstring's "Robustness affordances".
                fulfilled = {
                    b.get("tool_use_id")
                    for b in assistant_blocks
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                for b in list(assistant_blocks):
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and b.get("id")
                        and b["id"] not in fulfilled
                    ):
                        assistant_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": b["id"],
                                "content": (
                                    "Step did not complete — turn ended "
                                    "before a result was produced "
                                    "(cancellation or backend crash). "
                                    "Re-send the prompt to retry."
                                ),
                                "is_error": True,
                            }
                        )
                        fulfilled.add(b["id"])
                if assistant_blocks:
                    await _checkpoint()
                if not saw_done:
                    # Always emit assistant.done so subscribers' UIs clear
                    # their working indicator, even on error / cancellation.
                    await self._broadcast({"type": "assistant.done", "data": {}})

    # ─────────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────────

    async def _broadcast(self, ev: dict[str, Any]) -> None:
        """Stamp `ev` with a monotonic id, append to the replay buffer, and
        push to every live subscriber. Slow subscribers are dropped (queue
        full) so one stalled WS can't pin the producer."""
        self._next_event_id += 1
        wrapped = {**ev, "_id": self._next_event_id}
        self._recent.append(wrapped)
        if len(self._recent) > _REPLAY_BUFFER_SIZE:
            # Slice rather than del-from-head so memory churn is one
            # allocation per overflow rather than O(n) shifts.
            self._recent = self._recent[-_REPLAY_BUFFER_SIZE:]
        async with self._subs_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(wrapped)
            except asyncio.QueueFull:
                logger.warning(
                    "session %s subscriber queue full; dropping event %d",
                    self.session_id,
                    wrapped["_id"],
                )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level registry — one runner per active session, lazily created
# ─────────────────────────────────────────────────────────────────────────────

_runners: dict[str, SessionRunner] = {}
_runners_lock = asyncio.Lock()


async def get_runner(session_id: str) -> SessionRunner:
    """Get-or-create a runner for `session_id`."""
    async with _runners_lock:
        runner = _runners.get(session_id)
        if runner is None:
            runner = SessionRunner(session_id)
            _runners[session_id] = runner
        return runner


async def drop_runner(session_id: str) -> None:
    """Cancel any running turn and forget the runner.

    Called by the session-delete route so a fresh runner is created if a
    new session reuses the same id (shouldn't happen with UUIDs, but the
    invariant is cheap to maintain).
    """
    async with _runners_lock:
        runner = _runners.pop(session_id, None)
    if runner is not None:
        await runner.cancel_turn()


async def drop_all_runners() -> None:
    """Cancel every running turn — used by the wipe-all-sessions route."""
    async with _runners_lock:
        runners = list(_runners.values())
        _runners.clear()
    for r in runners:
        await r.cancel_turn()
