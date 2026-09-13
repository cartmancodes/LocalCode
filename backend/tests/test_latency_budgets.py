"""Latency and cost budgets for one turn — fast, deterministic, no vendor CLI.

The soak asks what a session costs over a day. This asks what a turn costs
right now: does the event loop keep running while a turn streams, how fast can
the bus fan out, how many times does a 500-boundary turn touch the disk, and
how long before a viewer sees anything at all.

**Every budget below is a named constant with a comment saying what it
protects, where the number came from, and whether it is `audit-derived` (a
figure the audit or an earlier task measured) or `chosen-here` (picked in this
task from a measurement printed by these tests). A future reader hitting a
failure needs to know whether they are looking at a regression or at a busy
laptop, and only provenance answers that.

**On absolute stall budgets.** The first version of the stall case asserted
"worst loop gap < 50 ms", and on this machine that failed roughly one run in
ten with nothing wrong — under concurrent load the watchdog coroutine itself
went unscheduled for 158 ms. ``test_storage_offload.py`` had already met this
and replaced it with a load-relative verdict; the same shape is used here (see
that module's docstring for the full argument):

  * the watchdog must TICK while the turn runs — work that blocks the loop
    produces no ticks at all, and a busy machine does not make that check
    stricter, because the turn slows down with everything else;
  * the worst gap during the turn is compared against a baseline measured
    through the same watchdog with nothing running, so the machine's own
    scheduling latency cancels out of both sides.

The 50 ms figure survives as the sanity ceiling on the BASELINE: if an idle
loop cannot tick within 50 ms, the machine is too loaded to measure anything
and the test SKIPS with the number it measured, rather than failing the code
for the machine's behaviour.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from backend.app.orchestrator.base import Event, RunContext
from backend.app.session_runner.accumulator import TurnAccumulator
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store

from .fakes.load import burst_events
from .fakes.providers import ScriptedProvider
from .fakes.stall_detector import StallReport, detect_stalls
from .test_storage_cost import counting_file_io

# ─────────────────────────────────────────────────────────────────────────────
# Budgets
# ─────────────────────────────────────────────────────────────────────────────

# The turn the stall case runs. audit-derived: the brief's 2000 events, which
# is a long streaming answer with heavy tool use — 500 tool boundaries, each
# one a checkpoint opportunity, which is where blocking I/O would sit.
TURN_EVENTS = 2000
TURN_TOOL_PAIRS = 500

# audit-derived: the 50 ms the brief asks for, kept as a ceiling on the
# BASELINE rather than on the measurement. The audit measured an fsync at
# 0.04 ms, so an idle loop that cannot tick inside 50 ms is not measuring this
# code at all — it is measuring a machine with no scheduling headroom left.
BASELINE_CEILING_MS = 50.0

# chosen-here: the same shape ``test_storage_offload.py`` uses. The allowance
# is whichever is larger of (baseline x ratio) and (baseline + headroom), so
# the comparison is neither absurdly strict on an idle machine — where the
# baseline is the watchdog's own ~6 ms sampling granularity — nor toothless on
# a loaded one. 25 ms of headroom is half the brief's absolute figure; the
# regression this catches (a persist moved back on-loop) costs tens to hundreds
# of ms per occurrence. Measured on an idle machine: worst 6.4-8.8 ms against a
# 6.4 ms baseline.
STALL_GAP_RATIO = 3.0
STALL_GAP_HEADROOM_MS = 25.0

# chosen-here: work that runs ON the loop produces exactly zero ticks, so two
# is already proof of the opposite. Same constant, same reasoning, as the
# storage-offload suite.
STALL_MIN_TICKS = 2

# chosen-here: scheduling noise can only make a gap worse, so one clean trial
# is proof; a real stall is unconditional and fails every trial. Three bounds
# the flake rate at p**3 for a per-trial spike probability p.
STALL_ATTEMPTS = 3

# How long the baseline watchdog observes an idle loop. chosen-here: 0.15 s is
# ~30 samples at the detector's 5 ms period — enough that the baseline is this
# machine's sampling latency rather than one scheduling accident — and it is
# comparable to how long the measured turn takes (~40 ms).
BASELINE_WINDOW_S = 0.15

# audit-derived: the brief's 10 000 events to 3 subscribers.
BUS_EVENTS = 10_000
BUS_SUBSCRIBERS = 3
# chosen-here: the producer drains its viewers every 128 events, so the
# measurement is dominated by the DELIVERY path rather than by the drop path.
# Without this, a subscriber queue (512 + 1) fills after 512 events and the
# rest of the run measures gap accounting instead of fan-out.
BUS_DRAIN_EVERY = 128

# chosen-here: a throughput FLOOR, not a time budget, because the machine
# varies and the defect does not. Measured 680 000-690 000 events/s over three
# trials; 50 000/s is ~14x below that, which a heavily loaded laptop still
# clears, while the regression it guards (D15.4 — the replay ring re-sliced a
# 2048-entry list on every event past the cap) is an order-of-magnitude loss,
# not a 20% one.
BUS_THROUGHPUT_FLOOR_EVENTS_PER_S = 50_000.0

# audit-derived: the brief's 500 tool boundaries.
CHECKPOINT_BOUNDARIES = 500

# chosen-here: the throttle's growth arm doubles the bar it sets for the next
# write, so the number of writes over a turn is logarithmic in the final
# message — measured 5 for 1000 boundaries. 12 leaves room for the time arm to
# fire a few times on a machine slow enough to spend seconds inside the turn,
# and still fails by 40x if the throttle is removed and every boundary writes.
CHECKPOINT_MAX_WRITES = 12

# audit-derived (D13.2): the same 3x bound `test_storage_cost.py` holds the
# checkpoint path to. Measured 1.9x here.
CHECKPOINT_BYTES_RATIO_MAX = 3.0

# chosen-here: time from ``execute_turn`` starting to the first event landing
# in a subscriber's queue. Measured 1.0 ms — the prompt's fsync and the index
# write happen first, so this is not a free path. 250 ms is ~250x the
# measurement; it catches a turn that does something synchronous and slow
# before it says anything to the viewer (a config parse, a full-log read), and
# nothing else.
FIRST_EVENT_BUDGET_S = 0.25


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def streaming_turn(events: int, tool_pairs: int):
    """A turn of ``events`` events, ``tool_pairs`` of them tool boundaries.

    Generated lazily so the provider does not hold the whole turn in memory —
    which would move the allocation cost inside the measured window.
    """

    async def run(ctx: RunContext) -> AsyncIterator[Event]:
        for i in range(tool_pairs):
            tool_id = f"lat-{i:04d}"
            yield Event(
                type="assistant.tool_use",
                data={"id": tool_id, "name": "Read", "input": {"path": "x" * 200}},
            )
            yield Event(
                type="tool.result",
                data={"tool_use_id": tool_id, "content": "R" * 800, "is_error": False},
            )
        for i in range(max(events - tool_pairs * 2 - 1, 0)):
            yield Event(type="assistant.text", data={"text": f"tok{i} "})
        yield Event(type="assistant.done", data={})

    return run


async def _new_session(home: Path) -> tuple[str, Path]:
    cwd = home / "proj"
    meta = await session_store.create_session(provider="scripted", model="m", cwd=str(cwd))
    return str(meta["id"]), cwd


async def _run_turn(session_id: str, cwd: Path, *, events: int, pairs: int) -> None:
    await execute_turn(
        session_id=session_id,
        bus=EventBus(session_id),
        approval_q=asyncio.Queue(),
        provider=ScriptedProvider(streaming_turn(events, pairs)),
        provider_name="scripted",
        model="m",
        cwd=str(cwd),
        additional_dirs=[],
        upstream_id=None,
        fleet_override=None,
        permission_mode=None,
        prompt="stream me two thousand events",
    )


async def _baseline_gap() -> StallReport:
    """The machine's own sampling latency, measured through the same watchdog
    with nothing else running."""
    async with detect_stalls() as report:
        await asyncio.sleep(BASELINE_WINDOW_S)
    return report


def _stall_reason(measured: StallReport, ticks: int, baseline: StallReport) -> str | None:
    """Why this trial does not prove the turn stayed off the loop, or None.

    A trial whose watchdog never sampled proves nothing, so it counts as a
    reason: no measurement may pass by being vacuous.
    """
    if measured.samples <= 1:
        return f"watchdog never sampled ({measured})"
    if ticks < STALL_MIN_TICKS:
        return (
            f"the loop ticked {ticks} times during the turn — work running on "
            f"the loop produces none at all ({measured})"
        )
    allowed = max(
        baseline.worst_gap_ms * STALL_GAP_RATIO,
        baseline.worst_gap_ms + STALL_GAP_HEADROOM_MS,
    )
    if measured.worst_gap_ms > allowed:
        return (
            f"worst gap {measured.worst_gap_ms:.1f} ms during the turn vs "
            f"{baseline.worst_gap_ms:.1f} ms idle, allowed {allowed:.1f} ms"
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. A long turn must not block the event loop
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_two_thousand_event_turn_never_blocks_the_event_loop(
    isolated_store: Path,
) -> None:
    """Every other session's events are served by the same loop this turn runs
    on. A turn that holds it — a persist that stopped being offloaded, a JSON
    dump of a 2 MB message inline — stops the whole backend, and no functional
    test can see it: the turn still produces the right events."""
    session_id, cwd = await _new_session(isolated_store)

    # One warm-up turn, outside every measured window. The first turn on a
    # process pays import, page-cache and allocator costs that are not what
    # this measures, and charging them here produced a 31 ms gap against an
    # otherwise 7 ms measurement.
    await _run_turn(session_id, cwd, events=TURN_EVENTS, pairs=TURN_TOOL_PAIRS)

    reasons: list[str] = []
    for attempt in range(1, STALL_ATTEMPTS + 1):
        baseline = await _baseline_gap()
        if baseline.worst_gap_ms > BASELINE_CEILING_MS:
            pytest.skip(
                f"the idle event loop could not tick within "
                f"{BASELINE_CEILING_MS:.0f} ms (worst {baseline.worst_gap_ms:.1f} ms "
                f"over {baseline.samples} samples) — this machine is too loaded "
                "to measure a stall"
            )
        started = time.monotonic()
        async with detect_stalls() as measured:
            before = measured.samples
            await _run_turn(session_id, cwd, events=TURN_EVENTS, pairs=TURN_TOOL_PAIRS)
            ticks = measured.samples - before
        turn_s = time.monotonic() - started
        reason = _stall_reason(measured, ticks, baseline)
        if reason is None:
            allowed = max(
                baseline.worst_gap_ms * STALL_GAP_RATIO,
                baseline.worst_gap_ms + STALL_GAP_HEADROOM_MS,
            )
            print(
                f"\n[latency] {TURN_EVENTS}-event turn in {turn_s * 1000:.1f} ms: "
                f"worst loop gap {measured.worst_gap_ms:.2f} ms over {ticks} ticks, "
                f"idle baseline {baseline.worst_gap_ms:.2f} ms "
                f"(allowed {allowed:.1f} ms)"
            )
            return
        reasons.append(f"  attempt {attempt}: {reason}")

    pytest.fail(
        f"a {TURN_EVENTS}-event turn blocked the event loop on all "
        f"{STALL_ATTEMPTS} attempts\n" + "\n".join(reasons)
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Bus fan-out
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_bus_fans_ten_thousand_events_out_to_three_subscribers() -> None:
    """Throughput, not wall time: the machine varies, the defect does not.

    D15.4 trimmed the replay ring by re-slicing a list on every event past the
    cap, which is O(ring) per event. Asserting a floor in events/second catches
    that (and anything else that makes fan-out super-linear) without asserting
    that this laptop is fast.
    """
    bus = EventBus("throughput")
    subscriptions = [await bus.subscribe() for _ in range(BUS_SUBSCRIBERS)]
    events = burst_events(BUS_EVENTS)

    delivered = 0
    started = time.perf_counter()
    for index, ev in enumerate(events):
        await bus.broadcast(ev)
        if index % BUS_DRAIN_EVERY == BUS_DRAIN_EVERY - 1:
            for sub in subscriptions:
                while True:
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    delivered += 1
    elapsed = time.perf_counter() - started
    for sub in subscriptions:
        while True:
            try:
                sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            delivered += 1
    throughput = BUS_EVENTS / elapsed

    print(
        f"\n[latency] bus: {BUS_EVENTS} events to {BUS_SUBSCRIBERS} subscribers in "
        f"{elapsed * 1000:.1f} ms = {throughput:,.0f} events/s "
        f"(floor {BUS_THROUGHPUT_FLOOR_EVENTS_PER_S:,.0f}/s), {delivered} deliveries"
    )

    assert throughput > BUS_THROUGHPUT_FLOOR_EVENTS_PER_S, (
        f"{throughput:,.0f} events/s fanning out to {BUS_SUBSCRIBERS} subscribers"
    )
    # The measurement is only meaningful if the events were actually delivered
    # rather than dropped: a bus that dropped everything would be arbitrarily
    # "fast". Viewers drained often enough to keep up must receive everything.
    assert delivered == BUS_EVENTS * BUS_SUBSCRIBERS, (
        f"{delivered} deliveries for {BUS_EVENTS} events x {BUS_SUBSCRIBERS} "
        "subscribers — viewers that kept up still lost events"
    )
    assert bus.last_event_id == BUS_EVENTS


# ─────────────────────────────────────────────────────────────────────────────
# 3. Checkpoint cost per tool boundary
# ─────────────────────────────────────────────────────────────────────────────


async def test_five_hundred_tool_boundaries_cost_a_handful_of_writes(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint writes the WHOLE message accumulated so far, so one per
    boundary is O(boundaries x final size) — Task 13's quadratic write. The
    throttle turns that into a logarithmic number of writes whose total volume
    is a small multiple of the final message; both halves are asserted, because
    a write count alone could be satisfied by writing once and losing the turn.
    """
    cwd = isolated_store / "proj"
    meta = await session_store.create_session(provider="scripted", model="m", cwd=str(cwd))
    session_id = str(meta["id"])
    acc = TurnAccumulator()

    with counting_file_io(monkeypatch) as counter:
        started = time.perf_counter()
        for i in range(CHECKPOINT_BOUNDARIES // 2):
            acc.add_tool_use({"id": f"t{i}", "name": "Read", "input": {"path": "x" * 200}})
            await acc.checkpoint(session_id)
            acc.add_tool_result({"tool_use_id": f"t{i}", "content": "R" * 800})
            await acc.checkpoint(session_id)
        await acc.checkpoint(session_id, final=True)
        elapsed = time.perf_counter() - started

    log = Path(meta["cwd"]) / ".localcode" / "sessions" / session_id / "messages.jsonl"
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    final_bytes = len(lines[-1].encode("utf-8"))
    written = counter.message_bytes_written()
    writes = counter.message_writes()

    print(
        f"\n[latency] {CHECKPOINT_BOUNDARIES} tool boundaries in {elapsed * 1000:.1f} ms: "
        f"{writes} writes (max {CHECKPOINT_MAX_WRITES}), {written} bytes for a "
        f"{final_bytes}-byte message ({written / final_bytes:.2f}x, budget "
        f"{CHECKPOINT_BYTES_RATIO_MAX:.0f}x)"
    )

    assert writes <= CHECKPOINT_MAX_WRITES, (
        f"{writes} writes for {CHECKPOINT_BOUNDARIES} boundaries — the "
        "checkpoint throttle is not throttling"
    )
    assert written < CHECKPOINT_BYTES_RATIO_MAX * final_bytes, (
        f"wrote {written} bytes for a {final_bytes}-byte message"
    )
    # The turn is still fully persisted: 250 tool_use + 250 tool_result blocks.
    messages, _before, _more = await session_store.list_messages(session_id)
    assert len(messages) == 1
    assert len(messages[0]["content"]) == CHECKPOINT_BOUNDARIES


# ─────────────────────────────────────────────────────────────────────────────
# 4. First-event latency
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_first_event_reaches_a_subscriber_promptly(
    isolated_store: Path,
) -> None:
    """What the user experiences as "did it hear me?".

    Two numbers, because they fail differently: the first event of any kind
    (``session.started``, which lands after the prompt has been fsynced) and
    the first event the PROVIDER produced. A regression in persistence moves
    the first; a regression in the drain loop moves the second.
    """
    session_id, cwd = await _new_session(isolated_store)
    bus = EventBus(session_id)
    subscription = await bus.subscribe()

    async def one_text_turn(ctx: RunContext) -> AsyncIterator[Event]:
        yield Event(type="assistant.text", data={"text": "hello"})
        yield Event(type="assistant.done", data={})

    started = time.perf_counter()
    turn = asyncio.create_task(
        execute_turn(
            session_id=session_id,
            bus=bus,
            approval_q=asyncio.Queue(),
            provider=ScriptedProvider(one_text_turn),
            provider_name="scripted",
            model="m",
            cwd=str(cwd),
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt="hello",
        )
    )
    try:
        first = await asyncio.wait_for(subscription.queue.get(), FIRST_EVENT_BUDGET_S * 20)
        first_s = time.perf_counter() - started
        while True:
            ev = await asyncio.wait_for(
                subscription.queue.get(), FIRST_EVENT_BUDGET_S * 20
            )
            if ev["type"] == "assistant.text":
                break
        first_text_s = time.perf_counter() - started
    finally:
        await turn

    print(
        f"\n[latency] first event ({first['type']}) reached a subscriber after "
        f"{first_s * 1000:.2f} ms, first provider event after "
        f"{first_text_s * 1000:.2f} ms (budget {FIRST_EVENT_BUDGET_S * 1000:.0f} ms)"
    )

    assert first["type"] == "session.started"
    assert first_s < FIRST_EVENT_BUDGET_S, (
        f"the first event took {first_s * 1000:.0f} ms to reach a viewer"
    )
    assert first_text_s < FIRST_EVENT_BUDGET_S, (
        f"the provider's first event took {first_text_s * 1000:.0f} ms to reach a viewer"
    )
