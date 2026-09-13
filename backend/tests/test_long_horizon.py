"""Long-horizon cases: what happens to a turn that does not go straight through.

Every other test in this suite asks a component a question. These ask the
*turn* — the real ``execute_turn``, a real session directory under a temp root,
a real provider object over a fake vendor — what it leaves behind when it is
interrupted, gated, delegated, wedged, or handed two megabytes.

The shared rule underneath all five: whatever happens, the session stays
readable and the turn ends. A transcript with a tool_use nobody answered, a
working indicator that never clears, a step that waits out ten minutes on a
backend that was never going to answer, a 2 MB log inlined into the next
prompt — each of those is a way the harness stops being usable *after* the
failure it was supposed to survive.

No case here re-implements the runner. ``execute_turn`` is called directly, in
full, and the assertions are made on the bus it broadcast to and the files it
wrote.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from backend.app.orchestrator.base import Event
from backend.app.session_runner.accumulator import _INCOMPLETE_RESULT
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store
from backend.tests.fakes.claude_client import result_message, text_message
from backend.tests.fakes.providers import (
    FakeOrchestratorModel,
    FakeSubProviders,
    FakeWorkerPool,
    ScriptedProvider,
    asking_behaviour,
    claude_provider,
    fleet_provider_with,
)

# Long enough not to flake on a loaded machine; short enough that a turn that
# genuinely hangs fails this suite instead of the whole run.
WAIT_S = 20.0

DISPATCH = "dispatch_subagent"


class Recorder:
    """Every event a turn broadcast, in order, as the bus saw them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)

    @property
    def types(self) -> list[str]:
        return [str(e.get("type")) for e in self.events]

    def of(self, ev_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == ev_type]

    def text(self) -> str:
        return "".join(
            str((e.get("data") or {}).get("text", "")) for e in self.of("assistant.text")
        )


async def new_session(home: Path, *, provider: str = "scripted") -> tuple[str, Path]:
    cwd = home / "proj"
    meta = await session_store.create_session(provider=provider, model="m", cwd=str(cwd))
    return str(meta["id"]), cwd


async def run_turn(
    session_id: str,
    *,
    provider: Any,
    cwd: Path,
    prompt: str = "do the thing",
    provider_name: str = "scripted",
    approval_q: asyncio.Queue[dict[str, Any]] | None = None,
    fleet_override: dict[str, Any] | None = None,
    permission_mode: str | None = None,
) -> Recorder:
    """One real turn, start to finish, with everything it broadcast captured."""
    recorder = Recorder()
    await execute_turn(
        session_id=session_id,
        bus=EventBus(session_id, on_event=recorder),
        approval_q=approval_q or asyncio.Queue(),
        provider=provider,
        provider_name=provider_name,
        model="m",
        cwd=str(cwd),
        additional_dirs=[],
        upstream_id=None,
        fleet_override=fleet_override,
        permission_mode=permission_mode,
        prompt=prompt,
    )
    return recorder


async def stored_blocks(session_id: str) -> list[list[dict[str, Any]]]:
    """The content blocks of every assistant message in the session, in order."""
    messages, _before, _more = await session_store.list_messages(session_id)
    return [list(m["content"]) for m in messages if m["role"] == "assistant"]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Resume after interrupt
# ─────────────────────────────────────────────────────────────────────────────


class TestResumeAfterInterrupt:
    """A turn cancelled mid-tool must leave a transcript the next turn can
    build on — not a dangling call the vendor will reject on the way back in.
    """

    @staticmethod
    def _wedged_after_a_tool_call(reached: asyncio.Event) -> Any:
        async def run(ctx: Any) -> Any:
            yield Event(type="assistant.text", data={"text": "Editing the file."})
            yield Event(
                type="assistant.tool_use",
                data={"id": "toolu_stall", "name": "Edit", "input": {"file_path": "a.py"}},
            )
            reached.set()
            await asyncio.Event().wait()  # the tool result never arrives
            yield Event(type="assistant.done", data={})  # pragma: no cover

        return run

    async def test_a_cancelled_turn_persists_a_coherent_message(
        self, isolated_store: Path
    ) -> None:
        session_id, cwd = await new_session(isolated_store)
        reached = asyncio.Event()
        recorder = Recorder()
        turn = asyncio.create_task(
            execute_turn(
                session_id=session_id,
                bus=EventBus(session_id, on_event=recorder),
                approval_q=asyncio.Queue(),
                provider=ScriptedProvider(self._wedged_after_a_tool_call(reached)),  # type: ignore[arg-type]
                provider_name="scripted",
                model="m",
                cwd=str(cwd),
                additional_dirs=[],
                upstream_id=None,
                fleet_override=None,
                permission_mode=None,
                prompt="edit it",
            )
        )
        await asyncio.wait_for(reached.wait(), WAIT_S)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(turn, WAIT_S)

        blocks = await stored_blocks(session_id)

        assert len(blocks) == 1
        assert [b["type"] for b in blocks[0]] == ["text", "tool_use", "tool_result"]
        repair = blocks[0][-1]
        assert repair["tool_use_id"] == "toolu_stall"
        assert repair["is_error"] is True
        assert repair["content"] == _INCOMPLETE_RESULT
        # Cancellation is reported; the terminal event still goes out, because
        # every viewer clears its working indicator on it.
        assert "error" in recorder.types
        assert recorder.types.count("assistant.done") == 1

    async def test_the_next_turn_on_that_session_continues(
        self, isolated_store: Path
    ) -> None:
        session_id, cwd = await new_session(isolated_store)
        reached = asyncio.Event()
        turn = asyncio.create_task(
            execute_turn(
                session_id=session_id,
                bus=EventBus(session_id),
                approval_q=asyncio.Queue(),
                provider=ScriptedProvider(self._wedged_after_a_tool_call(reached)),  # type: ignore[arg-type]
                provider_name="scripted",
                model="m",
                cwd=str(cwd),
                additional_dirs=[],
                upstream_id=None,
                fleet_override=None,
                permission_mode=None,
                prompt="edit it",
            )
        )
        await asyncio.wait_for(reached.wait(), WAIT_S)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(turn, WAIT_S)

        second = await run_turn(
            session_id,
            provider=ScriptedProvider(
                [
                    Event(type="assistant.text", data={"text": "Picking up where we left off."}),
                    Event(type="assistant.done", data={"cost_usd": 0.01}),
                ]
            ),
            cwd=cwd,
            prompt="carry on",
        )

        messages, _before, _more = await session_store.list_messages(session_id)
        roles = [m["role"] for m in messages]
        blocks = await stored_blocks(session_id)

        assert roles == ["user", "assistant", "user", "assistant"]
        assert blocks[1] == [{"type": "text", "text": "Picking up where we left off."}]
        assert second.types.count("assistant.done") == 1
        # Every tool_use in the whole session has exactly one result.
        calls = [b["id"] for msg in blocks for b in msg if b["type"] == "tool_use"]
        results = [b["tool_use_id"] for msg in blocks for b in msg if b["type"] == "tool_result"]
        assert sorted(results) == sorted(calls)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Approval branching
# ─────────────────────────────────────────────────────────────────────────────


class TestApprovalBranching:
    """Approve, deny-with-feedback and time-out through the whole turn.

    The gate is the real one (``evaluate_tool_request`` via the real
    ``ClaudeProvider``'s ``can_use_tool``), the card travels on the real bus,
    and the answer arrives on the queue ``execute_turn`` owns — so what is
    asserted here is what the user sees AND what the model is told, which are
    two different halves nobody else checks together.
    """

    @pytest.fixture(autouse=True)
    def _fast_gate(self, monkeypatch: pytest.MonkeyPatch, fresh_settings: Any) -> None:
        # Short enough that the timeout branch is a test rather than a wait.
        monkeypatch.setenv("TOOL_APPROVAL_TIMEOUT_S", "1")

    async def _turn_answered_with(
        self, home: Path, answer: dict[str, Any] | None
    ) -> tuple[Recorder, list[Any]]:
        session_id, cwd = await new_session(home, provider="claude")
        cwd.mkdir(parents=True, exist_ok=True)
        decisions: list[Any] = []
        provider = claude_provider(
            (),
            behaviour=asking_behaviour(
                "Write",
                {"file_path": str(cwd / "a.py"), "content": "print('hi')"},
                [text_message("Written."), result_message()],
                decisions=decisions,
            ),
        )
        approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        recorder = Recorder()

        async def answer_when_asked() -> None:
            while True:
                cards = recorder.of("pipeline.awaiting_approval")
                if cards:
                    if answer is not None:
                        await approval_q.put({"id": cards[0]["data"]["id"], **answer})
                    return
                await asyncio.sleep(0.01)

        answerer = asyncio.create_task(answer_when_asked())
        try:
            await asyncio.wait_for(
                execute_turn(
                    session_id=session_id,
                    bus=EventBus(session_id, on_event=recorder),
                    approval_q=approval_q,
                    provider=provider,
                    provider_name="claude",
                    model="m",
                    cwd=str(cwd),
                    additional_dirs=[],
                    upstream_id=None,
                    fleet_override=None,
                    permission_mode="default",
                    prompt="write a.py",
                ),
                WAIT_S,
            )
        finally:
            answerer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await answerer
            await provider.aclose()
        return recorder, decisions

    async def test_approve_allows_the_tool_and_ends_the_turn(
        self, isolated_store: Path
    ) -> None:
        recorder, decisions = await self._turn_answered_with(isolated_store, {"value": "yes"})

        card = recorder.of("pipeline.awaiting_approval")[0]["data"]
        assert card["kind"] == "tool"
        assert card["tool"] == "Write"
        assert recorder.of("pipeline.approval_received")[0]["data"]["value"] == "yes"
        assert isinstance(decisions[0], PermissionResultAllow)
        assert recorder.types.count("assistant.done") == 1
        assert "Written." in recorder.text()

    async def test_deny_with_feedback_reaches_the_model_verbatim(
        self, isolated_store: Path
    ) -> None:
        recorder, decisions = await self._turn_answered_with(
            isolated_store, {"value": "no", "feedback": "write it under src/ instead"}
        )

        received = recorder.of("pipeline.approval_received")[0]["data"]
        assert received["value"] == "no"
        assert received["feedback"] == "write it under src/ instead"
        denial = decisions[0]
        assert isinstance(denial, PermissionResultDeny)
        # The refusal the model reads has to name the tool AND carry the
        # reason — "permission denied" alone is what sends it back into a
        # retry loop on the same call.
        assert "Write" in denial.message
        assert "write it under src/ instead" in denial.message
        assert recorder.types.count("assistant.done") == 1

    async def test_a_timeout_denies_and_still_ends_the_turn(
        self, isolated_store: Path
    ) -> None:
        recorder, decisions = await self._turn_answered_with(isolated_store, None)

        received = recorder.of("pipeline.approval_received")[0]["data"]
        assert received["value"] == "timeout"
        assert isinstance(decisions[0], PermissionResultDeny)
        assert "timeout" in decisions[0].message
        # The card is answered on every path — the UI clears it on this event,
        # and the runner clears its pending-approval record on it.
        assert len(recorder.of("pipeline.approval_received")) == 1
        assert recorder.types.count("assistant.done") == 1


# ─────────────────────────────────────────────────────────────────────────────
# Fleet scaffolding — shared by cases 3, 4 and 5
# ─────────────────────────────────────────────────────────────────────────────


def fleet_roles(*names: str) -> dict[str, Any]:
    """A per-session fleet override pinning exactly these roles onto claude."""
    return {"roles": {name: {"provider": "claude", "model": "m"} for name in names}}


def text_then_done(text: str) -> list[Event]:
    return [
        Event(type="assistant.text", data={"text": text}),
        Event(type="assistant.done", data={"usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]


async def run_fleet_turn(
    home: Path,
    *,
    model: FakeOrchestratorModel,
    subs: FakeSubProviders,
    roles: tuple[str, ...],
    prompt: str = "add a retry to the uploader and review it",
) -> tuple[Recorder, FakeWorkerPool, Path]:
    """One fleet turn end-to-end: real provider, real dispatch, fake vendors."""
    session_id, cwd = await new_session(home, provider="fleet")
    cwd.mkdir(parents=True, exist_ok=True)
    pool = FakeWorkerPool()
    provider = fleet_provider_with(pool)
    try:
        recorder = await asyncio.wait_for(
            run_turn(
                session_id,
                provider=provider,
                cwd=cwd,
                prompt=prompt,
                provider_name="fleet",
                fleet_override=fleet_roles(*roles),
            ),
            WAIT_S,
        )
    finally:
        await provider.aclose()
    return recorder, pool, cwd


# ─────────────────────────────────────────────────────────────────────────────
# 3. Subagent handoff across the artifact store
# ─────────────────────────────────────────────────────────────────────────────


class TestSubagentHandoff:
    """A plan too big to inline is handed over as a POINTER, and the pointer
    has to resolve. Before the artifact store the plan was inlined at any size,
    which spent the coder's context on the document it was meant to execute.
    """

    @pytest.fixture(autouse=True)
    def _small_inline_budget(
        self, monkeypatch: pytest.MonkeyPatch, fresh_settings: Any
    ) -> None:
        monkeypatch.setenv("ARTIFACT_INLINE_MAX_BYTES", "600")

    async def test_the_coder_is_handed_a_resolvable_artifact_pointer(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = "# Retry the uploader\n\n" + "\n".join(
            f"{i}. Step {i}: do the thing carefully." for i in range(1, 60)
        )
        subs = FakeSubProviders(
            {
                "planner": text_then_done(plan),
                "coder": text_then_done("Implemented every step."),
            }
        ).install(monkeypatch)
        model = FakeOrchestratorModel(
            [
                (DISPATCH, {"name": "planner", "prompt": "plan it"}),
                (DISPATCH, {"name": "coder", "prompt": "build it"}),
            ]
        ).install(monkeypatch)

        _recorder, _pool, cwd = await run_fleet_turn(
            isolated_store, model=model, subs=subs, roles=("planner", "coder")
        )

        assert len(plan.encode()) > 600, "the fixture has to exceed the inline budget"
        coder_prompt = subs.prompt_for("coder")
        # What the coder got is a bounded summary, not the document…
        assert len(coder_prompt) < len(plan)
        assert "Step 42" not in coder_prompt
        # …carrying the artifact that holds the document.
        artifact_paths = [
            line
            for line in coder_prompt.splitlines()
            if "artifact" in line and ".txt" in line
        ]
        assert artifact_paths, coder_prompt
        stored = Path(artifact_paths[0].split("full output at ")[1].split(" (artifact")[0])
        # Off the loop, like every other file read in this tree.
        assert await asyncio.to_thread(stored.read_text, encoding="utf-8") == plan

    async def test_the_plan_file_on_disk_is_the_whole_plan(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``save_plan`` writes ``_full_output``, not the bounded view: this
        file is the user-facing document and the coder is told to read it."""
        plan = "# Retry the uploader\n\n" + "\n".join(
            f"{i}. Step {i}: do the thing carefully." for i in range(1, 60)
        )
        subs = FakeSubProviders({"planner": text_then_done(plan)}).install(monkeypatch)
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "planner", "prompt": "plan it"})]
        ).install(monkeypatch)

        _recorder, _pool, cwd = await run_fleet_turn(
            isolated_store, model=model, subs=subs, roles=("planner", "coder")
        )

        plans = sorted((cwd / ".localcode" / "plans").glob("*.md"))
        assert len(plans) == 1
        assert plans[0].read_text(encoding="utf-8") == plan


# ─────────────────────────────────────────────────────────────────────────────
# 4. Wedged backend fast-fail
# ─────────────────────────────────────────────────────────────────────────────


class TestWedgedBackendFastFail:
    """A backend that produces NOTHING must be called out quickly and honestly.

    Honesty is the whole point of the wording: "still working" for a backend
    that has said nothing is a lie the user acts on, and ten minutes of it is
    the failure mode this window exists to cut short.
    """

    @pytest.fixture(autouse=True)
    def _tiny_grace(self, monkeypatch: pytest.MonkeyPatch, fresh_settings: Any) -> None:
        from backend.app.orchestrator.fleet import provider as fleet_provider_mod

        # The heartbeat cadence is a module constant, not a setting, and the
        # grace window is counted in heartbeats — so both have to shrink or
        # the grace window cannot be reached in a test at all.
        monkeypatch.setattr(fleet_provider_mod, "HEARTBEAT_INTERVAL_S", 0.05)
        monkeypatch.setenv("FLEET_STARTUP_GRACE_S", "0.1")
        monkeypatch.setenv("FLEET_STEP_TIMEOUT_S", "5")

    @staticmethod
    def _never_answers() -> Any:
        async def run(ctx: Any) -> Any:
            await asyncio.Event().wait()
            yield Event(type="assistant.done", data={})  # pragma: no cover

        return run

    async def test_the_step_is_abandoned_with_the_no_response_yet_wording(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subs = FakeSubProviders({None: self._never_answers()}).install(monkeypatch)
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "build it"})]
        ).install(monkeypatch)

        recorder, pool, _cwd = await run_fleet_turn(
            isolated_store, model=model, subs=subs, roles=("coder",)
        )

        heartbeats = [
            e
            for e in recorder.of("assistant.text")
            if (e.get("data") or {}).get("heartbeat")
        ]
        assert heartbeats, "a silent backend must still narrate the wait"
        assert any("no response yet" in h["data"]["text"] for h in heartbeats)
        assert not any("still working" in h["data"]["text"] for h in heartbeats)

        failed = [e for e in recorder.of("tool.result") if (e.get("data") or {}).get("is_error")]
        assert failed, recorder.types
        detail = failed[-1]["data"]["content"]
        assert "produced NO output" in detail
        assert "NOT a slow model" in detail
        # The worker holding a wedged CLI is reclaimed, not left running.
        assert pool.killed

    async def test_the_turn_ends_with_one_terminal_event_not_a_hang(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `WAIT_S` bound on the turn is half the assertion: a hang fails
        this test rather than parking the suite.

        Exactly ONE terminal event, and nothing asserted about what sits either
        side of it: the orchestrator's message pump and its dispatch sink are
        two producers on one merged queue, so a step's last card and the
        turn's terminal event are not ordered relative to each other. Pinning
        that order here would only add a flake.
        """
        subs = FakeSubProviders({None: self._never_answers()}).install(monkeypatch)
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "build it"})]
        ).install(monkeypatch)

        recorder, _pool, _cwd = await run_fleet_turn(
            isolated_store, model=model, subs=subs, roles=("coder",)
        )

        assert recorder.types.count("assistant.done") == 1
        assert "assistant.done" in recorder.types

    async def test_a_second_wedged_dispatch_is_refused_outright(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hard-fail cap. Without it a wedged role is re-dispatched until
        ``max_turns``, at a step budget each."""
        subs = FakeSubProviders({None: self._never_answers()}).install(monkeypatch)
        model = FakeOrchestratorModel(
            [
                (DISPATCH, {"name": "coder", "prompt": "build it"}),
                (DISPATCH, {"name": "coder", "prompt": "build it again"}),
                (DISPATCH, {"name": "coder", "prompt": "one more time"}),
            ]
        ).install(monkeypatch)

        await run_fleet_turn(isolated_store, model=model, subs=subs, roles=("coder",))

        texts = [
            "\n".join(b.get("text", "") for b in (result.get("content") or []))
            for _tool, _args, result in model.calls
        ]
        assert "did not respond" in texts[0]
        assert "STOP" in texts[1]
        # The third dispatch never reached a backend at all.
        assert "REFUSING to dispatch" in texts[2]
        assert len(subs.contexts) == 2, "a refused dispatch must not run a step"

    async def test_an_aborting_orchestrator_still_closes_the_turn_out(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other end of the same failure: the model loop gives up after the
        refusal. The turn owes the user an ``error`` AND the one terminal
        event, and a spinner that never clears is the worse of the two."""
        subs = FakeSubProviders({None: self._never_answers()}).install(monkeypatch)
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "build it"})],
            raise_after="did not respond",
        ).install(monkeypatch)

        recorder, _pool, _cwd = await run_fleet_turn(
            isolated_store, model=model, subs=subs, roles=("coder",)
        )

        assert recorder.of("error"), recorder.types
        assert "orchestrator aborted" in recorder.of("error")[-1]["data"]["message"]
        assert recorder.types.count("assistant.done") == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. Context eviction
# ─────────────────────────────────────────────────────────────────────────────


class TestContextEviction:
    """Two megabytes of tool output must not come back as context.

    It used to: a step returned its whole transcript, so a coder that `cat`ed a
    test log spent the rest of the turn's window on it — and changed the prompt
    prefix, so the cache the rest of the harness keeps warm went cold for every
    later turn too.
    """

    NEEDLE = "SENTINEL-DEEP-INSIDE-THE-LOG"

    @staticmethod
    def _two_megabyte_tool_result(needle: str) -> Any:
        blob = ("filler line that says nothing\n" * 40_000) + needle + "\n" + (
            "more filler\n" * 20_000
        )

        async def run(ctx: Any) -> Any:
            yield Event(
                type="assistant.tool_use",
                data={"id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
            )
            yield Event(
                type="tool.result",
                data={"tool_use_id": "t1", "content": blob, "is_error": False},
            )
            yield Event(type="assistant.text", data={"text": "The suite passed."})
            yield Event(type="assistant.done", data={"usage": {"input_tokens": 10}})

        return run

    async def test_the_orchestrator_never_sees_the_megabytes(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subs = FakeSubProviders(
            {None: self._two_megabyte_tool_result(self.NEEDLE)}
        ).install(monkeypatch)
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "run the tests"})]
        ).install(monkeypatch)

        await run_fleet_turn(isolated_store, model=model, subs=subs, roles=("coder",))

        _tool, _args, result = model.calls[0]
        context = "\n".join(b.get("text", "") for b in (result.get("content") or []))

        assert self.NEEDLE not in context
        # The digest cap is 4000 chars and the narrative is one sentence; an
        # exact bound would only pin the formatting, so this bounds the order
        # of magnitude the eviction rule promises.
        assert len(context) < 10_000, len(context)
        assert "The suite passed." in context

    async def test_the_step_still_reports_what_the_tool_did(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eviction is not silence: the gate roles verify a worker's claims
        against its tool activity, so the digest has to survive the trim."""
        subs = FakeSubProviders(
            {None: self._two_megabyte_tool_result(self.NEEDLE)}
        ).install(monkeypatch)
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "coder", "prompt": "run the tests"})]
        ).install(monkeypatch)

        await run_fleet_turn(isolated_store, model=model, subs=subs, roles=("coder",))

        _tool, _args, result = model.calls[0]
        context = "\n".join(b.get("text", "") for b in (result.get("content") or []))

        assert "pytest -q" in context
