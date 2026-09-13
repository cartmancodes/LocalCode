"""The Claude provider holds one live ``ClaudeSDKClient`` per session.

Every test here is about something that only breaks *on the second turn*,
which is exactly what the one-shot ``query()`` this replaced could never get
wrong:

  * the client is reused (the latency and prompt-cache win), and rebuilt — not
    mutated — when what it was built with changes;
  * **turn 2's approval card lands on turn 2's sink.** The client is built once
    with one ``can_use_tool``, but the sink and the approval queue are created
    fresh per turn. A callback that captured turn 1's pair would publish the
    card where nobody is listening and then wait for an answer on a dead queue:
    every approval from turn 2 on would silently time out into a denial. This
    is the failure ``_TurnBinding`` exists to prevent;
  * a cancelled turn ``interrupt()``s the CLI instead of leaving it working on
    a turn nobody reads, **and then drops the client**: a turn that never
    reached its ``ResultMessage`` leaves an unread tail on the connection, and
    reusing it hands turn 1's leftovers to turn 2;
  * nothing slow (a CLI spawn, a disconnect) happens under the provider-wide
    dict lock, so one stalled connect cannot wedge every other session plus
    ``DELETE`` and shutdown;
  * the client is released — on ``close_session``, on ``aclose``, by LRU
    eviction past the cap, and at the end of an anonymous (headless) run.

No test needs the `claude` CLI: the provider's factory seam takes
``FakeClientFactory`` (``backend/tests/fakes/claude_client.py``).
"""
from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

from backend.app.orchestrator import claude as claude_mod
from backend.app.orchestrator.base import Event, RunContext
from backend.tests.fakes.claude_client import (
    FakeClaudeClient,
    FakeClientFactory,
    ending_without_result,
    hanging,
    result_message,
    text_message,
)

# Long enough not to fail on a loaded machine, short enough that a genuinely
# stuck gate fails the test instead of hanging the suite.
WAIT_S = 5.0


def provider_with(factory: FakeClientFactory) -> claude_mod.ClaudeProvider:
    provider = claude_mod.ClaudeProvider()
    provider._factory = factory
    return provider


async def drain(provider: claude_mod.ClaudeProvider, ctx: RunContext) -> list[Event]:
    return [ev async for ev in provider.run(ctx)]


def a_turn(tmp_path: Path, **overrides: Any) -> RunContext:
    base: dict[str, Any] = dict(
        model="m",
        prompt="hello",
        cwd=str(tmp_path),
        session_id="s1",
    )
    base.update(overrides)
    return RunContext(**base)


class TestReuse:
    async def test_two_identical_turns_share_one_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        factory = FakeClientFactory()
        provider = provider_with(factory)

        first = await drain(provider, a_turn(tmp_path))
        second = await drain(provider, a_turn(tmp_path))

        assert [ev.type for ev in first] == ["assistant.done"]
        assert [ev.type for ev in second] == ["assistant.done"]
        # One client, connected once, asked twice — the whole point: no second
        # CLI start-up and no cold prompt cache on turn 2.
        assert len(factory.clients) == 1
        client = factory.clients[0]
        assert client.calls.count("connect") == 1
        assert client.turns == 2
        assert client.prompts == ["hello", "hello"]
        assert not client.disconnected

    async def test_a_captured_upstream_session_id_does_not_rebuild(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """``resume`` is build-time only, so it is NOT in the signature.

        The runner learns the upstream session id from turn 1's result and
        passes it on turn 2. If that counted as a change, every session would
        rebuild its client on turn 2 and the cache win would never land.
        """
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        await drain(provider, a_turn(tmp_path, upstream_session_id="upstream-1"))

        assert len(factory.clients) == 1
        assert factory.clients[0].turns == 2
        # The first build had nothing to resume — the conversation now lives in
        # the process we are still talking to.
        assert factory.clients[0].options.resume is None

    async def test_a_changed_model_closes_the_old_client_and_builds_a_new_one(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path, model="m1"))
        await drain(provider, a_turn(tmp_path, model="m2"))

        assert len(factory.clients) == 2
        # Rebuilt, never mutated: a setter on the live client is what silently
        # invalidates the prompt cache reuse exists for.
        assert factory.clients[0].disconnected
        assert factory.clients[0].options.model == "m1"
        assert factory.clients[1].options.model == "m2"
        assert not factory.clients[1].disconnected

    async def test_a_changed_tool_set_rebuilds(self, tmp_path: Path, fresh_settings) -> None:
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        await drain(
            provider,
            a_turn(tmp_path, extras={"claude_disallowed_tools": ["WebFetch"]}),
        )

        # Reusing here would run the second turn under the first turn's
        # permissions.
        assert len(factory.clients) == 2
        assert factory.clients[0].disconnected

    async def test_changed_setting_sources_rebuild(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """``setting_sources`` / ``skills`` are part of the signature too.

        ``claude_disable_settings`` varies them without touching
        ``allowed_tools``, and a reused client would keep serving the previous
        turn's settings sources — a permissions difference the user cannot see.
        """
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        await drain(provider, a_turn(tmp_path, extras={"claude_disable_settings": True}))

        assert len(factory.clients) == 2
        assert factory.clients[0].disconnected
        assert factory.clients[0].options.setting_sources is None
        assert factory.clients[1].options.setting_sources == []

    async def test_different_sessions_get_different_clients(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path, session_id="s1"))
        await drain(provider, a_turn(tmp_path, session_id="s2"))

        assert len(factory.clients) == 2
        assert not any(c.disconnected for c in factory.clients)


def _asks_to_write(tmp_path: Path) -> Any:
    """A turn that calls the permission callback once, then finishes."""

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        result = await client.options.can_use_tool(
            "Write",
            {"file_path": str(tmp_path / "a.py"), "content": "x"},
            ToolPermissionContext(),
        )
        assert isinstance(result, PermissionResultAllow), "the card was not answered"
        yield result_message()

    return behaviour


class TestTheCallbackIsReboundEachTurn:
    async def test_turn_twos_card_lands_on_turn_twos_sink(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """THE hazard of a persistent client.

        The sink and the approval queue are per-turn objects; the client and
        its callback are not. Turn 2 here uses a *different* approval queue,
        and the card has to come out of turn 2's own event stream and be
        answerable on turn 2's queue — on one client, built once.
        """
        factory = FakeClientFactory(_asks_to_write(tmp_path))
        provider = provider_with(factory)

        for turn in (1, 2):
            channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            agen = provider.run(a_turn(tmp_path, approval_channel=channel)).__aiter__()
            # wait_for, not a bare await: a callback still bound to turn 1's
            # sink never publishes here, and the failure we want is a failed
            # test rather than a hung suite.
            card = await asyncio.wait_for(agen.__anext__(), WAIT_S)
            assert card.type == "pipeline.awaiting_approval", f"turn {turn}"
            assert card.data["tool"] == "Write"

            await channel.put({"id": card.data["id"], "value": "yes"})
            rest = [ev.type async for ev in agen]
            assert sorted(rest) == ["assistant.done", "pipeline.approval_received"], (
                f"turn {turn}"
            )

        # One client served both turns — so the second card proves the callback
        # was rebound, not that a fresh client brought a fresh closure.
        assert len(factory.clients) == 1
        assert factory.clients[0].turns == 2

    async def test_a_turn_with_no_channel_still_denies_rather_than_hanging(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """A rebound sink must not leave a *stale* channel behind either.

        Turn 1 attaches an operator; turn 2 is headless (a reconnect with no
        approval channel). If the callback still held turn 1's queue it would
        wait the full approval timeout for an answer nobody can send.
        """
        factory = FakeClientFactory(_asks_to_write(tmp_path))
        provider = provider_with(factory)

        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        agen = provider.run(a_turn(tmp_path, approval_channel=channel)).__aiter__()
        card = await asyncio.wait_for(agen.__anext__(), WAIT_S)
        await channel.put({"id": card.data["id"], "value": "yes"})
        assert [ev.type async for ev in agen]

        # Headless: the gate denies (the fake asserts it was allowed), so the
        # turn surfaces an error instead of waiting on turn 1's queue.
        events = await asyncio.wait_for(
            drain(provider, a_turn(tmp_path, approval_channel=None)), WAIT_S
        )
        assert [ev.type for ev in events] == ["error"]
        assert len(factory.clients) == 1


class _NeverAcksInterrupt(FakeClaudeClient):
    """A CLI that takes the interrupt and never answers it.

    Faithful to the SDK's shape: ``interrupt()`` is a control request, and the
    SDK waits for the CLI's ack up to its own 60 s timeout. The turn holds the
    per-session handle lock for every second of that.
    """

    async def interrupt(self) -> None:
        self.calls.append("interrupt")
        await asyncio.Event().wait()


class _NeverAcksFactory(FakeClientFactory):
    def __call__(self, options: Any = None, **_kwargs: Any) -> FakeClaudeClient:
        client = _NeverAcksInterrupt(options=options, behaviour=self.behaviour)
        self.clients.append(client)
        return client


class TestCancellation:
    async def test_a_cancelled_turn_interrupts_the_client_and_propagates(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        started = asyncio.Event()
        factory = FakeClientFactory(hanging(started))
        provider = provider_with(factory)

        agen = provider.run(a_turn(tmp_path)).__aiter__()
        pulling = asyncio.create_task(agen.__anext__())
        await asyncio.wait_for(started.wait(), WAIT_S)

        pulling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulling

        client = factory.clients[0]
        # The CLI outlives the turn now, so it has to be told to stop; without
        # this it keeps working (and billing) on a turn nobody is reading.
        assert "interrupt" in client.calls
        # And then it goes: the turn never reached its ResultMessage, so its
        # unread tail would be delivered to the next turn on this connection.
        # See test_turn_ones_leftovers_never_reach_turn_two.
        assert client.disconnected
        assert provider._clients == {}

    async def test_turn_ones_leftovers_never_reach_turn_two(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The desync a kept-after-interrupt client causes.

        Every turn's messages arrive on ONE per-connection stream and
        ``receive_response()`` stops at the ``ResultMessage``. So an interrupted
        turn's unread tail — trailing deltas, plus the result the CLI still
        emits for it — is what the next ``receive_response()`` reads first:
        turn 2 would render turn 1's text and then finish on turn 1's result,
        reporting the wrong outcome while its real answer slid into turn 3.
        """
        released = asyncio.Event()
        turn = 0

        async def script(client: FakeClaudeClient) -> AsyncIterator[Any]:
            nonlocal turn
            turn += 1
            if turn == 1:
                yield text_message("turn-1 partial")
                # Held until the test has cancelled turn 1, so the tail is
                # written to the connection *after* the turn ended — exactly
                # what a CLI that has not noticed the interrupt does.
                await released.wait()
                yield text_message("turn-1 leftover")
                yield result_message("turn-1-result")
            else:
                yield text_message("turn-2 answer")
                yield result_message("turn-2-result")

        factory = FakeClientFactory(script)
        provider = provider_with(factory)

        agen = provider.run(a_turn(tmp_path)).__aiter__()
        first = await asyncio.wait_for(agen.__anext__(), WAIT_S)
        assert first.data["text"] == "turn-1 partial"

        pulling = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0)
        pulling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulling

        released.set()
        for _ in range(10):  # let the turn-1 producer write its tail
            await asyncio.sleep(0)

        events = await asyncio.wait_for(drain(provider, a_turn(tmp_path)), WAIT_S)

        texts = [ev.data["text"] for ev in events if ev.type == "assistant.text"]
        assert texts == ["turn-2 answer"]
        done = [ev for ev in events if ev.type == "assistant.done"]
        assert len(done) == 1
        assert done[0].data["upstream_session_id"] == "turn-2-result"
        # Turn 2 is a fresh connection — that is the only way its stream can be
        # clean.
        assert len(factory.clients) == 2
        assert factory.clients[0].disconnected

    async def test_a_stop_inside_the_tail_check_still_drops_the_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The 50-250 ms window every single turn spends in ``_unread_tail``.

        ``completed`` is set at the ``ResultMessage``; the tail check runs
        after it. A cancel landing in between left the flag True, so the
        ``finally`` skipped the discard and kept a connection whose tail nobody
        had finished reading — the one thing the flag exists to prevent, and
        reachable from the most ordinary gesture in the product: the viewer
        renders ``assistant.done``, the user presses Stop, and the consumer
        walks away while the check is still waiting on the deferred agent.
        """
        turns = {"n": 0}

        async def script(client: FakeClaudeClient) -> AsyncIterator[Any]:
            turns["n"] += 1
            if turns["n"] == 1:
                yield result_message("turn-1-result")
                # The backgrounded agent's output, landing DURING the check.
                await asyncio.sleep(0.02)
                yield text_message("LEAK")
                yield result_message("turn-1-second-result")
            else:
                yield text_message("turn-2 answer")
                yield result_message("turn-2-result")

        factory = FakeClientFactory(script)
        provider = provider_with(factory)

        agen = provider.run(a_turn(tmp_path)).__aiter__()
        while True:
            ev = await asyncio.wait_for(agen.__anext__(), WAIT_S)
            if ev.type == "assistant.done":
                break
        # Stop, delivered where Stop actually lands.
        await asyncio.wait_for(agen.aclose(), WAIT_S)

        assert provider._clients == {}, (
            "a turn cancelled inside the tail check kept its client"
        )
        assert factory.clients[0].disconnected

        second = await asyncio.wait_for(drain(provider, a_turn(tmp_path)), WAIT_S)
        texts = [ev.data.get("text") for ev in second if ev.type == "assistant.text"]
        assert texts == ["turn-2 answer"], "turn 2 read turn 1's deferred tail"
        done = [ev for ev in second if ev.type == "assistant.done"]
        assert len(done) == 1
        assert done[0].data["upstream_session_id"] == "turn-2-result"
        # Turn 2 is a fresh connection: the only way its stream can be clean.
        assert len(factory.clients) == 2

    async def test_an_interrupt_that_never_acks_is_bounded_and_still_discards(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ledger 1183 (Task 4 minor): the interrupt is not allowed to hold the
        handle lock for the SDK's 60 s control timeout.

        That is four times ``SessionRunner._CANCEL_GRACE_S``, so a CLI slow to
        ack turned every cancelled turn into a *detached* one with the next
        turn queued behind the lock. A timeout is treated as a failed
        interrupt: the client goes, exactly as it does when the interrupt
        raises.
        """
        monkeypatch.setattr(claude_mod, "_INTERRUPT_TIMEOUT_S", 0.1)
        started = asyncio.Event()
        factory = _NeverAcksFactory(hanging(started))
        provider = provider_with(factory)

        agen = provider.run(a_turn(tmp_path)).__aiter__()
        pulling = asyncio.create_task(agen.__anext__())
        await asyncio.wait_for(started.wait(), WAIT_S)

        pulling.cancel()
        began = time.monotonic()
        _done, pending = await asyncio.wait({pulling}, timeout=WAIT_S)
        elapsed = time.monotonic() - began
        assert not pending, "the unacked interrupt held the turn open"
        with pytest.raises(asyncio.CancelledError):
            await pulling

        client = factory.clients[0]
        assert "interrupt" in client.calls
        # Bounded, and well inside the runner's 5 s cancellation grace window.
        assert elapsed < 1.0, f"the interrupt took {elapsed:.2f}s"
        # And a CLI we stopped waiting for is never handed to the next turn.
        assert client.disconnected
        assert provider._clients == {}

    async def test_a_turn_queued_behind_a_stopped_one_gets_a_live_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The other half of "a Stop drops the client".

        The handle is fetched from the provider's dict and only THEN waited on,
        so a turn queued behind a Stopped one is holding a reference to the
        very handle that Stop is about to discard — the discard happens from
        inside the lock it is waiting for. Handing it over unchecked means turn
        2 writes to a transport turn 1 disconnected, and fails for a reason
        that is entirely turn 1's.
        """
        started = asyncio.Event()
        turns = {"n": 0}

        async def script(client: FakeClaudeClient) -> AsyncIterator[Any]:
            turns["n"] += 1
            if turns["n"] == 1:
                started.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover - cancelled before it yields
            else:
                yield text_message("turn-2 answer")
                yield result_message("turn-2-result")

        factory = FakeClientFactory(script)
        provider = provider_with(factory)

        agen = provider.run(a_turn(tmp_path)).__aiter__()
        pulling = asyncio.create_task(agen.__anext__())
        await asyncio.wait_for(started.wait(), WAIT_S)
        handle = provider._clients["s1"]

        # Turn 2 arrives while turn 1 still holds the handle's lock. The
        # private `_waiters` read is the only way to prove it is actually
        # QUEUED — a test that merely slept could pass by starting turn 2 after
        # the discard, which is the case that was never broken.
        queued = asyncio.create_task(drain(provider, a_turn(tmp_path)))
        for _ in range(int(WAIT_S / 0.005)):
            await asyncio.sleep(0.005)
            if handle.lock.locked() and getattr(handle.lock, "_waiters", None):
                break
        assert getattr(handle.lock, "_waiters", None), "turn 2 never reached the lock"

        pulling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pulling

        events = await asyncio.wait_for(queued, WAIT_S)

        assert [ev.type for ev in events] == ["assistant.text", "assistant.done"], (
            f"the queued turn inherited turn 1's discarded client: {events}"
        )
        assert events[-1].data["upstream_session_id"] == "turn-2-result"
        assert factory.clients[0].disconnected
        assert len(factory.clients) == 2


class TestTheTurnLock:
    async def test_a_turn_abandoned_by_its_consumer_does_not_wedge_the_session(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """``run()`` holds the handle's lock across its yields, so a consumer
        that walks away mid-stream must not leave it held forever — the next
        turn on that session would block on it with nothing to wait for.

        It does not, because the lock is released by the generator's ``finally``
        and asyncio finalizes an abandoned async generator (``aclose``) as soon
        as the consumer drops it.
        """

        async def two_step(client: FakeClaudeClient) -> AsyncIterator[Any]:
            yield text_message("partial")
            yield result_message()

        factory = FakeClientFactory(two_step)
        provider = provider_with(factory)

        with pytest.raises(RuntimeError):
            async for _ev in provider.run(a_turn(tmp_path)):
                raise RuntimeError("the consumer blew up mid-stream")

        gc.collect()
        events = await asyncio.wait_for(drain(provider, a_turn(tmp_path)), WAIT_S)

        assert events and events[-1].type == "assistant.done"


class TestAStreamThatEndsWithoutAResult:
    async def test_a_cli_that_exits_mid_turn_leaves_no_reusable_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """EOF is not completion.

        ``receive_response()`` returns both on the ``ResultMessage`` *and* at
        stream EOF — the SDK's reader queues its `end` sentinel from a
        ``finally``, so a `claude` that simply exits (crash, OOM kill, closed
        transport) ends the loop with no result and no error. Reading that as
        "the turn completed" would keep a client whose CLI is gone and hand it
        to the next turn, which would then write to a closed transport.
        """
        factory = FakeClientFactory(ending_without_result("partial"))
        provider = provider_with(factory)

        events = await drain(provider, a_turn(tmp_path))

        # What the provider saw: output, then the stream simply stopped.
        assert [ev.type for ev in events] == ["assistant.text"]
        assert provider._clients == {}
        assert factory.clients[0].disconnected

        # So the next turn connects a fresh CLI instead of talking to a corpse.
        await drain(provider, a_turn(tmp_path))
        assert len(factory.clients) == 2


class BreakAfterTheTurnFactory:
    """Builds clients whose stream fails only AFTER the turn has drained.

    The turn itself must succeed — otherwise the test proves nothing about the
    check, only about the error path that already has its own cases — so the
    failure is armed by the turn's own ``receive_response`` finishing, which is
    exactly when the drain check runs.
    """

    class Client(FakeClaudeClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.turn_drained = False

        async def receive_response(self) -> AsyncIterator[Any]:
            if self.turn_drained:
                raise RuntimeError("the transport went away")
            async for message in super().receive_response():
                yield message
            self.turn_drained = True

    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any = None, **_kwargs: Any) -> FakeClaudeClient:
        client = self.Client(options=options, behaviour=self.behaviour)
        self.clients.append(client)
        return client


class TestADeferredTailOnTheConnection:
    """A result is not always the run's last word.

    ``claude-agent-sdk``'s ``DEFERRING_TASK_TYPES`` (``_internal/query.py``):
    when the CLI backgrounds delegated agent work, a ``ResultMessage`` can
    arrive with that work still in flight, and the follow-up frames plus a
    SECOND result land afterwards. ``receive_response()`` stops at the first
    result, so those frames stay buffered on the one per-connection stream —
    and the provider used to treat "reached the result" as "the connection is
    drained".

    What that cost, before this: turn 2 opened by reading turn 1's background
    output, presented it as its own answer, and terminated on turn 1's second
    result — so turn 2's real answer slid into turn 3. Silent misattribution,
    with no error anywhere. The SDK cannot close the gap itself (its own
    comment: it "needs a run-boundary signal from the CLI"), but the provider
    can refuse to reuse a connection it can SEE is not drained, which is the
    same ``_discard`` the interrupt path already uses.
    """

    @staticmethod
    def _deferred_tail() -> Any:
        """Turn 1 backgrounds an agent; its output and a second result arrive
        after turn 1's own result. Every later turn is ordinary."""
        turns = {"n": 0}

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            turns["n"] += 1
            n = turns["n"]
            yield text_message(f"answer to turn {n}")
            yield result_message()
            if n == 1:
                yield text_message("STALE deferred agent output")
                yield result_message()

        return behaviour

    async def test_the_next_turn_gets_its_own_answer_not_the_deferred_tail(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        factory = FakeClientFactory(self._deferred_tail())
        provider = provider_with(factory)

        first = await drain(provider, a_turn(tmp_path))
        assert [ev.data.get("text") for ev in first if ev.type == "assistant.text"] == [
            "answer to turn 1"
        ]

        second = await drain(provider, a_turn(tmp_path))
        texts = [ev.data.get("text") for ev in second if ev.type == "assistant.text"]

        assert "STALE deferred agent output" not in texts, (
            "turn 2 surfaced turn 1's backgrounded output as its own answer"
        )
        assert texts == ["answer to turn 2"]
        # …and it is one terminal event, on the turn it belongs to.
        assert [ev.type for ev in second].count("assistant.done") == 1

    async def test_a_connection_with_an_unread_tail_is_not_reused(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The mechanism, stated directly: the client that left a tail is
        dropped, so turn 2 connects a fresh CLI (``resume`` carries the
        conversation) rather than reading leftovers."""
        factory = FakeClientFactory(self._deferred_tail())
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        assert provider._clients == {}, "the desynced client was kept for reuse"
        assert factory.clients[0].disconnected

        await drain(provider, a_turn(tmp_path))
        assert len(factory.clients) == 2

    async def test_an_ordinary_turn_still_keeps_its_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The other direction, and the one that pays for the whole design: a
        turn that drained cleanly must NOT be charged a reconnect. A check that
        discarded on every turn would be indistinguishable from one that
        works, except in the prompt-cache bill."""
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        await drain(provider, a_turn(tmp_path))

        assert len(factory.clients) == 1
        assert factory.clients[0].turns == 2

    @staticmethod
    def _trickle(frames: int) -> Any:
        """A turn whose result is followed by ``frames`` lifecycle frames,
        10 ms apart — the shape a backgrounded agent actually produces."""
        from claude_agent_sdk import SystemMessage

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            yield text_message("the answer")
            yield result_message()
            for n in range(frames):
                await asyncio.sleep(0.01)
                yield SystemMessage(subtype="task_updated", data={"n": n})

        return behaviour

    async def test_a_trickle_that_never_ends_reconnects_instead_of_guessing(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """Two failures at once, and the second is the subtle one.

        **It must end.** The check walks past benign frames, so a per-frame
        window is not a bound: frames arriving faster than it restart it
        forever, the drain pump never returns, nothing seals the merged queue,
        and the turn never completes — with no timeout anywhere above it in
        ``turn.py``. A regression here HANGS, which is why the bound below is an
        ``asyncio.wait_for`` and not only an assertion.

        **And what it must conclude on expiry is "unknown", not "clean".** The
        deadline is sized for exactly the case where a deferred task is still
        forwarding lifecycle frames — which is the case where its real content
        (a second ``ResultMessage``, the agent's own text) may still be behind
        the trickle when the budget runs out. "Everything I looked at was
        harmless" is not "there is nothing left", and reading it that way would
        reintroduce a bounded version of the misattribution this check exists to
        prevent. So the client is dropped: a reconnect in the pathological case
        is the right price.
        """
        factory = FakeClientFactory(self._trickle(200))
        provider = provider_with(factory)

        started = time.monotonic()
        try:
            events = await asyncio.wait_for(
                drain(provider, a_turn(tmp_path)), WAIT_S
            )
            elapsed = time.monotonic() - started
            # The turn itself is untouched — it had already reached its result.
            assert [ev.type for ev in events] == ["assistant.text", "assistant.done"]
            # The check's ceiling plus room for the turn itself on a loaded
            # machine. The failure this guards is unbounded, so the margin does
            # not have to be tight to be meaningful.
            assert elapsed < 2.0, (
                f"the turn took {elapsed:.2f}s — the tail check is walking "
                "benign frames without a deadline"
            )
            assert provider._clients == {}, (
                "the check ran out of time and kept the client anyway — the "
                "frames that decide reuse were never reached"
            )
        finally:
            await provider.aclose()
        assert factory.clients[0].disconnected

    async def test_a_trickle_that_ends_in_time_still_keeps_the_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The other side of the same boundary, and the reason the deadline is
        a reconnect threshold rather than a hair trigger.

        A short burst of post-turn lifecycle frames is ordinary, and the check
        is supposed to sit through it: it reaches the end of the trickle, finds
        the connection quiet, and keeps the client. Without this case, "discard
        on expiry" could be satisfied by discarding always — which is the same
        bill as never having held a client across turns at all.

        Three frames, 10 ms apart, then silence: ~30 ms of trickle plus one
        50 ms window to confirm the quiet, against a 250 ms budget. The margin
        is deliberately ~3x, because a loaded machine stretches those sleeps
        (one measured run showed 21 ms of scheduling latency where 10 was
        asked for).
        """
        factory = FakeClientFactory(self._trickle(3))
        provider = provider_with(factory)

        try:
            first = await asyncio.wait_for(drain(provider, a_turn(tmp_path)), WAIT_S)
            assert [ev.type for ev in first] == ["assistant.text", "assistant.done"]
            assert list(provider._clients), (
                "a burst of lifecycle frames that ENDED cost a reconnect"
            )
            second = await asyncio.wait_for(drain(provider, a_turn(tmp_path)), WAIT_S)
        finally:
            await provider.aclose()

        assert len(factory.clients) == 1, "the second turn built a new client"
        assert [ev.data.get("text") for ev in second if ev.type == "assistant.text"] == [
            "the answer"
        ]

    async def test_a_trailing_rate_limit_event_is_kept_and_still_reported(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """A ``RateLimitEvent`` after the result is benign for REUSE and
        precious as DATA, and those two facts pull in opposite directions.

        The CLI emits one only when the rate-limit status TRANSITIONS, so it is
        the single measurement of the user's remaining plan headroom anyone
        gets until the next transition — dropping it on the floor (which
        "anything non-system means discard" would do, along with the client)
        leaves the meter stale with nothing to say why. It is therefore walked
        past for the reuse decision AND translated through the normal path.
        """
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo

        turns = {"n": 0}

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            turns["n"] += 1
            yield text_message(f"answer {turns['n']}")
            yield result_message()
            if turns["n"] == 1:
                yield RateLimitEvent(
                    rate_limit_info=RateLimitInfo(
                        status="allowed_warning",
                        resets_at=1_700_000_000,
                        rate_limit_type="seven_day_opus",
                        utilization=0.85,
                    ),
                    uuid="u1",
                    session_id="upstream-1",
                )

        factory = FakeClientFactory(behaviour)
        provider = provider_with(factory)

        first = await drain(provider, a_turn(tmp_path))
        second = await drain(provider, a_turn(tmp_path))
        await provider.aclose()

        quota_events = [ev for ev in first if ev.type == "quota.limit"]
        assert len(quota_events) == 1, [ev.type for ev in first]
        assert quota_events[0].data["utilization"] == 0.85
        assert quota_events[0].data["status"] == "allowed_warning"
        # Reported, and not paid for with a reconnect.
        assert len(factory.clients) == 1, "a quota measurement cost a reconnect"
        assert [ev.data.get("text") for ev in second if ev.type == "assistant.text"] == [
            "answer 2"
        ]

    async def test_a_check_that_cannot_answer_drops_the_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The check asks "is this connection in a state I understand?", and an
        exception is the answer "no".

        Reading it as "drained" — which swallowing the failure did — keeps a
        connection that has just failed, and the next turn is the one that
        discovers it. A reconnect is cheap; a turn lost to a corpse is not.
        """

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            yield text_message("the answer")
            yield result_message()

        factory = BreakAfterTheTurnFactory(behaviour)
        provider = provider_with(factory)  # type: ignore[arg-type]

        events = await drain(provider, a_turn(tmp_path))
        assert [ev.type for ev in events] == ["assistant.text", "assistant.done"], (
            "the failed check cost the turn its own result"
        )
        assert provider._clients == {}, "a client whose state is unknown was kept"

        await drain(provider, a_turn(tmp_path))
        await provider.aclose()
        assert len(factory.clients) == 2

    async def test_a_post_turn_system_frame_does_not_cost_a_reconnect(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """The SDK may forward a ``session_state_changed`` frame after the
        result on a perfectly healthy connection, and ``_translate`` drops it.
        Treating that as a desync would rebuild the client — and go to the
        model with a cold prompt cache — on every such turn."""
        from claude_agent_sdk import SystemMessage

        turns = {"n": 0}

        async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
            turns["n"] += 1
            yield text_message(f"answer {turns['n']}")
            yield result_message()
            if turns["n"] == 1:
                yield SystemMessage(subtype="session_state_changed", data={})

        factory = FakeClientFactory(behaviour)
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        second = await drain(provider, a_turn(tmp_path))

        assert len(factory.clients) == 1, "a harmless system frame forced a rebuild"
        assert [ev.data.get("text") for ev in second if ev.type == "assistant.text"] == [
            "answer 2"
        ]


class TestRelease:
    async def test_close_session_disconnects_and_the_next_turn_builds_fresh(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path))
        await provider.close_session("s1")

        assert factory.clients[0].disconnected
        assert provider._clients == {}

        await drain(provider, a_turn(tmp_path))
        assert len(factory.clients) == 2
        assert factory.clients[1].turns == 1

    async def test_close_session_for_an_unknown_id_is_a_no_op(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # The delete route calls this for every provider-owned session,
        # including ones that never ran a turn.
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await provider.close_session("never-ran")

        # Nothing invented and nothing touched: no handle registered for the
        # unknown id, and no client built to close.
        assert provider._clients == {}
        assert factory.clients == []

    async def test_aclose_disconnects_everything(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        factory = FakeClientFactory()
        provider = provider_with(factory)

        await drain(provider, a_turn(tmp_path, session_id="s1"))
        await drain(provider, a_turn(tmp_path, session_id="s2"))
        await provider.aclose()

        assert all(c.disconnected for c in factory.clients)
        assert provider._clients == {}

    async def test_an_anonymous_run_leaves_no_client_behind(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """Fleet steps run this provider in a worker process with
        session_id=None. Without the anonymous key being closed at the end of
        the run, every step would leak a live CLI."""
        factory = FakeClientFactory()
        provider = provider_with(factory)

        events = await drain(provider, a_turn(tmp_path, session_id=None))

        assert [ev.type for ev in events] == ["assistant.done"]
        assert provider._clients == {}
        assert factory.clients[0].disconnected

    async def test_the_cap_evicts_the_least_recently_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings
    ) -> None:
        monkeypatch.setenv("CLAUDE_MAX_LIVE_CLIENTS", "2")
        factory = FakeClientFactory()
        provider = provider_with(factory)

        for session_id in ("s1", "s2", "s3"):
            await drain(provider, a_turn(tmp_path, session_id=session_id))

        assert sorted(provider._clients) == ["s2", "s3"]
        # s1 is the oldest, so its CLI is the one reaped — the session is not
        # lost, its next turn reconnects.
        assert factory.clients[0].disconnected
        assert not factory.clients[1].disconnected
        assert not factory.clients[2].disconnected


class TestTheProviderLockIsNotHeldAcrossIo:
    async def test_a_stalled_connect_does_not_block_another_sessions_teardown(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        """``connect()`` spawns a CLI; ``disconnect()`` reaps one.

        Holding the provider-wide dict lock across either would queue every
        other session's turn — and ``DELETE /api/sessions/{id}`` and shutdown —
        behind one spawn, which is exactly what ``session_runner/registry.py``
        promises cannot happen.
        """
        factory = FakeClientFactory()
        provider = provider_with(factory)
        await drain(provider, a_turn(tmp_path, session_id="s1"))

        # The next client parks inside connect() until the gate opens.
        factory.connect_gate = asyncio.Event()
        stalled = asyncio.create_task(drain(provider, a_turn(tmp_path, session_id="s2")))
        for _ in range(100):
            if len(factory.clients) == 2:
                break
            await asyncio.sleep(0)
        assert len(factory.clients) == 2, "the second client never started connecting"

        # The delete path for a DIFFERENT session must not wait on that spawn.
        await asyncio.wait_for(provider.close_session("s1"), WAIT_S)
        assert factory.clients[0].disconnected

        factory.connect_gate.set()
        await asyncio.wait_for(stalled, WAIT_S)
        assert provider._clients.get("s2") is not None

    async def test_a_failed_connect_disconnects_the_half_built_client(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # connect() may have spawned the CLI before failing; nothing else holds
        # the client, so skipping this orphans the process.
        factory = FakeClientFactory(fail_connect="no such binary")
        provider = provider_with(factory)

        events = await drain(provider, a_turn(tmp_path))

        assert [ev.type for ev in events] == ["error"]
        assert "no such binary" in events[0].data["message"]
        assert factory.clients[0].disconnected
        assert provider._clients == {}
        # And the per-key builder is released, so a retry is not wedged.
        assert provider._builders == {}
        assert [ev.type for ev in await drain(provider, a_turn(tmp_path))] == ["error"]


class _RecordingProvider:
    """Stands in for whichever provider owns the session being deleted."""

    name = "claude"

    def __init__(self, explode: bool = False) -> None:
        self.closed: list[str] = []
        self.explode = explode

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)
        if self.explode:
            raise RuntimeError("teardown exploded")


class TestTheDeleteRoutesReleaseTheProvider:
    """Dropping the runner cancels the turn but tells the provider nothing.

    Without these calls the deleted session's client — and its `claude`
    subprocess — stays alive for the life of the backend.
    """

    @pytest.fixture
    def recorder(self, monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
        from backend.app.routes import sessions as routes

        rec = _RecordingProvider()

        async def fake_get_provider(name: str) -> Any:
            return rec

        monkeypatch.setattr(routes, "get_provider", fake_get_provider)
        return rec

    async def test_deleting_one_session_closes_its_provider_session(
        self, isolated_store: Path, recorder: _RecordingProvider
    ) -> None:
        from backend.app.routes import sessions as routes
        from backend.app.storage.sessions import store as session_store

        meta = await session_store.create_session(
            provider="claude", model="m", cwd=str(isolated_store / "proj")
        )

        await routes.delete_session(meta["id"])

        assert recorder.closed == [meta["id"]]
        assert await session_store.get_session(meta["id"]) is None

    async def test_a_failing_teardown_does_not_fail_the_delete(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.app.routes import sessions as routes
        from backend.app.storage.sessions import store as session_store

        rec = _RecordingProvider(explode=True)

        async def fake_get_provider(name: str) -> Any:
            return rec

        monkeypatch.setattr(routes, "get_provider", fake_get_provider)
        meta = await session_store.create_session(
            provider="claude", model="m", cwd=str(isolated_store / "proj")
        )

        # The directory is going away either way; a teardown error must not
        # turn a successful delete into a 500.
        await routes.delete_session(meta["id"])

        assert rec.closed == [meta["id"]]
        assert await session_store.get_session(meta["id"]) is None

    async def test_wiping_everything_closes_every_session(
        self, isolated_store: Path, recorder: _RecordingProvider
    ) -> None:
        from backend.app.routes import sessions as routes
        from backend.app.storage.sessions import store as session_store

        ids = [
            (
                await session_store.create_session(
                    provider="claude", model="m", cwd=str(isolated_store / "proj")
                )
            )["id"]
            for _ in range(2)
        ]

        await routes.delete_all_sessions()

        assert sorted(recorder.closed) == sorted(ids)
        assert await session_store.list_sessions() == []
