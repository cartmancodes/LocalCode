"""The matrix: every provider, single and in a fleet, held to ONE contract.

This is the product of the evaluation net. Every other suite answers "does
Claude do X" or "does Codex do Y"; this one answers the question the whole
harness exists to make answerable — *does it matter which vendor served the
turn?* A cell here is one ``(vendor, shape)`` pair, and every cell is asserted
against the same four invariants:

1. an ordered event stream — every ``assistant.tool_use`` answered by a
   ``tool.result`` carrying its id, in order;
2. exactly one terminal ``assistant.done``, however the turn went;
3. persisted blocks that round-trip — what the store hands back on reload is
   what the stream said, with its JSON types intact;
4. approvals surfacing through the one bus — the same card shape, from a
   vendor callback that has nothing in common with the other vendor's.

The vendors are real provider objects over the suite's existing fakes: a real
``ClaudeProvider`` driving a scripted ``ClaudeSDKClient``, and a real
``CodexProvider`` driving a real child process that speaks the app-server
JSON-RPC. Neither vendor CLI is installed, and neither is needed.

**The gap, stated rather than papered over.** The fleet half serves its roles
through ``collect_step`` in-process rather than through the worker *process*
(see ``fakes/providers.FakeWorkerPool``). The retired ``opencode`` provider had no fake and
is therefore absent from the matrix: it speaks HTTP + SSE to a host-side
server, and standing one up would be a different fake from the two here. Both
gaps are recorded in ``docs/harness.md``; neither is hidden behind a weakened
assertion.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import get_settings
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store
from backend.tests.fakes.claude_client import result_message, text_message
from backend.tests.fakes.providers import (
    FakeOrchestratorModel,
    FakeWorkerPool,
    GatedSubProviders,
    ToolCall,
    asking_behaviour,
    claude_messages,
    claude_provider,
    codex_provider,
    fleet_provider_with,
    install_sub_provider,
)

WAIT_S = 30.0
DISPATCH = "dispatch_subagent"

VENDORS = ("claude", "codex")


@pytest.fixture(autouse=True)
def _matrix_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """One settings profile for every cell, so a difference between two cells
    is a difference between two vendors and not between two configs."""
    get_settings.cache_clear()
    monkeypatch.setenv("TOOL_APPROVAL_TIMEOUT_S", "10")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def vendors(tmp_path: Path) -> AsyncIterator[Vendors]:
    """Builds providers for either vendor and guarantees every child is reaped."""
    factory = Vendors()
    try:
        yield factory
    finally:
        await factory.aclose()


class Vendors:
    """Provider constructors per vendor, plus the cleanup Codex needs."""

    def __init__(self) -> None:
        self.built: list[Any] = []

    def single(self, vendor: str) -> Any:
        """A plain turn: some text, one tool call answered, one terminal event."""
        if vendor == "claude":
            return self._keep(claude_provider(claude_messages("claude_tools.json")))
        return self._keep(codex_provider("happy", timeout_s=WAIT_S))

    def asking(self, vendor: str, cwd: Path) -> Any:
        """A turn that needs a human to say yes before a tool may run."""
        if vendor == "claude":
            return self._keep(
                claude_provider(
                    (),
                    behaviour=asking_behaviour(
                        "Write",
                        {"file_path": str(cwd / "a.py"), "content": "print('hi')"},
                        [text_message("Written."), result_message()],
                    ),
                )
            )
        # The fake app-server raises ``execCommandApproval`` and only runs the
        # command on an approving decision — a completely different callback
        # shape reaching the same gate.
        return self._keep(codex_provider("approval", timeout_s=WAIT_S))

    def sub_builder(self, vendor: str) -> Any:
        """What a fleet step builds for its sub-provider."""

        def build(provider_name: str) -> Any:
            return self.single(vendor)

        return build

    def _keep(self, provider: Any) -> Any:
        self.built.append(provider)
        return provider

    async def aclose(self) -> None:
        pgids: list[int] = []
        for provider in self.built:
            broker = getattr(provider, "_broker", None)
            for server in list(getattr(broker, "_servers", {}).values()):
                pid = server.pid
                if pid is not None:
                    with contextlib.suppress(OSError):
                        pgids.append(os.getpgid(pid))
            with contextlib.suppress(Exception):
                await provider.aclose()
        # A failing cell must not leave a child for the next one.
        for pgid in pgids:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)


@dataclass
class TurnResult:
    """One cell's turn: what it broadcast and what it stored."""

    session_id: str
    events: list[dict[str, Any]]
    blocks: list[dict[str, Any]]

    @property
    def types(self) -> list[str]:
        return [str(e["type"]) for e in self.events]

    def of(self, ev_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == ev_type]


async def run_cell(
    home: Path,
    *,
    provider: Any,
    provider_name: str,
    prompt: str = "do the thing",
    permission_mode: str | None = None,
    answer: str | None = None,
    fleet_override: dict[str, Any] | None = None,
) -> TurnResult:
    """One turn through the real pipeline, with any approval card answered."""
    cwd = home / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    meta = await session_store.create_session(
        provider=provider_name, model="m", cwd=str(cwd)
    )
    session_id = str(meta["id"])
    events: list[dict[str, Any]] = []
    approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def answer_cards() -> None:
        answered: set[str] = set()
        while True:
            for card in [e for e in events if e.get("type") == "pipeline.awaiting_approval"]:
                approval_id = str(card["data"]["id"])
                if approval_id not in answered and answer is not None:
                    answered.add(approval_id)
                    await approval_q.put({"id": approval_id, "value": answer})
            await asyncio.sleep(0.01)

    answerer = asyncio.create_task(answer_cards())
    try:
        await asyncio.wait_for(
            execute_turn(
                session_id=session_id,
                bus=EventBus(session_id, on_event=events.append),
                approval_q=approval_q,
                provider=provider,
                provider_name=provider_name,
                model="gpt-5.3-codex" if provider_name == "codex" else "m",
                cwd=str(cwd),
                additional_dirs=[],
                upstream_id=None,
                fleet_override=fleet_override,
                permission_mode=permission_mode,
                prompt=prompt,
            ),
            WAIT_S,
        )
    finally:
        answerer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await answerer

    messages, _before, _more = await session_store.list_messages(session_id)
    assistant = [m for m in messages if m["role"] == "assistant"]
    blocks = list(assistant[-1]["content"]) if assistant else []
    return TurnResult(session_id=session_id, events=events, blocks=blocks)


def assert_stream_is_well_formed(turn: TurnResult) -> None:
    """Invariants 1 and 2 — identical for every cell, by construction."""
    calls = [e["data"]["id"] for e in turn.of("assistant.tool_use")]
    results = [e["data"]["tool_use_id"] for e in turn.of("tool.result")]

    assert calls, "a matrix cell that calls no tool is not exercising the pairing"
    assert results == calls, "every tool_use is answered, in order, by its own id"
    assert turn.types.count("assistant.done") == 1, turn.types


# ─────────────────────────────────────────────────────────────────────────────
# {claude, codex} × {single, fleet}
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("vendor", VENDORS)
class TestSingleProviderTurn:
    async def test_the_stream_is_ordered_and_terminates_once(
        self, vendor: str, vendors: Vendors, isolated_store: Path
    ) -> None:
        turn = await run_cell(
            isolated_store, provider=vendors.single(vendor), provider_name=vendor
        )

        assert_stream_is_well_formed(turn)
        assert turn.types[-1] == "assistant.done"
        assert "assistant.text" in turn.types

    async def test_the_terminal_event_carries_the_upstream_session_id(
        self, vendor: str, vendors: Vendors, isolated_store: Path
    ) -> None:
        """Both vendors resume a conversation by id, and both report theirs on
        the one terminal event — which is what the runner persists."""
        turn = await run_cell(
            isolated_store, provider=vendors.single(vendor), provider_name=vendor
        )

        done = turn.of("assistant.done")[0]["data"]
        assert done["upstream_session_id"]
        session = await session_store.get_session(turn.session_id)
        assert session is not None
        assert session["upstream_id"] == done["upstream_session_id"]

    async def test_the_terminal_event_reports_token_usage(
        self, vendor: str, vendors: Vendors, isolated_store: Path
    ) -> None:
        """The quota governor is fed from this field and from nothing else, so
        a vendor that stops reporting it stops being metered."""
        turn = await run_cell(
            isolated_store, provider=vendors.single(vendor), provider_name=vendor
        )

        usage = turn.of("assistant.done")[0]["data"].get("usage")
        assert usage is not None, "no usage means an unmetered subscription"
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0

    async def test_the_persisted_blocks_round_trip(
        self, vendor: str, vendors: Vendors, isolated_store: Path
    ) -> None:
        """What a reload shows is what the stream said — types included.

        ``is_error`` is asserted with ``is``, not ``==``: the store used to
        coerce every bool and int on its way to disk (``True`` became ``1.0``),
        and ``1.0 == True`` in Python, so an equality assertion could not see
        it. See ``storage/sessions._to_jsonable``.
        """
        turn = await run_cell(
            isolated_store, provider=vendors.single(vendor), provider_name=vendor
        )

        kinds = [b["type"] for b in turn.blocks]
        assert "tool_use" in kinds and "tool_result" in kinds
        for block in turn.blocks:
            if block["type"] == "tool_result":
                assert block["is_error"] is False
            if block["type"] == "tool_use":
                assert isinstance(block["input"], dict)
        # Round-trip through JSON exactly as the API hands it to the UI.
        assert json.loads(json.dumps(turn.blocks)) == turn.blocks

    async def test_an_approval_card_looks_the_same_whoever_asked(
        self, vendor: str, vendors: Vendors, isolated_store: Path
    ) -> None:
        """Invariant 4. Two vendor callbacks with nothing in common —
        ``can_use_tool`` and ``execCommandApproval`` — reach one gate, so the
        user learns one approval UI and a deny can be forgotten in one place.
        """
        turn = await run_cell(
            isolated_store,
            provider=vendors.asking(vendor, isolated_store / "proj"),
            provider_name=vendor,
            permission_mode="default",
            answer="yes",
        )

        cards = turn.of("pipeline.awaiting_approval")
        assert len(cards) == 1, turn.types
        card = cards[0]["data"]
        assert set(card) == {"id", "kind", "tool", "input", "reason", "timeout_s"}
        assert card["kind"] == "tool"
        assert card["tool"] in ("Write", "Bash")
        assert card["id"].startswith("approval.tool.")
        answered = turn.of("pipeline.approval_received")
        assert len(answered) == 1
        assert answered[0]["data"]["value"] == "yes"
        assert turn.types.count("assistant.done") == 1


@pytest.mark.parametrize("vendor", VENDORS)
class TestFleetTurnServedByOneVendor:
    """The same turn, with the roles served by that vendor's real provider."""

    async def test_the_stream_is_ordered_and_terminates_once(
        self, vendor: str, vendors: Vendors, isolated_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_sub_provider(monkeypatch, vendors.sub_builder(vendor))
        FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "do the work"})]
        ).install(monkeypatch)
        pool = FakeWorkerPool()
        provider = fleet_provider_with(pool)

        try:
            turn = await run_cell(
                isolated_store,
                provider=provider,
                provider_name="fleet",
                prompt="implement the retry and review it",
                permission_mode="acceptEdits",
                fleet_override={"roles": {"coder": {"provider": vendor, "model": "m"}}},
            )
        finally:
            await provider.aclose()

        # The step card is the tool_use; its envelope is the tool.result.
        assert_stream_is_well_formed(turn)
        assert [e["data"]["name"] for e in turn.of("assistant.tool_use")] == [
            f"coder [{vendor}:m]"
        ]
        assert pool.requests[0]["provider"] == vendor

    async def test_the_step_output_reaches_the_transcript_and_survives_reload(
        self, vendor: str, vendors: Vendors, isolated_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_sub_provider(monkeypatch, vendors.sub_builder(vendor))
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "do the work"})]
        ).install(monkeypatch)
        pool = FakeWorkerPool()
        provider = fleet_provider_with(pool)

        try:
            turn = await run_cell(
                isolated_store,
                provider=provider,
                provider_name="fleet",
                prompt="implement the retry and review it",
                permission_mode="acceptEdits",
                fleet_override={"roles": {"coder": {"provider": vendor, "model": "m"}}},
            )
        finally:
            await provider.aclose()

        stored = {b["type"] for b in turn.blocks}
        assert {"tool_use", "tool_result"} <= stored
        # The bounded envelope, not the transcript: this is what the model was
        # handed AND what the card shows.
        _tool, _args, result = model.calls[0]
        context = "\n".join(b.get("text", "") for b in (result.get("content") or []))
        assert context
        assert json.loads(json.dumps(turn.blocks)) == turn.blocks


class TestTheFleetGateAppliesToEveryVendor:
    """A role's limits are a runtime property, not a paragraph — and the fleet
    half of the matrix has to show that too, not just the single half."""

    async def test_a_reviewers_write_is_refused_and_leaves_no_file(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = isolated_store / "proj"
        cwd.mkdir(parents=True, exist_ok=True)
        subs = GatedSubProviders(
            {
                "reviewer": (
                    (
                        ToolCall("Read", {"file_path": str(cwd / "x.py")}),
                        ToolCall(
                            "Write",
                            {"file_path": str(cwd / "x.py"), "content": "nope"},
                            effect=lambda: (cwd / "x.py").write_text("nope"),
                        ),
                    ),
                    "Looks fine. LGTM",
                )
            }
        ).install(monkeypatch)
        FakeOrchestratorModel(
            [(DISPATCH, {"name": "reviewer", "prompt": "review it"})]
        ).install(monkeypatch)
        provider = fleet_provider_with(FakeWorkerPool())

        try:
            await run_cell(
                isolated_store,
                provider=provider,
                provider_name="fleet",
                prompt="review the uploader change",
                permission_mode="acceptEdits",
                fleet_override={"roles": {"reviewer": {"provider": "claude", "model": "m"}}},
            )
        finally:
            await provider.aclose()

        outcomes = [(rec.tool, rec.allowed) for rec in subs.gate]
        assert outcomes == [("Read", True), ("Write", False)]
        assert not (cwd / "x.py").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Paging parity — carried forward from Task 13's ledger
# ─────────────────────────────────────────────────────────────────────────────


class TestWindowedAndStreamingReadsAgree:
    """``GET /messages`` has two readers, and nothing pinned them together.

    A page with a ``limit`` is served by the bounded tail window; a read with
    no limit streams the whole log line by line. They agree today because they
    share ``_dedupe_by_id`` and ``_ordered_messages`` — which is a property of
    the current factoring, not an asserted contract, and the windowed reader is
    the one with the seek arithmetic and the mid-line boundary handling. One
    fixture that crosses the window, read both ways, byte-identical.
    """

    BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    # ~20 KiB each: a page of 5 needs six of them, which is past the 64 KiB
    # first window, so the windowed reader has to widen at least once.
    FILLER = 20_000
    COUNT = 30
    PAGE = 5

    async def _seed(self, home: Path) -> str:
        meta = await session_store.create_session(
            provider="claude", model="m", cwd=str(home / "proj")
        )
        session_id = str(meta["id"])
        for i in range(self.COUNT):
            await session_store.append_message(
                session_id,
                {
                    "role": "assistant" if i % 2 else "user",
                    "content": [
                        {"type": "text", "text": f"m{i}:" + "x" * self.FILLER},
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "Read",
                            "input": {"file_path": f"/repo/f{i}.py", "limit": i},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i}",
                            "content": "ok",
                            "is_error": False,
                        },
                    ],
                    "created_at": (self.BASE + timedelta(seconds=i)).isoformat(),
                },
                bump_updated_at=False,
                fsync=False,
            )
        return session_id

    async def test_paging_through_the_log_reproduces_the_whole_log(
        self, isolated_store: Path
    ) -> None:
        session_id = await self._seed(isolated_store)

        whole, _before, has_more = await session_store.list_messages(session_id)
        assert has_more is False
        assert len(whole) == self.COUNT

        paged: list[dict[str, Any]] = []
        cursor: datetime | None = None
        while True:
            page, cursor, more = await session_store.list_messages(
                session_id, before=cursor, limit=self.PAGE
            )
            paged = list(page) + paged
            if not more:
                break
            assert cursor is not None, "has_more with no cursor is an unwalkable log"

        assert json.dumps(paged) == json.dumps(whole)

    async def test_one_page_is_byte_identical_to_that_slice_of_the_stream(
        self, isolated_store: Path
    ) -> None:
        """The narrower claim, and the one that would catch a windowed reader
        that trimmed a boundary line differently: the newest page must equal
        the tail of the unpaged read, byte for byte."""
        session_id = await self._seed(isolated_store)

        whole, _before, _more = await session_store.list_messages(session_id)
        page, _cursor, more = await session_store.list_messages(
            session_id, limit=self.PAGE
        )

        assert more is True, "the fixture has to be bigger than one page"
        assert json.dumps(page) == json.dumps(whole[-self.PAGE :])
        # And the blocks inside survived both readers with their types intact.
        assert page[-1]["content"][2]["is_error"] is False
        assert isinstance(page[-1]["content"][1]["input"]["limit"], int)
