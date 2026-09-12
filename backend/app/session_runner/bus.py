"""Event fan-out + replay buffer for a single session.

Splitting this out of ``SessionRunner`` isolates the "who is watching and what
did they miss" concern from turn execution. The bus knows nothing about
providers, prompts, or persistence — it just stamps, retains, and fans out.

Delivery is lossy by design (one stalled browser must not pin the producer) but
never *silently* lossy, which is what the audit found. Three rules make a drop
survivable:

  * A drop is reported to the subscriber it happened to, as one ``stream.gap``
    event per contiguous run of drops carrying that run's total. The frontend
    refetches ``/messages`` when it sees one.
  * Terminal events (``assistant.done`` / ``error``) are never dropped — they
    displace the oldest queued event instead. A UI whose working indicator
    never clears is the worst failure mode in this system; losing an
    intermediate delta is not.
  * The replay ring is larger than a subscriber queue, so ``?since=`` can cover
    any gap a queue is able to open (see :mod:`.config`).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import REPLAY_BUFFER_SIZE, SUBSCRIBER_QUEUE_MAX

logger = logging.getLogger(__name__)

# Events a subscriber must receive even when its queue is full. Every viewer
# clears its working indicator on one of these, so dropping one leaves a
# spinner running until the user reloads the page.
TERMINAL_EVENT_TYPES = frozenset({"assistant.done", "error"})


@dataclass(slots=True)
class _Subscriber:
    """One viewer's queue plus the bookkeeping a gap report needs.

    ``dropped`` and ``gap`` describe the *current* run of drops only: any
    successful delivery ends the run, so the next drop opens a fresh report
    instead of inflating one the viewer may already have consumed.
    """

    queue: asyncio.Queue[dict[str, Any]]
    # Highest `_id` actually handed to this subscriber — the `resume_from` a
    # gap report carries, i.e. the last point this viewer is known to be whole.
    last_delivered_id: int = 0
    # Events lost in the run of drops currently in progress.
    dropped: int = 0
    # The `stream.gap` event already queued for that run, if any. Held by
    # reference so further drops in the same run update its total in place
    # rather than queueing a second report. Mutating it is safe: a gap sits at
    # the tail of a queue that is by definition full, so the consumer cannot
    # have read it yet — reaching it means draining the queue, and the first
    # delivery that then succeeds ends the run and drops this reference.
    gap: dict[str, Any] | None = None


class EventBus:
    """Stamps every event with a monotonic ``_id``, keeps a bounded replay
    ring, and pushes to every live subscriber.

    A subscriber that cannot keep up loses intermediate events rather than
    blocking the turn — but it is told, and it can always re-sync: see
    :func:`broadcast`.
    """

    __slots__ = (
        "session_id",
        "_subs_lock",
        "_subscribers",
        "_recent",
        "_next_id",
        "_on_event",
    )

    def __init__(
        self,
        session_id: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.session_id = session_id
        # Guards the subscribers list so concurrent subscribe/unsubscribe
        # from different WS handlers don't race.
        self._subs_lock = asyncio.Lock()
        self._subscribers: list[_Subscriber] = []
        # Ring buffer keyed by the monotonic `_id` stamped onto each event. A
        # `deque(maxlen=...)` evicts its head on append in O(1); the previous
        # list-and-slice re-copied the whole buffer on *every* event past the
        # cap, which is the wrong shape for the one hot path in the bus.
        self._recent: deque[dict[str, Any]] = deque(maxlen=REPLAY_BUFFER_SIZE)
        self._next_id = 0
        # Optional observer, called with every stamped event before fan-out.
        # `SessionRunner` uses it to remember the outstanding approval card so
        # a viewer that connects later can still answer a gate it never saw.
        # Failures here are logged and swallowed: an observer must not cost a
        # subscriber its events.
        self._on_event = on_event

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
        # One slot beyond the advertised cap, reserved for the out-of-band
        # `stream.gap` marker — it has to fit into a queue that is, by
        # definition, already full. If reporting a loss consumed a normal slot
        # it would itself drop an event, and the report would understate the
        # gap it is reporting.
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX + 1)
        async with self._subs_lock:
            if since_id is None:
                replay: list[dict[str, Any]] = []
            else:
                replay = [ev for ev in self._recent if ev["_id"] > since_id]
            # After replay this viewer is whole up to whatever has been stamped
            # so far. Events from before it subscribed were never dropped *on
            # it* and must not show up in a gap report.
            self._subscribers.append(_Subscriber(queue=q, last_delivered_id=self._next_id))
        return q, replay

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._subs_lock:
            self._subscribers = [s for s in self._subscribers if s.queue is not q]

    async def broadcast(self, ev: dict[str, Any]) -> None:
        """Stamp ``ev`` with a monotonic id, append to the replay buffer, and
        push to every live subscriber.

        Never raises because of a delivery failure. ``execute_turn`` records
        that a turn's terminal event reached the bus only once this returns, so
        a failure escaping from one subscriber would make an event that earlier
        subscribers already hold look un-broadcast — and the turn would then
        emit a second terminal event, the exact duplicate the terminal-event
        contract forbids.
        """
        self._next_id += 1
        wrapped = {**ev, "_id": self._next_id}
        self._recent.append(wrapped)
        if self._on_event is not None:
            try:
                self._on_event(wrapped)
            except Exception:
                logger.exception("event observer failed for session %s", self.session_id)
        terminal = wrapped.get("type") in TERMINAL_EVENT_TYPES
        async with self._subs_lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                self._deliver(sub, wrapped, terminal=terminal)
            except Exception:
                # One sick queue must cost neither the other viewers their
                # event nor the caller its return. See the docstring.
                logger.exception(
                    "session %s: delivering event %d to a subscriber failed",
                    self.session_id,
                    wrapped["_id"],
                )

    # ── delivery ─────────────────────────────────────────────────────────

    def _deliver(
        self, sub: _Subscriber, wrapped: dict[str, Any], *, terminal: bool
    ) -> None:
        if sub.queue.qsize() < SUBSCRIBER_QUEUE_MAX:
            sub.queue.put_nowait(wrapped)
            sub.last_delivered_id = wrapped["_id"]
            # A delivery closes the current run of drops: any later drop gets
            # its own report, positioned after this event rather than folded
            # into a marker the viewer has already gone past.
            sub.dropped = 0
            sub.gap = None
            return
        if terminal:
            self._deliver_terminal(sub, wrapped)
            return
        sub.dropped += 1
        self._report_gap(sub)

    def _deliver_terminal(self, sub: _Subscriber, wrapped: dict[str, Any]) -> None:
        """Enqueue a terminal event into a queue that is already at its cap.

        The reserved slot usually absorbs it. When even that is taken (a gap
        marker is sitting there) the *oldest* queued event gives way: an
        intermediate delta the viewer will refetch anyway is a far smaller loss
        than a working indicator that never clears.
        """
        try:
            sub.queue.put_nowait(wrapped)
        except asyncio.QueueFull:
            try:
                displaced: dict[str, Any] | None = sub.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - full, then empty
                displaced = None
            # The terminal event takes the slot we just freed *first*: a gap
            # report squeezing in ahead of it would fill that slot and cost
            # this subscriber the very event that cannot be dropped.
            sub.queue.put_nowait(wrapped)
            if displaced is not None:
                if displaced is sub.gap:
                    # We discarded this subscriber's own report; the next drop
                    # queues a fresh one.
                    sub.gap = None
                elif displaced.get("type") != "stream.gap":
                    # A real event just went missing. Fold it into the report
                    # so the total still accounts for everything the viewer
                    # lacks, even though the marker sits after it.
                    sub.dropped += 1
                    self._report_gap(sub)
        sub.last_delivered_id = wrapped["_id"]

    def _report_gap(self, sub: _Subscriber) -> None:
        """Ensure this subscriber is carrying a gap report for the run of drops
        in progress, with the run's current total.

        One report per contiguous run: the first drop queues it, later drops in
        the same run update it in place. ``resume_from`` is the last id the
        viewer definitely holds, so a client knows where its picture stopped
        being complete.
        """
        if sub.gap is not None:
            sub.gap["data"]["dropped"] = sub.dropped
            return
        gap: dict[str, Any] = {
            "type": "stream.gap",
            # Deliberately unstamped: a gap is synthesized per subscriber and
            # is not in the replay ring, so it must not burn an `_id` that a
            # `?since=` replay would then be unable to account for.
            "data": {"dropped": sub.dropped, "resume_from": sub.last_delivered_id},
        }
        try:
            sub.queue.put_nowait(gap)
        except asyncio.QueueFull:
            # Even the reserved slot is taken (a terminal event displaced the
            # previous marker). The count keeps accumulating and goes out with
            # the next report that fits.
            logger.warning(
                "session %s: %d dropped events not yet reportable (queue full)",
                self.session_id,
                sub.dropped,
            )
            return
        sub.gap = gap
        logger.warning(
            "session %s subscriber queue full; events after %d are being dropped",
            self.session_id,
            sub.last_delivered_id,
        )
