"""Every failure mode the audit named, injected — and what the USER then sees.

The rule for this module, and the only reason it is separate from the unit
suites that cover some of the same ground: each case asserts the *user-visible*
outcome of a real failure, end to end. A clear `error` and a terminal
`assistant.done`, never a hang, never a silent success, never one turn's output
presented as another's. Where a unit suite already pins the mechanism, the case
here drives it through the composition above it rather than repeating the unit
assertion.

**What is deliberately not duplicated here, and where it lives instead:**

* the pool's v2 framing at 512 KiB, and that a byte count is not a character
  count — ``test_worker_pool.py::TestResultFraming``. The case here is the
  step ABOVE it: does an oversize result survive the envelope on its way to the
  orchestrator, and stay bounded when it gets there.
* the "no response yet" wording and the abort a wedged backend produces —
  ``test_long_horizon.py::TestWedgedBackendFastFail``. The case here adds the
  half that suite does not measure: that the fast-fail happens inside the
  GRACE window rather than at the step ceiling, which is the whole point of
  having two budgets.
* an approval nobody answers —
  ``test_long_horizon.py::TestApprovalBranching::test_a_timeout_denies_and_still_ends_the_turn``
  already drives the real gate through a real ``execute_turn`` and asserts both
  what the user saw and what the model was told. A second copy here would
  assert less, not more.
* the hard-fail cap for a non-timeout failure —
  ``test_worker_pool.py::TestDispatchCaps``. The case here generalises it to
  every failure class, which is the regression the audit actually asked for.
* a prompt for a session that is already gone —
  ``test_lifecycle.py::test_turn_for_a_deleted_session_reports_the_deletion_and_terminates``.
  The case here is the harder one: the delete lands WHILE the turn is
  streaming.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from backend.app.orchestrator import claude as claude_mod
from backend.app.orchestrator import registry as provider_registry
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.fleet.constants import (
    DISPATCH_HARD_FAIL_CAP,
    StepNotAttemptedError,
    StepTimeoutError,
)
from backend.app.orchestrator.fleet.models import RoleConfig, Step
from backend.app.orchestrator.fleet.pool import WorkerPool, worker_key
from backend.app.orchestrator.fleet.provider import FleetProvider
from backend.app.routes.sessions import chat_ws
from backend.app.session_runner import registry as runner_registry
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store
from backend.tests.fakes.claude_client import (
    FakeClaudeClient,
    FakeClientFactory,
    result_message,
    text_message,
)
from backend.tests.fakes.providers import (
    FakeSubProviders,
    FakeWorkerPool,
    ScriptedProvider,
)

from .test_event_integrity import FakeWebSocket
from .test_worker_pool import (
    _ECHO_WORKER,
    _alive,
    _dispatch_tool,
    _Recorder,
    _repo_root,
    _request,
    _wait_for,
)

# Long enough not to flake on a loaded machine; short enough that a case which
# genuinely hangs fails this suite instead of parking the whole run. Every
# injection here is a candidate for hanging — that is what is being tested — so
# nothing awaits a turn without this bound.
WAIT_S = 20.0

# The oversize result. audit-derived: D-B2's defect was a result larger than
# the stdout reader's undocumented 64 KiB line limit; 512 KiB is comfortably
# past it and is the size ``test_worker_pool.py`` frames at.
BIG_RESULT_BYTES = 512 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Shared scaffolding
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registries() -> Any:
    """A runner left in the registry keeps a turn task alive into the next
    test, where it fails something unrelated."""
    yield
    for runner in list(runner_registry._runners.values()):
        task = runner._turn_task
        if task is not None and not task.done():
            task.cancel()
    runner_registry._runners.clear()
    runner_registry._detached_turns.clear()
    provider_registry._singletons.clear()


@pytest.fixture
async def real_pool(tmp_path: Path) -> AsyncIterator[WorkerPool]:
    pool = WorkerPool(
        max_workers=4,
        idle_timeout_s=300.0,
        worker_module=_ECHO_WORKER,
        repo_root=_repo_root(),
        pid_dir=tmp_path / "workers",
    )
    try:
        yield pool
    finally:
        with contextlib.suppress(Exception):
            await pool.aclose()


def _provider_over(pool: Any) -> FleetProvider:
    provider = FleetProvider()
    provider._pool = pool
    provider._pool_loop = asyncio.get_running_loop()
    return provider


async def _step(
    provider: FleetProvider,
    *,
    prompt: str,
    session_id: str = "sess-inject",
    cwd: str | None = None,
    role: str = "coder",
) -> tuple[list[Event], dict[str, Any], BaseException | None]:
    """One fleet step through the production step runner.

    Returns its events, the envelopes it recorded, and whatever it raised —
    ``_safe_run`` is what catches the last of those in production, and a test
    that swallowed it would not be able to tell an abort from a success.
    """
    ctx = RunContext(model="m", prompt="go", cwd=cwd, session_id=session_id)
    outputs: dict[str, Any] = {}
    events: list[Event] = []
    try:
        async for ev in provider._run_step_with_role(
            Step(id="step-1", role=role, prompt=prompt),
            RoleConfig(provider="claude", model="m", system_prompt="s"),
            ctx,
            outputs,
        ):
            events.append(ev)
    except (StepTimeoutError, StepNotAttemptedError) as exc:
        return events, outputs, exc
    return events, outputs, None


def _errors(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type == "tool.result" and e.data.get("is_error")]


# ─────────────────────────────────────────────────────────────────────────────
# 1. The sub-provider process boundary
# ─────────────────────────────────────────────────────────────────────────────


class TestTheWorkerBoundary:
    """Real worker processes, real framing, the real step runner above them."""

    async def test_an_oversize_result_reaches_the_step_intact(
        self, real_pool: WorkerPool
    ) -> None:
        """D-B2: a result larger than one line used to deadlock BOTH sides —
        the parent raised on the line at 64 KiB and then blocked in
        ``proc.wait()`` forever, because the child was still writing the rest
        into a pipe nobody drained. So the assertion is not only "no
        truncation": it is that the step finished at all, and that every byte
        the worker produced is in the envelope the step recorded, because the
        planner's plan file is written from exactly that.

        What is NOT asserted here: that the orchestrator's view is bounded.
        This worker frames its own envelope, so the eviction that bounds a
        real step (``collect_step`` → the artifact store) never runs — faking
        it would make a fake's arithmetic look like production's. The bound is
        ``test_long_horizon.py::TestContextEviction``'s subject, against the
        real collector.
        """
        provider = _provider_over(real_pool)

        events, outputs, raised = await _step(
            provider, prompt=f"big:{BIG_RESULT_BYTES}"
        )

        assert raised is None, raised
        envelope = outputs["step-1"]
        card = [e for e in events if e.type == "tool.result"][-1]
        print(
            f"\n[inject] {BIG_RESULT_BYTES} bytes crossed the worker boundary "
            f"as {len(envelope.summary.encode('utf-8'))} bytes of envelope "
            f"(full_bytes {envelope.full_bytes})"
        )
        assert len(envelope.summary.encode("utf-8")) == BIG_RESULT_BYTES, (
            "the oversize result did not survive the worker boundary intact"
        )
        assert envelope.summary == "B" * BIG_RESULT_BYTES
        assert not card.data["is_error"]

    async def test_a_worker_that_exits_without_a_result_names_why(
        self, real_pool: WorkerPool
    ) -> None:
        """Exit 0 and no result is the worst shape of failure: nothing raised,
        nothing returned. The step's error card is the only place the user can
        learn anything, so it has to carry the exit code AND the stderr tail —
        without them the report is "it failed", which is unactionable."""
        provider = _provider_over(real_pool)

        events, _outputs, raised = await _step(
            provider, prompt="crash:auth prompt on stdin"
        )

        assert raised is None
        detail = _errors(events)[-1].data["content"]
        print(f"\n[inject] worker exited without a result: {detail!r}")
        assert "exited without a result" in detail
        assert "exit=0" in detail
        assert "auth prompt on stdin" in detail

    async def test_a_worker_killed_mid_request_fails_the_step_not_the_turn(
        self, real_pool: WorkerPool
    ) -> None:
        """An OOM kill, or an operator with ``kill -9``. The pending future must
        be failed by the reader noticing the pipe closed — if it is simply left
        unresolved the step waits out its whole budget on a process that no
        longer exists, and the user watches a spinner for ten minutes."""
        key = worker_key("sess-killed", "claude", "m", None)
        first, result = await real_pool.submit(key, _request("hang"))
        await asyncio.wait_for(first.wait(), WAIT_S)
        pid = real_pool.worker_pid(key)
        assert pid is not None and _alive(pid)

        os.kill(pid, signal.SIGKILL)

        with pytest.raises(RuntimeError) as excinfo:
            await asyncio.wait_for(result, WAIT_S)
        print(f"\n[inject] worker killed mid-request: {str(excinfo.value)!r}")
        assert "exited without a result" in str(excinfo.value)
        await _wait_for(
            lambda: key not in real_pool.keys, "the dead worker to deregister"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Backends that never finish
# ─────────────────────────────────────────────────────────────────────────────


class TestBackendsThatDoNotFinish:
    """Two budgets, two different failures, and the difference is the point.

    A backend that says NOTHING is wedged and is failed inside the startup
    grace; a backend that streams forever is alive but unbounded and is failed
    at the step ceiling. Collapsing them would make one of the two honest
    messages a lie.
    """

    GRACE_S = 0.2
    CEILING_S = 1.0

    @pytest.fixture(autouse=True)
    def _tiny_budgets(
        self, monkeypatch: pytest.MonkeyPatch, fresh_settings: None
    ) -> None:
        from backend.app.orchestrator.fleet import provider as fleet_provider_mod

        monkeypatch.setattr(fleet_provider_mod, "HEARTBEAT_INTERVAL_S", 0.05)
        monkeypatch.setenv("FLEET_STARTUP_GRACE_S", str(self.GRACE_S))
        monkeypatch.setenv("FLEET_STEP_TIMEOUT_S", str(self.CEILING_S))

    @staticmethod
    def _silent_backend(monkeypatch: pytest.MonkeyPatch) -> FakeSubProviders:
        async def run(ctx: RunContext) -> AsyncIterator[Event]:
            await asyncio.Event().wait()
            yield Event(type="assistant.done", data={})  # pragma: no cover

        return FakeSubProviders({None: run}).install(monkeypatch)

    @staticmethod
    def _endless_backend(monkeypatch: pytest.MonkeyPatch) -> FakeSubProviders:
        async def run(ctx: RunContext) -> AsyncIterator[Event]:
            while True:
                yield Event(type="assistant.text", data={"text": "still going "})
                await asyncio.sleep(0.01)

        return FakeSubProviders({None: run}).install(monkeypatch)

    async def test_a_silent_backend_fails_in_the_grace_window_not_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The wording is asserted by ``test_long_horizon.py``; what is
        asserted here is the TIMING, which is the only reason the grace window
        exists. Failing a silent backend at the step ceiling instead would be
        ten minutes of "still working" about a backend that never spoke."""
        self._silent_backend(monkeypatch)
        pool = FakeWorkerPool()
        provider = _provider_over(pool)

        started = time.monotonic()
        events, _outputs, raised = await asyncio.wait_for(
            _step(provider, prompt="do it", cwd=str(tmp_path)), WAIT_S
        )
        elapsed = time.monotonic() - started

        detail = _errors(events)[-1].data["content"]
        print(
            f"\n[inject] a silent backend was failed after {elapsed:.2f} s "
            f"(grace {self.GRACE_S} s, ceiling {self.CEILING_S} s)"
        )
        assert isinstance(raised, StepTimeoutError)
        assert "produced NO output" in detail
        assert elapsed < self.CEILING_S, (
            f"the silent backend was not failed until {elapsed:.2f} s — the "
            "startup grace did not fire and the step ran to its ceiling"
        )
        # The worker holding the wedged backend is reclaimed, not left running.
        assert pool.killed

    async def test_a_backend_that_streams_forever_is_stopped_at_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """It IS responding, so the grace window must not fire — only the
        absolute ceiling bounds it, and the message must blame the budget
        rather than accusing a backend that is visibly alive."""
        self._endless_backend(monkeypatch)
        pool = FakeWorkerPool()
        provider = _provider_over(pool)

        started = time.monotonic()
        events, _outputs, raised = await asyncio.wait_for(
            _step(provider, prompt="do it", cwd=str(tmp_path)), WAIT_S
        )
        elapsed = time.monotonic() - started

        detail = _errors(events)[-1].data["content"]
        print(
            f"\n[inject] an endless backend was stopped after {elapsed:.2f} s "
            f"(ceiling {self.CEILING_S} s): {detail!r}"
        )
        assert isinstance(raised, StepTimeoutError)
        assert "exceeded" in detail and "budget" in detail
        assert "produced NO output" not in detail, (
            "a backend that streamed was accused of saying nothing"
        )
        assert elapsed >= self.GRACE_S
        assert pool.killed


# ─────────────────────────────────────────────────────────────────────────────
# 3. The hard-fail cap, for every failure class
# ─────────────────────────────────────────────────────────────────────────────


# Every way a step can fail, and what the cap must do about it. The audit found
# a cap that counted ONE of these (timeouts), so a deterministic failure of any
# other kind was re-dispatched until ``max_turns`` ran out — at a step budget
# each, holding the session lock. ``StepNotAttemptedError`` is the deliberate
# exception: that step never reached a sub-provider, so it demonstrated nothing
# about the backend and charging it would refuse a role for a failure it was
# never given the chance to have.
FAILURE_CLASSES: dict[str, BaseException] = {
    "timeout": StepTimeoutError("coder step exceeded 600s budget"),
    "worker_died": RuntimeError("sub-provider worker exited without a result (exit=1)"),
    "oversize_result": ValueError("result payload is not valid JSON"),
    "broken_pipe": OSError("[Errno 32] Broken pipe"),
    "vendor_error": RuntimeError("claude: not logged in"),
}


@pytest.mark.parametrize("failure", sorted(FAILURE_CLASSES))
async def test_every_failure_class_is_bounded_by_the_hard_fail_cap(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression test for the unbounded retry chain, generalised.

    Whatever the step raised, the orchestrator may be handed the role
    ``DISPATCH_HARD_FAIL_CAP`` times and no more — after that the dispatch is
    refused without running anything, and the refusal tells the orchestrator to
    stop rather than leaving it to infer that from a failure message.
    """
    recorder = _Recorder(FAILURE_CLASSES[failure])
    tool, _registry = _dispatch_tool(monkeypatch, recorder)

    results = [
        await tool({"name": "coder", "prompt": "work"})
        for _ in range(DISPATCH_HARD_FAIL_CAP + 2)
    ]

    assert all(r["is_error"] for r in results)
    assert recorder.calls == DISPATCH_HARD_FAIL_CAP, (
        f"{failure}: the step ran {recorder.calls} times against a cap of "
        f"{DISPATCH_HARD_FAIL_CAP}"
    )
    assert "STOP" in results[DISPATCH_HARD_FAIL_CAP - 1]["content"][0]["text"]
    for refused in results[DISPATCH_HARD_FAIL_CAP:]:
        assert "REFUSING to dispatch" in refused["content"][0]["text"]


async def test_a_step_that_never_reached_a_backend_is_not_charged_to_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same rule, and the one a blunt "count everything"
    cap gets wrong: a step dropped while still queued behind another step on
    the same worker says nothing about that backend."""
    recorder = _Recorder(StepNotAttemptedError("still QUEUED behind another step"))
    tool, _registry = _dispatch_tool(monkeypatch, recorder)

    results = [
        await tool({"name": "coder", "prompt": "work"})
        for _ in range(DISPATCH_HARD_FAIL_CAP + 2)
    ]

    assert all(r["is_error"] for r in results)
    assert recorder.calls == DISPATCH_HARD_FAIL_CAP + 2, (
        "a step that never ran was charged against the backend's retry cap"
    )
    assert "not held against" in results[-1]["content"][0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. The Claude connection
# ─────────────────────────────────────────────────────────────────────────────


class TestTheClaudeConnection:
    """Injections against the SDK's own edges, through a real turn.

    The unit-level contracts live in ``test_claude_client_reuse.py`` and
    ``test_usage.py``; what is asserted here is what the SESSION ends up
    holding, because that is what the user reads tomorrow.
    """

    @staticmethod
    async def _turn(
        session_id: str, cwd: Path, provider: Any, prompt: str
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        await asyncio.wait_for(
            execute_turn(
                session_id=session_id,
                bus=EventBus(session_id, on_event=events.append),
                approval_q=asyncio.Queue(),
                provider=provider,
                provider_name="claude",
                model="m",
                cwd=str(cwd),
                additional_dirs=[],
                upstream_id=None,
                fleet_override=None,
                permission_mode=None,
                prompt=prompt,
            ),
            WAIT_S,
        )
        return events

    async def test_a_deferred_tail_never_becomes_the_next_turns_transcript(
        self, isolated_store: Path, fresh_settings: None
    ) -> None:
        """``DEFERRING_TASK_TYPES``: a ``ResultMessage`` can arrive with
        backgrounded agent work still in flight, and the follow-up frames plus
        a second result land after it on the same connection. Turn 2 used to
        read them, present them as its own answer and terminate on turn 1's
        second result — silent misattribution, persisted into the transcript
        the user reads tomorrow."""
        turns = {"n": 0}

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            turns["n"] += 1
            n = turns["n"]
            yield text_message(f"answer to turn {n}")
            yield result_message()
            if n == 1:
                yield text_message("STALE deferred agent output")
                yield result_message()

        cwd = isolated_store / "proj"
        meta = await session_store.create_session(
            provider="claude", model="m", cwd=str(cwd)
        )
        session_id = str(meta["id"])
        provider = claude_mod.ClaudeProvider()
        provider._factory = FakeClientFactory(behaviour)

        await self._turn(session_id, cwd, provider, "first")
        second = await self._turn(session_id, cwd, provider, "second")
        await provider.aclose()

        texts = [
            e["data"].get("text")
            for e in second
            if e.get("type") == "assistant.text"
        ]
        assert texts == ["answer to turn 2"], texts

        messages, _before, _more = await session_store.list_messages(session_id)
        transcript = [
            block.get("text", "")
            for message in messages
            for block in message["content"]
            if block.get("type") == "text"
        ]
        print(f"\n[inject] deferred tail: persisted transcript is {transcript}")
        assert "STALE deferred agent output" not in transcript, (
            "the previous turn's backgrounded output was persisted as this "
            "turn's answer"
        )
        assert transcript == ["first", "answer to turn 1", "second", "answer to turn 2"]

    async def test_a_usage_attribute_that_raises_still_ends_the_turn_with_done(
        self, isolated_store: Path, fresh_settings: None
    ) -> None:
        """``parse_claude_usage`` reads ``result.usage`` through ``getattr``
        with a default, which covers a MISSING attribute and not a raising one.
        It runs inside the per-turn loop, so an escape there turned a
        successful turn into an ``error`` with no ``assistant.done`` from the
        provider at all — a working indicator cleared only by the turn's own
        backstop, and a turn's answer reported as a failure."""

        class RaisingUsage:
            """A ``ResultMessage`` whose usage blows up on access."""

            subtype = "success"
            duration_ms = 12
            num_turns = 1
            session_id = "upstream-1"
            total_cost_usd = 0.25
            result = "done"
            is_error = False

            @property
            def usage(self) -> dict[str, int]:
                raise RuntimeError("usage is unavailable on this result")

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            yield text_message("the answer")
            yield result_message()

        cwd = isolated_store / "proj"
        meta = await session_store.create_session(
            provider="claude", model="m", cwd=str(cwd)
        )
        session_id = str(meta["id"])
        provider = claude_mod.ClaudeProvider()
        provider._factory = FakeClientFactory(behaviour)

        # The injection: every ResultMessage this turn parses is the raising
        # one. Patched at the parse site so the SDK's own dataclass (which
        # would refuse the property) is not involved.
        real_parse = claude_mod.parse_claude_usage
        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            claude_mod,
            "parse_claude_usage",
            lambda message, **kw: real_parse(RaisingUsage(), **kw),
        )
        try:
            events = await self._turn(session_id, cwd, provider, "go")
        finally:
            monkey.undo()
            await provider.aclose()

        types = [e.get("type") for e in events]
        print(f"\n[inject] raising usage attribute: events were {types}")
        assert types.count("assistant.done") == 1, types
        assert "error" not in types, "a telemetry field cost the turn its result"
        done = [e for e in events if e["type"] == "assistant.done"][-1]
        # Usage is absent (or zeroed), the turn's own numbers survive.
        assert done["data"].get("cost_usd") == 0.25
        assert (done["data"].get("usage") or {}).get("input_tokens", 0) == 0

    @pytest.mark.parametrize("end_sentinel", [False, True])
    async def test_a_failed_stream_drops_its_client_either_way(
        self, end_sentinel: bool, tmp_path: Path, fresh_settings: None
    ) -> None:
        """The fake's own fidelity, made explicit.

        The real SDK's reader queues its end sentinel from a ``finally``, so a
        stream that died on an exception ALSO ends; this fake only queued the
        failure. That difference could quietly become a test's premise, so the
        error path is driven BOTH ways here — with the sentinel and without —
        and the provider's behaviour has to be identical: surface the error,
        drop the client, connect a fresh one next turn.
        """

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            yield text_message("partial answer")
            raise RuntimeError("the transport closed")

        factory = FakeClientFactory(behaviour, end_sentinel_after_error=end_sentinel)
        provider = claude_mod.ClaudeProvider()
        provider._factory = factory
        ctx = RunContext(model="m", prompt="go", cwd=str(tmp_path), session_id="s1")

        events = [ev async for ev in provider.run(ctx)]
        try:
            assert [e.type for e in events] == ["assistant.text", "error"]
            assert "transport closed" in events[-1].data["message"]
            assert provider._clients == {}, "a wedged client was kept for reuse"
            assert factory.clients[0].disconnected

            # And the next turn is served by a new connection, not a corpse.
            await provider.run(ctx).__anext__()
        finally:
            await provider.aclose()
        assert len(factory.clients) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 5. The viewer and the session, removed mid-turn
# ─────────────────────────────────────────────────────────────────────────────


class _WebSocketHeldOpen(FakeWebSocket):
    """A viewer that stays connected until the test disconnects it.

    ``FakeWebSocket`` returns from ``chat_ws`` as soon as its script runs out,
    which is the right shape for a connect-and-read case and the wrong one for
    "the socket dies in the middle of a turn": the handler has to still be
    forwarding events when the disconnect lands.
    """

    def __init__(self, *, since: str | None = None) -> None:
        super().__init__(since=since)
        self.disconnect = asyncio.Event()

    async def receive_text(self) -> str:
        await self.disconnect.wait()
        raise WebSocketDisconnect(code=1006)


class TestTheViewerAndTheSession:
    async def test_a_websocket_that_dies_mid_turn_does_not_stop_the_turn(
        self, isolated_store: Path
    ) -> None:
        """The turn belongs to the session, not to the tab. A browser that
        crashes mid-answer must not cancel work the user is paying for — and
        the tail it missed has to be there when it comes back, which is what
        ``?since=`` is for."""
        cwd = isolated_store / "proj"
        meta = await session_store.create_session(
            provider="scripted", model="m", cwd=str(cwd)
        )
        session_id = str(meta["id"])
        released = asyncio.Event()

        async def run(ctx: RunContext) -> AsyncIterator[Event]:
            yield Event(type="assistant.text", data={"text": "before the drop"})
            await released.wait()
            yield Event(type="assistant.text", data={"text": "after the drop"})
            yield Event(type="assistant.done", data={})

        runner = await runner_registry.get_runner(session_id)
        assert runner is not None
        assert runner.start_turn(
            provider=ScriptedProvider(run),
            provider_name="scripted",
            model="m",
            cwd=str(cwd),
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt="stream it",
        )

        ws = _WebSocketHeldOpen()
        viewer = asyncio.create_task(chat_ws(ws, session_id))  # type: ignore[arg-type]
        await _wait_for(
            lambda: any(
                e.get("data", {}).get("text") == "before the drop" for e in ws.sent
            ),
            "the first chunk to reach the viewer",
        )
        last_seen = max(int(e["_id"]) for e in ws.sent if "_id" in e)

        ws.disconnect.set()
        await asyncio.wait_for(viewer, WAIT_S)

        # The turn carries on with nobody watching.
        released.set()
        task = runner._turn_task
        assert task is not None
        await asyncio.wait_for(task, WAIT_S)

        # The reconnect gets the tail it missed — including the terminal event,
        # without which the restored tab spins forever.
        reconnect = FakeWebSocket(since=str(last_seen))
        await chat_ws(reconnect, session_id)  # type: ignore[arg-type]
        replayed = [
            e["data"].get("text") for e in reconnect.sent if e.get("type") == "assistant.text"
        ]
        print(
            f"\n[inject] viewer dropped after event {last_seen}; the reconnect "
            f"replayed {[t for t in replayed if t]} and "
            f"{len(reconnect.of_type('assistant.done'))} terminal event(s)"
        )
        assert "after the drop" in replayed
        assert "before the drop" not in replayed, "the replay re-sent seen events"
        assert reconnect.of_type("assistant.done"), "the tail lost the terminal event"

        messages, _before, _more = await session_store.list_messages(session_id)
        assistant = [m for m in messages if m["role"] == "assistant"][-1]
        assert assistant["content"][0]["text"] == "before the dropafter the drop"

    async def test_deleting_the_session_mid_turn_ends_it_cleanly(
        self, isolated_store: Path
    ) -> None:
        """``DELETE /api/sessions/{id}`` while a turn is streaming. The turn
        has to end — bounded, so the DELETE cannot hang — without corrupting
        what is on disk and without leaving an exception nobody retrieves,
        which surfaces as "Task exception was never retrieved" at GC and
        nowhere a user would ever look."""
        cwd = isolated_store / "proj"
        meta = await session_store.create_session(
            provider="scripted", model="m", cwd=str(cwd)
        )
        session_id = str(meta["id"])
        streaming = asyncio.Event()

        async def run(ctx: RunContext) -> AsyncIterator[Event]:
            yield Event(
                type="assistant.tool_use",
                data={"id": "t1", "name": "Edit", "input": {"path": "a.py"}},
            )
            streaming.set()
            await asyncio.Event().wait()
            yield Event(type="assistant.done", data={})  # pragma: no cover

        runner = await runner_registry.get_runner(session_id)
        assert runner is not None
        events: list[dict[str, Any]] = []
        runner._bus._on_event = events.append  # type: ignore[assignment]
        assert runner.start_turn(
            provider=ScriptedProvider(run),
            provider_name="scripted",
            model="m",
            cwd=str(cwd),
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt="edit it",
        )
        await asyncio.wait_for(streaming.wait(), WAIT_S)

        assert await session_store.delete_session(session_id)
        await asyncio.wait_for(runner_registry.drop_runner(session_id), WAIT_S)

        task = runner._turn_task
        assert task is not None and task.done()
        assert task.cancelled(), "the cancelled turn left an exception to retrieve"

        types = [e.get("type") for e in events]
        print(f"\n[inject] session deleted mid-turn: events were {types}")
        assert types.count("assistant.done") == 1, types
        assert "error" in types
        assert runner_registry._runners == {}
        assert runner_registry._detached_turns == set()
        # The session is gone, and asking for it is an empty answer rather than
        # a traceback out of the store.
        assert await session_store.get_session(session_id) is None
        assert await session_store.list_messages(session_id) == ([], None, False)
