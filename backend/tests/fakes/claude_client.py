"""A stand-in for ``claude_agent_sdk.ClaudeSDKClient``.

``ClaudeProvider`` now holds a live client per session, so its tests have to be
able to ask "was this the *same* client?" — which a monkeypatched module-level
``query`` function could not answer. The provider exposes a factory seam
(``_client_factory`` / ``self._factory``) and this is what the suite injects
through it: no `claude` CLI on PATH, no network, and every lifecycle call
(``connect`` / ``query`` / ``receive_response`` / ``interrupt`` /
``disconnect``) recorded in order on the instance.

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


class FakeClaudeClient:
    """Records its lifecycle calls and replays a scripted turn."""

    def __init__(self, options: Any = None, behaviour: Behaviour | None = None) -> None:
        self.options = options
        self.behaviour: Behaviour = behaviour or one_result
        # Every call in order. Reuse tests assert on this, so "connect" must
        # appear exactly once per client however many turns it serves.
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self.connected = False

    # ── the surface ClaudeProvider uses ──────────────────────────────────
    async def connect(self, prompt: Any = None) -> None:
        self.calls.append("connect")
        self.connected = True

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self.calls.append("query")
        self.prompts.append(prompt if isinstance(prompt, str) else repr(prompt))

    async def receive_response(self) -> AsyncIterator[Any]:
        self.calls.append("receive_response")
        async for message in self.behaviour(self):
            yield message

    async def interrupt(self) -> None:
        self.calls.append("interrupt")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

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
    changed signature built a second one.
    """

    def __init__(self, behaviour: Behaviour | None = None) -> None:
        self.clients: list[FakeClaudeClient] = []
        self.behaviour = behaviour

    def __call__(self, options: Any = None, **_kwargs: Any) -> FakeClaudeClient:
        client = FakeClaudeClient(options=options, behaviour=self.behaviour)
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


def hanging(started: asyncio.Event) -> Behaviour:
    """A turn that never ends, so the test can cancel it."""

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable

    return behaviour
