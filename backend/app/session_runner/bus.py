"""Event fan-out + replay buffer for a single session.

Splitting this out of ``SessionRunner`` isolates the "who is watching and what
did they miss" concern from turn execution. The bus knows nothing about
providers, prompts, or persistence — it just stamps, retains, and fans out.

Delivery is lossy by design (one stalled browser must not pin the producer) but
never *silently* lossy, which is what the audit found. Four rules make a drop
survivable:

  * A drop is reported to the subscriber it happened to, as one ``stream.gap``
    event per contiguous run of drops carrying that run's total. The frontend
    refetches ``/messages`` when it sees one.
  * A report is *owed* until it is queued, and it is never written off. Nothing
    resets the count except queueing the report that carries it, and a terminal
    event — which may be the last thing a viewer ever receives — takes whatever
    is owed out with it.
  * Terminal events (``assistant.done`` / ``error``) are never dropped — they
    displace the oldest queued event instead. A UI whose working indicator never
    clears is the worst failure mode in this system; losing an intermediate
    delta is not.
  * The replay ring is larger than a subscriber queue, so ``?since=`` can cover
    any gap a queue is able to open (see :mod:`.config`) — and when the ring has
    nevertheless evicted past a client's ``since_id``, the replay itself starts
    with a gap report rather than silently beginning mid-stream.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import REPLAY_BUFFER_SIZE, SUBSCRIBER_QUEUE_MAX

logger = logging.getLogger(__name__)

# Events a subscriber must receive even when its queue is full. Every viewer
# clears its working indicator on one of these, so dropping one leaves a
# spinner running until the user reloads the page.
TERMINAL_EVENT_TYPES = frozenset({"assistant.done", "error"})

_GAP = "stream.gap"


def _gap_event(*, dropped: int, resume_from: int) -> dict[str, Any]:
    """A gap report. Deliberately unstamped: a gap is synthesized per
    subscriber and is not in the replay ring, so it must not burn an ``_id``
    that a ``?since=`` replay would then be unable to account for."""
    return {"type": _GAP, "data": {"dropped": dropped, "resume_from": resume_from}}


@dataclass(slots=True)
class Subscription:
    """What a new viewer needs to become whole.

    ``watermark`` is the highest ``_id`` stamped at the moment this subscriber
    was registered — the boundary between "the caller must be handed this
    itself" and "the live queue will carry it". The WS handler needs it to
    decide whether to re-emit an outstanding approval card without risking
    either a duplicate or a miss.
    """

    queue: asyncio.Queue[dict[str, Any]]
    replay: list[dict[str, Any]] = field(default_factory=list)
    watermark: int = 0


@dataclass(slots=True)
class _Subscriber:
    """One viewer's queue plus the bookkeeping a gap report needs."""

    queue: asyncio.Queue[dict[str, Any]]
    # Highest `_id` actually handed to this subscriber — the `resume_from` a
    # gap report carries, i.e. the last point this viewer is known to be whole.
    last_delivered_id: int = 0
    # Drops this subscriber has NOT yet been told about. Only `_report_gap`
    # clears it, and only by queueing the report that carries the count: a
    # number written off here is an event lost in silence.
    pending_drops: int = 0
    # The `stream.gap` event queued for the run of drops in progress, if any.
    # Held by reference so further drops in the same run update its total in
    # place rather than queueing a second report. Mutating it is safe: a tracked
    # marker sits at the tail of a queue that is by definition full, so the
    # consumer cannot have read it yet — reaching it means draining the queue,
    # and the first delivery that then succeeds drops this reference.
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

    async def subscribe(self, since_id: int | None = None) -> Subscription:
        """Register a subscriber and return its queue, replay and watermark.

        ``since_id`` is the highest ``_id`` the caller already received; events
        newer than that are returned for replay. Pass ``None`` on first connect
        to skip replay.

        If the ring has already evicted past ``since_id`` the replay cannot
        start where the client stopped, so it is prefixed with a gap report.
        The report is what triggers the refetch: ``ChatPane`` calls
        ``loadMessages`` when it receives a ``stream.gap``, and otherwise only
        on a reconnect where it never saw an event id at all
        (``wasReconnect && lastEventId.current === 0``). A client that has been
        receiving events has no other way back — without the marker it appends
        a tail onto a transcript with a hole and nothing ever tells it.
        """
        # One slot beyond the advertised cap, reserved for the out-of-band
        # `stream.gap` marker — it has to fit into a queue that is, by
        # definition, already full. If reporting a loss consumed a normal slot
        # it would itself drop an event, and the report would understate the
        # gap it is reporting.
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX + 1)
        async with self._subs_lock:
            watermark = self._next_id
            replay: list[dict[str, Any]] = []
            if since_id is not None:
                replay = [ev for ev in self._recent if ev["_id"] > since_id]
                oldest = self._recent[0]["_id"] if self._recent else None
                if oldest is not None and oldest > since_id + 1:
                    replay.insert(
                        0,
                        _gap_event(dropped=oldest - since_id - 1, resume_from=since_id),
                    )
                    logger.warning(
                        "session %s: replay from %d starts at %d; the ring had "
                        "already evicted the gap",
                        self.session_id,
                        since_id,
                        oldest,
                    )
            # After replay this viewer is whole up to the watermark. Events from
            # before it subscribed were never dropped *on it* and must not show
            # up in a gap report.
            self._subscribers.append(_Subscriber(queue=q, last_delivered_id=watermark))
        return Subscription(queue=q, replay=replay, watermark=watermark)

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
            sub.gap = None
            # Anything still owed (a report that found no room earlier) goes
            # out now that there is some. The count is never written off.
            if sub.pending_drops:
                self._report_gap(sub)
            return
        if terminal:
            self._deliver_terminal(sub, wrapped)
            return
        sub.pending_drops += 1
        self._report_gap(sub)

    def _deliver_terminal(self, sub: _Subscriber, wrapped: dict[str, Any]) -> None:
        """Enqueue a terminal event into a queue that is already at its cap,
        taking any owed gap report out with it.

        A terminal event may be the last thing this subscriber ever receives, so
        a report still owed afterwards is a report that is never delivered. That
        was the hole: a queued-but-untracked marker displaced here vanished along
        with the drops it described, ``assistant.done`` then cleared the working
        indicator, and the viewer was left looking at a transcript with an
        unannounced gap in it and no reason to refetch.

        Room is made for the event first and the report second, at most one
        displacement each: a second victim can only add to the count the one
        report already carries.
        """
        q = sub.queue
        if q.full():
            self._displace_oldest(sub)
        if sub.pending_drops and sub.gap is None and q.maxsize and q.maxsize - q.qsize() < 2:
            # Something is owed and no queued marker can carry it, so the report
            # needs a slot of its own beside the terminal event's.
            self._displace_oldest(sub)
        # `reserve=1` keeps the terminal event's slot inviolate: a report that
        # squeezed into it would cost this subscriber the one event that must
        # never be dropped.
        self._report_gap(sub, reserve=1)
        q.put_nowait(wrapped)
        sub.last_delivered_id = wrapped["_id"]

    def _displace_oldest(self, sub: _Subscriber) -> None:
        """Drop the head of a full queue to make room, accounting for what was
        lost. A displaced marker carries a total of its own — adding it to the
        owed count is what keeps that total alive; over-reporting is harmless
        (the client's response is a full ``/messages`` refetch either way),
        losing it is not."""
        try:
            displaced = sub.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - full, then empty
            return
        if displaced.get("type") == _GAP:
            if displaced is sub.gap:
                sub.gap = None
            sub.pending_drops += int((displaced.get("data") or {}).get("dropped") or 0)
        else:
            sub.pending_drops += 1

    def _report_gap(self, sub: _Subscriber, *, reserve: int = 0) -> None:
        """Hand this subscriber's owed drops to a queued gap report.

        One report per contiguous run: the first drop queues it, later drops in
        the same run fold into it in place. ``resume_from`` is the last id the
        viewer definitely holds, so a client knows where its picture stopped
        being complete. ``pending_drops`` is cleared only here, and only once
        the count is actually carried by something queued.

        ``reserve`` is how many free slots must remain untouched — the caller
        uses it to protect a slot it is about to need for an event of its own.
        """
        if sub.pending_drops == 0:
            return
        if sub.gap is not None:
            # Folding into a queued marker needs no slot at all.
            sub.gap["data"]["dropped"] += sub.pending_drops
            sub.pending_drops = 0
            return
        q = sub.queue
        if q.maxsize and q.maxsize - q.qsize() <= reserve:
            # No room we are allowed to take. The count stays owed — the next
            # delivery or terminal event carries it out.
            logger.warning(
                "session %s: %d dropped events not yet reportable (queue full)",
                self.session_id,
                sub.pending_drops,
            )
            return
        gap = _gap_event(dropped=sub.pending_drops, resume_from=sub.last_delivered_id)
        q.put_nowait(gap)
        sub.gap = gap
        sub.pending_drops = 0
        logger.warning(
            "session %s subscriber queue full; events after %d are being dropped",
            self.session_id,
            sub.last_delivered_id,
        )
