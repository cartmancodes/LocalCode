"""Event-loop stall detector: measures how long the loop could not run.

What it measures
----------------
A coroutine that does blocking work inline — ``open()``, ``json.load()``,
``os.fsync()``, ``shutil.rmtree()`` — holds the single event loop for the
whole duration, so *every* session's events stop being served, not just the
caller's. That failure is invisible to a functional test: the operation
returns the right value either way. The only observable difference is that
the loop stops ticking.

``detect_stalls`` runs a watchdog coroutine that records the wall gap
between consecutive ticks. When the loop is blocked, the tick spanning the
blocking call is delayed by at least the length of the block, so the worst
observed gap is a direct behavioural measurement of "did this operation run
off the loop".

Why 5 ms sampling
-----------------
The watchdog is itself a loop task, so its sample period is the
measurement's granularity: a stall shorter than one period can hide between
two ticks. 5 ms sits an order of magnitude below the ~50 ms threshold a
test should assert on, so a real stall shows up as a gap around 10x the
noise floor and the verdict never turns on scheduler jitter. Sampling much
faster makes the watchdog a busy loop whose own wake-ups contribute the
jitter it is trying to measure; much slower and a 50 ms stall can be missed
entirely.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass


@dataclass
class StallReport:
    """Mutable handle the watchdog writes into; read it after the block."""

    interval_s: float
    samples: int = 0
    worst_gap_s: float = 0.0

    @property
    def worst_gap_ms(self) -> float:
        return self.worst_gap_s * 1000.0

    def __str__(self) -> str:
        return (
            f"worst gap {self.worst_gap_ms:.1f} ms over {self.samples} samples "
            f"at {self.interval_s * 1000:.0f} ms sampling"
        )


async def _sample(report: StallReport, loop: asyncio.AbstractEventLoop) -> None:
    last = loop.time()
    while True:
        await asyncio.sleep(report.interval_s)
        now = loop.time()
        report.samples += 1
        report.worst_gap_s = max(report.worst_gap_s, now - last)
        last = now


@asynccontextmanager
async def detect_stalls(interval_s: float = 0.005) -> AsyncIterator[StallReport]:
    """Sample the loop every ``interval_s`` for the duration of the block.

    Assert on ``report.worst_gap_ms`` afterwards, and assert
    ``report.samples > 1`` as well: a report with no samples proves nothing,
    and a test that passes because the watchdog never ran is not a test.
    """
    loop = asyncio.get_running_loop()
    report = StallReport(interval_s=interval_s)
    task = asyncio.create_task(_sample(report, loop))
    # Let the watchdog reach its first await before the measured work starts,
    # otherwise its own startup latency is charged to the operation as a stall.
    await asyncio.sleep(0)
    try:
        yield report
    finally:
        # Let the watchdog tick once more before it is cancelled. The sample
        # that *spans* a blocking call only lands after the call returns, so
        # cancelling immediately would report zero samples for the very stall
        # the caller is measuring.
        await asyncio.sleep(interval_s * 2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
