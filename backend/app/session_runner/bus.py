"""Event fan-out + replay buffer for a single session.

Splitting this out of ``SessionRunner`` isolates the "who is watching and what
did they miss" concern from turn execution. The bus knows nothing about
providers, prompts, or persistence — it just stamps, retains, and fans out.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import REPLAY_BUFFER_SIZE, SUBSCRIBER_QUEUE_MAX

logger = logging.getLogger(__name__)


class EventBus:
    """Stamps every event with a monotonic ``_id``, keeps a bounded replay
    ring, and pushes to every live subscriber. Slow subscribers are dropped
    (their queue fills) so one stalled WS can't pin the producer; they
    re-sync via the replay buffer on reconnect.
    """

    __slots__ = ("session_id", "_subs_lock", "_subscribers", "_recent", "_next_id")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Guards the subscribers list so concurrent subscribe/unsubscribe
        # from different WS handlers don't race.
        self._subs_lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        # Ring buffer keyed by the monotonic `_id` stamped onto each event.
        self._recent: list[dict[str, Any]] = []
        self._next_id = 0

    @property
    def last_event_id(self) -> int:
        return self._next_id

    async def subscribe(
        self, since_id: int | None = None
    ) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Register a subscriber and return its queue plus any replay events.

        ``since_id`` is the highest ``_id`` the caller already received;
        events with id > since_id from the recent buffer are returned for
        replay. Pass ``None`` on first connect to skip replay.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
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

    async def broadcast(self, ev: dict[str, Any]) -> None:
        """Stamp ``ev`` with a monotonic id, append to the replay buffer, and
        push to every live subscriber. Slow subscribers are dropped (queue
        full) so one stalled WS can't pin the producer."""
        self._next_id += 1
        wrapped = {**ev, "_id": self._next_id}
        self._recent.append(wrapped)
        if len(self._recent) > REPLAY_BUFFER_SIZE:
            # Slice rather than del-from-head so memory churn is one
            # allocation per overflow rather than O(n) shifts.
            self._recent = self._recent[-REPLAY_BUFFER_SIZE:]
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
