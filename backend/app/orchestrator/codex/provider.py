"""Codex as a first-class peer: `codex app-server` behind the same harness.

The point of this module is not that LocalCode can talk to Codex — the
OpenCode provider already could, after a fashion. The point is that everything
Tasks 2-9 built for Claude now applies to Codex unchanged:

* **One approval bus, not two.** ``execCommandApproval`` and
  ``applyPatchApproval`` do not get a Codex-shaped gate. They are translated
  into a tool name plus a mapping and handed to
  :func:`approvals.evaluate_tool_request` — the same function
  ``build_can_use_tool`` hands Claude's ``can_use_tool`` to. The card on the
  bus is byte-identical in shape, the role policy is the same table, and a
  ``deny`` is denied for the same reasons. ``build_can_use_tool`` itself is
  deliberately NOT called: that is the claude-agent-sdk adapter, and routing
  through it would drag SDK result types into a provider that has none.
* **Role policies apply.** ``policy_for_role`` / ``resolve_roots`` /
  ``normalize_permission_mode`` are resolved exactly as ``claude.py`` resolves
  them, so a ``codex`` fleet role is bounded by the same table a ``claude``
  one is.
* **Extra directories work.** ``additional_dirs`` is forwarded to
  ``thread/start``. OpenCode could not do this at all (it binds a session to
  one project directory), which is half the reason this provider exists.

:class:`_TurnBinding` is the subtle part; read its docstring before changing
how the approval handler is built. The rest is translation.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...config import get_settings
from ...usage import TurnUsage
from ..approvals import EventSink, evaluate_tool_request
from ..base import Event, RunContext
from ..permissions import (
    ToolPolicy,
    normalize_permission_mode,
    path_decision,
    policy_for_role,
    resolve_roots,
)
from .client import CodexBroker, CodexUnavailable
from .protocol import (
    DECISION_APPROVED,
    DECISION_DENIED,
    ITEM_NOTIFICATIONS,
    ITEM_TYPE_AGENT_MESSAGE,
    ITEM_TYPE_COMMAND_EXECUTION,
    ITEM_TYPE_ERROR,
    ITEM_TYPE_FILE_CHANGE,
    ITEM_TYPE_MCP_TOOL_CALL,
    ITEM_TYPE_REASONING,
    ITEM_TYPE_WEB_SEARCH,
    N_ITEM_COMPLETED,
    N_ITEM_STARTED,
    N_ITEM_UPDATED,
    N_TURN_COMPLETED,
    N_TURN_FAILED,
    R_EXEC_APPROVAL,
    pick,
)

logger = logging.getLogger(__name__)

# Sentinel for the merged-event queue — same shape as claude.py's.
_DONE = object()


@dataclass
class _TurnBinding:
    """The per-turn halves of the approval handler, behind one mutable holder.

    THE HAZARD, and it bites Codex harder than it bit Claude. The app-server
    lives in :class:`CodexBroker` for the life of the *workspace*, so one
    process serves every turn of every session rooted there. The approval
    handler is registered on it once. But two of that handler's inputs are born
    fresh every turn: ``run()`` builds a new :class:`EventSink`, and
    ``SessionRunner`` builds a new approval queue. A handler that closed over
    turn 1's pair would, on turn 2, push the card onto a sink that is already
    closed — nobody ever sees it — and then wait for an answer on a queue
    nobody writes to, until the approval timeout denies it. Every approval
    after the first turn of a workspace would silently time out.

    So the handler closes over this holder and reads it when a request
    actually arrives. ``run()`` rebinds the fields at the top of each turn
    while holding ``CodexAppServer.turn_lock``, and therefore before any tool
    call of that turn can reach the handler.
    """

    policy: ToolPolicy
    mode: str
    timeout_s: float
    sink: EventSink | None = None
    approval_channel: asyncio.Queue[dict[str, Any]] | None = None


def _command_text(raw: Any) -> str:
    """Render an ``execCommandApproval`` command for the decision and the card.

    The wire carries either a string or an argv list depending on the CLI
    release, and the card must show the whole thing either way — a command the
    user cannot read is a question they cannot answer.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Sequence) and not isinstance(raw, bytes | bytearray):
        return " ".join(str(part) for part in raw)
    return "" if raw is None else str(raw)


def _changed_paths(params: Mapping[str, Any]) -> list[str]:
    """Every path an ``applyPatchApproval`` touches.

    Accepts both shapes the schema has used: a mapping of path → change, and a
    list of change objects carrying a path field.
    """
    changes = pick(params, "changes", "fileChanges", "file_changes", default=None)
    paths: list[str] = []
    if isinstance(changes, Mapping):
        paths = [str(key) for key in changes]
    elif isinstance(changes, Sequence) and not isinstance(changes, str | bytes):
        for change in changes:
            value = pick(change, "path", "file_path", "filePath", default=None)
            if value:
                paths.append(str(value))
    if not paths:
        single = pick(params, "path", "file_path", "filePath", default=None)
        if single:
            paths.append(str(single))
    return paths


def _patch_primary_path(paths: Sequence[str], policy: ToolPolicy) -> str:
    """Which changed path goes in ``file_path`` for the shared decision.

    A patch can touch many files and ``extract_paths`` only reads one key for
    an ``Edit``, so *which* path is handed to the core decides what gets
    containment-checked. Handing it the first path that ``path_decision``
    objects to means the shared gate itself produces the refusal, with its own
    reason — rather than this module growing a second, drifting copy of the
    roots check. When every path is fine, the first one is representative and
    the card carries the full list anyway.
    """
    for raw in paths:
        with contextlib.suppress(Exception):
            if path_decision(Path(raw), policy).outcome == "deny":
                return raw
    return paths[0] if paths else ""


def approval_tool_request(
    kind: str, params: Mapping[str, Any], policy: ToolPolicy
) -> tuple[str, dict[str, Any]]:
    """Translate one Codex approval request into ``(tool_name, tool_input)``.

    This is the whole of the Codex-specific approval code. Everything after it
    — the policy, the card, the queue, the timeout, the audit log — is the
    shared path Claude takes.
    """
    if kind == R_EXEC_APPROVAL:
        return "Bash", {"command": _command_text(pick(params, "command", "argv"))}
    paths = _changed_paths(params)
    return "Edit", {"file_path": _patch_primary_path(paths, policy), "paths": paths}


def _usage_from(payload: Mapping[str, Any] | None, *, model: str, session_id: str | None) -> Any:
    """Token counts in the same shape ``claude.py`` puts on ``assistant.done``.

    Zeros when the payload carries nothing, never absent: Task 11 reads these
    keys, and a missing key there is a crash in the reader rather than a turn
    with no data.
    """
    usage = payload if isinstance(payload, Mapping) else {}

    def count(*names: str) -> int:
        value = pick(usage, *names, default=0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return TurnUsage(
        provider=CodexProvider.name,
        model=model,
        input_tokens=count("input_tokens", "inputTokens"),
        output_tokens=count("output_tokens", "outputTokens"),
        cache_read_tokens=count(
            "cached_input_tokens",
            "cachedInputTokens",
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cache_read_tokens",
        ),
        cache_creation_tokens=count(
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
            "cache_creation_tokens",
        ),
        cost_usd=None,
        session_id=session_id,
        ts=0.0,
    )


class _Translator:
    """Codex item frames → LocalCode Events, with the per-turn state that needs.

    Stateful for two reasons, both of which would otherwise show up in the UI:

    * ``item/updated`` can carry the accumulated text rather than a delta, so
      the text already emitted for an item is remembered and only the new
      suffix is sent. Emitting the payload verbatim would repeat the whole
      message on every update.
    * a ``tool.result`` needs the ``tool_use`` that preceded it. When an item
      arrives only as ``completed`` (Codex is free to skip ``started``), the
      pair is synthesized so the transcript never holds a dangling result.

    No branch here raises on an unknown item type: it is logged once at DEBUG
    and skipped. See ``protocol.py`` for why that rule is absolute.
    """

    def __init__(self, *, thread_id: str, model: str, session_id: str | None) -> None:
        self._thread_id = thread_id
        self._model = model
        self._session_id = session_id
        self._emitted_text: dict[str, str] = {}
        self._opened_tools: set[str] = set()
        self._unknown_logged: set[str] = set()
        # "An error already reached the user." A turn that errored must not
        # also report success, and must not report the same failure twice.
        self.saw_error = False

    def handle(self, frame: Mapping[str, Any]) -> list[Event]:
        method = str(frame.get("method") or "")
        params = frame.get("params")
        params = params if isinstance(params, Mapping) else {}
        if method in ITEM_NOTIFICATIONS:
            item = pick(params, "item", default=None)
            if not isinstance(item, Mapping):
                return []
            return list(self._handle_item(method, item))
        if method == N_TURN_FAILED:
            self.saw_error = True
            error = pick(params, "error", default={}) or {}
            message = str(
                pick(error, "message", "detail", default="") or "the codex turn failed"
            )
            return [self._error(message)]
        if method == N_TURN_COMPLETED:
            return list(self._handle_completed(params))
        logger.debug("codex frame %s has no translation", method)
        return []

    # ── items ──────────────────────────────────────────────────────────────

    def _handle_item(self, method: str, item: Mapping[str, Any]) -> Iterator[Event]:
        item_type = str(pick(item, "type", "item_type", default="") or "")
        item_id = str(pick(item, "id", "item_id", default="") or f"codex-{id(item):x}")
        if item_type == ITEM_TYPE_AGENT_MESSAGE:
            yield from self._text(item_id, self._item_text(item))
        elif item_type == ITEM_TYPE_REASONING:
            # Only when it carries something a human can read. Codex emits
            # reasoning items with no summary text at all, and an empty
            # assistant.text is a blank bubble in the transcript.
            yield from self._text(item_id, self._item_text(item))
        elif item_type == ITEM_TYPE_COMMAND_EXECUTION:
            yield from self._tool_pair(
                method,
                item_id,
                name="Bash",
                tool_input={"command": _command_text(pick(item, "command", "argv"))},
                item=item,
            )
        elif item_type == ITEM_TYPE_FILE_CHANGE:
            yield from self._file_change(method, item_id, item)
        elif item_type == ITEM_TYPE_MCP_TOOL_CALL:
            server = str(pick(item, "server", "server_name", default="") or "")
            tool = str(pick(item, "tool", "tool_name", "name", default="mcp") or "mcp")
            yield from self._tool_pair(
                method,
                item_id,
                name=f"mcp__{server}__{tool}" if server else tool,
                tool_input=dict(pick(item, "arguments", "args", "input", default={}) or {}),
                item=item,
            )
        elif item_type == ITEM_TYPE_WEB_SEARCH:
            yield from self._tool_pair(
                method,
                item_id,
                name="WebSearch",
                tool_input={"query": str(pick(item, "query", "q", default="") or "")},
                item=item,
            )
        elif item_type == ITEM_TYPE_ERROR:
            if method == N_ITEM_COMPLETED or method == N_ITEM_STARTED:
                self.saw_error = True
                yield self._error(
                    str(pick(item, "message", "error", "detail", default="") or "codex error")
                )
        else:
            if item_type not in self._unknown_logged:
                self._unknown_logged.add(item_type)
                logger.debug("skipping unknown codex item type %r", item_type)

    @staticmethod
    def _item_text(item: Mapping[str, Any]) -> str:
        return str(pick(item, "text", "delta", "content", "summary", default="") or "")

    def _text(self, item_id: str, text: str) -> Iterator[Event]:
        if not text:
            return
        already = self._emitted_text.get(item_id, "")
        if text == already:
            return
        # ``item/updated`` may carry either a delta or the accumulated text.
        # Emitting the suffix covers both without the UI ever seeing the same
        # sentence twice.
        delta = text[len(already):] if text.startswith(already) else text
        self._emitted_text[item_id] = already + delta
        if delta:
            yield Event(type="assistant.text", data={"text": delta})

    def _tool_pair(
        self,
        method: str,
        item_id: str,
        *,
        name: str,
        tool_input: dict[str, Any],
        item: Mapping[str, Any],
    ) -> Iterator[Event]:
        if method == N_ITEM_UPDATED:
            return  # progress only; nothing new to render
        if item_id not in self._opened_tools:
            self._opened_tools.add(item_id)
            yield Event(
                type="assistant.tool_use",
                data={"id": item_id, "name": name, "input": tool_input},
            )
        if method != N_ITEM_COMPLETED:
            return
        exit_code = pick(item, "exit_code", "exitCode", default=None)
        try:
            is_error = exit_code is not None and int(exit_code) != 0
        except (TypeError, ValueError):
            is_error = False
        status = str(pick(item, "status", default="") or "")
        if status in {"failed", "error"}:
            is_error = True
        content = pick(
            item,
            "aggregated_output",
            "aggregatedOutput",
            "output",
            "result",
            "text",
            default="",
        )
        yield Event(
            type="tool.result",
            data={
                "tool_use_id": item_id,
                "content": content if isinstance(content, str) else str(content),
                "is_error": is_error,
            },
        )

    def _file_change(
        self, method: str, item_id: str, item: Mapping[str, Any]
    ) -> Iterator[Event]:
        changes = pick(item, "changes", "fileChanges", "file_changes", default=None)
        paths: list[str] = []
        kinds: list[str] = []
        if isinstance(changes, Mapping):
            for key, value in changes.items():
                paths.append(str(key))
                kinds.append(str(pick(value, "kind", "type", default="") or ""))
        elif isinstance(changes, Sequence) and not isinstance(changes, str | bytes):
            for change in changes:
                value = pick(change, "path", "file_path", "filePath", default=None)
                if value:
                    paths.append(str(value))
                    kinds.append(str(pick(change, "kind", "type", default="") or ""))
        if not paths:
            single = pick(item, "path", "file_path", "filePath", default=None)
            if single:
                paths.append(str(single))
                kinds.append(str(pick(item, "kind", "type", default="") or ""))
        # A creation is a Write and everything else is an Edit — the same two
        # names the rest of the harness (and the role table) already knows.
        name = "Write" if kinds and kinds[0] in {"add", "create", "created"} else "Edit"
        yield from self._tool_pair(
            method,
            item_id,
            name=name,
            tool_input={"file_path": paths[0] if paths else "", "paths": paths},
            item=item,
        )

    # ── turn end ───────────────────────────────────────────────────────────

    def _handle_completed(self, params: Mapping[str, Any]) -> Iterator[Event]:
        if self.saw_error:
            # The turn already surfaced a failure (a silent turn, an error
            # item). ``assistant.done`` on top of that would clear the UI's
            # working indicator with a success it did not earn; the runner
            # emits the single terminal event instead.
            return
        turn = pick(params, "turn", default={}) or {}
        usage_payload = pick(params, "usage", default=None) or pick(
            turn, "usage", default=None
        )
        usage = _usage_from(usage_payload, model=self._model, session_id=self._session_id)
        yield Event(
            type="assistant.done",
            data={
                "upstream_session_id": self._thread_id,
                "usage": asdict(usage),
            },
        )

    def _error(self, message: str) -> Event:
        return Event(
            type="error", data={"message": message, "provider": CodexProvider.name}
        )


class CodexProvider:
    name = "codex"

    def __init__(self, *, broker: CodexBroker | None = None) -> None:
        # Injected by tests so nothing needs the real CLI on PATH.
        self._broker = broker or CodexBroker()
        # One binding per WORKSPACE, because one app-server serves a workspace.
        # Created once and mutated per turn — see _TurnBinding.
        self._bindings: dict[str, _TurnBinding] = {}
        # Guards the binding dict only; never held across a spawn.
        self._bindings_lock: asyncio.Lock = asyncio.Lock()

    # ── policy ─────────────────────────────────────────────────────────────

    @staticmethod
    def _policy_for(ctx: RunContext) -> tuple[ToolPolicy, str, float]:
        """Exactly what ``claude.py`` resolves, from exactly the same helpers.

        A second, provider-local reading of the role table is how the two
        vendors drift apart, which is the failure this whole harness exists to
        prevent.
        """
        settings = get_settings()
        roots = resolve_roots(ctx.cwd, ctx.additional_dirs)
        policy = policy_for_role(ctx.role, roots, settings.denied_path_list())
        mode = normalize_permission_mode(
            ctx.permission_mode, allow_bypass=settings.allow_bypass_permissions
        )
        return policy, mode, settings.tool_approval_timeout_s

    def _make_approval_handler(self, binding: _TurnBinding):
        """The single bridge from Codex's approval callbacks to the shared gate."""

        async def handle(kind: str, params: dict[str, Any]) -> str:
            tool_name, tool_input = approval_tool_request(kind, params, binding.policy)
            decision = await evaluate_tool_request(
                tool_name,
                tool_input,
                # Read at call time, never captured: the server outlives the
                # turn and these four are reborn with it.
                policy=binding.policy,
                mode=binding.mode,
                sink=binding.sink,
                approval_channel=binding.approval_channel,
                timeout_s=binding.timeout_s,
            )
            return DECISION_APPROVED if decision.outcome == "allow" else DECISION_DENIED

        return handle

    async def _server_for(
        self, ctx: RunContext, *, policy: ToolPolicy, mode: str, timeout_s: float
    ) -> tuple[Any, _TurnBinding]:
        workspace = ctx.cwd or ""
        async with self._bindings_lock:
            binding = self._bindings.get(workspace)
            if binding is None:
                binding = _TurnBinding(policy=policy, mode=mode, timeout_s=timeout_s)
                self._bindings[workspace] = binding
        server = await self._broker.get(workspace)
        # Re-registering is harmless and idempotent in effect: every handler
        # for this workspace reads the SAME binding object, so which one is
        # installed cannot matter. What would matter — two handlers over two
        # different bindings — is exactly what the dict above prevents.
        server.set_approval_handler(self._make_approval_handler(binding))
        return server, binding

    async def _thread_for(self, ctx: RunContext, server: Any) -> str:
        if ctx.upstream_session_id:
            return await server.thread_resume(ctx.upstream_session_id)
        return await server.thread_start(
            ctx.cwd, ctx.additional_dirs, model=ctx.model
        )

    # ── Provider protocol ──────────────────────────────────────────────────

    async def open_session(self, ctx: RunContext) -> str:
        policy, mode, timeout_s = self._policy_for(ctx)
        server, _ = await self._server_for(ctx, policy=policy, mode=mode, timeout_s=timeout_s)
        return await self._thread_for(ctx, server)

    async def close_session(self, session_id: str) -> None:
        """No-op: the app-server belongs to a WORKSPACE, not to a session.

        Several LocalCode sessions rooted in one directory share one process,
        so closing it when one of them goes away would kill the others' agent
        mid-turn. ``aclose`` reclaims them at shutdown.
        """
        return None

    async def aclose(self) -> None:
        await self._broker.aclose_all()

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        policy, mode, timeout_s = self._policy_for(ctx)
        # The approval handler's card and the turn's items are two producers;
        # the sink is the handler's half. The handler runs inside the JSON-RPC
        # reader task, so a single-iterator run() would hold its card in a
        # frame nobody drains — the user would be asked a question they cannot
        # see. Same structure as claude.py, for the same reason.
        sink = EventSink()
        try:
            server, binding = await self._server_for(
                ctx, policy=policy, mode=mode, timeout_s=timeout_s
            )
            thread_id = await self._thread_for(ctx, server)
        except CodexUnavailable as exc:
            # The binary is missing or the handshake failed. One error Event
            # naming the binary and the fix — never an exception through the
            # WebSocket layer, and never a fallback to a key.
            logger.warning("codex app-server unavailable: %s", exc)
            yield Event(type="error", data={"message": str(exc), "provider": self.name})
            return
        except Exception as exc:
            logger.exception("starting a codex turn failed")
            yield Event(
                type="error",
                data={"message": str(exc) or repr(exc), "provider": self.name},
            )
            return

        async with server.turn_lock:
            # Rebind the per-turn halves before anything can call the handler.
            # Under turn_lock, so no other turn on this server can be mid-flight
            # while we do it. See _TurnBinding.
            binding.policy = policy
            binding.mode = mode
            binding.timeout_s = timeout_s
            binding.sink = sink
            binding.approval_channel = ctx.approval_channel

            merged: asyncio.Queue[Event | object] = asyncio.Queue()
            translator = _Translator(
                thread_id=thread_id, model=ctx.model, session_id=ctx.session_id
            )

            async def _pump_items() -> None:
                frames = server.turn_start(thread_id, ctx.prompt)
                try:
                    async for frame in frames:
                        for ev in translator.handle(frame):
                            await merged.put(ev)
                except asyncio.CancelledError:
                    # The app-server outlives the turn, so a cancelled turn has
                    # to tell it to stop: otherwise it keeps burning the user's
                    # quota on a turn nobody is reading and the next turn queues
                    # behind it.
                    with contextlib.suppress(Exception):
                        await server.turn_interrupt(thread_id)
                    raise
                except Exception as exc:
                    logger.exception("the codex turn pump raised")
                    await merged.put(
                        Event(
                            type="error",
                            data={"message": str(exc) or repr(exc), "provider": self.name},
                        )
                    )
                finally:
                    # An approval handler can still be mid-flight when the item
                    # stream ends; closing the sink lets its pump drain the tail
                    # and exit instead of waiting forever.
                    with contextlib.suppress(Exception):
                        await frames.aclose()
                    await sink.close()

            async def _pump_sink() -> None:
                while True:
                    ev = await sink.get()
                    if ev is None:
                        break
                    await merged.put(ev)

            item_task = asyncio.create_task(_pump_items())
            sink_task = asyncio.create_task(_pump_sink())

            async def _seal_when_drained() -> None:
                await asyncio.gather(item_task, sink_task)
                await merged.put(_DONE)

            seal_task = asyncio.create_task(_seal_when_drained())

            try:
                while True:
                    ev = await merged.get()
                    if ev is _DONE:
                        break
                    yield ev  # type: ignore[misc]
            finally:
                for task in (item_task, sink_task, seal_task):
                    if not task.done():
                        task.cancel()
                for task in (item_task, sink_task, seal_task):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                # The binding's sink belongs to this turn and is now closed.
                # Clearing it means a stray approval arriving between turns
                # takes the headless path (deny) rather than pushing a card
                # onto a sink nobody is draining.
                binding.sink = None
                binding.approval_channel = None
