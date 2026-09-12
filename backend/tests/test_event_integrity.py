"""Event-delivery integrity: a dropped event is never a silent one.

Three audits independently landed on the same conclusion — the bus loses events
for a slow browser, tells nobody, and the replay ring was too small to cover the
gap it had just opened. The defects, and the test that pins each:

  * D15.1 — a full subscriber queue dropped events with no signal at all. Now
    every contiguous run of drops produces exactly one ``stream.gap`` event
    carrying the run's total, and terminal events (``assistant.done`` /
    ``error``) are never dropped: they displace the oldest queued event. A
    working indicator that never clears is the worst failure mode here.
  * D15.2 — ``REPLAY_BUFFER_SIZE`` (256) was smaller than
    ``SUBSCRIBER_QUEUE_MAX`` (512), so a viewer that had dropped even one event
    could not recover it with ``?since=``. The ring is now the larger of the
    two, and the relationship is asserted so a retune cannot quietly invert it.
  * D15.3 — a reconnecting tab could never answer a pending approval: the card
    lived only in the ring, and replay happens only with ``?since=``. The runner
    now remembers the outstanding gate and the WS handler re-emits it once.
  * D15.4 — the ring trimmed by re-slicing a list on every event past the cap;
    it is a ``deque(maxlen=...)`` now. What is observable (and tested) is the
    retention contract: exactly the last ``REPLAY_BUFFER_SIZE`` events.

Everything here is driven by explicit broadcasts and ``asyncio.Event`` handoffs
— no sleeps, no wall-clock thresholds.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from backend.app.orchestrator import registry as provider_registry
from backend.app.orchestrator.base import Event, RunContext
from backend.app.routes.sessions import chat_ws
from backend.app.session_runner import bus as bus_mod
from backend.app.session_runner import registry as runner_registry
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.config import REPLAY_BUFFER_SIZE, SUBSCRIBER_QUEUE_MAX
from backend.app.session_runner.runner import SessionRunner
from backend.app.storage.sessions import store as session_store

_APPROVAL_ID = "approval.plan"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _drain(q: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything a subscriber would see, in order."""
    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _gaps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "stream.gap"]


def _real(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") != "stream.gap"]


async def _fill(bus: EventBus, count: int, start: int = 0) -> None:
    for i in range(start, start + count):
        await bus.broadcast({"type": "assistant.text", "data": {"text": str(i)}})


def _approval_event() -> dict[str, Any]:
    return {
        "type": "pipeline.awaiting_approval",
        "data": {
            "id": _APPROVAL_ID,
            "kind": "plan",
            "plan": "step 1, step 2",
            "message": "approve?",
            "timeout_s": 300,
        },
    }


class ExplodingQueue(asyncio.Queue):  # type: ignore[type-arg]
    """A subscriber queue whose ``put_nowait`` fails in a way the bus does not
    expect — the shape of a corrupted or monkeypatched consumer."""

    def put_nowait(self, item: Any) -> None:
        raise RuntimeError("this queue is broken")


# ─────────────────────────────────────────────────────────────────────────────
# D15.2 — the ring must be able to cover any gap a queue can open
# ─────────────────────────────────────────────────────────────────────────────


def test_the_replay_ring_is_larger_than_a_subscriber_queue() -> None:
    """``?since=`` is a lie if a subscriber can drop an event the ring has
    already evicted. Inverting these two numbers re-opens D15.2."""
    assert REPLAY_BUFFER_SIZE > SUBSCRIBER_QUEUE_MAX


async def test_replay_covers_a_gap_as_wide_as_a_subscriber_queue() -> None:
    """The behaviour the size change buys: a viewer that went away for longer
    than its queue is deep still resumes from the ring, with no hole."""
    bus = EventBus("s")
    q, _ = await bus.subscribe()
    await bus.broadcast({"type": "session.started", "data": {}})
    seen = _drain(q)
    assert [e["_id"] for e in seen] == [1]
    await bus.unsubscribe(q)

    # Wider than the old 256-event ring and wider than a subscriber queue.
    await _fill(bus, 600)

    q2, replay = await bus.subscribe(since_id=1)
    assert [e["_id"] for e in replay] == list(range(2, 602))
    assert _gaps(replay) == []
    # A fresh subscriber is whole as of its subscription, so nothing is
    # reported as lost on its own queue either.
    assert _drain(q2) == []


async def test_the_ring_retains_exactly_the_last_replay_buffer_size_events() -> None:
    """D15.4's observable contract: a bounded ring that keeps the newest
    window, whatever the trim is implemented with."""
    bus = EventBus("s")
    await _fill(bus, REPLAY_BUFFER_SIZE + 10)

    _q, replay = await bus.subscribe(since_id=0)
    assert len(replay) == REPLAY_BUFFER_SIZE
    assert replay[0]["_id"] == 11
    assert replay[-1]["_id"] == REPLAY_BUFFER_SIZE + 10
    assert [e["_id"] for e in replay] == sorted(e["_id"] for e in replay)


# ─────────────────────────────────────────────────────────────────────────────
# D15.1 — a drop is reported, and a terminal event is never dropped
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_subscriber_that_never_drains_is_told_exactly_what_it_lost() -> None:
    bus = EventBus("s")
    q, _ = await bus.subscribe()
    total = SUBSCRIBER_QUEUE_MAX + 50

    await _fill(bus, total)

    seen = _drain(q)
    gaps = _gaps(seen)
    assert len(gaps) == 1, [e.get("type") for e in seen[-5:]]
    assert gaps[0]["data"]["dropped"] == 50
    real = _real(seen)
    # Everything up to the overflow arrived, in order, with no interior hole.
    assert [e["_id"] for e in real] == list(range(1, SUBSCRIBER_QUEUE_MAX + 1))
    # The report sits where the loss began: immediately after the last event
    # this subscriber actually received.
    assert seen.index(gaps[0]) == len(real)
    assert gaps[0]["data"]["resume_from"] == real[-1]["_id"]
    # Nothing went missing silently: delivered + reported == broadcast.
    assert len(real) + gaps[0]["data"]["dropped"] == total


async def test_a_terminal_event_displaces_the_oldest_instead_of_being_dropped() -> None:
    """The spinner-forever failure. The queue here is physically full (data
    events plus the gap marker), so the done can only land by displacing."""
    bus = EventBus("s")
    q, _ = await bus.subscribe()
    await _fill(bus, SUBSCRIBER_QUEUE_MAX + 50)
    assert q.full()

    await bus.broadcast({"type": "assistant.done", "data": {"cost_usd": 0.5}})

    seen = _drain(q)
    assert seen[-1]["type"] == "assistant.done"
    assert seen[-1]["data"]["cost_usd"] == 0.5
    # The *oldest* queued event gave way, not the newest.
    assert _real(seen)[0]["_id"] == 2
    assert all(e["_id"] != 1 for e in seen if "_id" in e)
    # And the displaced event is folded into the report, so the viewer is still
    # told about everything it is missing: 50 dropped + the displaced one.
    assert _gaps(seen)[0]["data"]["dropped"] == 51


async def test_an_error_event_also_bypasses_the_queue_cap() -> None:
    """``error`` is terminal for the UI in the same way ``assistant.done`` is."""
    bus = EventBus("s")
    q, _ = await bus.subscribe()
    await _fill(bus, SUBSCRIBER_QUEUE_MAX + 50)

    await bus.broadcast({"type": "error", "data": {"message": "provider died"}})

    seen = _drain(q)
    assert seen[-1]["type"] == "error"
    assert seen[-1]["data"]["message"] == "provider died"


async def test_two_runs_of_drops_produce_two_reports_not_one_and_not_fifty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coalescing is per contiguous run: a successful delivery closes a run, so
    the next drop opens a fresh report rather than extending a marker the viewer
    may already have consumed."""
    monkeypatch.setattr(bus_mod, "SUBSCRIBER_QUEUE_MAX", 4)
    bus = EventBus("s")
    q, _ = await bus.subscribe()

    await _fill(bus, 4)  # ids 1-4 fill the queue to its cap
    await _fill(bus, 3, start=4)  # ids 5-7 dropped → one report, total 3
    early = [q.get_nowait(), q.get_nowait()]  # viewer catches up a little
    await _fill(bus, 1, start=7)  # id 8 fits again → the run is closed
    await _fill(bus, 2, start=8)  # ids 9-10 dropped → a second report, total 2

    seen = early + _drain(q)
    gaps = _gaps(seen)
    assert [g["data"]["dropped"] for g in gaps] == [3, 2]
    # The reports bracket the successful delivery that separated the two runs.
    assert [e.get("_id", "gap") for e in seen] == [1, 2, 3, 4, "gap", 8, "gap"]
    assert gaps[0]["data"]["resume_from"] == 4
    assert gaps[1]["data"]["resume_from"] == 8
    # 5 delivered + 5 reported == 10 broadcast.
    assert len(_real(seen)) + sum(g["data"]["dropped"] for g in gaps) == 10


async def test_delivered_ids_stay_monotonic_across_drops_and_a_forced_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bus_mod, "SUBSCRIBER_QUEUE_MAX", 8)
    bus = EventBus("s")
    q, _ = await bus.subscribe()

    await _fill(bus, 20)
    q.get_nowait()
    await _fill(bus, 3, start=20)
    await bus.broadcast({"type": "assistant.done", "data": {}})

    ids = [e["_id"] for e in _real(_drain(q))]
    assert ids == sorted(set(ids)), ids
    assert len(set(ids)) == len(ids)


# ─────────────────────────────────────────────────────────────────────────────
# broadcast() must never propagate a per-subscriber failure
# ─────────────────────────────────────────────────────────────────────────────


async def test_one_broken_subscriber_costs_neither_the_others_nor_the_caller() -> None:
    """``execute_turn`` marks the turn's terminal event as broadcast only once
    ``broadcast`` returns. If a failure delivering to one subscriber escaped,
    the turn would emit a second terminal event for an event the other
    subscribers already hold — the duplicate the terminal contract forbids.
    """
    bus = EventBus("s")
    broken, _ = await bus.subscribe()
    healthy, _ = await bus.subscribe()
    # Swap in a queue that fails in a way the delivery path does not expect.
    bus._subscribers[0].queue = ExplodingQueue()

    await bus.broadcast({"type": "assistant.text", "data": {"text": "hi"}})
    await bus.broadcast({"type": "assistant.done", "data": {}})

    assert [e["type"] for e in _drain(healthy)] == ["assistant.text", "assistant.done"]
    assert _drain(broken) == []


async def test_a_failing_event_observer_does_not_cost_a_subscriber_its_events() -> None:
    def observer(_ev: dict[str, Any]) -> None:
        raise RuntimeError("observer is broken")

    bus = EventBus("s", on_event=observer)
    q, _ = await bus.subscribe()

    await bus.broadcast({"type": "assistant.done", "data": {}})

    assert [e["type"] for e in _drain(q)] == ["assistant.done"]


# ─────────────────────────────────────────────────────────────────────────────
# D15.3 — a pending approval survives a reconnect
# ─────────────────────────────────────────────────────────────────────────────


class ApprovalGateProvider:
    """Raises an approval gate and blocks on the channel, like the fleet's.

    ``card_on_bus`` is set after the card has been yielded — the generator only
    resumes once the consumer has broadcast it — so a test can wait for the
    gate without sleeping.
    """

    name = "gate"

    def __init__(self) -> None:
        self.card_on_bus = asyncio.Event()

    async def open_session(self, ctx: RunContext) -> str:
        return ""

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        yield Event(type="pipeline.awaiting_approval", data=_approval_event()["data"])
        self.card_on_bus.set()
        assert ctx.approval_channel is not None
        msg = await ctx.approval_channel.get()
        yield Event(
            type="pipeline.approval_received",
            data={"id": _APPROVAL_ID, "value": msg.get("value", "yes")},
        )
        yield Event(type="assistant.done", data={})

    async def aclose(self) -> None:
        return None


class FakeWebSocket:
    """Enough of Starlette's ``WebSocket`` to drive ``chat_ws`` directly.

    A real socket would need a TestClient thread and its own event loop; this
    keeps the handler on the test's loop so the runner, the bus and the
    assertions all see the same state. ``script`` is consumed by
    ``receive_text``; the handler returns cleanly once it runs out.
    """

    def __init__(self, *, since: str | None = None, script: list[str] | None = None):
        self.query_params: dict[str, str] = {} if since is None else {"since": since}
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed = False
        self._script = list(script or [])

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(dict(data))

    async def receive_text(self) -> str:
        if self._script:
            return self._script.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.sent if e.get("type") == event_type]


@pytest.fixture(autouse=True)
def clean_registries() -> Any:
    """A runner left behind keeps a turn task alive into the next test."""
    yield
    for runner in list(runner_registry._runners.values()):
        task = runner._turn_task
        if task is not None and not task.done():
            task.cancel()
    runner_registry._runners.clear()
    runner_registry._detached_turns.clear()
    provider_registry._singletons.clear()


async def test_the_runner_clears_a_pending_gate_on_the_decision_and_on_turn_end() -> None:
    """The card's lifecycle, independent of any viewer. Turn end clears it too:
    an unanswered gate must not be re-offered for a turn that is over."""
    runner = SessionRunner("s")

    await runner._bus.broadcast(_approval_event())
    pending = runner.pending_approval
    assert pending is not None
    assert pending.approval_id == _APPROVAL_ID
    assert pending.event["data"]["plan"] == "step 1, step 2"

    await runner._bus.broadcast(
        {"type": "pipeline.approval_received", "data": {"id": _APPROVAL_ID, "value": "yes"}}
    )
    assert runner.pending_approval is None

    await runner._bus.broadcast(_approval_event())
    assert runner.pending_approval is not None
    await runner._bus.broadcast({"type": "assistant.done", "data": {}})
    assert runner.pending_approval is None


async def _session_with_open_gate(home: Path) -> tuple[str, Any, ApprovalGateProvider]:
    """Start a real turn and leave it blocked on its approval gate."""
    meta = await session_store.create_session(
        provider="stub", model="m", cwd=str(home / "proj")
    )
    session_id = str(meta["id"])
    runner = await runner_registry.get_runner(session_id)
    assert runner is not None
    provider = ApprovalGateProvider()
    assert runner.start_turn(
        provider=provider,  # type: ignore[arg-type]
        provider_name="stub",
        model="m",
        cwd=None,
        additional_dirs=[],
        upstream_id=None,
        fleet_override=None,
        permission_mode=None,
        prompt="plan it",
    )
    await asyncio.wait_for(provider.card_on_bus.wait(), timeout=5)
    return session_id, runner, provider


async def test_a_fresh_viewer_is_offered_the_gate_the_previous_tab_abandoned(
    isolated_store: Path,
) -> None:
    """D15.3: the user closed the only tab mid-gate. Without this they waited
    out APPROVAL_TIMEOUT_S (5 minutes) staring at a turn that could not move."""
    session_id, runner, _provider = await _session_with_open_gate(isolated_store)
    assert runner.pending_approval is not None

    ws = FakeWebSocket()  # fresh connection: no `?since=`, so no replay
    await chat_ws(ws, session_id)  # type: ignore[arg-type]

    cards = ws.of_type("pipeline.awaiting_approval")
    assert len(cards) == 1, [e.get("type") for e in ws.sent]
    assert cards[0]["data"]["id"] == _APPROVAL_ID
    assert cards[0]["data"]["plan"] == "step 1, step 2"


async def test_a_replayed_gate_is_not_offered_twice(isolated_store: Path) -> None:
    """De-duplication by approval id: a `?since=` replay that already carried
    the card must not be followed by a second copy of it."""
    session_id, runner, _provider = await _session_with_open_gate(isolated_store)

    ws = FakeWebSocket(since="0")  # replay everything, card included
    await chat_ws(ws, session_id)  # type: ignore[arg-type]

    assert len(ws.of_type("pipeline.awaiting_approval")) == 1, [
        e.get("type") for e in ws.sent
    ]


async def test_an_answered_gate_is_not_offered_to_the_next_viewer(
    isolated_store: Path,
) -> None:
    session_id, runner, _provider = await _session_with_open_gate(isolated_store)

    await runner.submit_approval({"type": "approval", "id": _APPROVAL_ID, "value": "yes"})
    task = runner._turn_task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)
    assert runner.pending_approval is None

    ws = FakeWebSocket()
    await chat_ws(ws, session_id)  # type: ignore[arg-type]

    assert ws.of_type("pipeline.awaiting_approval") == []
