"""The soak: 200 turns on one session, measured rather than described.

Every other suite asks whether the harness behaves. This one asks what it
costs to keep behaving — for a session that is used all day rather than for
one turn. The defects it exists to catch are the ones that are invisible at
turn 3 and fatal at turn 300: a checkpoint that rewrites the whole message
(Task 13's quadratic write), a per-turn task or file descriptor nobody closes,
a replay ring that retains instead of evicting, a page read that grows with the
log.

Marked ``slow`` and excluded from the default run (``pyproject.toml``'s
``addopts``), because a minute of soak on every ``pytest -q`` is a minute
nobody will keep paying. ``make soak`` runs it — together with the rest of the
suite under ``-W error::UserWarning`` and a per-test wall clock. See
``docs/harness.md`` §11.

**Every number is printed on success.** A soak whose measurements only appear
when it fails teaches nobody the trend, and the trend is the whole product: the
ratios below moved from 100x to 1.2x because someone could see them.

What this is NOT: a benchmark. Nothing here asserts on wall-clock speed except
one deliberately generous paging ceiling, because the machine running it is a
laptop that may be compiling something else. The assertions are ratios,
counts and byte totals, which are the same on a loaded machine as on an idle
one.
"""
from __future__ import annotations

import asyncio
import os
import resource
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store

from .fakes.load import SoakProvider
from .test_storage_cost import counting_file_io

pytestmark = pytest.mark.slow

# ── the load ────────────────────────────────────────────────────────────────
# The brief's numbers, and they are affordable: one turn costs ~4 ms here, so
# the whole soak is ~1 s of turns plus measurement. Keeping them literal means
# the assertions below are statements about 10 000 events, not about a sample
# somebody shrank when it got slow.
TURNS = 200
EVENTS_PER_TURN = 50
BIG_RESULT_EVERY = 20
BIG_RESULT_BYTES = 256 * 1024

# ── budgets ─────────────────────────────────────────────────────────────────

# audit-derived (D13.2, and the same bound `test_storage_cost.py` holds the
# checkpoint path to): total bytes on disk must stay within a small constant
# multiple of the content the provider actually produced. The pre-fix code
# appended a full snapshot of the growing message per tool boundary, which is
# O(boundaries x size) — at these numbers, hundreds of megabytes. Measured
# here: 1.17x.
DISK_RATIO_MAX = 3.0

# chosen-here: peak *traced* Python-object growth across the whole soak.
# Measured 3.3 MB, and it does not grow with the turn count (2.19 MB of
# retained objects at 200 turns, 2.45 MB at 400) because the only structure
# that retains anything is the bounded replay ring. 8 MiB is ~2.4x the
# measurement — loose enough never to flake on allocator behaviour, tight
# enough that a leak of ~40 KB per turn (one small tool result kept) trips it
# inside 200 turns.
TRACEMALLOC_PEAK_GROWTH_MAX = 8 * 1024 * 1024

# chosen-here, and deliberately coarse: ``ru_maxrss`` is a high-water mark for
# the whole process, it includes the tracemalloc bookkeeping itself and it
# never goes down, so it can only be a cross-check on the number above.
# Measured 4.7 MiB of growth; 128 MiB catches "the soak ate the machine"
# without pretending to a precision this metric does not have.
RSS_GROWTH_MAX_BYTES = 128 * 1024 * 1024

# chosen-here: the turn loop creates no tasks of its own, so the honest budget
# is zero. The tolerance is for the loop's own bookkeeping (a timer handle, an
# executor thread's future) rather than for anything this code is allowed to
# leak — one leaked task per turn would be 200.
TASK_GROWTH_MAX = 2

# chosen-here: same reasoning. Every file the store opens is closed inside the
# call that opened it, so the count must be flat across the soak; 2 absorbs a
# transient from the sampling itself (listing /dev/fd opens one).
FD_GROWTH_MAX = 2

# chosen-here: 2 s for the first page of a 400-message, ~4.4 MB log. Measured
# 2.9 ms, so this is not a speed assertion — it is a fast-fail for the shape of
# regression that makes paging linear in the log (re-reading or re-parsing the
# whole file), which at these sizes costs hundreds of milliseconds and climbs.
FIRST_PAGE_BUDGET_S = 2.0

# audit-derived (D13.4): one page must read a bounded tail, not the file. The
# store widens its read window up to ``_TAIL_MAX_SPAN_BYTES`` (1 MiB) and then
# streams, so the ceiling is that span plus room for the page's own large
# messages — a page of 50 messages here carries two 256 KiB tool results.
FIRST_PAGE_READ_MAX_BYTES = 2 * 1024 * 1024


def _open_fds() -> int:
    """Open descriptors for this process. ``/dev/fd`` is a directory on both
    macOS and Linux; listing it is the portable-enough answer that needs no
    dependency."""
    return len(os.listdir("/dev/fd"))


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _drain(queue: asyncio.Queue[dict[str, Any]]) -> int:
    """Consume everything a live viewer would have read this turn.

    A subscriber that never drains would fill at ``SUBSCRIBER_QUEUE_MAX`` and
    turn the soak into a measurement of the drop path instead of the delivery
    path. Draining keeps the bus doing the work a real viewer makes it do.
    """
    count = 0
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return count
        count += 1


async def test_two_hundred_turns_stay_inside_every_cost_budget(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One session, 200 turns, 10 000 events — and what it left behind."""
    cwd = isolated_store / "proj"
    meta = await session_store.create_session(provider="soak", model="m", cwd=str(cwd))
    session_id = str(meta["id"])
    session_dir = Path(meta["cwd"]) / ".localcode" / "sessions" / session_id

    provider = SoakProvider(
        events_per_turn=EVENTS_PER_TURN,
        big_result_every=BIG_RESULT_EVERY,
        big_result_bytes=BIG_RESULT_BYTES,
    )
    # ONE bus for the whole session, as a ``SessionRunner`` holds one: the
    # replay ring's boundedness across 10 000 events is part of what is being
    # measured, and a fresh bus per turn would hide it.
    bus = EventBus(session_id)
    subscription = await bus.subscribe()

    tracemalloc.start()
    try:
        traced_before, _ = tracemalloc.get_traced_memory()
        tasks_before = len(asyncio.all_tasks())
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        fd_samples = [_open_fds()]
        delivered = 0

        started = time.monotonic()
        for turn in range(1, TURNS + 1):
            script = provider.next_script()
            await execute_turn(
                session_id=session_id,
                bus=bus,
                approval_q=asyncio.Queue(),
                provider=provider,
                provider_name="soak",
                model="m",
                cwd=str(cwd),
                additional_dirs=[],
                upstream_id=None,
                fleet_override=None,
                permission_mode=None,
                prompt=script.count_prompt(),
            )
            delivered += _drain(subscription.queue)
            if turn % 20 == 0:
                fd_samples.append(_open_fds())
        soak_s = time.monotonic() - started

        traced_after, traced_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tasks_after = len(asyncio.all_tasks())
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    rss_growth = (rss_after - rss_before) * (1 if os.uname().sysname == "Darwin" else 1024)
    peak_growth = traced_peak - traced_before
    retained_growth = traced_after - traced_before

    # ── the first page, timed and byte-counted ──────────────────────────────
    page_started = time.monotonic()
    with counting_file_io(monkeypatch) as counter:
        page, _next_before, has_more = await session_store.list_messages(
            session_id, limit=50
        )
    page_s = time.monotonic() - page_started
    page_read_bytes = counter.bytes_read["messages.jsonl"]

    disk_bytes = _dir_bytes(session_dir)
    content_bytes = provider.content_bytes
    disk_ratio = disk_bytes / content_bytes

    print(
        "\n[soak] "
        f"{TURNS} turns x {EVENTS_PER_TURN} events = {provider.events_emitted} events "
        f"in {soak_s:.2f} s ({soak_s / TURNS * 1000:.1f} ms/turn), "
        f"{delivered} delivered to one subscriber\n"
        f"[soak] disk {disk_bytes} bytes for {content_bytes} content bytes "
        f"({disk_ratio:.2f}x, budget {DISK_RATIO_MAX:.0f}x)\n"
        f"[soak] tracemalloc peak growth {peak_growth} bytes "
        f"({peak_growth / 1024 / 1024:.2f} MiB, budget "
        f"{TRACEMALLOC_PEAK_GROWTH_MAX / 1024 / 1024:.0f} MiB), "
        f"retained growth {retained_growth} bytes\n"
        f"[soak] ru_maxrss growth {rss_growth / 1024 / 1024:.1f} MiB "
        f"(budget {RSS_GROWTH_MAX_BYTES / 1024 / 1024:.0f} MiB)\n"
        f"[soak] tasks {tasks_before} -> {tasks_after}, "
        f"fds {fd_samples[0]} -> {fd_samples[-1]} "
        f"(min {min(fd_samples)}, max {max(fd_samples)})\n"
        f"[soak] first page: {len(page)} messages in {page_s * 1000:.1f} ms, "
        f"read {page_read_bytes} of {disk_bytes} bytes on disk"
    )

    # ── disk: the permanent guard against the quadratic checkpoint ──────────
    assert disk_ratio < DISK_RATIO_MAX, (
        f"{disk_bytes} bytes on disk for {content_bytes} bytes of content "
        f"({disk_ratio:.1f}x) — a checkpoint is writing the message more than "
        "once per turn"
    )

    # ── memory ──────────────────────────────────────────────────────────────
    assert peak_growth < TRACEMALLOC_PEAK_GROWTH_MAX, (
        f"traced memory peaked {peak_growth} bytes above the pre-soak baseline "
        f"over {TURNS} turns"
    )
    assert rss_growth < RSS_GROWTH_MAX_BYTES, (
        f"RSS high-water mark grew {rss_growth} bytes across the soak"
    )

    # ── tasks and descriptors ───────────────────────────────────────────────
    assert tasks_after - tasks_before <= TASK_GROWTH_MAX, (
        f"{tasks_after - tasks_before} tasks outlived the soak "
        f"({tasks_before} -> {tasks_after}) — a turn is leaking one"
    )
    assert max(fd_samples) - min(fd_samples) <= FD_GROWTH_MAX, (
        f"open descriptors moved between {min(fd_samples)} and "
        f"{max(fd_samples)} across the soak: {fd_samples}"
    )

    # ── paging stays cheap ──────────────────────────────────────────────────
    assert page_s < FIRST_PAGE_BUDGET_S, (
        f"one page of a {disk_bytes}-byte log took {page_s:.2f} s"
    )
    assert page_read_bytes < FIRST_PAGE_READ_MAX_BYTES, (
        f"one page read {page_read_bytes} bytes of a {disk_bytes}-byte log"
    )
    assert len(page) == 50
    assert has_more is True

    # ── the session is still coherent ───────────────────────────────────────
    messages, _before, _more = await session_store.list_messages(session_id)
    assert len(messages) == TURNS * 2, (
        f"{len(messages)} messages after {TURNS} turns — expected one user and "
        "one assistant message each"
    )
    assert [m["role"] for m in messages[:2]] == ["user", "assistant"]

    created = [str(m["created_at"]) for m in messages]
    assert created == sorted(created), "persisted messages are out of order"
    assert len({m["id"] for m in messages}) == len(messages), "duplicate message ids"

    tool_uses: list[str] = []
    tool_results: list[str] = []
    for message in messages:
        for block in message["content"]:
            if block.get("type") == "tool_use":
                tool_uses.append(str(block["id"]))
            elif block.get("type") == "tool_result":
                tool_results.append(str(block["tool_use_id"]))
    assert tool_uses, "the soak produced no tool calls"
    assert tool_uses == provider.tool_ids, "a tool_use was lost or reordered"
    assert tool_uses == tool_results, (
        "every tool_use must have its matching tool_result, in order — "
        f"{len(tool_uses)} calls, {len(tool_results)} results"
    )
