"""A stand-in for ``claude_agent_sdk.ClaudeSDKClient``.

``ClaudeProvider`` now holds a live client per session, so its tests have to be
able to ask "was this the *same* client?" — which a monkeypatched module-level
``query`` function could not answer. The provider exposes a factory seam
(``_client_factory`` / ``self._factory``) and this is what the suite injects
through it: no `claude` CLI on PATH, no network, and every lifecycle call
(``connect`` / ``query`` / ``receive_response`` / ``interrupt`` /
``disconnect``) recorded in order on the instance.

**One buffer per connection, not per turn.** This is the detail an earlier
version of this fake got wrong, and getting it wrong hid a real desync bug: the
SDK creates a single message stream when the client connects
(``_internal/query.py``) and ``receive_response()`` stops as soon as it yields a
``ResultMessage`` (``client.py``). So anything a turn leaves unread — trailing
deltas, or the result the CLI still emits for an interrupted turn — is what the
*next* ``receive_response()`` on that client reads first. The fake reproduces
that: ``connect()`` makes the buffer, ``query()`` starts a producer writing into
it, and ``receive_response()`` multiplexes over it. A fake that handed each turn
its own generator can never fail the way production does.

A *behaviour* — an async generator taking the client — stands in for what the
CLI does during a turn: yield scripted messages, invoke the permission callback
mid-stream, raise, or hang so the caller can cancel it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import ResultMessage, StreamEvent

# An async generator function: behaviour(client) -> messages for one turn.
Behaviour = Callable[["FakeClaudeClient"], AsyncIterator[Any]]


def result_message(session_id: str = "upstream-1") -> ResultMessage:
    """The terminal message of a turn — what ``receive_response`` stops on."""
    return ResultMessage(
        subtype="success",
        duration_ms=12,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.25,
        result="done",
    )


def text_message(text: str, session_id: str = "upstream-1") -> StreamEvent:
    """A mid-stream token delta — what the UI streams live."""
    return StreamEvent(
        uuid="ev-1",
        session_id=session_id,
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    )


async def one_result(client: FakeClaudeClient) -> AsyncIterator[Any]:
    """The default turn: nothing but a successful result."""
    yield result_message()


class _Failure:
    """A producer's exception, carried down the connection buffer so
    ``receive_response()`` raises it where the real client would."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _EndOfStream:
    """The connection's EOF — the CLI exited.

    The SDK's reader queues its ``{"type": "end"}`` sentinel from a ``finally``,
    so a clean CLI exit ends ``receive_response()`` with neither a
    ``ResultMessage`` nor an error. A fake that can only end a stream *with* a
    result cannot express that, and it is the case in which treating "the loop
    ended" as "the turn completed" marks a dead client reusable. Once seen, the
    stream stays ended for every later call, as a closed transport does.
    """


class FakeClaudeClient:
    """Records its lifecycle calls and replays scripted turns."""

    def __init__(
        self,
        options: Any = None,
        behaviour: Behaviour | None = None,
        *,
        connect_gate: asyncio.Event | None = None,
        fail_connect: str | None = None,
    ) -> None:
        self.options = options
        self.behaviour: Behaviour = behaviour or one_result
        # Every call in order. Reuse tests assert on this, so "connect" must
        # appear exactly once per client however many turns it serves.
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self.connected = False
        # Lets a test park a connect() mid-spawn and prove nothing else is
        # queued behind it.
        self._connect_gate = connect_gate
        self._fail_connect = fail_connect
        # The per-connection buffer. None until connect().
        self._stream: asyncio.Queue[Any] | None = None
        self._producers: list[asyncio.Task[None]] = []
        self._ended = False

    # ── the surface ClaudeProvider uses ──────────────────────────────────
    async def connect(self, prompt: Any = None) -> None:
        self.calls.append("connect")
        if self._connect_gate is not None:
            await self._connect_gate.wait()
        if self._fail_connect is not None:
            raise RuntimeError(self._fail_connect)
        self.connected = True
        self._stream = asyncio.Queue()

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self.calls.append("query")
        self.prompts.append(prompt if isinstance(prompt, str) else repr(prompt))
        assert self._stream is not None, "query() before connect()"
        self._producers.append(asyncio.create_task(self._produce(self._stream)))

    async def _produce(self, stream: asyncio.Queue[Any]) -> None:
        """Write this turn's messages onto the connection buffer.

        Deliberately outlives ``receive_response()``: a CLI that has not yet
        noticed an interrupt keeps emitting, and where those messages land is
        the whole point of this fake.
        """
        saw_result = False
        try:
            async for message in self.behaviour(self):
                saw_result = saw_result or isinstance(message, ResultMessage)
                await stream.put(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - carried to the consumer
            await stream.put(_Failure(exc))
        else:
            if not saw_result:
                # The behaviour ran out without a result: the CLI exited.
                await stream.put(_EndOfStream())

    async def receive_response(self) -> AsyncIterator[Any]:
        self.calls.append("receive_response")
        assert self._stream is not None, "receive_response() before connect()"
        stream = self._stream
        while True:
            if self._ended:
                return
            item = await stream.get()
            if isinstance(item, _Failure):
                raise item.exc
            if isinstance(item, _EndOfStream):
                self._ended = True
                return
            yield item
            if isinstance(item, ResultMessage):
                return

    async def interrupt(self) -> None:
        self.calls.append("interrupt")
        # Does NOT stop the producer, and that is faithful: the CLI may still
        # emit trailing deltas and a ResultMessage for the interrupted turn.

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False
        for task in self._producers:
            task.cancel()
        self._producers.clear()
        self._stream = None

    # ── conveniences for assertions ──────────────────────────────────────
    @property
    def turns(self) -> int:
        return self.calls.count("query")

    @property
    def disconnected(self) -> bool:
        return "disconnect" in self.calls


class FakeClientFactory:
    """Drop-in for the ``ClaudeSDKClient`` class itself.

    Keeps every client it built, in build order, so a test can prove a second
    turn reused the first client (``len(factory.clients) == 1``) or that a
    changed signature built a second one. ``connect_gate`` / ``fail_connect``
    are read at build time, so a test can make only the *next* client slow or
    broken.
    """

    def __init__(
        self,
        behaviour: Behaviour | None = None,
        *,
        connect_gate: asyncio.Event | None = None,
        fail_connect: str | None = None,
    ) -> None:
        self.clients: list[FakeClaudeClient] = []
        self.behaviour = behaviour
        self.connect_gate = connect_gate
        self.fail_connect = fail_connect

    def __call__(self, options: Any = None, **_kwargs: Any) -> FakeClaudeClient:
        client = FakeClaudeClient(
            options=options,
            behaviour=self.behaviour,
            connect_gate=self.connect_gate,
            fail_connect=self.fail_connect,
        )
        self.clients.append(client)
        return client

    @property
    def live(self) -> list[FakeClaudeClient]:
        return [c for c in self.clients if c.connected]


def raising(message: str) -> Behaviour:
    """A turn whose stream blows up — a vanished CLI, a broken pipe."""

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        raise RuntimeError(message)
        yield  # pragma: no cover - makes this an async generator

    return behaviour


def ending_without_result(text: str = "partial") -> Behaviour:
    """A turn the CLI exits out of: some output, then EOF, no result.

    `claude` dying (OOM-killed, crashed, its transport closed) looks exactly
    like this from the SDK side, and the client it leaves behind is unusable.
    """

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        yield text_message(text)

    return behaviour


def hanging(started: asyncio.Event) -> Behaviour:
    """A turn that never ends, so the test can cancel it."""

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable

    return behaviour
