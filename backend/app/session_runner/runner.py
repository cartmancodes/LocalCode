"""``SessionRunner`` — owns one session's turn lifecycle and concurrency.

It no longer does fan-out (delegated to :class:`EventBus`) or message
assembly (delegated to :func:`execute_turn` / :class:`TurnAccumulator`). What
remains is exactly the session-scoped concurrency it must own: the per-session
turn lock, the per-turn approval channel, and the background task handle.

The runner outlives any individual WS — it's only torn down when the session
is deleted (see :mod:`.registry`).
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..orchestrator.base import Provider
from .bus import EventBus
from .turn import execute_turn


class SessionRunner:
    """Owns a single session's turn execution and event fan-out.

    One runner per session, created lazily by ``get_runner()``. WS handlers
    are viewers: they ``subscribe()`` for live events and ``unsubscribe()``
    on disconnect; the turn keeps running across reconnects.
    """

    __slots__ = ("session_id", "_lock", "_bus", "_turn_task", "_approval_q")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Serializes turn execution within a session so a second prompt
        # arriving mid-turn waits (or is rejected — see `start_turn`).
        self._lock = asyncio.Lock()
        self._bus = EventBus(session_id)
        self._turn_task: asyncio.Task[None] | None = None
        # Refreshed per turn so a stale approval click from a previous turn
        # can't satisfy the next turn's gate. Routed in via `submit_approval`.
        self._approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # ── subscription API — used by the WS handler ────────────────────────

    @property
    def is_running(self) -> bool:
        t = self._turn_task
        return t is not None and not t.done()

    @property
    def last_event_id(self) -> int:
        return self._bus.last_event_id

    async def subscribe(
        self, since_id: int | None = None
    ) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Register a subscriber; returns its queue plus any replay events."""
        return await self._bus.subscribe(since_id)

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        await self._bus.unsubscribe(q)

    async def submit_approval(self, msg: dict[str, Any]) -> None:
        """Forward a ``{type:"approval"}`` frame from the WS into the current
        turn's approval channel. No-op if no turn is waiting on it (the
        provider just won't see this message)."""
        await self._approval_q.put(msg)

    # ── turn execution ───────────────────────────────────────────────────

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
        permission_mode: str | None,
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
                permission_mode=permission_mode,
                prompt=prompt,
            )
        )
        return True

    # Upper bound on how long we'll wait for a cancelled turn to unwind.
    # A turn wedged in a non-cancellable native call (e.g. a hung sub-
    # provider on a daemon thread) must NOT make session-delete / shutdown
    # hang — we detach after this and let the daemon thread die at exit.
    _CANCEL_GRACE_S = 5.0

    async def cancel_turn(self) -> None:
        """Cancel the running turn (used on session delete or shutdown).

        Bounded: if the task doesn't unwind within the grace window we give
        up waiting and return anyway. Previously an unbounded ``await t`` on
        a wedged turn made ``DELETE /api/sessions/{id}`` hang indefinitely.
        """
        t = self._turn_task
        if t is None or t.done():
            return
        t.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=self._CANCEL_GRACE_S)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            # TimeoutError → turn is wedged; detach and move on. The turn
            # task and its daemon sub-provider thread are abandoned (daemon
            # threads die with the process); the session is still removed.
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
        permission_mode: str | None,
        prompt: str,
    ) -> None:
        async with self._lock:
            # Fresh approval queue per turn so a click on the previous turn's
            # gate can't satisfy this turn's gate. Built before the turn so
            # `submit_approval` and the RunContext see the same queue.
            self._approval_q = asyncio.Queue()
            await execute_turn(
                session_id=self.session_id,
                bus=self._bus,
                approval_q=self._approval_q,
                provider=provider,
                provider_name=provider_name,
                model=model,
                cwd=cwd,
                additional_dirs=additional_dirs,
                upstream_id=upstream_id,
                fleet_override=fleet_override,
                permission_mode=permission_mode,
                prompt=prompt,
            )
