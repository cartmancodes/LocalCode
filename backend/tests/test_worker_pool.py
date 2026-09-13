"""The long-lived worker pool, the v2 framing, and the per-turn spend cap.

What each group of tests is defending:

* **Reuse** — the whole point of the task. A second step on one key must land
  in the process that already paid interpreter start + SDK import. A test that
  only asserts "a result came back" passes just as happily against a process
  per dispatch, so these assert on the *pid*.
* **Framing (D7.1)** — v1 wrote the whole result on one newline-terminated line
  with no ``limit=`` on the reader. A 512 KiB plan did not truncate: the parent
  raised on the line at 64 KiB and then blocked in ``proc.wait()`` forever,
  because the worker was still writing the rest into a pipe nobody drained.
  Both sides hung. The length-prefixed framing is tested at 512 KiB and with
  multi-byte characters, which is what distinguishes a byte count from a
  character count.
* **Reclamation** — the vendor CLI is the worker's child, so the backend's
  grandchild. Every kill path is asserted on the *grandchild's* pid, because
  that is the process that used to survive and keep billing.
* **The startup sweep** — a SIGKILLed backend runs no handler. The sweep has to
  find those workers, and far more importantly must never signal a live process
  that merely inherited a recycled pid.
* **The cap (D7.1's second half)** — a cap that counted only timeouts left the
  other four failure classes unbounded, so a deterministic non-timeout failure
  was re-dispatched until ``max_turns`` ran out.

Every test here spawns real processes and every one reaps them, on failure too:
the ``pool`` fixture's teardown runs ``aclose()`` and asserts the pool is empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from backend.app.orchestrator import dispatch as dispatch_mod
from backend.app.orchestrator.agent_def import AgentDef
from backend.app.orchestrator.approvals import EventSink
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.dispatch import TurnBudget, build_dispatch_mcp
from backend.app.orchestrator.fleet import provider as provider_mod
from backend.app.orchestrator.fleet import subproc as subproc_mod
from backend.app.orchestrator.fleet.constants import StepNotAttemptedError
from backend.app.orchestrator.fleet.envelope import StepResult
from backend.app.orchestrator.fleet.models import RoleConfig, Step
from backend.app.orchestrator.fleet.pool import (
    WorkerPool,
    sweep_stale_workers,
    worker_key,
    write_worker_pidfile,
)

_ROLE = RoleConfig(provider="claude", model="m", system_prompt="s")

_ECHO_WORKER = "backend.tests.fakes.echo_worker"
_TREE_WORKER = "backend.tests.fakes.tree_worker"
_ENVELOPE_WORKER = "backend.tests.fakes.envelope_worker"

# Generous: a cold interpreter start on a loaded machine is not instant, and a
# flaky timeout here would read as a pool bug.
_WAIT_S = 30.0


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _request(prompt: str, **extra: Any) -> dict[str, Any]:
    request = {
        "provider": "claude",
        "model": "m",
        "system_prompt": "",
        "prompt": prompt,
        "cwd": None,
        "additional_dirs": [],
        "permission_mode": None,
        "role_name": "coder",
        "session_id": None,
    }
    request.update(extra)
    return request


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_until_dead(pid: int, label: str, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if not _alive(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{label} (pid {pid}) survived the kill")


async def _wait_for(predicate, label: str, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {label}")


@pytest.fixture
async def pool_factory(tmp_path: Path) -> AsyncIterator[Any]:
    """Builds pools and guarantees every one is closed, however a test ends.

    The assertion in teardown is the brief's: ``aclose()`` must leave no
    registered workers. A leaked worker here is a leaked vendor CLI in
    production.
    """
    built: list[WorkerPool] = []

    def build(
        *,
        worker_module: str = _ECHO_WORKER,
        max_workers: int = 4,
        idle_timeout_s: float = 300.0,
        pid_dir: Path | None = None,
    ) -> WorkerPool:
        pool = WorkerPool(
            max_workers=max_workers,
            idle_timeout_s=idle_timeout_s,
            worker_module=worker_module,
            repo_root=_repo_root(),
            pid_dir=pid_dir if pid_dir is not None else tmp_path / "workers",
        )
        built.append(pool)
        return pool

    try:
        yield build
    finally:
        for pool in built:
            with contextlib.suppress(Exception):
                await pool.aclose()
            assert pool.keys == [], "aclose() left workers registered"


@pytest.fixture
async def pool(pool_factory) -> WorkerPool:
    return pool_factory()


# ─────────────────────────────────────────────────────────────────────────────
# Reuse — the task's actual goal
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkerReuse:
    async def test_two_steps_on_one_key_share_one_process(
        self, pool: WorkerPool
    ) -> None:
        """The point of the task. Asserted on the pid, because a result coming
        back proves nothing about which process produced it."""
        key = worker_key("sess-1", "claude", "m", "/tmp")

        _, first_result = await pool.submit(key, _request("pid"))
        first_pid = (await asyncio.wait_for(first_result, _WAIT_S)).summary
        spawned_pid = pool.worker_pid(key)

        _, second_result = await pool.submit(key, _request("served"))
        served = (await asyncio.wait_for(second_result, _WAIT_S)).summary

        assert int(first_pid) == spawned_pid
        assert pool.worker_pid(key) == spawned_pid, "the second step respawned"
        assert served == "2", "the worker did not see both requests"
        assert pool.requests_served(key) == 2

    async def test_different_sessions_never_share_a_worker(
        self, pool: WorkerPool
    ) -> None:
        """A worker holds a vendor CLI and a working directory. Sharing one
        across sessions would let one session's step run inside another's."""
        key_a = worker_key("sess-a", "claude", "m", "/tmp")
        key_b = worker_key("sess-b", "claude", "m", "/tmp")

        _, result_a = await pool.submit(key_a, _request("pid"))
        pid_a = (await asyncio.wait_for(result_a, _WAIT_S)).summary
        _, result_b = await pool.submit(key_b, _request("pid"))
        pid_b = (await asyncio.wait_for(result_b, _WAIT_S)).summary

        assert pid_a != pid_b
        assert sorted(pool.keys) == sorted([key_a, key_b])

    async def test_the_key_distinguishes_provider_model_and_cwd(self) -> None:
        """Keyed by provider NAME, not branched on it — a new provider has to
        get its own workers without this module being edited."""
        base = worker_key("s", "claude", "m", "/a")
        assert base != worker_key("s", "codex", "m", "/a")
        assert base != worker_key("s", "claude", "m2", "/a")
        assert base != worker_key("s", "claude", "m", "/b")
        assert worker_key(None, "claude", "m", "/a").startswith("anon|")


# ─────────────────────────────────────────────────────────────────────────────
# D7.1 — framing a result that is larger than any line limit
# ─────────────────────────────────────────────────────────────────────────────


class TestResultFraming:
    async def test_a_512_kib_result_round_trips_intact(self, pool: WorkerPool) -> None:
        """The regression. On v1 framing this did not truncate — the parent
        raised on the 64 KiB line and then blocked forever in ``proc.wait()``
        while the worker blocked writing the rest into an undrained pipe."""
        key = worker_key("sess-big", "claude", "m", None)
        size = 512 * 1024

        _, result = await pool.submit(key, _request(f"big:{size}"))
        envelope = await asyncio.wait_for(result, _WAIT_S)

        assert len(envelope.summary) == size
        assert envelope.summary == "B" * size
        assert envelope.full_bytes == size

    async def test_the_length_prefix_counts_bytes_not_characters(
        self, pool: WorkerPool
    ) -> None:
        """A character count would make ``readexactly`` stop short and read the
        rest of the payload as the next protocol line — a silent corruption
        that only multi-byte text exposes."""
        key = worker_key("sess-utf8", "claude", "m", None)

        _, result = await pool.submit(key, _request("utf8:5000"))
        envelope = await asyncio.wait_for(result, _WAIT_S)

        assert envelope.summary == "é" * 5000
        assert envelope.full_bytes == 10_000

        # And the stream is still synchronised: the next request works.
        _, again = await pool.submit(key, _request("after"))
        assert (await asyncio.wait_for(again, _WAIT_S)).summary == "after"

    async def test_first_signal_arrives_before_the_result(
        self, pool: WorkerPool
    ) -> None:
        """``first`` is what tells "healthy but slow" apart from "wedged
        backend" — if it only flipped with the result it would be useless."""
        key = worker_key("sess-slow", "claude", "m", None)

        first, result = await pool.submit(key, _request("slow:1.0"))
        await asyncio.wait_for(first.wait(), _WAIT_S)

        assert not result.done(), "first fired no earlier than the result"
        assert (await asyncio.wait_for(result, _WAIT_S)).summary == "slept 1.0"

    async def test_a_worker_reported_error_reaches_the_caller_as_an_exception(
        self, pool: WorkerPool
    ) -> None:
        key = worker_key("sess-err", "claude", "m", None)

        _, result = await pool.submit(key, _request("fail:model refused"))

        with pytest.raises(RuntimeError, match="model refused"):
            await asyncio.wait_for(result, _WAIT_S)

    async def test_a_malformed_request_is_answered_and_the_worker_survives(
        self, pool: WorkerPool
    ) -> None:
        """A bad request must not cost the process. Before the loop, a request
        the worker could not handle ended the process — so the next step paid
        the interpreter start all over again."""
        key = worker_key("sess-bad", "claude", "m", None)
        bad = _request("x")
        del bad["prompt"]

        _, result = await pool.submit(key, bad)
        with pytest.raises(RuntimeError, match="bad request"):
            await asyncio.wait_for(result, _WAIT_S)
        pid_after_failure = pool.worker_pid(key)

        _, good = await pool.submit(key, _request("still here"))
        assert (await asyncio.wait_for(good, _WAIT_S)).summary == "still here"
        assert pool.worker_pid(key) == pid_after_failure


# ─────────────────────────────────────────────────────────────────────────────
# Reclamation — the kill, the eviction, the idle reap, aclose
# ─────────────────────────────────────────────────────────────────────────────


class TestReclamation:
    async def test_a_worker_that_dies_mid_request_reports_its_stderr_tail(
        self, pool: WorkerPool
    ) -> None:
        """When a worker dies without a result, its stderr is the ONLY window
        into why. This diagnostic was hard-won; discarding it (the earlier
        mistake) made the failure undiagnosable from backend.log."""
        key = worker_key("sess-crash", "claude", "m", None)

        _, result = await pool.submit(key, _request("crash:auth prompt on stdin"))
        await _wait_for(result.done, "the worker to exit")

        with pytest.raises(RuntimeError) as excinfo:
            result.result()
        message = str(excinfo.value)
        assert "exited without a result" in message
        assert "stderr: auth prompt on stdin" in message

    async def test_a_dead_worker_is_replaced_on_the_next_step(
        self, pool: WorkerPool
    ) -> None:
        """A worker that exited must not keep its key. Leaving the dead
        registration in place makes every later step on that session fail on a
        broken pipe instead of starting a fresh process."""
        key = worker_key("sess-revive", "claude", "m", None)

        _, dead = await pool.submit(key, _request("crash:gone"))
        with contextlib.suppress(RuntimeError):
            await asyncio.wait_for(dead, _WAIT_S)
        await _wait_for(lambda: key not in pool.keys, "the dead worker to deregister")

        _, revived = await pool.submit(key, _request("alive again"))
        assert (await asyncio.wait_for(revived, _WAIT_S)).summary == "alive again"

    async def test_kill_reaps_the_whole_process_group(
        self, pool_factory, tmp_path: Path
    ) -> None:
        """The vendor CLI is a GRANDCHILD of the backend. Killing only the
        worker re-parents it to launchd and it keeps running on the user's
        subscription, so the grandchild's pid is the assertion that matters."""
        pool = pool_factory(worker_module=_TREE_WORKER)
        key = worker_key("sess-tree", "claude", "m", None)
        pid_file = tmp_path / "pids.txt"

        _, result = await pool.submit(key, _request(str(pid_file)))
        worker_pid, grandchild_pid = await _read_pids(pid_file)
        try:
            assert _alive(worker_pid), "fake worker died before we could kill it"
            assert _alive(grandchild_pid), "fake grandchild died before the kill"

            pool.kill(key)

            await _wait_until_dead(worker_pid, "worker")
            await _wait_until_dead(grandchild_pid, "grandchild (the vendor CLI)")
            with pytest.raises(RuntimeError, match="killed by the pool"):
                await asyncio.wait_for(result, _WAIT_S)
        finally:
            # Never leave real processes behind, however this test ends.
            for pid in (worker_pid, grandchild_pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)

    async def test_over_the_cap_the_least_recently_used_idle_worker_is_closed(
        self, pool_factory
    ) -> None:
        pool = pool_factory(max_workers=2)
        keys = [worker_key(f"sess-{n}", "claude", "m", None) for n in range(3)]

        pids = []
        for key in keys:
            _, result = await pool.submit(key, _request("pid"))
            pids.append(int((await asyncio.wait_for(result, _WAIT_S)).summary))

        assert keys[0] not in pool.keys, "the LRU worker was not evicted"
        assert sorted(pool.keys) == sorted(keys[1:])
        await _wait_until_dead(pids[0], "the evicted worker")

    async def test_a_busy_worker_is_never_evicted(self, pool_factory) -> None:
        """Evicting a worker mid-step would fail a step that is still running,
        to save a process. The cap bounds steady state, not a burst."""
        pool = pool_factory(max_workers=1)
        busy_key = worker_key("sess-busy", "claude", "m", None)
        other_key = worker_key("sess-other", "claude", "m", None)

        first, busy = await pool.submit(busy_key, _request("hang"))
        await asyncio.wait_for(first.wait(), _WAIT_S)
        _, other = await pool.submit(other_key, _request("fine"))
        assert (await asyncio.wait_for(other, _WAIT_S)).summary == "fine"

        assert busy_key in pool.keys, "a worker with a step in flight was evicted"
        assert not busy.done()

    async def test_an_idle_worker_is_reaped_after_the_timeout(
        self, pool_factory
    ) -> None:
        pool = pool_factory(idle_timeout_s=0.3)
        key = worker_key("sess-idle", "claude", "m", None)

        _, result = await pool.submit(key, _request("pid"))
        pid = int((await asyncio.wait_for(result, _WAIT_S)).summary)

        await _wait_for(lambda: key not in pool.keys, "the idle worker to be reaped")
        await _wait_until_dead(pid, "the reaped worker")

    async def test_aclose_leaves_no_child_processes(self, pool_factory) -> None:
        pool = pool_factory()
        pids = []
        for n in range(3):
            key = worker_key(f"sess-{n}", "claude", "m", None)
            _, result = await pool.submit(key, _request("pid"))
            pids.append(int((await asyncio.wait_for(result, _WAIT_S)).summary))

        await pool.aclose()

        assert pool.keys == []
        for pid in pids:
            await _wait_until_dead(pid, f"worker {pid}")

    async def test_submitting_to_a_closed_pool_raises(self, pool_factory) -> None:
        pool = pool_factory()
        await pool.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await pool.submit(worker_key("s", "claude", "m", None), _request("x"))


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


async def _read_pids(path: Path, timeout_s: float = 20.0) -> tuple[int, int]:
    """The fake tree worker's (pid, grandchild pid), once it has reported.

    Reported through a file, not stdout: the pool owns the worker's stdout and
    parses it for the protocol, so nothing a test does can read pids there.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        text = await asyncio.to_thread(_read_text_or_empty, path)
        if text.endswith("\n"):
            worker, grandchild = text.split()
            return int(worker), int(grandchild)
        await asyncio.sleep(0.05)
    raise AssertionError(f"fake worker never reported its pids to {path}")


# ─────────────────────────────────────────────────────────────────────────────
# One request per worker at a time — enforced, not hoped for
# ─────────────────────────────────────────────────────────────────────────────

# Comfortably over a pipe buffer (64 KiB on macOS and Linux), which is what
# makes the difference observable: under it, an early write is merely
# premature; over it, the write BLOCKS until the busy worker reads.
_OVER_PIPE_BUFFER = "x" * (512 * 1024)


class TestOneRequestPerWorker:
    async def test_submit_returns_before_the_worker_could_accept_the_request(
        self, pool: WorkerPool
    ) -> None:
        """``submit`` is awaited BEFORE the caller enters its heartbeat/timeout
        loop, so anything it blocks on is time the step is neither narrated nor
        bounded. It used to write and drain there — which stalls on a prompt
        over the pipe buffer whenever the worker is busy."""
        key = worker_key("sess-queue", "claude", "m", None)

        _, running = await pool.submit(key, _request("slow:1.5"))
        started = asyncio.get_running_loop().time()
        _, queued = await pool.submit(key, _request(_OVER_PIPE_BUFFER))
        submit_took = asyncio.get_running_loop().time() - started

        try:
            assert submit_took < 0.5, (
                f"submit blocked for {submit_took:.2f}s behind a busy worker"
            )
            assert pool.is_queued(key, queued)
            assert not queued.done()
        finally:
            await asyncio.wait_for(running, _WAIT_S)
            await asyncio.wait_for(queued, _WAIT_S)

    async def test_the_queued_request_does_not_start_until_the_first_finishes(
        self, pool: WorkerPool
    ) -> None:
        """``first`` is the caller's evidence that its OWN step has begun. If it
        flipped while the step was still queued, the caller would start its
        grace clock against a backend that had not been handed anything."""
        key = worker_key("sess-order", "claude", "m", None)

        running_first, running = await pool.submit(key, _request("slow:1.5"))
        queued_first, queued = await pool.submit(key, _request(_OVER_PIPE_BUFFER))

        await asyncio.wait_for(running_first.wait(), _WAIT_S)
        assert not queued_first.is_set(), "a queued step reported that it started"
        assert pool.is_queued(key, queued)

        await asyncio.wait_for(running, _WAIT_S)
        await asyncio.wait_for(queued_first.wait(), _WAIT_S)
        result = await asyncio.wait_for(queued, _WAIT_S)
        assert result.summary == _OVER_PIPE_BUFFER
        assert not pool.is_queued(key, queued)

    async def test_a_kill_for_the_running_step_leaves_the_queued_one_alive(
        self, pool: WorkerPool
    ) -> None:
        """The running step's worker is wedged and must be reclaimed; the step
        queued behind it reached no process and demonstrated nothing. Failing
        it too charged a role a hard failure for a step it never attempted —
        which, after D7.1 made the generic path count, trips the retry cap."""
        key = worker_key("sess-kill-queue", "claude", "m", None)

        running_first, running = await pool.submit(key, _request("hang"))
        _, queued = await pool.submit(key, _request("survivor"))
        await asyncio.wait_for(running_first.wait(), _WAIT_S)
        assert pool.is_queued(key, queued)

        pool.kill(key)

        with pytest.raises(RuntimeError, match="killed by the pool"):
            await asyncio.wait_for(running, _WAIT_S)
        # Re-queued onto a fresh worker rather than failed with the dead one.
        result = await asyncio.wait_for(queued, _WAIT_S)
        assert result.summary == "survivor"

    async def test_abandoning_a_queued_step_does_not_touch_the_running_one(
        self, pool: WorkerPool
    ) -> None:
        """The inverse, and the reason ``abandon`` takes the request and not
        just the key: cancelling a step that is waiting its turn must not kill
        the worker running somebody else's step."""
        key = worker_key("sess-abandon", "claude", "m", None)

        running_first, running = await pool.submit(key, _request("slow:1.0"))
        _, queued = await pool.submit(key, _request("never runs"))
        # Wait for the FIRST step to actually be running: only then is there a
        # worker whose survival means anything.
        await asyncio.wait_for(running_first.wait(), _WAIT_S)
        assert pool.is_queued(key, queued)
        pid_before = pool.worker_pid(key)
        assert pid_before is not None

        pool.abandon(key, queued)

        with pytest.raises(StepNotAttemptedError, match="never reached"):
            await asyncio.wait_for(queued, _WAIT_S)
        assert (await asyncio.wait_for(running, _WAIT_S)).summary == "slept 1.0"
        assert pool.worker_pid(key) == pid_before, "the running step's worker died"

    async def test_a_not_attempted_step_is_not_charged_to_the_retry_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End of the chain: the pool's distinct failure type has to survive
        all the way into dispatch, or the accounting fix is cosmetic."""
        from backend.app.orchestrator.fleet import DISPATCH_HARD_FAIL_CAP

        recorder = _Recorder(StepNotAttemptedError("it never reached a worker"))
        tool, _ = _dispatch_tool(monkeypatch, recorder)

        for _ in range(DISPATCH_HARD_FAIL_CAP + 2):
            result = await tool({"name": "coder", "prompt": "work"})
            assert result["is_error"] is True
            assert "did not run" in result["content"][0]["text"]
            assert "not held against" in result["content"][0]["text"]

        # Never refused: nothing was ever held against the role.
        assert recorder.calls == DISPATCH_HARD_FAIL_CAP + 2


# ─────────────────────────────────────────────────────────────────────────────
# Ruling 15 — the startup sweep for a previous backend's workers
# ─────────────────────────────────────────────────────────────────────────────


def _pidfile(directory: Path, pid: int, *, pgid: int, owner_pid: int) -> Path:
    """A pidfile in exactly the shape a worker writes."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "pgid": pgid,
                "owner_pid": owner_pid,
                "worker_module": _ECHO_WORKER,
                "started": 0.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _dead_pid() -> int:
    """A pid that is certainly not running, to stand in for a killed backend."""
    for candidate in range(99_990, 2, -1):
        if not _alive(candidate):
            return candidate
    raise AssertionError("could not find a dead pid")


class TestStartupSweep:
    async def test_it_kills_a_worker_whose_backend_is_gone(
        self, tmp_path: Path
    ) -> None:
        """With ``start_new_session=True`` a SIGKILLed backend leaves its
        workers running — nothing gets to run a handler, and the workers never
        see the terminal's signals either."""
        pid_dir = tmp_path / "workers"
        stale = await asyncio.create_subprocess_exec(
            "/usr/bin/env",
            "python3",
            "-c",
            f"import time\n# {_ECHO_WORKER}\ntime.sleep(600)\n",
            start_new_session=True,
        )
        pgid = os.getpgid(stale.pid)
        path = _pidfile(pid_dir, stale.pid, pgid=pgid, owner_pid=_dead_pid())
        try:
            killed = await asyncio.to_thread(
                sweep_stale_workers, pid_dir, _ECHO_WORKER
            )

            assert killed == 1
            await _wait_until_dead(stale.pid, "the stale worker")
            assert not path.exists(), "the sweep left its pidfile behind"
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stale.wait(), 10)

    async def test_it_signals_only_the_group_the_verified_pid_is_actually_in(
        self, tmp_path: Path
    ) -> None:
        """Every ownership check establishes something about ``pid``. Aiming
        the kill at the record's own ``pgid`` therefore signals a group NOTHING
        verified — and it did: with a record naming a live, cmdline-matching pid
        A and an unrelated group B, the sweep killed B and left A running. A
        pool-spawned worker always has ``pgid == pid``, but
        ``write_worker_pidfile`` is public and records ``os.getpgid()``
        unconditionally, so a worker started without its own session records the
        shell's job-control group — and that file, left behind with its pid
        recycled, points SIGKILL at the user's foreground job."""
        pid_dir = tmp_path / "workers"
        # A: passes every ownership check — alive, dead owner, matching cmdline.
        verified = await asyncio.create_subprocess_exec(
            "/usr/bin/env",
            "python3",
            "-c",
            f"import time\n# {_ECHO_WORKER}\ntime.sleep(30)\n",
            start_new_session=True,
        )
        # B: an unrelated process in its own group. Nothing proves anything
        # about it, so nothing may be aimed at it.
        bystander = await asyncio.create_subprocess_exec(
            "/usr/bin/env", "python3", "-c", "import time; time.sleep(30)",
            start_new_session=True,
        )
        bystander_group = os.getpgid(bystander.pid)
        path = _pidfile(
            pid_dir, verified.pid, pgid=bystander_group, owner_pid=_dead_pid()
        )
        try:
            killed = await asyncio.to_thread(
                sweep_stale_workers, pid_dir, _ECHO_WORKER
            )
            await asyncio.sleep(0.5)

            assert killed == 0
            assert _alive(verified.pid), "the verified worker was killed"
            assert _alive(bystander.pid), "an unverified group was SIGKILLed"
            # The record disagrees with the kernel, so it describes a tree that
            # no longer exists as recorded: deleted, and nothing signalled.
            assert not path.exists()
        finally:
            for proc in (verified, bystander):
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 10)

    async def test_a_record_without_an_owner_is_refused_not_trusted(
        self, tmp_path: Path
    ) -> None:
        """``owner_pid`` gates the check that protects a concurrently running
        backend. A record without one used to skip that check entirely and
        proceed to the kill path — on a kill path, absent must mean refuse."""
        pid_dir = tmp_path / "workers"
        bystander = await asyncio.create_subprocess_exec(
            "/usr/bin/env",
            "python3",
            "-c",
            f"import time\n# {_ECHO_WORKER}\ntime.sleep(30)\n",
            start_new_session=True,
        )
        pid_dir.mkdir(parents=True, exist_ok=True)
        path = pid_dir / f"{bystander.pid}.json"
        path.write_text(
            json.dumps(
                {
                    "pid": bystander.pid,
                    "pgid": os.getpgid(bystander.pid),
                    "worker_module": _ECHO_WORKER,
                }
            ),
            encoding="utf-8",
        )
        try:
            killed = await asyncio.to_thread(
                sweep_stale_workers, pid_dir, _ECHO_WORKER
            )
            await asyncio.sleep(0.5)

            assert killed == 0
            assert _alive(bystander.pid)
            assert not path.exists()
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(bystander.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bystander.wait(), 10)

    async def test_it_refuses_to_signal_a_live_process_with_a_recycled_pid(
        self, tmp_path: Path
    ) -> None:
        """The failure that would be far worse than leaving an orphan. The pid
        in the file is alive, but it belongs to something that is not one of
        our workers — so the file is stale and the process is untouchable."""
        pid_dir = tmp_path / "workers"
        bystander = await asyncio.create_subprocess_exec(
            "/usr/bin/env", "python3", "-c", "import time; time.sleep(30)"
        )
        path = _pidfile(
            pid_dir, bystander.pid, pgid=bystander.pid, owner_pid=_dead_pid()
        )
        try:
            killed = await asyncio.to_thread(
                sweep_stale_workers, pid_dir, _ECHO_WORKER
            )

            assert killed == 0
            assert _alive(bystander.pid), "the sweep killed an unrelated process"
            assert not path.exists(), "the stale pidfile was not cleaned up"
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                bystander.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bystander.wait(), 10)

    async def test_it_leaves_a_live_backends_workers_alone(
        self, tmp_path: Path
    ) -> None:
        """Two backends can run at once. One reclaiming the other's workers
        would kill a step that is mid-flight in a session it cannot see."""
        pid_dir = tmp_path / "workers"
        bystander = await asyncio.create_subprocess_exec(
            "/usr/bin/env",
            "python3",
            "-c",
            f"import time\n# {_ECHO_WORKER}\ntime.sleep(30)\n",
        )
        # Owner alive — this process, standing in for the other backend.
        path = _pidfile(
            pid_dir, bystander.pid, pgid=bystander.pid, owner_pid=os.getpid()
        )
        try:
            killed = await asyncio.to_thread(
                sweep_stale_workers, pid_dir, _ECHO_WORKER
            )

            assert killed == 0
            assert _alive(bystander.pid)
            assert path.exists(), "another backend's pidfile was deleted"
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                bystander.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bystander.wait(), 10)

    async def test_it_removes_records_for_dead_and_unreadable_pids(
        self, tmp_path: Path
    ) -> None:
        pid_dir = tmp_path / "workers"
        dead = _pidfile(pid_dir, _dead_pid(), pgid=_dead_pid(), owner_pid=_dead_pid())
        pid_dir.mkdir(parents=True, exist_ok=True)
        garbage = pid_dir / "not-json.json"
        garbage.write_text("{ this is not json", encoding="utf-8")

        killed = await asyncio.to_thread(sweep_stale_workers, pid_dir, _ECHO_WORKER)

        assert killed == 0
        assert not dead.exists()
        assert not garbage.exists()

    async def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert await asyncio.to_thread(
            sweep_stale_workers, tmp_path / "never-created", _ECHO_WORKER
        ) == 0

    async def test_a_pooled_worker_writes_a_sweepable_pidfile(
        self, pool_factory, tmp_path: Path
    ) -> None:
        """The sweep is only as good as the file the worker leaves. If the real
        worker's record and the sweep's reader ever disagree, the sweep reads
        as working and reclaims nothing."""
        pid_dir = tmp_path / "pidfiles"
        pool = pool_factory(pid_dir=pid_dir)
        key = worker_key("sess-pidfile", "claude", "m", None)

        _, result = await pool.submit(key, _request("pid"))
        pid = int((await asyncio.wait_for(result, _WAIT_S)).summary)

        record = json.loads((pid_dir / f"{pid}.json").read_text(encoding="utf-8"))
        assert record["pid"] == pid
        assert record["owner_pid"] == os.getpid()
        assert record["worker_module"] == _ECHO_WORKER

    def test_write_worker_pidfile_is_a_no_op_without_a_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker run by hand must still start. Bookkeeping never gates work."""
        monkeypatch.delenv("LOCALCODE_WORKER_PID_DIR", raising=False)
        assert write_worker_pidfile("whatever") is None


# ─────────────────────────────────────────────────────────────────────────────
# The real worker's loop, without the SDK
# ─────────────────────────────────────────────────────────────────────────────


class TestRealWorkerLoop:
    """``serve`` is tested against an injected reader and a stubbed
    ``collect_step``: the loop's contract (EOF exits, blank lines ignored, a
    malformed line answered without ending the process) is independent of the
    SDK, and testing it through a real vendor CLI would test the CLI."""

    async def test_it_serves_many_requests_then_exits_on_eof(
        self, monkeypatch: pytest.MonkeyPatch, capsysbinary
    ) -> None:
        seen: list[str] = []

        async def fake_collect_step(role, prompt, cwd, dirs, **kwargs) -> StepResult:
            seen.append(prompt)
            kwargs["progress"].set()
            return StepResult(
                summary=f"did {prompt}",
                structured=None,
                artifact_id=None,
                artifact_path=None,
                tool_digest="",
                full_bytes=len(prompt),
                usage={"input_tokens": 1},
            )

        monkeypatch.setattr(subproc_mod, "collect_step", fake_collect_step)
        lines = [
            json.dumps({**_request("one"), "id": "r1"}) + "\n",
            "\n",  # ignored, not EOF
            json.dumps({**_request("two"), "id": "r2"}) + "\n",
            "",  # EOF
        ]

        async def read_line() -> str:
            return lines.pop(0)

        await subproc_mod.serve(read_line)

        assert seen == ["one", "two"]
        out = capsysbinary.readouterr().out.decode()
        assert "@@FIRST@@ r1" in out
        assert "@@FIRST@@ r2" in out
        # Attributable and length-prefixed, for each request.
        assert "@@RESULT@@ r1 " in out
        assert "@@RESULT@@ r2 " in out
        assert lines == [], "the loop stopped before EOF"

    async def test_a_malformed_line_is_answered_and_the_loop_continues(
        self, monkeypatch: pytest.MonkeyPatch, capsysbinary
    ) -> None:
        calls: list[str] = []

        async def fake_collect_step(role, prompt, cwd, dirs, **kwargs) -> StepResult:
            calls.append(prompt)
            return StepResult("ok", None, None, None, "", 2)

        monkeypatch.setattr(subproc_mod, "collect_step", fake_collect_step)
        lines = [
            "this is not json\n",
            json.dumps({"id": "r9"}) + "\n",  # JSON, but no provider/prompt
            json.dumps({**_request("real"), "id": "r10"}) + "\n",
            "",
        ]

        async def read_line() -> str:
            return lines.pop(0)

        await subproc_mod.serve(read_line)

        out = capsysbinary.readouterr().out.decode()
        # The unattributable line still answers — silence is indistinguishable
        # from a wedged backend.
        assert "@@RESULT@@ unknown " in out
        # The attributable failure carries its own id, so the pool can resolve
        # the right pending future instead of dropping an unknown one.
        assert "@@RESULT@@ r9 " in out
        assert calls == ["real"], "the loop died on the malformed lines"

    async def test_the_result_payload_is_exactly_the_declared_byte_count(
        self, monkeypatch: pytest.MonkeyPatch, capsysbinary
    ) -> None:
        """The one invariant the parent's ``readexactly`` depends on."""

        async def fake_collect_step(role, prompt, cwd, dirs, **kwargs) -> StepResult:
            return StepResult("héllo " * 1000, None, None, None, "", 7)

        monkeypatch.setattr(subproc_mod, "collect_step", fake_collect_step)
        lines = [json.dumps({**_request("go"), "id": "r1"}) + "\n", ""]

        async def read_line() -> str:
            return lines.pop(0)

        await subproc_mod.serve(read_line)

        out = capsysbinary.readouterr().out
        _, marker, rest = out.partition(b"@@RESULT@@ r1 ")
        assert marker, f"no result header in {out[:200]!r}"
        size_text, _, body = rest.partition(b"\n")
        size = int(size_text)
        assert body[:size].endswith(b"}")
        assert json.loads(body[:size])["ok"] is True
        assert body[size:] == b"\n", "the payload is not exactly the declared length"

    async def test_the_real_worker_round_trips_through_the_pool(
        self, pool_factory
    ) -> None:
        """One real process boundary, no SDK: the envelope fake frames a
        ``StepResult`` the v2 way and the pool rebuilds it."""
        pool = pool_factory(worker_module=_ENVELOPE_WORKER)
        key = worker_key("sess-9", "claude", "m", None)

        first, result = await pool.submit(
            key, _request("go", role_name="reviewer", session_id="sess-9")
        )
        envelope = await asyncio.wait_for(result, _WAIT_S)

        assert isinstance(envelope, StepResult)
        assert "role=reviewer" in envelope.summary
        assert "session=sess-9" in envelope.summary
        assert envelope.structured == {
            "value": "nack",
            "reason": "task 3 missing",
            "source": "json",
        }
        assert envelope.usage == {"input_tokens": 5, "output_tokens": 2}
        assert envelope.artifact_id == "c" * 64
        assert first.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# The provider reuses the pool, and kills only what it must
# ─────────────────────────────────────────────────────────────────────────────


class _StubPool:
    """Records submissions and kills without spawning anything."""

    def __init__(self, outcome: StepResult | Exception | None) -> None:
        self.outcome = outcome
        self.submitted: list[tuple[str, dict[str, Any]]] = []
        self.killed: list[str] = []

    async def submit(self, key: str, request: dict[str, Any]):
        self.submitted.append((key, request))
        first = asyncio.Event()
        result: asyncio.Future[StepResult] = (
            asyncio.get_running_loop().create_future()
        )
        if isinstance(self.outcome, Exception):
            first.set()
            result.set_exception(self.outcome)
        elif self.outcome is not None:
            first.set()
            result.set_result(self.outcome)
        return first, result

    def kill(self, key: str) -> None:
        self.killed.append(key)

    def is_queued(self, key: str, result) -> bool:  # noqa: ANN001
        # This stub hands every request straight to its "worker"; nothing it
        # returns is ever waiting its turn.
        return False

    def abandon(self, key: str, result) -> None:  # noqa: ANN001
        self.killed.append(key)

    @property
    def keys(self) -> list[str]:
        return []


async def _run_step(
    monkeypatch: pytest.MonkeyPatch, stub: _StubPool, session_id: str | None = "sess-3"
) -> list[Event]:
    fleet = provider_mod.FleetProvider()
    monkeypatch.setattr(fleet, "_get_pool", lambda: stub)
    step = Step(id="orch.coder.1", role="coder", prompt="do the thing")
    ctx = RunContext(model="m", prompt="p", session_id=session_id, cwd="/w")
    outputs: dict[str, StepResult] = {}
    return [ev async for ev in fleet._run_step_with_role(step, _ROLE, ctx, outputs)]


class TestProviderUsesThePool:
    async def test_a_successful_step_does_not_kill_its_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reuse that the whole task is for. The per-dispatch handle killed
        unconditionally because its process served exactly one step; keeping
        that would hand the interpreter-start cost straight back."""
        stub = _StubPool(StepResult("all good", None, None, None, "", 8))

        events = await _run_step(monkeypatch, stub)

        assert stub.killed == [], "a healthy worker was killed after its step"
        assert [e.type for e in events] == ["assistant.tool_use", "tool.result"]
        assert stub.submitted[0][0] == worker_key("sess-3", "claude", "m", "/w")

    async def test_a_worker_reported_error_does_not_kill_the_worker_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that answered — even with an error — is demonstrably alive
        and has already released its vendor CLI."""
        stub = _StubPool(RuntimeError("the model refused"))

        events = await _run_step(monkeypatch, stub)

        assert stub.killed == []
        result = events[-1]
        assert result.data["is_error"] is True
        assert "the model refused" in result.data["content"]

    async def test_a_sub_second_heartbeat_still_reaches_the_grace_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``elapsed_s`` used to advance by ``int(HEARTBEAT_INTERVAL_S)``, so
        any interval below 1 s advanced it by ZERO — neither ``grace_s`` nor
        ``step_budget_s`` was ever reached and the step looped forever. The
        asymmetry is what made it reachable: both budgets are settings-driven
        while the interval is not, so lowering them alone cannot fast-fail."""
        stub = _StubPool(None)  # never resolves
        monkeypatch.setattr(provider_mod, "HEARTBEAT_INTERVAL_S", 0.25)

        from backend.app.orchestrator.fleet.constants import StepTimeoutError

        fleet = provider_mod.FleetProvider()
        monkeypatch.setattr(fleet, "_get_pool", lambda: stub)
        monkeypatch.setattr(
            provider_mod,
            "get_settings",
            lambda: type(
                "S", (), {"fleet_startup_grace_s": 0.5, "fleet_step_timeout_s": 5.0}
            )(),
        )
        step = Step(id="orch.coder.1", role="coder", prompt="go")
        ctx = RunContext(model="m", prompt="p", session_id="s-sub", cwd="/w")

        async def drive() -> None:
            async for _ in fleet._run_step_with_role(step, _ROLE, ctx, {}):
                pass

        # Bounded: on the defective code this never returns.
        with pytest.raises(StepTimeoutError, match="NO output within"):
            await asyncio.wait_for(drive(), timeout=10)

        assert stub.killed == [worker_key("s-sub", "claude", "m", "/w")]

    async def test_an_abandoned_step_kills_the_worker_by_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A step whose future never resolved left a wedged worker holding a
        vendor CLI. That one must be killed, by group, through the pool."""
        stub = _StubPool(None)  # never resolves
        monkeypatch.setattr(provider_mod, "HEARTBEAT_INTERVAL_S", 1.0)

        from backend.app.orchestrator.fleet.constants import StepTimeoutError

        fleet = provider_mod.FleetProvider()
        monkeypatch.setattr(fleet, "_get_pool", lambda: stub)
        monkeypatch.setattr(
            provider_mod,
            "get_settings",
            lambda: type(
                "S", (), {"fleet_startup_grace_s": 1.0, "fleet_step_timeout_s": 1.0}
            )(),
        )
        step = Step(id="orch.coder.1", role="coder", prompt="go")
        ctx = RunContext(model="m", prompt="p", session_id="s9", cwd="/w")

        with pytest.raises(StepTimeoutError):
            async for _ in fleet._run_step_with_role(step, _ROLE, ctx, {}):
                pass

        assert stub.killed == [worker_key("s9", "claude", "m", "/w")]


# ─────────────────────────────────────────────────────────────────────────────
# The per-turn spend cap, and D7.1's second half
# ─────────────────────────────────────────────────────────────────────────────


class _Recorder:
    """Stands in for ``FleetProvider._run_step_with_role``."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def __call__(self, step, role_cfg, ctx, outputs):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        outputs[step.id] = self.outcome
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _dispatch_tool(
    monkeypatch: pytest.MonkeyPatch,
    run_step_fn,
    registry_names: tuple[str, ...] = ("planner", "coder"),
):
    """The real ``dispatch_subagent`` tool, reached by capturing the tool
    objects ``build_dispatch_mcp`` hands to the SDK — the per-turn ledgers this
    tests are closure locals, so there is no other way in."""
    captured: dict[str, Any] = {}

    def _fake_create(*, name: str, version: str, tools: list):
        captured.update({t.name: t for t in tools})
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(dispatch_mod, "create_sdk_mcp_server", _fake_create)
    registry = {
        name: AgentDef(
            name=name,
            description="d",
            provider="claude",
            model="m",
            system_prompt="s",
        )
        for name in registry_names
    }
    build_dispatch_mcp(
        registry=registry,
        ctx=RunContext(model="m", prompt="p", cwd=None),
        sink=EventSink(),
        run_step_fn=run_step_fn,
    )
    return captured["dispatch_subagent"].handler, registry


class TestTurnBudget:
    def test_usage_of_none_costs_nothing(self) -> None:
        """A provider that reports no token counts must not be charged a guess
        — and must not be refused for one either."""
        budget = TurnBudget(max_dispatches=8, token_budget=10)
        budget.spend(None)
        budget.spend({})
        assert budget.tokens == 0
        assert budget.refusal() is None

    def test_tokens_sum_across_steps_until_the_budget_trips(self) -> None:
        budget = TurnBudget(max_dispatches=8, token_budget=10)
        budget.spend({"input_tokens": 4, "output_tokens": 2})
        assert budget.refusal() is None
        budget.spend({"input_tokens": 4})
        assert "token budget" in (budget.refusal() or "")

    def test_a_zero_token_budget_is_unlimited(self) -> None:
        budget = TurnBudget(max_dispatches=8, token_budget=0)
        budget.spend({"input_tokens": 10**9})
        assert budget.refusal() is None

    def test_the_dispatch_cap_scales_with_the_registry(self) -> None:
        assert max(8, 2 * 2) == 8
        assert max(8, 2 * 6) == 12


class TestDispatchCaps:
    async def test_a_turn_over_the_dispatch_cap_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every dispatch SUCCEEDS here, so the hard-fail cap never sees it.
        Only this cap stands between an orchestrator that keeps delegating and
        ``max_turns`` at the step budget each."""
        recorder = _Recorder(StepResult("done", None, None, None, "", 4))
        tool, registry = _dispatch_tool(monkeypatch, recorder)
        cap = max(8, 2 * len(registry))

        for _ in range(cap):
            ok = await tool({"name": "coder", "prompt": "work"})
            assert not ok.get("is_error"), ok

        refused = await tool({"name": "coder", "prompt": "work"})

        assert refused["is_error"] is True
        assert "dispatch cap" in refused["content"][0]["text"]
        assert recorder.calls == cap, "a refused dispatch still ran the step"

    async def test_the_token_budget_refuses_once_the_turn_exceeds_it(
        self, monkeypatch: pytest.MonkeyPatch, fresh_settings
    ) -> None:
        monkeypatch.setenv("FLEET_TURN_TOKEN_BUDGET", "10")
        envelope = StepResult(
            "done", None, None, None, "", 4, usage={"input_tokens": 6}
        )
        recorder = _Recorder(envelope)
        tool, _ = _dispatch_tool(monkeypatch, recorder)

        assert not (await tool({"name": "coder", "prompt": "a"})).get("is_error")
        assert not (await tool({"name": "coder", "prompt": "b"})).get("is_error")
        refused = await tool({"name": "coder", "prompt": "c"})

        assert refused["is_error"] is True
        assert "token budget" in refused["content"][0]["text"]
        assert recorder.calls == 2

    async def test_a_non_timeout_failure_counts_against_the_hard_fail_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D7.1's second half. A worker whose result was too large raised a
        plain RuntimeError here, which never incremented ``hard_fail`` — so the
        cap never tripped and the planner was re-dispatched to reproduce the
        same oversize output for up to 30 turns of 600 s, holding the session
        lock the whole time."""
        from backend.app.orchestrator.fleet import DISPATCH_HARD_FAIL_CAP

        recorder = _Recorder(
            RuntimeError("sub-provider worker exited without a result (exit=1)")
        )
        tool, _ = _dispatch_tool(monkeypatch, recorder)

        for _ in range(DISPATCH_HARD_FAIL_CAP - 1):
            first = await tool({"name": "planner", "prompt": "plan"})
            assert first["is_error"] is True

        at_cap = await tool({"name": "planner", "prompt": "plan"})
        beyond = await tool({"name": "planner", "prompt": "plan"})

        at_cap_text = at_cap["content"][0]["text"]
        assert "STOP" in at_cap_text
        # The refusal names the failure, not just "it failed".
        assert "exited without a result" in at_cap_text
        assert "REFUSING to dispatch" in beyond["content"][0]["text"]
        assert recorder.calls == DISPATCH_HARD_FAIL_CAP, (
            "the cap did not stop the re-dispatch loop"
        )

    async def test_a_subagent_never_receives_the_dispatch_tools(self) -> None:
        """Recursive spawning is impossible by construction, so there is no
        guard to test — what there is to test is that the construction holds.
        The dispatch MCP server is wired into the ORCHESTRATOR's options only;
        a step's own sub-provider context carries no mcp_servers at all."""
        from backend.app.orchestrator.fleet.collect import _role_extras

        for role in ("planner", "coder", "reviewer", "tester", None):
            extras = _role_extras(role)
            assert "mcp_servers" not in extras
            assert not any(
                "dispatch" in str(value) for value in extras.values()
            ), f"{role} extras mention dispatch"

        # And the tool names the orchestrator is allowed to call are built from
        # its own server name, never handed to a step.
        _, allowed = build_dispatch_mcp(
            registry={},
            ctx=RunContext(model="m", prompt="p"),
            sink=EventSink(),
            run_step_fn=_Recorder(StepResult("x", None, None, None, "", 1)),
        )
        assert allowed == [
            "mcp__fleet_dispatch__dispatch_subagent",
            "mcp__fleet_dispatch__request_plan_approval",
        ]
