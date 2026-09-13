"""Scriptable load generators — synthetic turns whose cost is known exactly.

Task 12's fakes answer "did the harness behave correctly?". These answer "what
did it cost?", which needs a different property from a fake: every byte it
emits has to be counted as it is emitted, because a cost assertion is only as
honest as its denominator. A soak that measures 4 MB on disk against a guess at
what it produced measures nothing.

So a :class:`TurnScript` is both the event source and the ledger. It yields the
exact ``Event`` objects ``execute_turn`` consumes, and it accumulates
``content_bytes`` — the UTF-8 size of everything that will end up inside a
persisted message block (prompt text, assistant text, tool_use inputs,
tool_result contents). The soak's disk budget is a ratio against that number,
so a regression that writes the same content several times shows up as the
ratio it is, on any machine, at any scale.

What is deliberately NOT modelled: vendor framing, usage accounting, approval
gates. Those have fakes already (``fakes/providers.py``). A load generator that
also translated a vendor's wire shape would make its own ledger a guess.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from backend.app.orchestrator.base import Event, RunContext

# One tool_use/tool_result pair is the unit of checkpoint pressure: each pair
# is two checkpoints in ``execute_turn``. Text deltas are the unit of bus
# pressure. A turn made only of one or the other would exercise half the cost
# model, so the default profile carries both.
_TEXT_CHUNK = "The quick brown fox jumps over the lazy dog. "
_TOOL_NAMES = ("Read", "Edit", "Bash", "Grep")


def _tool_input(turn: int, index: int) -> dict[str, Any]:
    return {
        "file_path": f"/work/module_{turn:03d}_{index:02d}.py",
        "pattern": "def .*",
    }


def _tool_output(turn: int, index: int, size: int) -> str:
    """A result of exactly ``size`` bytes (ASCII, so bytes == characters)."""
    head = f"turn {turn} step {index}: "
    return (head + "R" * max(size - len(head), 0))[:size] if size else head


@dataclass
class TurnScript:
    """One synthetic turn's events, plus the ledger of what they contain.

    ``events_per_turn`` counts EVERY event the provider yields, terminal
    ``assistant.done`` included, so "200 turns of 50 events" is literally
    10 000 events through the bus rather than 10 000 plus bookkeeping.
    """

    turn: int
    events_per_turn: int = 50
    small_result_bytes: int = 200
    big_result_bytes: int = 0
    # Filled in as events are yielded — read it after the turn, never before.
    content_bytes: int = 0
    tool_ids: list[str] = field(default_factory=list)

    @property
    def prompt(self) -> str:
        return f"soak turn {self.turn}: keep going"

    def count_prompt(self) -> str:
        """The user prompt, charged to the ledger. The turn persists it as a
        message of its own, so leaving it out would make the disk ratio drift
        upward by one message per turn for no reason anyone could explain."""
        self.content_bytes += len(self.prompt.encode("utf-8"))
        return self.prompt

    def _pairs_and_texts(self) -> tuple[int, int]:
        """How the event budget is split: every event but the terminal one is
        either a text delta or half a tool pair, and the pairs come first so a
        turn is never left with a tool_use whose result did not fit."""
        usable = max(self.events_per_turn - 1, 0)
        pairs = usable // 2 - 4  # leave a handful of deltas per turn
        pairs = max(pairs, 1)
        texts = usable - pairs * 2
        return pairs, max(texts, 0)

    def events(self) -> Iterator[Event]:
        pairs, texts = self._pairs_and_texts()
        for i in range(pairs):
            tool_id = f"t{self.turn:03d}-{i:02d}"
            self.tool_ids.append(tool_id)
            tool_input = _tool_input(self.turn, i)
            name = _TOOL_NAMES[i % len(_TOOL_NAMES)]
            self.content_bytes += len(str(tool_input).encode("utf-8")) + len(name)
            yield Event(
                type="assistant.tool_use",
                data={"id": tool_id, "name": name, "input": tool_input},
            )
            # The big result rides on the first pair of the turn so the
            # checkpoint that follows it is the one that has to absorb it.
            size = self.big_result_bytes if (i == 0 and self.big_result_bytes) else (
                self.small_result_bytes
            )
            content = _tool_output(self.turn, i, size)
            self.content_bytes += len(content.encode("utf-8"))
            yield Event(
                type="tool.result",
                data={"tool_use_id": tool_id, "content": content, "is_error": False},
            )
        for _ in range(texts):
            self.content_bytes += len(_TEXT_CHUNK.encode("utf-8"))
            yield Event(type="assistant.text", data={"text": _TEXT_CHUNK})
        yield Event(
            type="assistant.done",
            data={"cost_usd": 0.001, "duration_ms": 5, "usage": None},
        )


class SoakProvider:
    """A ``Provider`` that replays one :class:`TurnScript` per ``run()``.

    Deliberately not :class:`~backend.tests.fakes.providers.ScriptedProvider`
    with a prebuilt list: 10 000 pre-materialised events (with a 256 KiB string
    in every twentieth turn) would be a few megabytes of Python objects held
    for the whole soak, and the soak is measuring memory. Each turn's events
    are generated as they are yielded and dropped as they are consumed.
    """

    name = "soak"

    def __init__(
        self,
        *,
        events_per_turn: int = 50,
        big_result_every: int = 20,
        big_result_bytes: int = 256 * 1024,
    ) -> None:
        self.events_per_turn = events_per_turn
        self.big_result_every = big_result_every
        self.big_result_bytes = big_result_bytes
        self.turn = 0
        self.content_bytes = 0
        self.events_emitted = 0
        self.tool_ids: list[str] = []

    def next_script(self) -> TurnScript:
        """The script for the turn about to run. The caller needs it before
        ``run()`` to get the prompt, so the turn counter advances here."""
        self.turn += 1
        big = (
            self.big_result_bytes
            if self.big_result_every and self.turn % self.big_result_every == 0
            else 0
        )
        self._script = TurnScript(
            turn=self.turn,
            events_per_turn=self.events_per_turn,
            big_result_bytes=big,
        )
        return self._script

    async def open_session(self, ctx: RunContext) -> str:
        return "upstream-soak"

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        script = self._script
        for ev in script.events():
            self.events_emitted += 1
            yield ev
        self.content_bytes += script.content_bytes
        self.tool_ids.extend(script.tool_ids)

    async def close_session(self, session_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


def burst_events(count: int, *, text: str = "delta ") -> list[dict[str, Any]]:
    """``count`` already-shaped bus payloads, for throughput measurement.

    Pre-materialised on purpose here — the bus benchmark is measuring the bus,
    so building the dicts inside the timed region would charge the allocator to
    the throughput number.
    """
    return [{"type": "assistant.text", "data": {"text": f"{text}{i}"}} for i in range(count)]


async def streaming_turn(count: int) -> AsyncIterator[Event]:
    """A turn of ``count`` text deltas and one terminal event, generated
    lazily — the stall detector's subject."""
    for i in range(count):
        yield Event(type="assistant.text", data={"text": f"tok{i} "})
    yield Event(type="assistant.done", data={})
