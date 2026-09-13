"""The suite's existing fakes, wrapped in the ``Provider`` interface.

Tasks 1-11 each grew the fake their own layer needed: a scripted
``ClaudeSDKClient`` (:mod:`.claude_client`), a real subprocess that speaks the
codex app-server protocol (:mod:`.fake_codex_app_server`), pool workers that
echo / build trees / return envelopes. Every one of them is *below* the
``Provider`` protocol — none of them is a provider. The evaluation net needs
the layer above: "run this same turn on claude and on codex, single and in a
fleet, and assert the same invariants". That is the only thing this module
adds. It composes those fakes; it re-implements none of them.

Three seams, and each is a real one the production code already has:

* ``ClaudeProvider._factory`` — the client class. Set it and the whole real
  provider runs (signature/rebuild rules, the two-producer merge, usage
  parsing, ``_discard``) with no `claude` CLI anywhere.
* ``CodexProvider(broker=CodexBroker(argv=...))`` — the app-server argv. Point
  it at ``fake_codex_app_server.py`` and the real JSON-RPC transport, framing,
  approval round-trip and translator all run against a real child process.
* ``registry._build_provider`` — what ``collect_step`` builds inside a fleet
  step. Patch it and a fleet step runs the real collector, envelope, artifact
  eviction and gate parse against a fake vendor.

**What is deliberately NOT faked, and what that costs.** The one thing these
cases do not cross is the worker *process* boundary: :class:`FakeWorkerPool`
runs ``collect_step`` in-process, exactly as ``fleet/subproc.py`` does in the
child, and keeps the pool's ``(first, result)`` contract. The process boundary
itself — framing, killpg reaping, queueing, pidfile sweeps — is
``test_worker_pool.py``'s subject and is tested there against real
subprocesses. Faking it here would duplicate that suite; ignoring the fleet
path entirely would leave the composition untested. See ``docs/harness.md``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from backend.app.orchestrator import claude as claude_mod
from backend.app.orchestrator import registry as registry_mod
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.codex import CodexBroker, CodexProvider
from backend.app.orchestrator.fleet.collect import collect_step
from backend.app.orchestrator.fleet.envelope import StepResult
from backend.app.orchestrator.fleet.models import RoleConfig

from .claude_client import FakeClaudeClient, FakeClientFactory

# ── where the hand-authored fixtures live ───────────────────────────────────
REPLAY_DIR = Path(__file__).resolve().parent.parent / "replay"
GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"
FAKE_CODEX_APP_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"


# ─────────────────────────────────────────────────────────────────────────────
# Replay fixtures → the objects each translator consumes
# ─────────────────────────────────────────────────────────────────────────────

def load_fixture(name: str) -> list[dict[str, Any]]:
    """The raw JSON array of one replay fixture."""
    return json.loads((REPLAY_DIR / name).read_text(encoding="utf-8"))


def _block(spec: dict[str, Any]) -> Any:
    """One content block of a Claude message, from its ``type`` tag."""
    kind = spec.get("type")
    if kind == "text":
        return TextBlock(text=spec.get("text", ""))
    if kind == "tool_use":
        return ToolUseBlock(
            id=spec["id"], name=spec["name"], input=spec.get("input") or {}
        )
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=spec["tool_use_id"],
            content=spec.get("content"),
            is_error=spec.get("is_error", False),
        )
    raise ValueError(f"replay fixture has no block type {kind!r}")


def rehydrate_claude(entry: dict[str, Any]) -> Any:
    """One recorded Claude message, as the SDK object ``_translate`` consumes.

    The ``__type__`` tag is the fixture's own, not the SDK's: a fixture is a
    plain JSON array so it can be read and edited, and this is the three lines
    that turn it back into the dataclasses the translator pattern-matches on.
    """
    kind = entry["__type__"]
    if kind == "StreamEvent":
        return StreamEvent(
            uuid=entry.get("uuid", "ev"),
            session_id=entry.get("session_id", "upstream-1"),
            event=entry.get("event") or {},
        )
    if kind == "AssistantMessage":
        return AssistantMessage(
            content=[_block(b) for b in entry.get("content") or []],
            model=entry.get("model", "claude-sonnet-4-6"),
        )
    if kind == "UserMessage":
        return UserMessage(content=[_block(b) for b in entry.get("content") or []])
    if kind == "ResultMessage":
        return ResultMessage(
            subtype=entry.get("subtype", "success"),
            duration_ms=entry.get("duration_ms", 0),
            duration_api_ms=entry.get("duration_api_ms", 0),
            is_error=entry.get("is_error", False),
            num_turns=entry.get("num_turns", 1),
            session_id=entry.get("session_id", "upstream-1"),
            total_cost_usd=entry.get("total_cost_usd"),
            usage=entry.get("usage"),
            result=entry.get("result"),
        )
    raise ValueError(f"replay fixture has no message type {kind!r}")


def claude_messages(name: str) -> list[Any]:
    """A whole Claude fixture, rehydrated in order."""
    return [rehydrate_claude(entry) for entry in load_fixture(name)]


def codex_frames(name: str) -> list[dict[str, Any]]:
    """A Codex fixture. Already the shape ``_Translator.handle`` takes — a
    JSON-RPC notification frame — so there is nothing to rehydrate."""
    return load_fixture(name)


# ─────────────────────────────────────────────────────────────────────────────
# Providers over the fakes
# ─────────────────────────────────────────────────────────────────────────────

def replay_behaviour(messages: Sequence[Any]) -> Callable[[FakeClaudeClient], Any]:
    """A :mod:`.claude_client` behaviour that replays ``messages`` for a turn."""

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        for message in messages:
            yield message

    return behaviour


def asking_behaviour(
    tool: str,
    tool_input: dict[str, Any],
    messages: Sequence[Any],
    *,
    decisions: list[Any] | None = None,
) -> Callable[[FakeClaudeClient], Any]:
    """A turn that asks the permission callback mid-stream, then replays.

    ``client.options.can_use_tool`` is the callback ``ClaudeProvider`` actually
    built for this turn, so the card, the wait and the vendor-shaped answer all
    go through the production gate. Each ``PermissionResultAllow`` /
    ``PermissionResultDeny`` is appended to ``decisions`` when one is given —
    what the *model* would have been told, which is the half of an approval no
    event carries.
    """

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        from claude_agent_sdk import ToolPermissionContext

        decision = await client.options.can_use_tool(
            tool, dict(tool_input), ToolPermissionContext()
        )
        if decisions is not None:
            decisions.append(decision)
        for message in messages:
            yield message

    return behaviour


def claude_provider(
    messages: Sequence[Any],
    *,
    behaviour: Callable[[FakeClaudeClient], Any] | None = None,
) -> claude_mod.ClaudeProvider:
    """The REAL ``ClaudeProvider``, talking to a scripted client."""
    provider = claude_mod.ClaudeProvider()
    provider._factory = FakeClientFactory(behaviour or replay_behaviour(messages))
    return provider


def codex_provider(scenario: str, *, timeout_s: float = 30.0) -> CodexProvider:
    """The REAL ``CodexProvider``, talking to the fake app-server subprocess."""
    broker = CodexBroker(
        argv=[sys.executable, str(FAKE_CODEX_APP_SERVER), scenario],
        startup_timeout_s=timeout_s,
        request_timeout_s=timeout_s,
    )
    return CodexProvider(broker=broker)


class ScriptedProvider:
    """A ``Provider`` that yields a fixed Event list — and records its contexts.

    For the cases whose subject is the *runner*, not a vendor translation: a
    turn that hangs, one that yields nothing, one whose tool result is two
    megabytes. Those need a provider that behaves precisely, and pushing them
    through a vendor fake would only add a translation nobody is asserting on.
    """

    name = "scripted"

    def __init__(
        self,
        events: Sequence[Event] | Callable[[RunContext], AsyncIterator[Event]],
        *,
        upstream_id: str = "upstream-scripted",
    ) -> None:
        self._events = events
        self._upstream_id = upstream_id
        self.contexts: list[RunContext] = []
        self.closed = False

    async def open_session(self, ctx: RunContext) -> str:
        return ctx.upstream_session_id or self._upstream_id

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        self.contexts.append(ctx)
        if callable(self._events):
            async for ev in self._events(ctx):
                yield ev
            return
        for ev in self._events:
            yield ev

    async def close_session(self, session_id: str) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class ToolCall:
    """One tool a scripted agent tries to use, and what happens if it may."""

    name: str
    input: dict[str, Any]
    # Run only when the gate allows the call — so a denial is observable as a
    # side effect that did NOT happen, which is the only proof that matters.
    effect: Callable[[], None] | None = None
    output: str = "ok"


@dataclass(frozen=True)
class GateRecord:
    """What the production gate decided about one scripted tool call."""

    role: str | None
    tool: str
    tool_input: dict[str, Any]
    allowed: bool


def tool_turn(
    calls: Sequence[ToolCall],
    *,
    role: str | None,
    text: str,
    recorder: list[GateRecord],
    usage: dict[str, int] | None = None,
) -> Callable[[FakeClaudeClient], Any]:
    """A turn that puts every tool call through the real permission callback.

    ``client.options.can_use_tool`` is the one ``ClaudeProvider`` built for this
    context, from the role policy ``collect_step`` set — so the role table, the
    path check and the headless-exec concession are all the production ones and
    the script only says what the agent *tried*. A refused call still becomes a
    ``tool_use``/``tool_result`` pair carrying the refusal, which is what the
    model would see.
    """

    async def behaviour(client: FakeClaudeClient) -> AsyncIterator[Any]:
        from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

        for index, call in enumerate(calls, start=1):
            decision = await client.options.can_use_tool(
                call.name, dict(call.input), ToolPermissionContext()
            )
            allowed = isinstance(decision, PermissionResultAllow)
            recorder.append(
                GateRecord(
                    role=role, tool=call.name, tool_input=dict(call.input), allowed=allowed
                )
            )
            if allowed and call.effect is not None:
                call.effect()
            call_id = f"{role or 'agent'}-t{index}"
            yield AssistantMessage(
                content=[ToolUseBlock(id=call_id, name=call.name, input=dict(call.input))],
                model="m",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=call_id,
                        content=call.output if allowed else getattr(decision, "message", ""),
                        is_error=not allowed,
                    )
                ]
            )
        if text:
            yield StreamEvent(
                uuid=f"{role}-text",
                session_id="sub",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        yield ResultMessage(
            subtype="success",
            duration_ms=7,
            duration_api_ms=6,
            is_error=False,
            num_turns=1,
            session_id="sub",
            total_cost_usd=0.01,
            usage=dict(usage or {"input_tokens": 40, "output_tokens": 12}),
        )

    return behaviour


class GatedSubProviders:
    """Fleet sub-providers that are REAL providers, scripted per role.

    ``_build_provider`` is handed only a provider name, so the role is read off
    ``ctx.role`` when the step runs and the scripted turn is built then — which
    is also the only moment the role exists. Each step is served by an actual
    ``ClaudeProvider`` over a scripted client, so a golden trace records what
    the harness really decided, not what a stand-in decided on its behalf.
    """

    def __init__(
        self, scripts: dict[str, tuple[Sequence[ToolCall], str]], *, vendor: str = "claude"
    ) -> None:
        self.scripts = scripts
        self.vendor = vendor
        self.gate: list[GateRecord] = []
        self.contexts: list[RunContext] = []

    def __call__(self, provider_name: str) -> Any:
        outer = self

        class _PerRole:
            name = provider_name

            async def open_session(self, ctx: RunContext) -> str:
                return ctx.upstream_session_id or "sub"

            async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
                outer.contexts.append(ctx)
                calls, text = outer.scripts.get(str(ctx.role), ((), ""))
                inner = claude_provider(
                    (),
                    behaviour=tool_turn(
                        calls, role=ctx.role, text=text, recorder=outer.gate
                    ),
                )
                try:
                    async for ev in inner.run(ctx):
                        yield ev
                finally:
                    await inner.aclose()

            async def close_session(self, session_id: str) -> None:
                return None

            async def aclose(self) -> None:
                return None

        return _PerRole()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> GatedSubProviders:
        install_sub_provider(monkeypatch, self)
        return self


class FakeSubProviders:
    """What ``collect_step`` builds inside a fleet step, scripted per role.

    ``_build_provider`` is handed only a provider NAME, so the role a step is
    playing is read off ``ctx.role`` at run time — which is also the only place
    it exists (``collect_step`` sets it, and it is what the permission policy
    is looked up by). Every context is recorded, so a test can assert on the
    prompt one role was actually handed: that is where prompt stitching and
    artifact hand-off are observable.
    """

    def __init__(
        self,
        scripts: dict[str | None, Any],
        *,
        vendor: str = "claude",
    ) -> None:
        self.scripts = scripts
        self.vendor = vendor
        self.contexts: list[RunContext] = []
        self.built: list[Any] = []

    def __call__(self, provider_name: str) -> ScriptedProvider:
        outer = self

        async def run(ctx: RunContext) -> AsyncIterator[Event]:
            outer.contexts.append(ctx)
            script = outer.scripts.get(ctx.role, outer.scripts.get(None, ()))
            if callable(script):
                async for ev in script(ctx):
                    yield ev
                return
            for ev in script:
                yield ev

        provider = ScriptedProvider(run)
        provider.name = provider_name  # type: ignore[misc]
        self.built.append(provider)
        return provider

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeSubProviders:
        install_sub_provider(monkeypatch, self)
        return self

    def prompt_for(self, role: str) -> str:
        """The prompt one role was handed — the last time it ran."""
        prompts = [ctx.prompt for ctx in self.contexts if ctx.role == role]
        assert prompts, f"{role} never ran"
        return prompts[-1]


def install_sub_provider(
    monkeypatch: pytest.MonkeyPatch, build: Callable[[str], Any]
) -> None:
    """Make every fleet sub-step build a fake provider.

    ``collect_step`` imports ``_build_provider`` from the registry *inside* the
    function, so patching the registry module is what a fleet step actually
    sees — and it is the same seam the production code uses to keep a
    sub-provider loop-local.
    """
    monkeypatch.setattr(registry_mod, "_build_provider", build)


# ─────────────────────────────────────────────────────────────────────────────
# The pool, without the process
# ─────────────────────────────────────────────────────────────────────────────

class FakeWorkerPool:
    """``WorkerPool``'s contract, served in-process.

    Implements exactly what ``FleetProvider._run_step_with_role`` asks of a
    pool — ``submit`` returning ``(first, result)``, ``is_queued``,
    ``abandon``, ``kill``, ``keys``, ``aclose`` — and runs each request through
    the same ``collect_step`` call ``fleet/subproc.py`` makes in the child, off
    the same request dict. So the step card, the heartbeats, the startup grace,
    the step ceiling, the envelope recording and the gate marking are all the
    production ones; only the pipe between them is gone.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self._tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._closed = False

    async def submit(
        self, key: str, request: dict[str, Any]
    ) -> tuple[asyncio.Event, asyncio.Future[StepResult]]:
        if self._closed:
            raise RuntimeError("worker pool is closed")
        self.requests.append({"key": key, **request})
        loop = asyncio.get_running_loop()
        first: asyncio.Event = asyncio.Event()
        result: asyncio.Future[StepResult] = loop.create_future()

        async def _serve() -> None:
            role = RoleConfig(
                provider=request["provider"],
                model=request["model"],
                system_prompt=request.get("system_prompt", ""),
            )
            try:
                step = await collect_step(
                    role,
                    request["prompt"],
                    request.get("cwd"),
                    request.get("additional_dirs") or [],
                    permission_mode=request.get("permission_mode"),
                    role_name=request.get("role_name"),
                    progress=_EventProgress(first),
                    session_id=request.get("session_id"),
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - the worker reports, never raises home
                if not result.done():
                    result.set_exception(RuntimeError(str(exc) or repr(exc)))
                return
            if not result.done():
                result.set_result(step)

        self._tasks.setdefault(key, []).append(asyncio.create_task(_serve()))
        return first, result

    def is_queued(self, key: str, result: asyncio.Future[StepResult]) -> bool:
        # Nothing queues: one in-process task per request, started immediately.
        return False

    def abandon(self, key: str, result: asyncio.Future[StepResult]) -> None:
        self.kill(key)

    def kill(self, key: str) -> None:
        self.killed.append(key)
        for task in self._tasks.pop(key, []):
            task.cancel()

    @property
    def keys(self) -> list[str]:
        return list(self._tasks)

    async def aclose(self) -> None:
        self._closed = True
        for key in list(self._tasks):
            self.kill(key)


class _EventProgress:
    """``collect_step``'s ``progress`` duck type, backed by an asyncio Event.

    ``subproc.py`` passes a marker-writing stand-in here and the real pool
    passes ``asyncio.Event`` semantics back to the caller; in-process the two
    are the same object.
    """

    __slots__ = ("_event",)

    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()


class FakeOrchestratorModel:
    """The orchestrator's own model loop, scripted.

    ``OrchestratorAgent`` is a ``claude_agent_sdk`` session whose only tools are
    the two MCP ones ``build_dispatch_mcp`` builds. Everything interesting about
    a fleet turn is downstream of the tool CALL, so a fake that decides which
    tools to call — and leaves the calling to the real handlers — exercises the
    whole of it: the dispatch ledgers, ``auto`` resolution, prompt stitching,
    the step runner, the envelope, the plan file.

    Two seams, both already in the production code: ``create_sdk_mcp_server`` is
    where the tool objects are handed to the SDK (captured here, because the
    per-turn ledgers are closure locals and there is no other way in), and
    ``query`` is the model loop itself.

    ``script`` is a list of ``(tool, args)`` pairs, or a callable taking the
    results so far and returning the next pair (or ``None`` to stop) — enough
    for "dispatch the reviewer only if the coder came back clean" without
    teaching this class anything about roles.
    """

    def __init__(
        self,
        script: Sequence[tuple[str, dict[str, Any]]]
        | Callable[[list[Any]], tuple[str, dict[str, Any]] | None],
        *,
        narrative: str = "Done.",
        cost_usd: float = 0.05,
        raise_after: str | None = None,
    ) -> None:
        self._script = script
        self._narrative = narrative
        self._cost_usd = cost_usd
        # A model that gives up after a failed dispatch, so the turn's
        # end-to-end behaviour on an aborted workflow can be asserted.
        self._raise_after = raise_after
        self.tools: dict[str, Any] = {}
        # (tool, args, result) per call, in order — the trace everything else
        # is derived from.
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.system_prompt: str | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeOrchestratorModel:
        from backend.app.orchestrator import dispatch as dispatch_mod
        from backend.app.orchestrator import orchestrator as orchestrator_mod

        monkeypatch.setattr(dispatch_mod, "create_sdk_mcp_server", self._capture)
        monkeypatch.setattr(orchestrator_mod, "query", self._query)
        return self

    def _capture(self, *, name: str, version: str, tools: list[Any]) -> dict[str, Any]:
        self.tools = {t.name: t for t in tools}
        return {"type": "sdk", "name": name}

    def _next(self) -> tuple[str, dict[str, Any]] | None:
        if callable(self._script):
            return self._script([c[2] for c in self.calls])
        if len(self.calls) < len(self._script):
            return self._script[len(self.calls)]
        return None

    async def _query(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.system_prompt = getattr(options, "system_prompt", None)
        while True:
            step = self._next()
            if step is None:
                break
            tool_name, args = step
            result = await self.tools[tool_name].handler(dict(args))
            self.calls.append((tool_name, dict(args), result))
            if self._raise_after is not None and self._raise_after in _tool_text(result):
                raise RuntimeError(
                    "orchestrator aborted: " + _tool_text(result).splitlines()[0]
                )
        yield StreamEvent(
            uuid="orch-1",
            session_id="orch",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": self._narrative},
            },
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=5,
            duration_api_ms=4,
            is_error=False,
            num_turns=len(self.calls) + 1,
            session_id="orch",
            total_cost_usd=self._cost_usd,
        )


def _tool_text(result: Any) -> str:
    """The text an MCP tool result carries — what the model would read."""
    if not isinstance(result, dict):
        return str(result)
    return "\n".join(
        str(block.get("text", ""))
        for block in result.get("content") or []
        if isinstance(block, dict)
    )


def fleet_provider_with(pool: FakeWorkerPool) -> Any:
    """A real ``FleetProvider`` whose pool is the in-process one.

    ``_get_pool`` rebuilds when the loop changes, so the loop is stamped too —
    otherwise the first step would quietly spawn real worker processes.
    """
    from backend.app.orchestrator.fleet.provider import FleetProvider

    provider = FleetProvider()
    provider._pool = pool  # type: ignore[assignment]
    provider._pool_loop = asyncio.get_running_loop()
    return provider
