"""Replay: a recorded provider stream must translate the same way every time.

The two translators (``claude._translate`` and ``codex._Translator``) are the
narrowest place in the harness — every event the UI renders and every block the
store persists comes out of them — and until now each was only ever exercised
by the test that built its own messages inline. That makes a translation change
invisible: the test moves with the code.

A fixture pins the translation to something outside the code. Each file here is
a JSON array of provider messages and each assertion below is about the Events
they become: the exact ordered types, the text the deltas assemble into, the
tool_use ids that must be matched by tool_result ids, and the keys of the
single terminal event. Then the same Event stream goes through the REAL
``execute_turn`` and the assertions continue on what landed on disk — because
"the translation is right" and "the transcript is right" are two different
claims, and the second one is the one a user reloads a page to see.

**These fixtures are hand-authored, not recordings.** Neither vendor CLI is
installed on the machine this was written on, and a recording made on one
would be a snapshot of one CLI build (and a live subscription) rather than a
statement about the contract. Each file is instead written from the shapes the
translator consumes — for Claude the ``StreamEvent`` / ``AssistantMessage`` /
``UserMessage`` / ``ResultMessage`` dataclasses, tagged with ``__type__`` and
rehydrated by ``fakes/providers.py``; for Codex the raw JSON-RPC item
notifications, whose every wire spelling comes from ``codex/protocol.py``. They
are small on purpose: a text turn, a tool_use/tool_result pair, a terminal
event. What they cannot do is discover that a vendor changed its shape; what
they do is make sure *we* did not. See ``docs/harness.md``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.app.orchestrator.base import Event
from backend.app.orchestrator.claude import _translate
from backend.app.orchestrator.codex.provider import _Translator
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store
from backend.tests.fakes.providers import (
    ScriptedProvider,
    claude_messages,
    codex_frames,
)

CLAUDE_BASIC = "claude_basic.json"
CLAUDE_TOOLS = "claude_tools.json"
CODEX_BASIC = "codex_basic.json"
CODEX_APPROVAL = "codex_approval.json"

# What ``_translate`` puts on an ``assistant.done`` for a turn nobody parsed
# usage for. ``usage`` is added by ``ClaudeProvider.run`` (it is the only caller
# that has a ``TurnUsage``), so it is asserted in ``test_matrix.py`` instead —
# here the claim is about the translator's own payload.
CLAUDE_DONE_KEYS = {"cost_usd", "duration_ms", "num_turns", "upstream_session_id"}
CODEX_DONE_KEYS = {"upstream_session_id", "usage"}


@dataclass(frozen=True)
class ReplayCase:
    """One fixture and everything its stream is required to produce."""

    fixture: str
    vendor: str
    event_types: tuple[str, ...]
    text: str
    # (tool name, tool_use id) in call order — the ids a tool.result must match.
    tool_calls: tuple[tuple[str, str], ...]
    done_keys: frozenset[str]
    blocks: tuple[dict[str, Any], ...]


CASES: tuple[ReplayCase, ...] = (
    ReplayCase(
        fixture=CLAUDE_BASIC,
        vendor="claude",
        event_types=("assistant.text", "assistant.text", "assistant.done"),
        text="Hello, world.",
        tool_calls=(),
        done_keys=frozenset(CLAUDE_DONE_KEYS),
        blocks=({"type": "text", "text": "Hello, world."},),
    ),
    ReplayCase(
        fixture=CLAUDE_TOOLS,
        vendor="claude",
        event_types=(
            "assistant.text",
            "assistant.tool_use",
            "tool.result",
            "assistant.text",
            "assistant.done",
        ),
        text="Reading the file. It prints hi.",
        tool_calls=(("Read", "toolu_replay_01"),),
        done_keys=frozenset(CLAUDE_DONE_KEYS),
        blocks=(
            {"type": "text", "text": "Reading the file."},
            {
                "type": "tool_use",
                "id": "toolu_replay_01",
                "name": "Read",
                "input": {"file_path": "/repo/app.py"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "toolu_replay_01",
                "content": "print('hi')\n",
                "is_error": False,
            },
            {"type": "text", "text": " It prints hi."},
        ),
    ),
    ReplayCase(
        fixture=CODEX_BASIC,
        vendor="codex",
        event_types=(
            "assistant.text",
            "assistant.tool_use",
            "tool.result",
            "assistant.text",
            "assistant.text",
            "assistant.done",
        ),
        text="Considering the request.Hello, world.",
        tool_calls=(("Bash", "item-2"),),
        done_keys=frozenset(CODEX_DONE_KEYS),
        blocks=(
            {"type": "text", "text": "Considering the request."},
            {
                "type": "tool_use",
                "id": "item-2",
                "name": "Bash",
                "input": {"command": "echo hi"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "item-2",
                "content": "hi\n",
                "is_error": False,
            },
            {"type": "text", "text": "Hello, world."},
        ),
    ),
    ReplayCase(
        fixture=CODEX_APPROVAL,
        vendor="codex",
        event_types=(
            "assistant.tool_use",
            "tool.result",
            "assistant.text",
            "assistant.done",
        ),
        text="Removed the build directory.",
        tool_calls=(("Bash", "item-9"),),
        done_keys=frozenset(CODEX_DONE_KEYS),
        blocks=(
            {
                "type": "tool_use",
                "id": "item-9",
                "name": "Bash",
                "input": {"command": "rm -rf build"},
            },
            {
                "type": "tool_result",
                "tool_use_id": "item-9",
                "content": "",
                "is_error": False,
            },
            {"type": "text", "text": "Removed the build directory."},
        ),
    ),
)

IDS = [case.fixture for case in CASES]


async def replay(case: ReplayCase) -> list[Event]:
    """The Events one fixture produces, through the production translator."""
    if case.vendor == "claude":
        events: list[Event] = []
        for message in claude_messages(case.fixture):
            events.extend([ev async for ev in _translate(message)])
        return events
    translator = _Translator(
        thread_id="thread-replay", model="gpt-5.3-codex", session_id="sess-replay"
    )
    return [ev for frame in codex_frames(case.fixture) for ev in translator.handle(frame)]


@pytest.mark.parametrize("case", CASES, ids=IDS)
class TestTranslation:
    async def test_the_event_types_are_exactly_these_in_this_order(
        self, case: ReplayCase
    ) -> None:
        assert tuple(ev.type for ev in await replay(case)) == case.event_types

    async def test_the_text_deltas_assemble_into_the_message(
        self, case: ReplayCase
    ) -> None:
        """Deltas are emitted once each: a translator that forwarded the final
        consolidated text as well would double every sentence."""
        events = await replay(case)
        assembled = "".join(
            ev.data.get("text", "") for ev in events if ev.type == "assistant.text"
        )
        assert assembled == case.text

    async def test_every_tool_use_is_answered_by_a_result_with_the_same_id(
        self, case: ReplayCase
    ) -> None:
        events = await replay(case)
        calls = [
            (ev.data["name"], ev.data["id"])
            for ev in events
            if ev.type == "assistant.tool_use"
        ]
        results = [ev.data["tool_use_id"] for ev in events if ev.type == "tool.result"]

        assert tuple(calls) == case.tool_calls
        assert results == [call_id for _name, call_id in calls]

    async def test_exactly_one_terminal_event_carrying_these_keys(
        self, case: ReplayCase
    ) -> None:
        events = await replay(case)
        done = [ev for ev in events if ev.type == "assistant.done"]

        assert len(done) == 1
        assert set(done[0].data) == set(case.done_keys)
        assert done[0] is events[-1]


class TestApprovalIsNotANotification:
    """The approval fixture leads with the server→client request that raises
    the card, and the translator must emit nothing for it.

    That is not an omission: an approval is answered by
    ``evaluate_tool_request`` through the one approval bus (``approvals.py``),
    not rendered from the item stream. A translator that invented an Event for
    it would put a second, unanswerable card in the transcript beside the real
    one. The round-trip itself is exercised against the fake app-server in
    ``test_codex_provider.py`` and in the matrix.
    """

    async def test_the_approval_request_frame_translates_to_nothing(self) -> None:
        frames = codex_frames(CODEX_APPROVAL)
        translator = _Translator(
            thread_id="thread-replay-2", model="gpt-5.3-codex", session_id=None
        )

        assert frames[0]["method"] == "execCommandApproval"
        assert translator.handle(frames[0]) == []

    async def test_the_command_that_followed_it_still_translates(self) -> None:
        events = await replay(CASES[3])

        assert events[0].data["input"] == {"command": "rm -rf build"}


# ─────────────────────────────────────────────────────────────────────────────
# The same stream, persisted
# ─────────────────────────────────────────────────────────────────────────────


async def persist(events: list[Event], home: Path) -> list[dict[str, Any]]:
    """Run ``events`` through the real turn pipeline; return the stored blocks.

    ``execute_turn`` — not a hand-rolled loop over ``TurnAccumulator`` — because
    the thing being asserted is what a reload shows, and the accumulator is only
    half of that: the checkpoint throttle, the dangling-result repair and the
    final append all live in the turn.
    """
    meta = await session_store.create_session(
        provider="scripted", model="m", cwd=str(home / "proj")
    )
    session_id = str(meta["id"])
    await execute_turn(
        session_id=session_id,
        bus=EventBus(session_id),
        approval_q=asyncio.Queue(),
        provider=ScriptedProvider(events),  # type: ignore[arg-type]
        provider_name="scripted",
        model="m",
        cwd=str(home / "proj"),
        additional_dirs=[],
        upstream_id=None,
        fleet_override=None,
        permission_mode=None,
        prompt="replay",
    )
    messages, _before, _more = await session_store.list_messages(session_id)
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant) == 1, "a turn persists exactly one assistant message"
    return list(assistant[0]["content"])


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_the_persisted_blocks_are_the_stream_in_order(
    case: ReplayCase, isolated_store: Path
) -> None:
    blocks = await persist(await replay(case), isolated_store)

    assert blocks == list(case.blocks)


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_no_persisted_tool_use_is_left_dangling(
    case: ReplayCase, isolated_store: Path
) -> None:
    """The repair in the turn's ``finally`` has nothing to do for a complete
    stream — which is the claim: a well-formed turn must not acquire synthetic
    results it did not need."""
    blocks = await persist(await replay(case), isolated_store)

    calls = {b["id"] for b in blocks if b["type"] == "tool_use"}
    results = [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"]

    assert sorted(results) == sorted(calls)
    assert len(results) == len(set(results)), "one result per call, not two"


async def test_heartbeats_never_reach_the_transcript(isolated_store: Path) -> None:
    """A heartbeat is live chrome — the fleet's "still working" ticker — and the
    only ``assistant.text`` the runner is allowed to drop."""
    events = await replay(CASES[1])
    with_chrome = [
        events[0],
        Event(type="assistant.text", data={"text": "_…coder still working…_", "heartbeat": True}),
        *events[1:],
    ]

    blocks = await persist(with_chrome, isolated_store)

    assert blocks == list(CASES[1].blocks)
    assert not any("still working" in str(b) for b in blocks)
