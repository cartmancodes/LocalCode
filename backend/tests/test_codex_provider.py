"""`CodexProvider` end to end, against the fake app-server.

The claim this task makes is not "LocalCode can talk to Codex" — the OpenCode
provider could already do that badly. It is that **Codex is behind the same
harness Claude is**, and that is only true if these hold:

  * the item stream becomes the same Events, in order, ending in exactly one
    ``assistant.done`` carrying the thread id and the token counts;
  * ``execCommandApproval`` / ``applyPatchApproval`` raise the SAME
    ``pipeline.awaiting_approval`` card Claude's tools raise, through the same
    ``evaluate_tool_request``, so a role that may not execute commands denies
    a Codex command without ever asking a human;
  * turn 2's card lands on turn 2's sink. The app-server outlives the turn,
    so an approval handler that captured turn 1's sink and queue would leave
    every later approval in the workspace to time out silently — the single
    most expensive bug available in this design, and the reason
    ``_TurnBinding`` exists;
  * failures are Events, not exceptions: a silent turn, a crashed server and
    a missing binary each reach the user as one ``error`` that says what to do;
  * the process group dies with the provider.

Nothing here needs the `codex` CLI. The one test that does is marked
``requires_cli`` and skips without it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import Settings, get_settings
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.codex import CodexBroker, CodexProvider
from backend.app.orchestrator.codex.client import CodexAppServer
from backend.app.orchestrator.codex.protocol import (
    DECISION_APPROVED,
    DECISION_DENIED,
    F_DECISION,
    R_EXEC_APPROVAL,
)
from backend.app.orchestrator.fleet.constants import VALID_PROVIDERS
from backend.app.schemas import CreateSessionRequest

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_codex_app_server.py"

# Generous enough for a cold interpreter start on a loaded machine, short
# enough that a genuinely stuck turn fails the test instead of the suite.
WAIT_S = 30.0


@pytest.fixture(autouse=True)
def _codex_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Short approval timeout, and no API key anywhere near the child.

    The timeout matters for the rebinding test: with the production 300s, a
    handler that published turn 2's card onto turn 1's dead sink would hang
    the suite instead of failing it.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("TOOL_APPROVAL_TIMEOUT_S", "5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_until_dead(pid: int, label: str, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if not _alive(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{label} (pid {pid}) survived the kill")


def _same_dir(left: str | Path, right: str | Path) -> bool:
    """Path comparison, resolved. Sync because ``resolve()`` touches the disk
    and the house rule keeps that off the event loop."""
    return Path(left).resolve() == Path(right).resolve()


def _under(root: Path, name: str) -> str:
    return str((root / name).resolve())


def _records(path: Path, kind: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        entry
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for entry in [json.loads(line)]
        if entry.get("kind") == kind
    ]


@pytest.fixture
async def provider_factory(tmp_path: Path) -> AsyncIterator[Any]:
    """Builds providers over the fake and guarantees every child is reaped."""
    built: list[CodexProvider] = []
    pgids: list[int] = []

    def factory(scenario: str, *, record: Path | None = None, **kwargs: Any) -> CodexProvider:
        argv = [sys.executable, str(FAKE), scenario]
        if record is not None:
            argv.append(str(record))
        broker = CodexBroker(
            argv=argv, startup_timeout_s=WAIT_S, request_timeout_s=WAIT_S, **kwargs
        )
        provider = CodexProvider(broker=broker)
        built.append(provider)
        return provider

    try:
        yield factory
    finally:
        for provider in built:
            for server in list(getattr(provider._broker, "_servers", {}).values()):
                pid = server.pid
                if pid is not None:
                    with contextlib.suppress(OSError):
                        pgids.append(os.getpgid(pid))
            with contextlib.suppress(Exception):
                await provider.aclose()
        # Belt and braces: the assertion this suite makes about process groups
        # is worthless if a failing test leaves one behind for the next.
        for pgid in pgids:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)


def mk_ctx(tmp_path: Path, **overrides: Any) -> RunContext:
    defaults: dict[str, Any] = dict(
        model="gpt-5.3-codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        session_id="sess-1",
    )
    defaults.update(overrides)
    return RunContext(**defaults)


async def drain(
    provider: CodexProvider, ctx: RunContext, *, answer: str | None = None
) -> list[Event]:
    """Run one turn to completion, optionally answering the first card."""

    async def _run() -> list[Event]:
        events: list[Event] = []
        async for ev in provider.run(ctx):
            events.append(ev)
            if ev.type == "pipeline.awaiting_approval" and answer is not None:
                assert ctx.approval_channel is not None
                ctx.approval_channel.put_nowait({"id": ev.data["id"], "value": answer})
        return events

    return await asyncio.wait_for(_run(), WAIT_S)


def types(events: list[Event]) -> list[str]:
    return [ev.type for ev in events]


async def _collect(agen: Any) -> list[Event]:
    """Whatever is left of an already-started turn."""
    return [ev async for ev in agen]


class TestHappyTurn:
    async def test_the_event_sequence_is_exactly_the_translation(
        self, provider_factory, tmp_path: Path
    ) -> None:
        provider = provider_factory("happy")
        events = await drain(provider, mk_ctx(tmp_path))

        # reasoning text, the command's tool_use/tool.result pair, the two
        # halves of the streamed message, then the single terminal event.
        assert types(events) == [
            "assistant.text",
            "assistant.tool_use",
            "tool.result",
            "assistant.text",
            "assistant.text",
            "assistant.done",
        ]
        assert events[0].data["text"] == "Considering the request."
        assert events[1].data["name"] == "Bash"
        assert events[1].data["input"] == {"command": "echo hi"}
        assert events[2].data["content"] == "hi\n"
        assert events[2].data["is_error"] is False
        # The streamed update and the completed item must not repeat the text.
        assert "".join(ev.data["text"] for ev in events[3:5]) == "Hello, world."

    async def test_a_completed_turn_is_one_row_in_the_usage_log(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """``GET /api/system/usage`` IS ``usage.jsonl``, and only the Claude
        provider ever appended to it — so a Codex-heavy day read as an idle
        one, while docs/architecture.md promised "one JSON line per turn".

        HOME is redirected for every test in this suite (conftest), so this
        asserts against the real default path, not an injected one.
        """
        provider = provider_factory("happy")
        events = await drain(provider, mk_ctx(tmp_path))
        assert types(events)[-1] == "assistant.done"

        log = tmp_path / ".localcode" / "usage.jsonl"
        rows = [
            json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line
        ]

        assert len(rows) == 1
        row = rows[0]
        assert row["provider"] == "codex"
        assert row["model"] == "gpt-5.3-codex"
        assert row["session_id"] == "sess-1"
        assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
        assert row["cache_read_tokens"] == 4
        # UsageLog.recent() selects on ts, so a zero here is a row that is
        # written and never reported.
        assert row["ts"] > 0
        # The same numbers the viewer was shown: one measurement, two readers.
        assert events[-1].data["usage"] == row

    async def test_an_errored_turn_is_not_metered(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """A turn that produced no response body produced no counts either.

        Logging zeros for it would understate nothing and overstate a turn
        that happened; the honest record is no row at all, which is what
        docs/harness.md §3 now says.
        """
        provider = provider_factory("silent")
        events = await drain(provider, mk_ctx(tmp_path))

        assert types(events) == ["error"]
        assert not (tmp_path / ".localcode" / "usage.jsonl").exists()

    async def test_done_carries_the_thread_id_and_the_token_counts(
        self, provider_factory, tmp_path: Path
    ) -> None:
        provider = provider_factory("happy")
        events = await drain(provider, mk_ctx(tmp_path))

        done = events[-1]
        assert done.type == "assistant.done"
        # The thread id is what the next turn resumes with; losing it starts a
        # new conversation on every message.
        assert done.data["upstream_session_id"] == "thread-fake-1"
        usage = done.data["usage"]
        assert usage["input_tokens"] == 11
        assert usage["output_tokens"] == 7
        assert usage["cache_read_tokens"] == 4
        # Absent in the payload, present as a zero: Task 11's reader indexes
        # these keys and a missing one is a crash there, not an empty cell.
        assert usage["cache_creation_tokens"] == 0
        assert usage["provider"] == "codex"

    async def test_exactly_one_terminal_event_per_turn(
        self, provider_factory, tmp_path: Path
    ) -> None:
        provider = provider_factory("happy")
        events = await drain(provider, mk_ctx(tmp_path))
        assert types(events).count("assistant.done") == 1
        assert "error" not in types(events)

    async def test_the_spawn_environment_carries_no_api_key(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "happy.jsonl"
        provider = provider_factory("happy", record=record)
        await drain(provider, mk_ctx(tmp_path))

        env = _records(record, "env")
        assert len(env) == 1
        # THE INVARIANT: LocalCode spawns `codex` and lets it find its own
        # OAuth token. It never invents a key for the child, and there is no
        # fallback path that would.
        assert "OPENAI_API_KEY" not in env[0]["secretish"]
        assert env[0]["secretish"] == []
        # And it runs in the workspace it was asked for.
        assert _same_dir(env[0]["cwd"], tmp_path)

    async def test_a_created_file_is_a_write_not_an_edit(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """The Write branch, which no other scenario reaches.

        Every other file-touching scenario reports ``kind: "update"``, so
        without this the mapping from a creation to ``Write`` was an
        unasserted claim — and the transcript would label a new file as an
        edit of a file that did not exist.
        """
        provider = provider_factory("file_create")
        events = await drain(provider, mk_ctx(tmp_path))

        assert types(events) == [
            "assistant.tool_use",
            "tool.result",
            "assistant.text",
            "assistant.done",
        ]
        assert events[0].data["name"] == "Write"
        assert events[0].data["input"]["file_path"] == _under(tmp_path, "new_module.py")
        assert events[0].data["input"]["paths"] == [_under(tmp_path, "new_module.py")]

    async def test_additional_dirs_are_forwarded_to_thread_start(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "dirs.jsonl"
        sibling = tmp_path.parent / "sibling"
        sibling.mkdir(exist_ok=True)
        provider = provider_factory("happy", record=record)

        await drain(provider, mk_ctx(tmp_path, additional_dirs=[str(sibling)]))

        starts = [r for r in _records(record, "request") if r["method"] == "thread/start"]
        # Half the reason this provider exists: OpenCode binds a session to one
        # project directory and could never be handed a sibling repo.
        assert starts[0]["params"]["additionalDirectories"] == [str(sibling)]


class TestApprovals:
    async def test_a_command_approval_raises_the_shared_card_and_yes_completes_the_turn(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "approval.jsonl"
        provider = provider_factory("approval", record=record)
        ctx = mk_ctx(tmp_path, approval_channel=asyncio.Queue())

        events = await drain(provider, ctx, answer="yes")

        assert types(events) == [
            "pipeline.awaiting_approval",
            "pipeline.approval_received",
            "assistant.tool_use",
            "tool.result",
            "assistant.text",
            "assistant.done",
        ]
        card = events[0]
        # The SAME card Claude's Bash raises: same type, same kind, same tool
        # name, from the same evaluate_tool_request.
        assert card.data["kind"] == "tool"
        assert card.data["tool"] == "Bash"
        assert card.data["input"]["command"] == "rm -rf build"
        assert card.data["id"].startswith("approval.tool.")
        decisions = _records(record, "decision")
        assert [d["decision"] for d in decisions] == ["approved"]
        assert events[3].data["is_error"] is False

    async def test_no_denies_the_command_and_the_provider_surfaces_the_refusal(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "approval.jsonl"
        provider = provider_factory("approval", record=record)
        ctx = mk_ctx(tmp_path, approval_channel=asyncio.Queue())

        events = await drain(provider, ctx, answer="no")

        assert [d["decision"] for d in _records(record, "decision")] == ["denied"]
        # The refused command still reaches the transcript as a failed tool
        # call — the fake never sent an item/started for it, so the pair is
        # synthesized rather than left dangling.
        result = next(ev for ev in events if ev.type == "tool.result")
        assert result.data["is_error"] is True
        assert "rejected" in result.data["content"]
        assert types(events)[-1] == "assistant.done"

    async def test_a_patch_approval_maps_to_edit_with_the_changed_paths(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "patch.jsonl"
        provider = provider_factory("patch_approval", record=record)
        ctx = mk_ctx(tmp_path, approval_channel=asyncio.Queue())

        events = await drain(provider, ctx, answer="yes")

        card = events[0]
        assert card.type == "pipeline.awaiting_approval"
        assert card.data["tool"] == "Edit"
        target = _under(tmp_path, "notes.md")
        assert target in card.data["input"]["file_path"]
        assert [d["decision"] for d in _records(record, "decision")] == ["approved"]
        assert types(events)[-1] == "assistant.done"

    async def test_a_role_that_may_not_execute_denies_without_asking_anyone(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """The whole point of one approval bus: the role table binds Codex too.

        ``planner`` is read-only in ``permissions._ROLE_DENY_TOOLS``, so the
        shared decision refuses the command outright — no card, no human, and
        the same reason a Claude planner would be refused for.
        """
        record = tmp_path / "role.jsonl"
        provider = provider_factory("approval", record=record)
        ctx = mk_ctx(tmp_path, role="planner", approval_channel=asyncio.Queue())

        events = await drain(provider, ctx, answer="yes")

        assert [d["decision"] for d in _records(record, "decision")] == ["denied"]
        assert "pipeline.awaiting_approval" not in types(events)

    async def test_turn_twos_card_lands_on_turn_twos_sink(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """THE rebinding hazard, stated as a test.

        One app-server serves the whole workspace, and the approval handler is
        registered on it once. A handler that captured turn 1's sink and queue
        would publish turn 2's card where nobody is listening and then wait on
        a queue nobody writes to — every approval after the first turn of a
        workspace would time out and deny, silently.
        """
        record = tmp_path / "two-turns.jsonl"
        provider = provider_factory("approval", record=record)

        first_channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        turn_one = await drain(
            provider, mk_ctx(tmp_path, approval_channel=first_channel), answer="yes"
        )
        thread_id = turn_one[-1].data["upstream_session_id"]

        # A fresh queue, as SessionRunner builds one for every turn.
        second_channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        turn_two = await drain(
            provider,
            mk_ctx(
                tmp_path,
                prompt="and again",
                upstream_session_id=thread_id,
                approval_channel=second_channel,
            ),
            answer="yes",
        )

        assert types(turn_two)[0] == "pipeline.awaiting_approval"
        assert types(turn_two)[-1] == "assistant.done"
        assert [d["decision"] for d in _records(record, "decision")] == ["approved", "approved"]
        # Turn 1's queue was never touched by turn 2 — the answer it consumed
        # was its own, and nothing was asked of it afterwards.
        assert first_channel.empty()
        # One process served both turns: the hazard is only real because the
        # server outlives the turn, so a test that spawned two would prove
        # nothing.
        assert len(_records(record, "env")) == 1

    async def test_an_approval_with_no_turn_in_flight_is_denied_not_auto_approved(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """A Stop is not a yes.

        The app-server outlives the turn, ``turn_interrupt`` is best effort,
        and the server is free to ask about the command already in flight a
        moment after the turn's reader has gone. The shared gate's headless
        branch ALLOWS ``Bash`` when the role's policy grants exec — and every
        interactive session resolves ``policy_for_role(None, ...)``, which
        does — so a handler that let a turn-less request fall through to it
        would answer that question with ``approved``: the user's Stop read
        back as consent to ``rm -rf``.
        """
        provider = provider_factory("happy")
        ctx = mk_ctx(tmp_path, approval_channel=asyncio.Queue())
        await drain(provider, ctx)  # a completed turn: the binding is cleared

        server = provider._broker._servers[str(tmp_path)]
        # The real path a stray request takes: the client's RPC dispatch, the
        # provider's real handler, the real evaluate_tool_request behind it.
        answer = await asyncio.wait_for(
            server._make_approval_handler(R_EXEC_APPROVAL)(
                {
                    "threadId": "thread-after-the-turn",
                    "callId": "stray-1",
                    "command": ["rm", "-rf", _under(tmp_path, "build")],
                    "cwd": str(tmp_path),
                }
            ),
            WAIT_S,
        )

        assert answer == {F_DECISION: DECISION_DENIED}
        # Denied without asking anyone: there is no sink a card could reach.
        assert ctx.approval_channel is not None
        assert ctx.approval_channel.empty()

    async def test_a_turns_teardown_does_not_unbind_the_turn_that_replaced_it(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """The binding is per-WORKSPACE; ``turn_lock`` is per-SERVER.

        So when the broker replaces a crashed app-server, turn B does not queue
        behind turn A's lock: it rebinds the workspace's binding while A is
        still in teardown. A ``finally`` that cleared the binding
        unconditionally would then hand B's next approval to the turn-less deny
        above — for a turn that is very much alive and has a human attached.
        """
        record = tmp_path / "rebind.jsonl"
        provider = provider_factory("approval", record=record)
        workspace = str(tmp_path)

        channel_a: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        gen_a = provider.run(mk_ctx(tmp_path, approval_channel=channel_a)).__aiter__()
        card_a = await asyncio.wait_for(gen_a.__anext__(), WAIT_S)
        assert card_a.type == "pipeline.awaiting_approval"

        binding = provider._bindings[workspace]
        # Exactly the window the ledger describes: the server turn A is running
        # on is forgotten (as a crash-replacement forgets it), so turn B spawns
        # its own and binds under a different lock.
        server_a = provider._broker._servers.pop(workspace)
        try:
            channel_b: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            gen_b = provider.run(
                mk_ctx(tmp_path, prompt="turn B", approval_channel=channel_b)
            ).__aiter__()
            card_b = await asyncio.wait_for(gen_b.__anext__(), WAIT_S)
            assert card_b.type == "pipeline.awaiting_approval"

            # Turn A tears down now, with B bound.
            await asyncio.wait_for(gen_a.aclose(), WAIT_S)
            assert binding.sink is not None

            # Behaviourally: an approval arriving after A's teardown still
            # reaches turn B's sink and is answerable by turn B's human.
            server_b = provider._broker._servers[workspace]
            probe = asyncio.create_task(
                server_b._make_approval_handler(R_EXEC_APPROVAL)(
                    {
                        "threadId": "thread-b",
                        "callId": "probe",
                        "command": ["ls", "-la"],
                        "cwd": workspace,
                    }
                )
            )
            probe_card = await asyncio.wait_for(gen_b.__anext__(), WAIT_S)
            assert probe_card.type == "pipeline.awaiting_approval"
            assert probe_card.data["input"]["command"] == "ls -la"
            channel_b.put_nowait({"id": probe_card.data["id"], "value": "yes"})
            assert await asyncio.wait_for(probe, WAIT_S) == {F_DECISION: DECISION_APPROVED}

            # And B's own card still completes its turn.
            channel_b.put_nowait({"id": card_b.data["id"], "value": "yes"})
            tail = await asyncio.wait_for(_collect(gen_b), WAIT_S)
            assert types(tail)[-1] == "assistant.done"
        finally:
            await server_a.close()


class TestFailures:
    async def test_a_silent_turn_is_an_error_not_a_bare_done(
        self, provider_factory, tmp_path: Path
    ) -> None:
        provider = provider_factory("silent")
        events = await drain(provider, mk_ctx(tmp_path))

        # A turn with no response body reported as a success is an empty
        # assistant bubble and a user with no idea anything went wrong.
        assert types(events) == ["error"]
        assert "without producing a response" in events[0].data["message"]
        assert events[0].data["provider"] == "codex"

    async def test_a_crash_mid_turn_is_an_error_naming_the_exit(
        self, provider_factory, tmp_path: Path
    ) -> None:
        provider = provider_factory("crash")
        events = await drain(provider, mk_ctx(tmp_path))

        assert types(events) == ["assistant.tool_use", "error"]
        message = events[-1].data["message"]
        assert "exited with code 3" in message
        # Never a done: the turn did not finish.
        assert "assistant.done" not in types(events)

    async def test_a_crashed_server_is_replaced_rather_than_reused(
        self, provider_factory, tmp_path: Path
    ) -> None:
        """A dead app-server must not be handed to the next turn.

        It would be written to, answered by nobody, and time out — one crash
        would wedge every later turn in that workspace.
        """
        record = tmp_path / "crash.jsonl"
        provider = provider_factory("crash", record=record)

        first = await drain(provider, mk_ctx(tmp_path))
        second = await drain(provider, mk_ctx(tmp_path))

        assert types(first)[-1] == "error"
        assert types(second)[-1] == "error"
        # Two processes: the second turn got a fresh one instead of the corpse.
        assert len(_records(record, "env")) == 2

    async def test_a_missing_binary_is_one_error_naming_the_binary_and_codex_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_BINARY", "definitely-not-a-real-binary")
        get_settings.cache_clear()
        # The real broker, resolving argv from settings: the missing-binary
        # path has to work for the object the app actually builds.
        provider = CodexProvider()
        try:
            events = await drain(provider, mk_ctx(tmp_path))
        finally:
            await provider.aclose()

        assert types(events) == ["error"]
        message = events[0].data["message"]
        assert "definitely-not-a-real-binary" in message
        assert "codex login" in message
        # Never a fallback to a key, and never an exception through the WS.
        assert "API key" in message

    async def test_a_busy_app_server_is_retried_rather_than_surfaced(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "busy.jsonl"
        provider = provider_factory("busy", record=record)

        events = await drain(provider, mk_ctx(tmp_path))

        # The user sees a slower turn, not a failed one.
        assert types(events)[-1] == "assistant.done"
        assert "error" not in types(events)
        starts = [r for r in _records(record, "request") if r["method"] == "turn/start"]
        assert len(starts) == 2


class TestBroker:
    async def test_two_open_sessions_in_one_workspace_share_one_process(
        self, provider_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "broker.jsonl"
        provider = provider_factory("happy", record=record)

        first = await provider.open_session(mk_ctx(tmp_path))
        second = await provider.open_session(mk_ctx(tmp_path, session_id="sess-2"))

        assert first == second == "thread-fake-1"
        # Cold start is a spawn plus a handshake; paying it per session is
        # what the broker exists to avoid.
        assert len(_records(record, "env")) == 1
        assert len(provider._broker._servers) == 1

    async def test_aclose_leaves_no_process_behind(
        self, provider_factory, tmp_path: Path
    ) -> None:
        provider = provider_factory("happy")
        await provider.open_session(mk_ctx(tmp_path))
        server = next(iter(provider._broker._servers.values()))
        pid = server.pid
        assert pid is not None and _alive(pid)

        await provider.aclose()

        await _wait_until_dead(pid, "the app-server")
        assert not _alive(pid), "the app-server survived aclose()"


class TestWiring:
    def test_codex_is_a_provider_everywhere_a_provider_is_named(self) -> None:
        # A provider the API accepts but the fleet rejects (or vice versa) is a
        # 422 nobody can explain, so the names are asserted together.
        assert CreateSessionRequest(provider="codex", model="gpt-5.3-codex").provider == "codex"
        assert "codex" in VALID_PROVIDERS
        assert Settings(default_provider="codex").default_provider == "codex"
        # The claim is about the catalog this repo *ships*, so read the field's
        # default rather than an ambient ``Settings()`` — that one merges the
        # developer's own .env, where MODEL_CATALOG is routinely overridden.
        shipped = Settings(model_catalog=Settings.model_fields["model_catalog"].default)
        assert any(entry.provider == "codex" for entry in shipped.catalog())
        assert CodexProvider.name == "codex"

    async def test_the_registry_builds_it(self) -> None:
        from backend.app.orchestrator import registry

        provider = await registry.get_provider("codex")
        assert isinstance(provider, CodexProvider)
        assert "codex" in registry.PROVIDER_NAMES


@pytest.mark.requires_cli
@pytest.mark.skipif(shutil.which("codex") is None, reason="the codex CLI is not on PATH")
async def test_the_real_app_server_completes_its_handshake(tmp_path: Path) -> None:
    """The one test that needs the binary. Everything above is the fake.

    It asserts only the handshake and a thread id: anything further would cost
    a real turn against a real subscription. If the protocol has moved, this
    is the test that says so — reconcile ``protocol.py`` against
    ``make codex-schema`` and update the fake alongside it.
    """
    server = CodexAppServer(workspace=str(tmp_path), startup_timeout_s=60.0)
    try:
        await server.start()
        thread_id = await server.thread_start(str(tmp_path), [])
        assert thread_id
    finally:
        await server.close()
