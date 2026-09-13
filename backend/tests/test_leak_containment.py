"""Containment: nothing the harness starts may outlive the turn that started it.

The leak this suite exists for is not a Python object — it is a **process**.
The vendor CLI is a grandchild of the backend (backend → pooled worker → CLI),
so a kill aimed at the worker alone re-parents the CLI to ``launchd`` and it
keeps consuming the user's paid subscription with no interface attached. D14.1
was exactly that, on the path where a step is abandoned rather than completed.

``test_worker_pool.py`` (Task 7) already asserts the pool's own kill paths
against real processes, including the process group. What is asserted HERE is
the composition above it: a real :class:`WorkerPool` driven by the real
``FleetProvider._run_step_with_role``, so the decision to abandon, the choice
of ``abandon`` over ``kill``, and the ``finally`` that makes it are the
production ones. A fake pool cannot fail these tests, which is why none is used
— ``fakes/providers.FakeWorkerPool`` deliberately stays in-process.

**The manual chain this stands in for.** Task 14's verification included one
step that cannot be automated: Ctrl-C on a live fleet turn against a real
vendor CLI, then checking with ``pgrep`` that nothing survived. No CLI is
installed here and no suite may depend on a paid subscription, so
``test_a_step_cancelled_mid_flight_leaves_no_worker_and_no_grandchild`` is the
automated stand-in: the same cancellation, through the same ``finally``, with
the assertion made on a real grandchild's pid. ``docs/harness.md`` §9 records
what remains manual.

Every test here reaps what it spawned even when it fails — a leaked test
process is the very defect under test.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.fleet.constants import StepTimeoutError
from backend.app.orchestrator.fleet.models import RoleConfig, Step
from backend.app.orchestrator.fleet.pool import WorkerPool, worker_key
from backend.app.orchestrator.fleet.provider import FleetProvider
from backend.app.session_runner import registry as runner_registry
from backend.app.storage.sessions import store as session_store

from .fakes.providers import ScriptedProvider
from .test_worker_pool import (
    _ECHO_WORKER,
    _TREE_WORKER,
    _WAIT_S,
    _alive,
    _read_pids,
    _repo_root,
    _request,
    _wait_for,
    _wait_until_dead,
)

# How many sessions the churn case cycles through. chosen-here: 50 is the
# brief's number and is enough that a per-session leak of one task, one runner
# or one descriptor is unmistakable rather than arguable.
CHURN_SESSIONS = 50

# The step budgets the abandonment cases run under. chosen-here: small enough
# that a test is a second rather than ten minutes, large enough that a loaded
# machine still gets the worker spawned and its pids reported before the
# ceiling fires. The production values (600 s / 75 s) are asserted nowhere here
# — what is under test is what happens AT the ceiling, not where it sits.
STEP_CEILING_S = 1.0
HEARTBEAT_S = 0.05


@pytest.fixture
async def pools(tmp_path: Path) -> AsyncIterator[Any]:
    """Builds REAL worker pools and guarantees each is closed and empty.

    The teardown assertion is the one that matters: a pool that leaves workers
    registered has leaked a vendor CLI in production.
    """
    built: list[WorkerPool] = []

    def build(
        *, worker_module: str = _TREE_WORKER, max_workers: int = 4
    ) -> WorkerPool:
        pool = WorkerPool(
            max_workers=max_workers,
            idle_timeout_s=300.0,
            worker_module=worker_module,
            repo_root=_repo_root(),
            pid_dir=tmp_path / "workers",
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


def fleet_provider_over(pool: WorkerPool) -> FleetProvider:
    """A real ``FleetProvider`` bound to ``pool``.

    The same seam ``_get_pool`` uses, with the loop stamped so the provider
    does not decide to rebuild — and unlike ``fakes.providers.fleet_provider_with``
    the pool here is the real one, spawning real processes. That is the whole
    point of this module.
    """
    provider = FleetProvider()
    provider._pool = pool
    provider._pool_loop = asyncio.get_running_loop()
    return provider


async def _drive_step(
    provider: FleetProvider, *, session_id: str, cwd: str, prompt: str
) -> tuple[list[Event], BaseException | None]:
    """Run one fleet step through the production step runner to completion.

    A step that exceeds its budget yields its error card and then RAISES
    ``StepTimeoutError`` — in production ``_safe_run`` turns that into the
    turn's ``error`` + ``assistant.done``. Here it is returned beside the
    events so a test can assert on both halves without swallowing it.
    """
    ctx = RunContext(model="m", prompt="go", cwd=cwd, session_id=session_id)
    step = Step(id="step-1", role="coder", prompt=prompt)
    role = RoleConfig(provider="claude", model="m", system_prompt="s")
    events: list[Event] = []
    try:
        async for ev in provider._run_step_with_role(step, role, ctx, {}):
            events.append(ev)
    except StepTimeoutError as exc:
        return events, exc
    return events, None


@pytest.fixture
def _tight_step_budgets(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    from backend.app.orchestrator.fleet import provider as fleet_provider_mod

    # The heartbeat cadence is a module constant and the budgets are settings;
    # the step loop counts elapsed time in heartbeats, so both have to move or
    # the ceiling is never reached inside a test.
    monkeypatch.setattr(fleet_provider_mod, "HEARTBEAT_INTERVAL_S", HEARTBEAT_S)
    monkeypatch.setenv("FLEET_STEP_TIMEOUT_S", str(STEP_CEILING_S))
    monkeypatch.setenv("FLEET_STARTUP_GRACE_S", str(STEP_CEILING_S))


def _kill_leftovers(*pids: int) -> None:
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


# ─────────────────────────────────────────────────────────────────────────────
# 1. A fleet step that has to be abandoned
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_tight_step_budgets")
class TestAbandonedSteps:
    """Both ways a step ends without a result, asserted on the grandchild.

    ``tree_worker`` spawns a child of its own and then hangs, which is the real
    shape: the process that used to survive is the grandchild, and a test that
    kills a childless worker and finds it gone proves nothing about it.
    """

    async def test_a_step_that_exceeds_its_ceiling_leaves_no_process_behind(
        self, pools: Any, tmp_path: Path
    ) -> None:
        """The timeout path. The step runner abandons the request, the pool
        sees it had reached a worker, and the whole group goes."""
        pool = pools()
        provider = fleet_provider_over(pool)
        pid_file = tmp_path / "timeout-pids.txt"

        events, raised = await _drive_step(
            provider,
            session_id="sess-timeout",
            cwd=str(tmp_path),
            prompt=str(pid_file),
        )

        worker_pid, grandchild_pid = await _read_pids(pid_file)
        try:
            errors = [e for e in events if e.type == "tool.result" and e.data["is_error"]]
            assert errors, [e.type for e in events]
            assert "exceeded" in errors[-1].data["content"]
            # The card AND the exception: the user sees the failed step, and
            # the pipeline above is told to stop rather than carrying on with
            # no output for this step.
            assert isinstance(raised, StepTimeoutError)
            await _wait_until_dead(worker_pid, "worker after the step ceiling")
            await _wait_until_dead(
                grandchild_pid, "grandchild (the vendor CLI) after the step ceiling"
            )
            print(
                f"\n[leak] step ceiling: worker {worker_pid} and grandchild "
                f"{grandchild_pid} both reaped"
            )
        finally:
            _kill_leftovers(worker_pid, grandchild_pid)

    async def test_a_step_cancelled_mid_flight_leaves_no_process_behind(
        self, pools: Any, tmp_path: Path
    ) -> None:
        """D14.1's path, and the automated stand-in for Task 14's manual Ctrl-C
        chain: the user stops a live turn, so the consumer of the step
        generator is cancelled while the worker is mid-request.

        Cancellation has to reach the step runner's ``finally`` — which
        cancels the future and ``abandon``s the key — or the worker survives
        holding a vendor CLI that nobody will ever read the output of.
        """
        pool = pools()
        provider = fleet_provider_over(pool)
        pid_file = tmp_path / "cancel-pids.txt"

        async def consume() -> None:
            await _drive_step(
                provider,
                session_id="sess-cancel",
                cwd=str(tmp_path),
                prompt=str(pid_file),
            )

        task = asyncio.create_task(consume())
        worker_pid, grandchild_pid = await _read_pids(pid_file)
        try:
            assert _alive(worker_pid) and _alive(grandchild_pid)

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            await _wait_until_dead(worker_pid, "worker after cancellation")
            await _wait_until_dead(
                grandchild_pid, "grandchild (the vendor CLI) after cancellation"
            )
            print(
                f"\n[leak] cancelled mid-step: worker {worker_pid} and grandchild "
                f"{grandchild_pid} both reaped"
            )
        finally:
            _kill_leftovers(worker_pid, grandchild_pid)

    async def test_closing_the_provider_reaps_the_whole_group(
        self, pools: Any, tmp_path: Path
    ) -> None:
        """Shutdown, with a step still in flight. ``aclose()`` runs on the
        lifespan path, and a worker that survives it is an orphan for as long
        as the machine stays up."""
        pool = pools()
        provider = fleet_provider_over(pool)
        pid_file = tmp_path / "aclose-pids.txt"
        key = worker_key("sess-aclose", "claude", "m", str(tmp_path))

        _first, result = await pool.submit(key, _request(str(pid_file)))
        worker_pid, grandchild_pid = await _read_pids(pid_file)
        try:
            await provider.aclose()

            await _wait_until_dead(worker_pid, "worker after provider.aclose()")
            await _wait_until_dead(
                grandchild_pid, "grandchild (the vendor CLI) after provider.aclose()"
            )
            assert pool.keys == []
            # The pending step is failed, not left hanging: a caller awaiting a
            # future nobody will ever resolve is a wedged turn.
            assert result.done()
            print(
                f"\n[leak] provider.aclose(): worker {worker_pid} and grandchild "
                f"{grandchild_pid} both reaped, pending step resolved"
            )
        finally:
            _kill_leftovers(worker_pid, grandchild_pid)


# ─────────────────────────────────────────────────────────────────────────────
# 2. The worker cap under concurrency
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_worker_cap_bounds_steady_state_not_a_burst(pools: Any) -> None:
    """What ``fleet_max_workers`` actually promises, asserted as promised.

    Eight sessions submit at once against a cap of two. The cap does NOT refuse
    work — evicting a worker with a step in flight would fail a live step to
    save a process — so all eight are served concurrently and the pool is over
    its cap for the duration of the burst. The promise is about *steady state*:
    the next spawn after the burst evicts the least-recently-used idle workers
    back to the cap, and the processes it evicted are gone, not merely
    deregistered. A test that asserted "never more than two alive" would be
    asserting a behaviour the pool deliberately does not have.
    """
    cap = 2
    pool = pools(worker_module=_ECHO_WORKER, max_workers=cap)
    keys = [worker_key(f"sess-{n}", "claude", "m", None) for n in range(8)]

    submitted = [await pool.submit(key, _request("pid")) for key in keys]
    pids = [
        int((await asyncio.wait_for(result, _WAIT_S)).summary)
        for _first, result in submitted
    ]
    assert len(set(pids)) == len(pids), "concurrent steps shared a worker process"
    burst_workers = len(pool.keys)

    # One more step, now that the burst is idle: this is the spawn that applies
    # the cap.
    _first, result = await pool.submit(
        worker_key("sess-late", "claude", "m", None), _request("pid")
    )
    late_pid = int((await asyncio.wait_for(result, _WAIT_S)).summary)

    await _wait_for(
        lambda: len(pool.keys) <= cap, f"the pool to settle back to {cap} workers"
    )
    evicted = [pid for pid in pids if pid not in {pool.worker_pid(k) for k in pool.keys}]
    for pid in evicted:
        await _wait_until_dead(pid, f"evicted worker {pid}")

    alive = [pid for pid in [*pids, late_pid] if _alive(pid)]
    print(
        f"\n[leak] cap {cap}: {len(keys)} concurrent steps ran in "
        f"{burst_workers} workers, {len(pool.keys)} registered and {len(alive)} "
        f"alive after the burst ({len(evicted)} evicted and reaped)"
    )
    assert burst_workers == len(keys), "the cap refused work instead of bounding it"
    assert len(pool.keys) <= cap
    assert len(alive) <= cap, f"{len(alive)} worker processes outlived the cap"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Session churn
# ─────────────────────────────────────────────────────────────────────────────


async def test_fifty_sessions_created_and_deleted_leave_nothing_behind(
    isolated_store: Path,
) -> None:
    """The registry holds a runner — and through it a bus, a lock, a turn task
    and an approval queue — for every session a viewer has touched. Fifty
    create/use/delete cycles must leave the process exactly as it started:
    no runner, no detached turn, no task, no descriptor.
    """
    cwd = isolated_store / "proj"
    tasks_before = len(asyncio.all_tasks())
    fds_before = len(os.listdir("/dev/fd"))

    async def one_turn(ctx: RunContext) -> AsyncIterator[Event]:
        yield Event(type="assistant.text", data={"text": "ok"})
        yield Event(type="assistant.done", data={})

    for n in range(CHURN_SESSIONS):
        meta = await session_store.create_session(
            provider="scripted", model="m", cwd=str(cwd)
        )
        session_id = str(meta["id"])
        runner = await runner_registry.get_runner(session_id)
        assert runner is not None
        subscription = await runner.subscribe()
        started = runner.start_turn(
            provider=ScriptedProvider(one_turn),
            provider_name="scripted",
            model="m",
            cwd=str(cwd),
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt=f"turn {n}",
        )
        assert started
        await asyncio.wait_for(runner._turn_task, _WAIT_S)
        await runner.unsubscribe(subscription.queue)
        assert await session_store.delete_session(session_id)
        await runner_registry.drop_runner(session_id)

    # Give anything that unwinds on its own callback one scheduling pass.
    await asyncio.sleep(0)
    tasks_after = len(asyncio.all_tasks())
    fds_after = len(os.listdir("/dev/fd"))

    print(
        f"\n[leak] {CHURN_SESSIONS} sessions created, run and deleted: "
        f"runners {len(runner_registry._runners)}, "
        f"detached turns {len(runner_registry._detached_turns)}, "
        f"tasks {tasks_before} -> {tasks_after}, fds {fds_before} -> {fds_after}"
    )

    assert runner_registry._runners == {}, "a runner outlived its session"
    assert runner_registry._detached_turns == set(), "a turn task was left detached"
    assert tasks_after <= tasks_before, (
        f"{tasks_after - tasks_before} tasks outlived {CHURN_SESSIONS} sessions"
    )
    assert fds_after <= fds_before + 1, (
        f"descriptors grew from {fds_before} to {fds_after} over "
        f"{CHURN_SESSIONS} sessions"
    )
    assert await session_store.list_sessions() == []
