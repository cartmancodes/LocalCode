"""D13.1 — the session store's filesystem work must not run on the loop.

Every method on ``SessionStore`` is ``async def``, which says nothing about
where the work happens: the pre-fix module called ``open()``, ``json.load()``,
``os.fsync()`` and ``shutil.rmtree()`` inline, so one session's append stalled
*every* session's event delivery. Measured at 0.04 ms average on a local SSD,
so this is a latent defect rather than a live stall — but the same code on a
network filesystem freezes the whole backend for as long as the server takes
to answer.

The test is therefore behavioural rather than a search for ``to_thread`` in
the source: make one filesystem call block for ``BLOCK_S``, run the operation,
and watch whether the loop kept running.

# How the verdict is reached, and why it isn't an absolute gap budget

An earlier version asserted ``worst_gap_ms < 50``. That measures the *machine's
scheduling latency* and only incidentally measures our code: on a host running
several agents concurrently the watchdog coroutine itself goes unscheduled for
150 ms at a stretch, so the test failed roughly one run in ten with no defect
present. Raising the number would have traded the flake for blindness — the
regression it exists to catch is a ~200 ms block.

Two load-relative checks replace it, each strong where the other is weak:

  * **Ticks during the operation.** Work that runs ON the loop lets the
    watchdog tick exactly zero times until the operation returns; work in a
    thread leaves the loop free for the whole blocking stretch. A starved
    machine ticks slowly, but the operation also takes longer, so ticks still
    happen — the check does not tighten under load. It can be fooled by a
    *partial* regression (one of several awaits moving back on-loop), which is
    what the second check is for.
  * **Worst gap versus a baseline run of the same operation with the patch
    off.** Both runs see the same scheduling latency, so comparing them
    cancels it out; only loop time our own code consumed remains.

Both are measured **best-of-``ATTEMPTS``**, which is what actually makes the
verdict stable. Scheduling noise can only ever make a gap *worse*, so a single
trial that comes back clean is proof the work was off the loop; a comparison
that happens to straddle a load spike is not evidence of a defect. A real
regression stalls *every* trial — the blocking call is unconditional — so the
retry cannot paper one over. (Sandwiching one baseline around one patched run
was tried first and still flaked 2 runs in 15: a 160 ms spike landed inside the
patched window and missed the baseline window entirely.)
"""
from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import get_settings
from backend.app.storage.sessions import store

from .fakes.stall_detector import StallReport, detect_stalls

# How long each patched filesystem call blocks. Every operation under test
# makes at least two, so a body that ran on the loop shows up as a gap of
# several hundred ms — far outside the scheduling noise this machine produces.
# Small enough that the module still runs in seconds.
BLOCK_S = 0.2

# How long the baseline run keeps the watchdog observing. Long enough for tens
# of samples, so the baseline reflects this machine's sampling latency rather
# than one scheduling accident.
BASELINE_WINDOW_S = 0.15

# Trials before the verdict is "stalled". The first clean one ends the test, so
# this costs nothing on a quiet machine and bounds the flake rate at roughly
# p**ATTEMPTS for a per-trial spike probability p.
ATTEMPTS = 3

# The loop must tick at least this many times while the operation is in flight.
# On-loop work produces zero; off-loop work produces tens even on a badly
# loaded machine, since the operation itself lasts at least BLOCK_S.
MIN_TICKS_DURING_OP = 2

# How much worse than the baseline the patched run's worst gap may be: either
# a multiple of it, or a fixed slice of one blocking call — whichever is larger,
# so the comparison is neither absurdly strict on an idle machine (where the
# baseline is ~5 ms) nor toothless on a loaded one.
GAP_RATIO = 3.0
GAP_HEADROOM_MS = 0.5 * BLOCK_S * 1000  # 100 ms — half of one blocking call


@contextmanager
def slow_filesystem() -> Iterator[None]:
    """Make every ``Path.open`` and ``shutil.rmtree`` take ``BLOCK_S``.

    ``time.sleep`` is the right stand-in: it holds the calling thread and
    releases the GIL, exactly like a real blocking syscall, so it stalls the
    loop if and only if the call runs on the loop's thread.
    """
    real_open = Path.open
    real_rmtree = shutil.rmtree

    def patched_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        time.sleep(BLOCK_S)
        return real_open(self, *args, **kwargs)

    def patched_rmtree(*args: Any, **kwargs: Any) -> Any:
        time.sleep(BLOCK_S)
        return real_rmtree(*args, **kwargs)

    Path.open = patched_open  # type: ignore[method-assign]
    shutil.rmtree = patched_rmtree
    try:
        yield
    finally:
        Path.open = real_open  # type: ignore[method-assign]
        shutil.rmtree = real_rmtree


async def _measure_patched(op: Callable[[], Awaitable[Any]]) -> tuple[StallReport, int]:
    """Run ``op`` with the filesystem slowed down. Returns the stall report and
    how many times the loop ticked while the operation was in flight."""
    async with detect_stalls() as report:
        with slow_filesystem():
            before = report.samples
            await op()
            ticks = report.samples - before
    return report, ticks


async def _measure_baseline(op: Callable[[], Awaitable[Any]]) -> StallReport:
    """Run ``op`` with the filesystem untouched, keeping the watchdog observing
    for ``BASELINE_WINDOW_S`` — a reading of the machine's own scheduling
    latency over a stretch comparable to the patched run's."""
    async with detect_stalls() as report:
        await asyncio.gather(op(), asyncio.sleep(BASELINE_WINDOW_S))
    return report


def _stall_reason(patched: StallReport, ticks: int, baseline: StallReport) -> str | None:
    """Why this trial does not prove the work ran off the loop, or None if it
    does. A trial whose watchdog never sampled proves nothing either, so it is
    a reason too — no measurement can pass by being vacuous."""
    if patched.samples <= 1:
        return f"watchdog never sampled ({patched})"
    if ticks < MIN_TICKS_DURING_OP:
        return (
            f"the loop ticked {ticks} times while the operation held "
            f"{BLOCK_S * 1000:.0f} ms blocking calls — work running on the loop "
            f"produces none at all ({patched})"
        )
    allowed = max(
        baseline.worst_gap_ms * GAP_RATIO, baseline.worst_gap_ms + GAP_HEADROOM_MS
    )
    if patched.worst_gap_ms > allowed:
        return (
            f"worst gap {patched.worst_gap_ms:.1f} ms with the filesystem slowed "
            f"vs {baseline.worst_gap_ms:.1f} ms without it, allowed {allowed:.1f} ms"
        )
    return None


async def _assert_loop_kept_running(
    label: str, prepare_and_run: Callable[[], Awaitable[tuple[StallReport, int, StallReport]]]
) -> None:
    """Pass as soon as one trial comes back clean; fail only if every trial
    stalled. See the module docstring for why best-of-N is the stable
    statistic and why it keeps its teeth."""
    reasons: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        patched, ticks, baseline = await prepare_and_run()
        reason = _stall_reason(patched, ticks, baseline)
        if reason is None:
            return
        reasons.append(f"  attempt {attempt}: {reason}")
    pytest.fail(
        f"{label}: the event loop was blocked on all {ATTEMPTS} attempts — the "
        "filesystem call is running on the loop\n" + "\n".join(reasons)
    )


async def _prepare(home: Path) -> str:
    meta = await store.create_session(
        provider="claude", model="claude-sonnet-4-6", cwd=str(home / "proj")
    )
    await store.append_message(
        meta["id"], {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    )
    return meta["id"]


# Every public coroutine on the store that touches the filesystem. Parametrized
# rather than one test each so a method added later without an offload has an
# obvious place to be covered.
OPERATIONS: dict[str, Callable[[str, Path], Awaitable[Any]]] = {
    "create_session": lambda sid, home: store.create_session(
        provider="claude", model="m", cwd=str(home / "proj2")
    ),
    "get_session": lambda sid, home: store.get_session(sid),
    "list_sessions": lambda sid, home: store.list_sessions(),
    "update_session": lambda sid, home: store.update_session(sid, title="renamed"),
    "append_message": lambda sid, home: store.append_message(
        sid, {"role": "user", "content": [{"type": "text", "text": "x"}]}
    ),
    "write_current": lambda sid, home: store.write_current(
        sid, {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}
    ),
    "list_messages": lambda sid, home: store.list_messages(sid, limit=10),
    "cleanup_expired": lambda sid, home: store.cleanup_expired(retention_days=7, force=True),
    "delete_session": lambda sid, home: store.delete_session(sid),
    "delete_all_sessions": lambda sid, home: store.delete_all_sessions(),
}


@pytest.mark.parametrize("name", sorted(OPERATIONS))
async def test_store_operation_runs_off_the_event_loop(
    name: str, isolated_store: Path
) -> None:
    op = OPERATIONS[name]

    async def trial() -> tuple[StallReport, int, StallReport]:
        # A fresh session per run: ``delete_session`` and
        # ``delete_all_sessions`` consume theirs, and the baseline is only
        # comparable if both runs do the same work.
        baseline_sid = await _prepare(isolated_store)
        baseline = await _measure_baseline(lambda: op(baseline_sid, isolated_store))
        patched_sid = await _prepare(isolated_store)
        patched, ticks = await _measure_patched(lambda: op(patched_sid, isolated_store))
        return patched, ticks, baseline

    await _assert_loop_kept_running(name, trial)


async def test_fleet_config_parse_runs_off_the_event_loop(
    isolated_store: Path, fresh_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13.6: the cache-miss path reads and parses YAML. Low severity — the
    mtime cache makes it rare — but it is the same defect in kind."""
    from backend.app.orchestrator.fleet.loader import _CFG_CACHE, load_fleet_config_async

    cfg_dir = isolated_store / "proj" / ".localcode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "fleet.yaml").write_text("name: offload-test\nmax_steps: 5\n")
    monkeypatch.setenv("LOCALCODE_FLEET_CONFIG", str(cfg_dir / "fleet.yaml"))
    # Warm the settings cache outside the measured windows: building Settings
    # reads .env, which is not the call under test.
    get_settings()
    cwd = str(isolated_store / "proj")

    async def load() -> None:
        # Both runs must take the cache-miss path — a cache hit parses nothing
        # and would make the measurement vacuous.
        _CFG_CACHE.clear()
        cfg = await load_fleet_config_async(cwd)
        assert cfg.name == "offload-test"

    async def trial() -> tuple[StallReport, int, StallReport]:
        baseline = await _measure_baseline(load)
        patched, ticks = await _measure_patched(load)
        return patched, ticks, baseline

    await _assert_loop_kept_running("load_fleet_config_async", trial)
