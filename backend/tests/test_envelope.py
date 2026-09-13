"""Step envelopes: one step's output must not be able to eat the context window.

Before Task 6 a step returned its whole transcript — narrative plus an uncapped
tool digest — straight into the orchestrator's context, so a coder that `cat`ed
a 2 MB test log cost the rest of the turn. These tests hold the two halves of
the fix: ``StepResult.context_text()`` is the only thing that decides what an
orchestrator reads, and ``collect_step`` evicts an oversized body to the
artifact store instead of inlining it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.app.artifacts import ArtifactStore
from backend.app.orchestrator import registry as registry_mod
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.fleet import provider as provider_mod
from backend.app.orchestrator.fleet.collect import collect_step, collect_text
from backend.app.orchestrator.fleet.envelope import MAX_TOOL_DIGEST_CHARS, StepResult
from backend.app.orchestrator.fleet.models import RoleConfig, Step

ROLE = RoleConfig(provider="claude", model="sonnet", system_prompt="be terse")


class _StubProvider:
    """Stands in for a sub-provider: replays a fixed event list, records the
    ``RunContext`` it was handed, and notes that it was closed."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events
        self.ctx = None
        self.closed = False

    async def run(self, ctx):  # noqa: ANN001 - mirrors the Provider protocol
        self.ctx = ctx
        for ev in self._events:
            yield ev

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    """Install a stub sub-provider. ``collect_step`` imports
    ``_build_provider`` from the registry module at call time, so patching the
    module attribute is enough."""

    def _install(events: list[Event]) -> _StubProvider:
        provider = _StubProvider(events)
        monkeypatch.setattr(registry_mod, "_build_provider", lambda name: provider)
        return provider

    return _install


def _text(body: str) -> Event:
    return Event(type="assistant.text", data={"text": body})


class TestWireRoundTrip:
    def test_every_field_survives(self) -> None:
        result = StepResult(
            summary="the summary",
            structured={"value": "lgtm", "reason": "fine", "source": "json"},
            artifact_id="a" * 64,
            artifact_path="/tmp/aa/aaa.txt",
            tool_digest="(tool activity from claude:sonnet)\n- Read input={}",
            full_bytes=1234,
            usage={"input_tokens": 10, "output_tokens": 3},
        )

        assert StepResult.from_wire(result.to_wire()) == result

    def test_nones_survive_as_nones(self) -> None:
        """A JSON ``null`` must not come back as the string ``"None"`` — a
        truthy artifact_id would make ``context_text`` advertise an artifact
        that does not exist."""
        result = StepResult(
            summary="",
            structured=None,
            artifact_id=None,
            artifact_path=None,
            tool_digest="",
            full_bytes=0,
        )

        back = StepResult.from_wire(result.to_wire())

        assert back == result
        assert back.artifact_id is None
        assert back.usage is None

    def test_a_truncated_payload_degrades_instead_of_raising(self) -> None:
        """The parent's stdout pump calls this; a KeyError there would be
        reported as the useless "worker exited without a result"."""
        back = StepResult.from_wire({"summary": "partial"})

        assert back.summary == "partial"
        assert back.full_bytes == 0
        assert back.structured is None
        assert back.tool_digest == ""

    def test_junk_typed_fields_are_coerced(self) -> None:
        back = StepResult.from_wire(
            {"summary": "s", "full_bytes": "nope", "structured": ["not", "a", "dict"]}
        )

        assert back.full_bytes == 0
        assert back.structured is None


class TestContextText:
    def test_a_small_output_is_verbatim(self) -> None:
        result = StepResult(
            summary="Changes: wrote one file.",
            structured=None,
            artifact_id=None,
            artifact_path=None,
            tool_digest="",
            full_bytes=24,
        )

        assert result.context_text() == "Changes: wrote one file."

    def test_a_digest_is_appended_under_the_marker_the_gate_strips(self) -> None:
        from backend.app.orchestrator.fleet.gate import TOOL_DIGEST_MARKER

        result = StepResult(
            summary="LGTM",
            structured=None,
            artifact_id=None,
            artifact_path=None,
            tool_digest="(tool activity from claude:sonnet)\n- Bash input={}",
            full_bytes=4,
        )

        text = result.context_text()

        assert TOOL_DIGEST_MARKER in text
        assert text.startswith("LGTM")
        assert "- Bash input={}" in text

    def test_a_digest_only_step_returns_just_the_digest(self) -> None:
        result = StepResult(
            summary="",
            structured=None,
            artifact_id=None,
            artifact_path=None,
            tool_digest="(tool activity from claude:sonnet)\n- Write input={}",
            full_bytes=0,
        )

        assert result.context_text() == (
            "(tool activity from claude:sonnet)\n- Write input={}"
        )

    def test_an_empty_step_is_empty(self) -> None:
        result = StepResult(
            summary="",
            structured=None,
            artifact_id=None,
            artifact_path=None,
            tool_digest="",
            full_bytes=0,
        )

        assert result.context_text() == ""

    def test_an_evicted_output_carries_a_pointer_line(self) -> None:
        result = StepResult(
            summary="head…\n… [truncated]\n…tail",
            structured=None,
            artifact_id="f" * 64,
            artifact_path="/artifacts/ff/ffff.txt",
            tool_digest="",
            full_bytes=2_000_000,
        )

        text = result.context_text()

        assert text.startswith("head…")
        assert "2000000 bytes" in text
        assert "f" * 12 in text
        assert "/artifacts/ff/ffff.txt" in text

    def test_an_oversized_digest_is_capped_even_from_a_foreign_envelope(self) -> None:
        result = StepResult(
            summary="fine",
            structured=None,
            artifact_id=None,
            artifact_path=None,
            tool_digest="x" * 50_000,
            full_bytes=4,
        )

        text = result.context_text()

        assert len(text) < MAX_TOOL_DIGEST_CHARS + 200
        assert "digest truncated" in text


class TestCollectStepEviction:
    async def test_a_two_megabyte_body_is_evicted_to_an_artifact(
        self, stub, tmp_localcode: Path
    ) -> None:
        body = "A" * 2_000_000
        provider = stub([_text(body), Event(type="assistant.done", data={})])

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.full_bytes == len(body.encode("utf-8"))
        assert result.artifact_id is not None
        # The whole point: what reaches context is kilobytes, not megabytes.
        assert len(result.summary) < 10_000
        context = result.context_text()
        assert len(context) < 10_000
        assert result.artifact_id[:12] in context
        # ...and the full body is still recoverable from the store.
        assert result.artifact_path is not None
        assert ArtifactStore().get_text(result.artifact_id) == body
        assert tmp_localcode in Path(result.artifact_path).parents
        assert provider.closed

    async def test_a_small_body_is_not_evicted(self, stub) -> None:
        stub([_text("Changes: edited one file.")])

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.artifact_id is None
        assert result.artifact_path is None
        assert result.summary == "Changes: edited one file."
        assert result.full_bytes == len("Changes: edited one file.")


class TestCollectStepDigest:
    async def test_the_tool_digest_is_capped_and_says_how_much_it_dropped(
        self, stub
    ) -> None:
        events: list[Event] = []
        for i in range(400):
            events.append(
                Event(
                    type="assistant.tool_use",
                    data={"id": f"t{i}", "name": "Bash", "input": {"command": "ls -la"}},
                )
            )
            events.append(
                Event(
                    type="tool.result",
                    data={"tool_use_id": f"t{i}", "content": "a file listing"},
                )
            )
        stub([_text("done"), *events])

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert len(result.tool_digest) <= MAX_TOOL_DIGEST_CHARS + 40
        assert "more tool calls" in result.tool_digest
        assert result.tool_digest.startswith("(tool activity from claude:sonnet)")

    async def test_a_tool_only_step_still_reports_what_it_did(self, stub) -> None:
        stub(
            [
                Event(
                    type="assistant.tool_use",
                    data={"id": "t1", "name": "Write", "input": {"path": "a.py"}},
                ),
                Event(
                    type="tool.result",
                    data={"tool_use_id": "t1", "content": "ok", "is_error": False},
                ),
            ]
        )

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.summary == ""
        assert "Write" in result.tool_digest
        assert "Write" in result.context_text()

    async def test_tool_result_content_blocks_are_flattened(self, stub) -> None:
        stub(
            [
                Event(
                    type="assistant.tool_use",
                    data={"id": "t1", "name": "Read", "input": None},
                ),
                Event(
                    type="tool.result",
                    data={
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "file body"}],
                        "is_error": True,
                    },
                ),
            ]
        )

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert "[ERR] file body" in result.tool_digest


class TestCollectStepStructured:
    async def test_a_gate_role_carries_its_parsed_verdict(self, stub) -> None:
        stub([_text('NACK: task 3 missing\n\n```json\n{"verdict": "nack", "reason": "t3"}\n```')])

        result = await collect_step(ROLE, "go", None, role_name="reviewer")

        assert result.structured == {"value": "nack", "reason": "t3", "source": "json"}

    async def test_a_chatty_tester_is_classified_from_its_line(self, stub) -> None:
        stub([_text("Tests:\n- test_x — PASS\n\nLGTM\nThanks!")])

        result = await collect_step(ROLE, "go", None, role_name="tester")

        assert result.structured is not None
        assert result.structured["value"] == "lgtm"
        assert result.structured["source"] == "line"

    async def test_a_non_gate_role_has_no_structured_verdict(self, stub) -> None:
        stub([_text("LGTM")])

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.structured is None

    async def test_an_evicted_gate_output_is_still_classified_on_the_full_text(
        self, stub
    ) -> None:
        """The verdict is parsed BEFORE summarizing — otherwise a long review
        would lose its verdict to the head/tail trim and fail safe for no
        reason."""
        stub([_text("B" * 2_000_000 + '\n\n```json\n{"verdict": "lgtm", "reason": "ok"}\n```')])

        result = await collect_step(ROLE, "go", None, role_name="reviewer")

        assert result.artifact_id is not None
        assert result.structured == {"value": "lgtm", "reason": "ok", "source": "json"}


class TestCollectStepUsage:
    async def test_usage_is_taken_from_the_sub_providers_done_event(self, stub) -> None:
        stub(
            [
                _text("done"),
                Event(
                    type="assistant.done",
                    data={
                        "cost_usd": 0.12,
                        "usage": {
                            "provider": "claude",
                            "model": "sonnet",
                            "input_tokens": 120,
                            "output_tokens": 30,
                            "cache_read_tokens": 900,
                            "cache_creation_tokens": 7,
                            "cost_usd": 0.12,
                        },
                    },
                ),
            ]
        )

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.usage == {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_tokens": 900,
            "cache_creation_tokens": 7,
        }

    async def test_no_usage_reported_leaves_the_field_none(self, stub) -> None:
        stub([_text("done"), Event(type="assistant.done", data={"cost_usd": None})])

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.usage is None

    async def test_a_malformed_usage_payload_never_breaks_the_step(self, stub) -> None:
        """Task 5's rule, restated on this path: usage is telemetry, and
        telemetry must not be able to fail a step that otherwise succeeded."""
        stub([_text("done"), Event(type="assistant.done", data={"usage": "not a dict"})])

        result = await collect_step(ROLE, "go", None, role_name="coder")

        assert result.usage is None
        assert result.summary == "done"


class TestCollectStepPlumbing:
    async def test_progress_is_set_on_the_first_event(self, stub) -> None:
        import threading

        stub([_text("hi")])
        progress = threading.Event()

        await collect_step(ROLE, "go", None, role_name="coder", progress=progress)

        assert progress.is_set()

    async def test_session_id_reaches_the_sub_context(self, stub) -> None:
        provider = stub([_text("hi")])

        await collect_step(ROLE, "go", "/tmp/x", ["/tmp/y"], session_id="sess-7")

        assert provider.ctx is not None
        assert provider.ctx.session_id == "sess-7"
        assert provider.ctx.cwd == "/tmp/x"
        assert provider.ctx.additional_dirs == ["/tmp/y"]
        assert provider.ctx.system_prompt == "be terse"

    async def test_an_error_event_raises_and_still_closes_the_provider(
        self, stub
    ) -> None:
        provider = stub(
            [_text("partial"), Event(type="error", data={"message": "backend exploded"})]
        )

        with pytest.raises(RuntimeError, match="backend exploded"):
            await collect_step(ROLE, "go", None, role_name="coder")

        assert provider.closed


class TestCollectTextWrapper:
    async def test_it_still_returns_a_string_with_the_tool_digest(self, stub) -> None:
        stub(
            [
                _text("Changes: one file."),
                Event(
                    type="assistant.tool_use",
                    data={"id": "t1", "name": "Edit", "input": {"path": "a.py"}},
                ),
            ]
        )

        text = await collect_text(ROLE, "go", None, role_name="coder")

        assert isinstance(text, str)
        assert text.startswith("Changes: one file.")
        assert "(tool activity from claude:sonnet)" in text
        assert "- Edit input=" in text

    async def test_it_is_bounded_for_a_huge_body(self, stub) -> None:
        stub([_text("C" * 2_000_000)])

        text = await collect_text(ROLE, "go", None, role_name="coder")

        assert len(text) < 10_000


class TestWorkerWireFormat:
    """Across a real process boundary: the parent only ever sees the worker's
    stdout, so an in-process ``StepResult`` proves nothing about the framing."""

    async def test_the_parent_rebuilds_the_envelope_the_worker_wrote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            provider_mod, "_WORKER_MODULE", "backend.tests.fakes.envelope_worker"
        )
        handle = provider_mod._SubprocHandle()
        await handle.start(ROLE, "go", None, [], None, "reviewer", "sess-9")

        result = await asyncio.wait_for(handle.result, timeout=30)

        assert isinstance(result, StepResult)
        # The request reached the worker with the role and the session on it.
        assert "role=reviewer" in result.summary
        assert "session=sess-9" in result.summary
        assert result.structured == {
            "value": "nack",
            "reason": "task 3 missing",
            "source": "json",
        }
        assert result.usage == {"input_tokens": 5, "output_tokens": 2}
        assert result.artifact_id == "c" * 64
        assert result.full_bytes == 2_000_000
        assert handle.first.is_set()

    async def test_ok_without_an_envelope_is_an_explicit_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker whose protocol drifts must say so, not resolve to an empty
        step the pipeline then tries to recover from by re-prompting."""
        handle = provider_mod._SubprocHandle.__new__(provider_mod._SubprocHandle)
        handle.first = asyncio.Event()
        handle.result = asyncio.get_running_loop().create_future()
        handle._proc = _FakeProc('@@RESULT@@ {"ok": true, "text": "legacy"}\n')
        handle._stderr = ""
        handle._pgid = None

        await handle._pump()

        with pytest.raises(RuntimeError, match="without a result envelope"):
            handle.result.result()


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process`` for ``_pump``."""

    def __init__(self, stdout: str) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = None
        self.returncode = 0

    async def wait(self) -> int:
        return 0


class _FakeStream:
    def __init__(self, text: str) -> None:
        self._lines = text.encode().splitlines(keepends=True)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _StubHandle:
    """Replaces ``_SubprocHandle`` so a step can be driven without spawning a
    process. Resolves immediately with a prepared envelope."""

    def __init__(self, result: StepResult) -> None:
        self.first = asyncio.Event()
        self.result: asyncio.Future[StepResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._payload = result
        self.start_args: tuple = ()
        self.killed = False

    async def start(self, *args) -> None:  # noqa: ANN002 - mirrors the real signature
        self.start_args = args
        self.first.set()
        self.result.set_result(self._payload)

    def kill(self) -> None:
        self.killed = True


def _envelope(summary: str, structured: dict | None = None) -> StepResult:
    return StepResult(
        summary=summary,
        structured=structured,
        artifact_id=None,
        artifact_path=None,
        tool_digest="",
        full_bytes=len(summary.encode("utf-8")),
    )


async def _run_step(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    envelope: StepResult,
    session_id: str | None = "sess-3",
) -> tuple[list[Event], dict[str, str], _StubHandle]:
    stub = _StubHandle(envelope)
    monkeypatch.setattr(provider_mod, "_SubprocHandle", lambda: stub)
    fleet = provider_mod.FleetProvider()
    step = Step(id=f"orch.{role}.1", role=role, prompt="do the thing")
    ctx = RunContext(model="m", prompt="p", session_id=session_id)
    outputs: dict[str, str] = {}

    events = [ev async for ev in fleet._run_step_with_role(step, ROLE, ctx, outputs)]
    return events, outputs, stub


class TestProviderConsumesTheEnvelope:
    async def test_the_step_records_bounded_context_text_not_the_transcript(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = StepResult(
            summary="head…\n… [truncated]\n…tail",
            structured=None,
            artifact_id="e" * 64,
            artifact_path="/artifacts/ee/e.txt",
            tool_digest="(tool activity from claude:m)\n- Bash input={}",
            full_bytes=2_000_000,
        )

        events, outputs, stub = await _run_step(monkeypatch, "coder", envelope)

        assert outputs["orch.coder.1"] == envelope.context_text()
        assert len(outputs["orch.coder.1"]) < 10_000
        assert "e" * 12 in outputs["orch.coder.1"]
        results = [ev for ev in events if ev.type == "tool.result"]
        assert results[-1].data["content"] == envelope.context_text()
        assert results[-1].data["is_error"] is False
        # Task 14's cancellation still runs on every exit path.
        assert stub.killed

    async def test_the_session_id_is_forwarded_to_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, stub = await _run_step(monkeypatch, "coder", _envelope("fine"))

        assert stub.start_args[-1] == "sess-3"

    async def test_a_json_nack_verdict_marks_the_card_errored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _envelope(
            "Lots of prose, and then the verdict.",
            {"value": "nack", "reason": "task 3 missing", "source": "json"},
        )

        events, _, _ = await _run_step(monkeypatch, "reviewer", envelope)

        assert [ev for ev in events if ev.type == "tool.result"][-1].data["is_error"]

    async def test_a_json_lgtm_verdict_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _envelope(
            "All tasks present.",
            {"value": "lgtm", "reason": "all 8 present", "source": "json"},
        )

        events, _, _ = await _run_step(monkeypatch, "reviewer", envelope)

        result = [ev for ev in events if ev.type == "tool.result"][-1]
        assert result.data["is_error"] is False

    async def test_an_envelope_without_a_verdict_is_parsed_from_its_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt and braces: a gate whose envelope lost its structured verdict
        must still be classified, and must not read as a pass."""
        envelope = _envelope("Task 6 missing.\n\nNACK: task 6 unimplemented")

        events, _, _ = await _run_step(monkeypatch, "reviewer", envelope)

        assert [ev for ev in events if ev.type == "tool.result"][-1].data["is_error"]

    async def test_a_chatty_reviewer_with_no_verdict_at_all_fails_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _envelope("Looks fine to me, nice work!")

        events, _, _ = await _run_step(monkeypatch, "reviewer", envelope)

        assert [ev for ev in events if ev.type == "tool.result"][-1].data["is_error"]

    async def test_a_non_gate_role_is_never_marked_errored_by_its_prose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A coder that quotes the reviewer protocol in its report is not a
        failed step."""
        envelope = _envelope("Changes: done. (The reviewer may NACK task 3.)")

        events, _, _ = await _run_step(monkeypatch, "coder", envelope)

        result = [ev for ev in events if ev.type == "tool.result"][-1]
        assert result.data["is_error"] is False
