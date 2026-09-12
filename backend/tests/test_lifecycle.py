"""Turn-lifecycle regressions: shutdown, orphaned processes, terminal events.

The audit's Task-14 defects all live in the boundary between a turn and the
things that outlive it — the process tree it spawned, the runner registry, the
session directory:

  * D14.1 — shutdown closed the providers but never cancelled the detached
    turn task, so the step's ``finally`` never killed its child. And the kill
    it would have run reached only the worker, not the vendor CLI the worker
    spawned, so every restart with a turn in flight left a billable orphan.
  * D14.2 — a fleet turn emitted ``assistant.done`` twice and the second one,
    carrying no ``cost_usd``, was the one that got persisted.
  * D14.3 — a prompt for a deleted session raised out of the turn task with no
    ``error`` and no ``assistant.done``, so the UI spun forever.
  * D14.4 — a dropped runner could keep running while a new runner served the
    same session id, i.e. two turns for one session.
  * D14.5 — ``cancel_turn`` swallowed the caller's own cancellation.

The process-group test is the one that proves D14.1: it asserts on the
*grandchild's* pid, because that is the process that used to survive.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from backend.app.main import lifespan
from backend.app.orchestrator import registry as provider_registry
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.fleet import provider as fleet_mod
from backend.app.orchestrator.fleet.models import FleetConfig, RoleConfig
from backend.app.session_runner import registry as runner_registry
from backend.app.session_runner.runner import SessionRunner
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store

# Worker module path for the process-group test, in the same dotted form the
# provider uses for the real worker.
_FAKE_WORKER = "backend.tests.fakes.tree_worker"


# ─────────────────────────────────────────────────────────────────────────────
# Doubles
# ─────────────────────────────────────────────────────────────────────────────


class RecordingBus:
    """``EventBus`` stand-in that keeps every broadcast in order.

    The turn's terminal-event invariant is about what subscribers see, so the
    tests assert on this list rather than on calls into the store.
    """

    def __init__(self, session_id: str = "rec") -> None:
        self.session_id = session_id
        self.events: list[dict[str, Any]] = []

    async def broadcast(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    @property
    def types(self) -> list[str]:
        return [e.get("type", "") for e in self.events]

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == event_type]


class StubProvider:
    """Provider that yields a fixed event list and records that it ran."""

    name = "stub"

    def __init__(self, events: list[Event] | None = None) -> None:
        self.events = events or []
        self.ran = False

    async def open_session(self, ctx: RunContext) -> str:
        return ctx.upstream_session_id or ""

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        self.ran = True
        for ev in self.events:
            yield ev

    async def aclose(self) -> None:
        return None


class HangingProvider:
    """Provider whose turn never finishes until it is cancelled.

    Appends to ``order`` when cancelled so a test can assert *when* the
    cancellation happened relative to provider shutdown.
    """

    name = "hanging"

    def __init__(self, order: list[str] | None = None) -> None:
        self.started = asyncio.Event()
        self.order = order if order is not None else []

    async def open_session(self, ctx: RunContext) -> str:
        return ""

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.order.append("turn cancelled")
            raise
        yield Event(type="assistant.done", data={})  # pragma: no cover

    async def aclose(self) -> None:
        return None


class StubbornTurn:
    """A turn task that swallows cancellation — the wedged-turn shape.

    ``cancel_turn`` detaches after its grace window precisely because a turn
    can sit in something that does not unwind; ``swallow`` says how many
    cancellations to absorb before finally going down, which is what lets a
    test distinguish the first attempt from shutdown's final one.
    """

    def __init__(self, swallow: int) -> None:
        self.swallow = swallow
        self.cancels = 0
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        while True:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancels += 1
                if self.cancels > self.swallow:
                    raise


def _start_turn_kwargs(provider: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "provider_name": "stub",
        "model": "m",
        "cwd": None,
        "additional_dirs": [],
        "upstream_id": None,
        "fleet_override": None,
        "permission_mode": None,
        "prompt": "hello",
    }


@pytest.fixture(autouse=True)
def clean_registries() -> Any:
    """Leave no runner or provider singleton behind for the next test.

    Both registries are module-level dicts; a runner left in one keeps a turn
    task alive across tests and makes the next test's assertions depend on
    execution order.
    """
    yield
    for runner in list(runner_registry._runners.values()):
        task = runner._turn_task
        if task is not None and not task.done():
            task.cancel()
    runner_registry._runners.clear()
    runner_registry._detached_turns.clear()
    provider_registry._singletons.clear()


async def _new_session(home: Path) -> str:
    meta = await session_store.create_session(
        provider="stub", model="m", cwd=str(home / "proj")
    )
    return str(meta["id"])


# ─────────────────────────────────────────────────────────────────────────────
# D14.2 — exactly one assistant.done per fleet turn
# ─────────────────────────────────────────────────────────────────────────────


def _one_role_config() -> FleetConfig:
    return FleetConfig(
        name="test",
        roles={"coder": RoleConfig(provider="claude", model="m", system_prompt="s")},
        entry_role="coder",
    )


def _stub_orchestrator(events: list[Event]) -> Any:
    async def _run(ctx: RunContext, cfg: FleetConfig) -> AsyncIterator[Event]:
        for ev in events:
            yield ev

    return _run


async def test_fleet_turn_emits_one_done_carrying_cost_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator's done carries the cost; only ``run()`` knows the wall
    time. Merging them is the only way to keep both and stay at one done."""
    monkeypatch.setattr(fleet_mod, "load_fleet_config", lambda cwd: _one_role_config())
    fleet = fleet_mod.FleetProvider()
    monkeypatch.setattr(
        fleet,
        "_run_orchestrated",
        _stub_orchestrator(
            [
                Event(type="assistant.text", data={"text": "working"}),
                Event(
                    type="assistant.done",
                    data={"cost_usd": 0.42, "duration_ms": 11, "num_turns": 3},
                ),
            ]
        ),
    )

    events = [ev async for ev in fleet.run(RunContext(model="m", prompt="p"))]

    dones = [ev for ev in events if ev.type == "assistant.done"]
    assert len(dones) == 1, [ev.type for ev in events]
    assert dones[0].data["cost_usd"] == 0.42
    assert dones[0].data["num_turns"] == 3
    assert isinstance(dones[0].data["duration_ms"], int)
    # Terminal means last: a subscriber clears its working indicator on it.
    assert events[-1].type == "assistant.done"


async def test_fleet_turn_still_emits_a_done_when_the_orchestrator_omits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error paths never reach a ResultMessage, so nobody upstream yields a
    done — the provider must still terminate the turn exactly once."""
    monkeypatch.setattr(fleet_mod, "load_fleet_config", lambda cwd: _one_role_config())
    fleet = fleet_mod.FleetProvider()
    monkeypatch.setattr(
        fleet,
        "_run_orchestrated",
        _stub_orchestrator([Event(type="error", data={"message": "backend died"})]),
    )

    events = [ev async for ev in fleet.run(RunContext(model="m", prompt="p"))]

    dones = [ev for ev in events if ev.type == "assistant.done"]
    assert len(dones) == 1
    assert isinstance(dones[0].data["duration_ms"], int)
    assert dones[0].data.get("cost_usd") is None


async def test_fleet_turn_with_no_roles_configured_emits_one_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = FleetConfig(name="empty", roles={}, entry_role="coder")
    monkeypatch.setattr(fleet_mod, "load_fleet_config", lambda cwd: empty)
    fleet = fleet_mod.FleetProvider()

    events = [ev async for ev in fleet.run(RunContext(model="m", prompt="p"))]

    assert [ev.type for ev in events] == ["error", "assistant.done"]


async def test_fleet_turn_persists_the_cost_it_reported(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-visible symptom of the double done, end to end: the accumulator
    keeps the last done it sees, so the spurious second one (no ``cost_usd``)
    was what got persisted and every fleet turn showed up free in history."""
    session_id = await _new_session(isolated_store)
    monkeypatch.setattr(fleet_mod, "load_fleet_config", lambda cwd: _one_role_config())
    provider = fleet_mod.FleetProvider()
    monkeypatch.setattr(
        provider,
        "_run_orchestrated",
        _stub_orchestrator(
            [
                Event(type="assistant.text", data={"text": "done thinking"}),
                Event(type="assistant.done", data={"cost_usd": 1.25, "num_turns": 2}),
            ]
        ),
    )
    bus = RecordingBus(session_id)

    await execute_turn(
        session_id=session_id,
        bus=bus,  # type: ignore[arg-type]
        approval_q=asyncio.Queue(),
        provider=provider,  # type: ignore[arg-type]
        provider_name="fleet",
        model="m",
        cwd=None,
        additional_dirs=[],
        upstream_id=None,
        fleet_override=None,
        permission_mode=None,
        prompt="hi",
    )

    assert bus.types.count("assistant.done") == 1, bus.types
    messages, _, _ = await session_store.list_messages(session_id)
    assistant = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["cost_usd"] == 1.25
    assert assistant[0]["duration_ms"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# D14.3 — a prompt for a deleted session
# ─────────────────────────────────────────────────────────────────────────────


async def test_turn_for_a_deleted_session_reports_the_deletion_and_terminates(
    isolated_store: Path,
) -> None:
    session_id = await _new_session(isolated_store)
    await session_store.delete_session(session_id)
    provider = StubProvider([Event(type="assistant.text", data={"text": "nope"})])
    bus = RecordingBus(session_id)

    # Must not raise: this runs in a detached task, where an exception shows up
    # only as "Task exception was never retrieved" at GC.
    await execute_turn(
        session_id=session_id,
        bus=bus,  # type: ignore[arg-type]
        approval_q=asyncio.Queue(),
        provider=provider,  # type: ignore[arg-type]
        provider_name="stub",
        model="m",
        cwd=None,
        additional_dirs=[],
        upstream_id=None,
        fleet_override=None,
        permission_mode=None,
        prompt="hi",
    )

    errors = bus.of_type("error")
    assert errors, bus.types
    assert "delete" in errors[0]["data"]["message"].lower()
    # Exactly one terminal event, or the UI either spins forever or clears a
    # later turn's indicator.
    assert bus.types.count("assistant.done") == 1
    # Nothing ran and nothing was resurrected on disk — no stray current.json.
    assert provider.ran is False
    messages, _, _ = await session_store.list_messages(session_id)
    assert messages == []


async def test_turn_surfaces_an_unexpected_persist_failure_and_terminates(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt append is inside the turn's try for *any* failure, not just
    the deleted-session one — an ``OSError`` must still clear the UI."""
    session_id = await _new_session(isolated_store)

    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(session_store, "append_message", boom)
    bus = RecordingBus(session_id)

    await execute_turn(
        session_id=session_id,
        bus=bus,  # type: ignore[arg-type]
        approval_q=asyncio.Queue(),
        provider=StubProvider(),  # type: ignore[arg-type]
        provider_name="stub",
        model="m",
        cwd=None,
        additional_dirs=[],
        upstream_id=None,
        fleet_override=None,
        permission_mode=None,
        prompt="hi",
    )

    assert "disk full" in bus.of_type("error")[0]["data"]["message"]
    assert bus.types.count("assistant.done") == 1


# ─────────────────────────────────────────────────────────────────────────────
# D14.1 — shutdown cancels turns, and the kill reaches the whole tree
# ─────────────────────────────────────────────────────────────────────────────


async def test_lifespan_shutdown_cancels_turns_before_closing_providers(
    isolated_store: Path,
) -> None:
    """Ordering is the defect: a provider closed first takes the child's event
    stream with it, and the step's ``finally`` — the only code that kills the
    child — then never runs."""
    order: list[str] = []
    session_id = await _new_session(isolated_store)

    class CloseRecorder:
        name = "recorder"

        async def aclose(self) -> None:
            order.append("providers closed")

    app = object()  # lifespan only needs something to bind to
    async with lifespan(app):  # type: ignore[arg-type]
        provider_registry._singletons["recorder"] = CloseRecorder()  # type: ignore[assignment]
        runner = await runner_registry.get_runner(session_id)
        assert runner is not None
        provider = HangingProvider(order)
        assert runner.start_turn(**_start_turn_kwargs(provider)) is True
        await asyncio.wait_for(provider.started.wait(), timeout=5)

    assert order == ["turn cancelled", "providers closed"]
    assert runner.is_running is False
    assert runner_registry._runners == {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pids_once(path: Path) -> tuple[int, int] | None:
    """The worker's (pid, grandchild pid), or None until it has reported."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.endswith("\n"):
        return None
    worker, grandchild = text.split()
    return int(worker), int(grandchild)


async def _read_pids(path: Path, timeout_s: float = 10.0) -> tuple[int, int]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        pids = _read_pids_once(path)
        if pids is not None:
            return pids
        await asyncio.sleep(0.05)
    raise AssertionError(f"fake worker never reported its pids to {path}")


async def _wait_until_dead(pid: int, label: str, timeout_s: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if not _alive(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{label} (pid {pid}) survived the kill")


async def test_kill_reaps_the_whole_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The vendor CLI is a grandchild of the backend. Killing only the worker
    re-parents it and it keeps running on the user's subscription, so the
    assertion that matters is the grandchild's."""
    monkeypatch.setattr(fleet_mod, "_WORKER_MODULE", _FAKE_WORKER)
    pid_file = tmp_path / "pids.txt"
    handle = fleet_mod._SubprocHandle()
    await handle.start(
        RoleConfig(provider="claude", model="m", system_prompt="s"),
        str(pid_file),
        None,
        [],
        None,
        "coder",
    )

    worker_pid, grandchild_pid = await _read_pids(pid_file)
    try:
        assert _alive(worker_pid), "fake worker died before we could kill it"
        assert _alive(grandchild_pid), "fake grandchild died before we could kill it"

        handle.kill()

        await _wait_until_dead(worker_pid, "worker")
        await _wait_until_dead(grandchild_pid, "grandchild (the vendor CLI)")
    finally:
        # Never leave real processes behind, however this test ends.
        for pid in (worker_pid, grandchild_pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handle.result, timeout=5)


# ─────────────────────────────────────────────────────────────────────────────
# D14.4 — one live generation per session
# ─────────────────────────────────────────────────────────────────────────────


async def test_dropped_runner_refuses_to_start_another_turn(
    isolated_store: Path,
) -> None:
    """A WS handler can hold a reference to a runner that was dropped. If that
    stale runner still starts turns, one session id has two turns running with
    two different locks."""
    session_id = await _new_session(isolated_store)
    runner = await runner_registry.get_runner(session_id)
    assert runner is not None

    await runner_registry.drop_runner(session_id)

    assert runner.is_retired is True
    provider = StubProvider([Event(type="assistant.done", data={})])
    assert runner.start_turn(**_start_turn_kwargs(provider)) is False
    await asyncio.sleep(0)
    assert provider.ran is False
    assert runner.is_running is False


async def test_get_runner_does_not_resurrect_a_deleted_session(
    isolated_store: Path,
) -> None:
    session_id = await _new_session(isolated_store)
    assert await runner_registry.get_runner(session_id) is not None

    await runner_registry.drop_runner(session_id)
    await session_store.delete_session(session_id)

    assert await runner_registry.get_runner(session_id) is None
    assert runner_registry._runners == {}


async def test_get_runner_returns_the_same_runner_for_a_live_session(
    isolated_store: Path,
) -> None:
    session_id = await _new_session(isolated_store)
    first = await runner_registry.get_runner(session_id)
    second = await runner_registry.get_runner(session_id)
    assert first is not None and first is second


async def test_a_detached_turn_is_logged_and_retried_at_shutdown(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bounded detach exists so ``DELETE`` cannot hang, but a detached turn
    may still own a child process — so it is logged (the only evidence an
    orphan exists) and retried once when the backend shuts down."""
    monkeypatch.setattr(SessionRunner, "_CANCEL_GRACE_S", 0.05)
    session_id = await _new_session(isolated_store)
    runner = await runner_registry.get_runner(session_id)
    assert runner is not None
    stubborn = StubbornTurn(swallow=1)
    task = asyncio.create_task(stubborn.run())
    runner._turn_task = task
    await stubborn.started.wait()

    with caplog.at_level(logging.WARNING):
        await runner_registry.drop_runner(session_id)

    assert stubborn.cancels == 1
    assert not task.done(), "a turn that swallows cancellation must be detached"
    assert any(session_id in r.getMessage() for r in caplog.records), caplog.messages
    assert task in runner_registry._detached_turns

    await runner_registry.drop_all_runners()

    assert stubborn.cancels == 2, "shutdown must make one more attempt"
    assert task.done()


# ─────────────────────────────────────────────────────────────────────────────
# D14.5 — minors
# ─────────────────────────────────────────────────────────────────────────────


async def test_cancel_turn_propagates_cancellation_to_its_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DELETE`` awaits ``cancel_turn``. If it swallows the handler's own
    cancellation the handler carries on and deletes the directory anyway."""
    monkeypatch.setattr(SessionRunner, "_CANCEL_GRACE_S", 30.0)
    runner = SessionRunner("wedged")
    stubborn = StubbornTurn(swallow=1)
    runner._turn_task = asyncio.create_task(stubborn.run())
    await stubborn.started.wait()

    waiter = asyncio.create_task(runner.cancel_turn())
    await asyncio.sleep(0.05)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    # The cancel was still attempted before giving up on the wait.
    assert stubborn.cancels >= 1
    runner._turn_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner._turn_task


def test_step_ids_do_not_accumulate_state_across_turns() -> None:
    """``_step_counters`` was a module-level dict that kept one counter per
    role for the life of the process. Scoping the sequence to a turn is what
    bounds it; ids restarting in a fresh sequence is the proof."""
    from backend.app.orchestrator import dispatch

    assert not hasattr(dispatch, "_step_counters"), (
        "step-id state must not live for the life of the process"
    )

    first_turn = dispatch.StepIdSequence()
    assert first_turn.next_id("coder") == "orch.coder.1"
    assert first_turn.next_id("coder") == "orch.coder.2"
    assert first_turn.next_id("reviewer") == "orch.reviewer.1"

    second_turn = dispatch.StepIdSequence()
    assert second_turn.next_id("coder") == "orch.coder.1"
