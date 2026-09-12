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
