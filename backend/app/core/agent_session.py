"""AgentSession — pi's ``core/agent-session.ts`` over an engine-owned loop.

Owns one engine, one :class:`SessionManager` and the extension runner, and
exposes the surface every mode (RPC, JSON, print, WebSocket) is built on:
``prompt`` / ``steer`` / ``follow_up`` / ``abort`` / ``subscribe`` /
``fork`` / ``navigate_tree`` / ``set_model`` / ``compact``.

Event flow for one prompt::

    input hook → before_agent_start hook → user entry appended
    → engine.prompt() … each engine event is
        (a) recorded (assistant messages, tool results, engine session id,
            compaction) as session entries,
        (b) offered to extensions (agent_start, turn_*, message_*, tool_*),
        (c) forwarded to subscribers in pi's AgentSessionEvent shape
    → agent_end → queued follow-ups run → agent_settled

Engine hooks are installed once per run and compile the extension events
down: ``tool_call`` → ``before_tool``, ``tool_result`` → ``after_tool``,
``agent_end`` → ``on_stop``. A permission prompt from the binary
(``can_use_tool`` / ``requestApproval``) resolves to the tool_call chain's
verdict; when no extension took a position the session asks through
``ctx.ui.confirm`` (``default_permission="ask"``) — a UI bridge answers it,
and with no UI attached it is denied.

Known limits, stated rather than hidden:

- ``navigate_tree`` moves the shell's leaf; the engine keeps its own
  context. The next prompt carries a "conversation rewound" note.
- A ``systemPrompt`` returned from ``before_agent_start`` after the engine
  started is ignored (engines take the prompt at start); the messages it
  returns are prepended to the user prompt as context blocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import tempfile
from collections.abc import Callable
from typing import Any, Literal

from .engines.base import Engine, EngineConfig, EngineHooks, NotSupportedError
from .extensions.runner import ExtensionRunner, SimpleExtensionContext
from .extensions.types import NoUIBridge, UIBridge
from .messages import message_text, now_ms, user_message
from .resources import (
    ResourceLoader,
    Resources,
    expand_prompt_template,
    expand_skill_command,
    skill_infos,
    template_infos,
)
from .session_manager import SessionManager

Listener = Callable[[dict[str, Any]], None]
QueueMode = Literal["all", "one-at-a-time"]
PermissionPolicy = Callable[[str, dict[str, Any], dict[str, Any]], Any]

ENGINE_SESSION_ENTRY = "engine_session"
MAX_BASH_OUTPUT = 100_000


class SessionExtensionContext(SimpleExtensionContext):
    """The real ``ctx`` handlers receive: live views onto the session."""

    def __init__(self, session: AgentSession, ui_bridge: UIBridge) -> None:
        super().__init__(
            cwd=session.cwd,
            mode=session.mode,
            ui_bridge=ui_bridge,
            session_manager=session.session_manager,
            project_trusted=session.project_trusted,
        )
        self._session = session

    @property  # type: ignore[override]
    def session_manager(self) -> SessionManager:
        return self._session.session_manager

    @session_manager.setter
    def session_manager(self, value: Any) -> None:  # set by the base __init__
        pass

    @property  # type: ignore[override]
    def model(self) -> dict[str, str] | None:
        return self._session.model

    @model.setter
    def model(self, value: Any) -> None:
        pass

    @property  # type: ignore[override]
    def thinking_level(self) -> str:
        return self._session.thinking_level

    @thinking_level.setter
    def thinking_level(self, value: Any) -> None:
        pass

    @property  # type: ignore[override]
    def signal(self) -> asyncio.Event | None:
        return self._session.abort_signal if self._session.is_streaming else None

    @signal.setter
    def signal(self, value: Any) -> None:
        pass

    def is_idle(self) -> bool:
        return not self._session.is_streaming

    def abort(self) -> None:
        self._session.request_abort()

    def has_pending_messages(self) -> bool:
        return self._session.pending_message_count > 0

    def shutdown(self) -> None:
        self._session.request_shutdown()

    def compact(self, custom_instructions: str | None = None) -> None:
        self._session.spawn(self._session.compact(custom_instructions))

    def get_system_prompt(self) -> str:
        return self._session.system_prompt or ""

    # command-only extras (pi: ExtensionCommandContext)
    async def wait_for_idle(self) -> None:
        await self._session.wait_for_idle()

    async def new_session(self, parent_session: str | None = None) -> dict[str, Any]:
        return await self._session.new_session(parent_session)

    async def fork(self, entry_id: str, position: str = "before") -> dict[str, Any]:
        return await self._session.fork(entry_id, position)  # type: ignore[arg-type]

    async def navigate_tree(self, target_id: str, summarize: bool = True) -> dict[str, Any]:
        return await self._session.navigate_tree(target_id, summarize=summarize)

    async def switch_session(self, session_path: str) -> dict[str, Any]:
        return await self._session.switch_session(session_path)

    async def reload(self) -> None:
        return None


class AgentSession:
    def __init__(
        self,
        *,
        engine: Engine,
        session_manager: SessionManager,
        runner: ExtensionRunner | None = None,
        mode: str = "rpc",
        ui_bridge: UIBridge | None = None,
        model: str | None = None,
        thinking_level: str = "off",
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        permission_mode: str | None = None,
        add_dirs: list[str] | None = None,
        project_trusted: bool = False,
        default_permission: Literal["ask", "allow", "deny"] = "ask",
        engine_extra: dict[str, Any] | None = None,
        resources: Resources | None = None,
    ) -> None:
        self.engine = engine
        self.resources = resources or Resources()
        self.session_manager = session_manager
        self.runner = runner or ExtensionRunner()
        self.mode = mode
        self.cwd = session_manager.get_cwd()
        self.project_trusted = project_trusted
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt
        self.permission_mode = permission_mode
        self.add_dirs = list(add_dirs or [])
        self.default_permission = default_permission
        self.engine_extra = dict(engine_extra or {})
        self.permission_policy: PermissionPolicy | None = None

        self._model_id: str | None = model
        self.thinking_level = thinking_level
        self.steering_mode: QueueMode = "one-at-a-time"
        self.follow_up_mode: QueueMode = "one-at-a-time"
        self.auto_compaction_enabled = True
        self.is_streaming = False
        self.is_compacting = False
        self.abort_signal = asyncio.Event()
        self.shutdown_requested = asyncio.Event()
        self.last_rate_limit: dict[str, Any] | None = None

        self._listeners: list[Listener] = []
        self._steering: list[str] = []
        self._follow_up: list[str] = []
        self._pending_next_turn: list[dict[str, Any]] = []
        self._pending_bash: list[dict[str, Any]] = []
        self._idle = asyncio.Event()
        self._idle.set()
        self._run_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._tool_verdicts: dict[str, bool] = {}
        self._config_tasks: list[asyncio.Task[Any]] = []
        self._engine_desynced = False
        self._started = False
        self._bash_proc: asyncio.subprocess.Process | None = None
        self._ui_bridge: UIBridge = ui_bridge if ui_bridge is not None else NoUIBridge()
        self.context = SessionExtensionContext(self, self._ui_bridge)
        self.runner.bind(self)  # type: ignore[arg-type]

    # ── properties ─────────────────────────────────────────────────────

    @property
    def model(self) -> dict[str, str] | None:
        mid = self._model_id or self.engine.model
        return {"provider": self.engine.name, "modelId": mid} if mid else None

    @property
    def session_id(self) -> str:
        return self.session_manager.get_session_id()

    @property
    def session_file(self) -> str | None:
        return self.session_manager.get_session_file()

    @property
    def session_name(self) -> str | None:
        return self.session_manager.get_session_name()

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self.session_manager.build_session_context()["messages"])

    @property
    def pending_message_count(self) -> int:
        return len(self._steering) + len(self._follow_up)

    @property
    def is_idle(self) -> bool:
        return not self.is_streaming

    # ── subscribers ────────────────────────────────────────────────────

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def off() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return off

    def _emit(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 — one bad listener must not stop the run
                pass

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── lifecycle ──────────────────────────────────────────────────────

    def _engine_session_id_from_branch(self) -> str | None:
        for entry in reversed(self.session_manager.get_branch()):
            if entry.get("type") == "custom" and entry.get("customType") == ENGINE_SESSION_ENTRY:
                data = entry.get("data") or {}
                if data.get("engine") == self.engine.name:
                    return data.get("sessionId")
        return None

    def _engine_config(self, *, resume: bool = True) -> EngineConfig:
        return EngineConfig(
            cwd=self.cwd,
            model=self._model_id,
            thinking_level=self.thinking_level,
            system_prompt=self.system_prompt,
            append_system_prompt=self.append_system_prompt,
            session_id=self._engine_session_id_from_branch() if resume else None,
            permission_mode=self.permission_mode,
            add_dirs=self.add_dirs,
            extra=self.engine_extra,
        )

    async def start(
        self, reason: str = "startup", previous_session_file: str | None = None
    ) -> None:
        await self.engine.start(self._engine_config())
        tools = self.runner.all_tools()
        if tools and self.engine.capabilities.custom_tools:
            await self.engine.mount_tools(tools, self._execute_extension_tool)
        self._started = True
        event: dict[str, Any] = {"type": "session_start", "reason": reason}
        if previous_session_file:
            event["previousSessionFile"] = previous_session_file
        await self.runner.emit(event, self.context)

    async def shutdown(self, reason: str = "quit", target_session_file: str | None = None) -> None:
        event: dict[str, Any] = {"type": "session_shutdown", "reason": reason}
        if target_session_file:
            event["targetSessionFile"] = target_session_file
        await self.runner.emit(event, self.context)
        await self.abort()
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await self.engine.close()
        self._started = False

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()

    async def wait_for_idle(self) -> None:
        await self._idle.wait()

    # ── prompting ──────────────────────────────────────────────────────

    async def prompt(
        self,
        text: str,
        *,
        images: list[dict[str, Any]] | None = None,
        streaming_behavior: str | None = None,
        source: str = "rpc",
        expand_prompt_templates: bool = True,
    ) -> None:
        if expand_prompt_templates and text.startswith("/"):
            if await self._try_extension_command(text):
                return
            expanded = expand_skill_command(text, self.resources.skills)
            if expanded is None:
                expanded = expand_prompt_template(text, self.resources.prompt_templates)
            text = expanded
        if self.is_compacting:
            raise RuntimeError("cannot submit a prompt while compaction is in progress")
        processed = await self.runner.emit_input(
            text, images, source, streaming_behavior if self.is_streaming else None, self.context
        )
        if processed["action"] == "handled":
            return
        text = processed.get("text", text)
        images = processed.get("images", images)
        if self.is_streaming:
            if streaming_behavior == "followUp":
                await self._queue_follow_up(text, images)
            elif streaming_behavior == "steer":
                await self._queue_steer(text, images)
            else:
                raise RuntimeError(
                    "agent is already processing; pass streaming_behavior "
                    "('steer' or 'followUp') to queue the message"
                )
            return
        await self._run(text, images)

    async def _try_extension_command(self, text: str) -> bool:
        name, _, args = text[1:].partition(" ")
        cmd = self.runner.get_command(name)
        if cmd is None:
            return False
        result = cmd.handler(args.strip(), self.context)
        if inspect.isawaitable(result):
            await result
        return True

    async def _settle_config(self) -> None:
        """Engine-side model/thinking changes are applied asynchronously; a
        prompt must not start before they landed."""
        pending, self._config_tasks = self._config_tasks, []
        for task in pending:
            with contextlib.suppress(Exception):
                await task

    async def _run(self, text: str, images: list[dict[str, Any]] | None) -> None:
        if not self._started:
            await self.start()
        await self._settle_config()
        async with self._run_lock:
            self._idle.clear()
            self.is_streaming = True
            self.abort_signal.clear()
            try:
                await self._run_once(text, images)
                while not self.abort_signal.is_set() and not self.shutdown_requested.is_set():
                    next_text = self._next_queued()
                    if next_text is None:
                        break
                    await self._run_once(next_text, None)
            finally:
                self.is_streaming = False
                self._idle.set()
                self._emit({"type": "agent_settled"})
                await self.runner.emit({"type": "agent_settled"}, self.context)

    def _next_queued(self) -> str | None:
        # A steer that the engine could not take natively runs next, then follow-ups.
        if self._steering:
            if self.steering_mode == "all":
                text = "\n\n".join(self._steering)
                self._steering.clear()
            else:
                text = self._steering.pop(0)
            self._emit_queue_update()
            return text
        if self._follow_up:
            if self.follow_up_mode == "all":
                text = "\n\n".join(self._follow_up)
                self._follow_up.clear()
            else:
                text = self._follow_up.pop(0)
            self._emit_queue_update()
            return text
        return None

    async def _run_once(self, text: str, images: list[dict[str, Any]] | None) -> None:
        prompt_text = text
        hook = await self.runner.emit_before_agent_start(
            text, images, self.system_prompt or "", self.context
        )
        preamble: list[str] = []
        for msg in hook["messages"]:
            self.session_manager.append_custom_message_entry(
                msg.get("customType", "extension"),
                msg.get("content", ""),
                bool(msg.get("display", False)),
                msg.get("details"),
            )
            body = msg.get("content")
            body_text = body if isinstance(body, str) else message_text({"content": body})
            preamble.append(
                f'<context source="{msg.get("customType", "extension")}">\n{body_text}\n</context>'
            )
        for msg in self._pending_next_turn:
            self.session_manager.append_message(msg)
            source = msg.get("customType", "extension")
            preamble.append(f'<context source="{source}">\n{message_text(msg)}\n</context>')
        self._pending_next_turn.clear()
        for bash in self._pending_bash:
            preamble.append(
                f"<bash_execution command={bash['command']!r} exit_code={bash['exitCode']}>\n"
                f"{bash['output']}\n</bash_execution>"
            )
        self._pending_bash.clear()
        if self._engine_desynced:
            preamble.append(
                "<note>The conversation was rewound to an earlier point in the session tree; "
                "the transcript below the rewind point no longer applies.</note>"
            )
            self._engine_desynced = False
        if preamble:
            prompt_text = "\n\n".join([*preamble, text])

        user_msg = user_message(text, images)
        entry_id = self.session_manager.append_message(user_msg)
        self._emit({"type": "entry_appended", "entry": self.session_manager.get_entry(entry_id)})
        engine_msg = user_message(prompt_text, images)

        produced_roles: list[str] = []
        hooks = self._engine_hooks()
        try:
            async for event in self.engine.prompt(engine_msg, hooks):
                await self._on_engine_event(event, produced_roles)
        except NotSupportedError as exc:
            self._emit({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 — surface, never crash the session
            self._emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            self._emit({"type": "agent_end", "messages": [], "willRetry": False})
            await self.runner.emit({"type": "agent_end", "messages": []}, self.context)

    async def _on_engine_event(self, event: dict[str, Any], produced_roles: list[str]) -> None:
        kind = event.get("type")
        sm = self.session_manager
        if kind == "engine_session":
            sid = event.get("sessionId")
            if sid and sid != self._engine_session_id_from_branch():
                eid = sm.append_custom_entry(
                    ENGINE_SESSION_ENTRY, {"engine": event.get("engine"), "sessionId": sid}
                )
                self._emit({"type": "entry_appended", "entry": sm.get_entry(eid)})
            return
        if kind == "message_end":
            eid = sm.append_message(event["message"])
            self._emit({"type": "entry_appended", "entry": sm.get_entry(eid)})
        elif kind == "tool_execution_end":
            result = event.get("result") or {}
            tr = {
                "role": "toolResult",
                "toolCallId": event.get("toolCallId"),
                "toolName": event.get("toolName"),
                "content": list(result.get("content") or []),
                "isError": bool(event.get("isError")),
                "timestamp": now_ms(),
            }
            if result.get("details") is not None:
                tr["details"] = result["details"]
            eid = sm.append_message(tr)
            self._emit({"type": "entry_appended", "entry": sm.get_entry(eid)})
        elif kind == "compaction":
            self._emit({"type": "compaction_start", "reason": "threshold"})
            leaf = sm.get_leaf_id() or ""
            summary = event.get("summary") or "The engine compacted its context."
            tokens = int(event.get("tokensBefore") or 0)
            eid = sm.append_compaction(summary, leaf, tokens)
            entry = sm.get_entry(eid)
            self._emit({"type": "entry_appended", "entry": entry})
            self._emit(
                {
                    "type": "compaction_end",
                    "reason": "threshold",
                    "result": {
                        "summary": summary,
                        "firstKeptEntryId": leaf,
                        "tokensBefore": tokens,
                    },
                    "aborted": False,
                    "willRetry": False,
                }
            )
            await self.runner.emit(
                {
                    "type": "session_compact",
                    "compactionEntry": entry,
                    "fromExtension": False,
                    "reason": "threshold",
                    "willRetry": False,
                },
                self.context,
            )
            return
        elif kind == "rate_limit":
            self.last_rate_limit = event.get("info")
        elif kind == "agent_end":
            self._emit(
                {"type": "agent_end", "messages": event.get("messages", []), "willRetry": False}
            )
            await self.runner.emit(
                {"type": "agent_end", "messages": event.get("messages", [])}, self.context
            )
            return
        elif kind == "error":
            self._emit(
                {
                    "type": "error",
                    "message": event.get("message"),
                    "willRetry": event.get("willRetry", False),
                }
            )
            return
        self._emit(event)
        if kind in (
            "agent_start",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        ):
            await self.runner.emit(event, self.context)

    # ── engine hooks (compile-down) ────────────────────────────────────

    def _engine_hooks(self) -> EngineHooks:
        return EngineHooks(
            before_tool=self._before_tool,
            after_tool=self._after_tool,
            on_stop=None,
            ask_user=self._ask_user,
            permission_request=self._permission_request,
        )

    async def _before_tool(
        self, name: str, call_id: str, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        original = dict(args)
        verdict = await self.runner.emit_tool_call(name, call_id, args, self.context)
        if verdict and verdict.get("block"):
            self._tool_verdicts[call_id] = False
            return {"block": True, "reason": verdict.get("reason", "blocked")}
        self._tool_verdicts[call_id] = True
        if args != original:
            return {"updated_input": dict(args)}
        return None

    async def _after_tool(
        self,
        name: str,
        call_id: str,
        args: dict[str, Any],
        content: list[dict[str, Any]],
        is_error: bool,
    ) -> dict[str, Any] | None:
        override = await self.runner.emit_tool_result(
            name, call_id, args, content, None, is_error, self.context
        )
        if not override:
            return None
        return {
            "content": override.get("content", content),
            "is_error": override.get("isError", is_error),
        }

    async def _permission_request(
        self, name: str, args: dict[str, Any], ctx: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.permission_policy is not None:
            result = self.permission_policy(name, args, ctx)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                return result
            return {"allow": bool(result)}
        call_id = ctx.get("tool_use_id")
        if call_id and self._tool_verdicts.get(call_id) is False:
            return {"allow": False, "message": "blocked by extension"}
        if self.default_permission == "allow":
            return {"allow": True}
        if self.default_permission == "deny" or not self.context.has_ui:
            return {
                "allow": False,
                "message": "denied: no approval UI attached (default_permission=ask)",
            }
        title = ctx.get("title") or f"Allow {name}?"
        detail = ctx.get("description") or _describe_args(args)
        ok = await self.context.ui.confirm(title, detail)
        return {"allow": ok, "message": "denied by user" if not ok else ""}

    async def _ask_user(self, questions: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not self.context.has_ui:
            return None
        answers: dict[str, Any] = {}
        for q in questions:
            qid = q.get("id") or q.get("header") or str(len(answers))
            options = [
                o.get("label") if isinstance(o, dict) else str(o) for o in q.get("options") or []
            ]
            if options:
                choice = await self.context.ui.select(str(q.get("question") or qid), options)
                answers[qid] = {"answers": [choice] if choice is not None else []}
            else:
                text = await self.context.ui.input(str(q.get("question") or qid))
                answers[qid] = {"answers": [text] if text is not None else []}
        return {"answers": answers}

    async def _execute_extension_tool(
        self, name: str, call_id: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        found = self.runner.get_tool(name)
        if found is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        _ext, definition = found
        try:
            result = await definition.execute(call_id, args, self.abort_signal, None, self.context)
        except Exception as exc:  # noqa: BLE001 — report to the model, don't crash
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }
        return {
            "content": list(result.get("content", [])),
            "isError": False,
            "details": result.get("details"),
        }

    # ── queues / abort ─────────────────────────────────────────────────

    def _emit_queue_update(self) -> None:
        self._emit(
            {
                "type": "queue_update",
                "steering": list(self._steering),
                "followUp": list(self._follow_up),
            }
        )

    async def _queue_steer(self, text: str, images: list[dict[str, Any]] | None) -> None:
        msg = user_message(text, images)
        if await self.engine.steer(msg):
            eid = self.session_manager.append_message(msg)
            self._emit({"type": "entry_appended", "entry": self.session_manager.get_entry(eid)})
            self._emit(
                {"type": "queue_update", "steering": [text], "followUp": list(self._follow_up)}
            )
            return
        # No native steer: stop the run, and the queued text runs next.
        self._steering.append(text)
        self._emit_queue_update()
        await self.engine.interrupt()

    async def _queue_follow_up(self, text: str, images: list[dict[str, Any]] | None) -> None:
        self._follow_up.append(text)
        self._emit_queue_update()
        with contextlib.suppress(NotSupportedError):
            await self.engine.follow_up(user_message(text, images))

    async def steer(
        self, text: str, images: list[dict[str, Any]] | None = None, *, source: str = "rpc"
    ) -> None:
        await self.prompt(text, images=images, streaming_behavior="steer", source=source)

    async def follow_up(
        self, text: str, images: list[dict[str, Any]] | None = None, *, source: str = "rpc"
    ) -> None:
        await self.prompt(text, images=images, streaming_behavior="followUp", source=source)

    def clear_queue(self) -> dict[str, list[str]]:
        cleared = {"steering": list(self._steering), "followUp": list(self._follow_up)}
        self._steering.clear()
        self._follow_up.clear()
        self._emit_queue_update()
        return cleared

    def request_abort(self) -> None:
        self.abort_signal.set()
        self.spawn(self.engine.interrupt())

    async def abort(self) -> None:
        self.abort_signal.set()
        self._steering.clear()
        self._follow_up.clear()
        await self.engine.interrupt()
        await self.wait_for_idle()

    # ── model / thinking ───────────────────────────────────────────────

    async def set_model(self, model: dict[str, str] | str) -> bool:
        if isinstance(model, str):
            provider, _, model_id = model.partition("/")
            if not model_id:
                provider, model_id = self.engine.name, provider
        else:
            provider, model_id = model.get("provider", self.engine.name), model.get("modelId", "")
        if provider != self.engine.name:
            raise NotSupportedError(
                f"this session runs on the {self.engine.name!r} engine; "
                f"start a new session for {provider!r}"
            )
        previous = self.model
        self._model_id = model_id
        await self.engine.set_model(model_id)
        self.session_manager.append_model_change(provider, model_id)
        await self.runner.emit(
            {
                "type": "model_select",
                "model": self.model,
                "previousModel": previous,
                "source": "set",
            },
            self.context,
        )
        self._emit({"type": "model_changed", "model": self.model})
        return True

    def get_thinking_level(self) -> str:
        return self.thinking_level

    def set_thinking_level(self, level: str) -> None:
        if level == self.thinking_level:
            return
        self.thinking_level = level
        self._config_tasks.append(self.spawn(self.engine.set_thinking_level(level)))
        self.session_manager.append_thinking_level_change(level)
        self._emit({"type": "thinking_level_changed", "level": level})
        self.spawn(
            self.runner.emit({"type": "thinking_level_select", "level": level}, self.context)
        )

    def get_available_thinking_levels(self) -> list[str]:
        return list(self.engine.capabilities.thinking_levels) or ["off"]

    # ── compaction ─────────────────────────────────────────────────────

    async def compact(self, custom_instructions: str | None = None) -> dict[str, Any] | None:
        if self.is_streaming:
            raise RuntimeError("cannot compact while the agent is running")
        sm = self.session_manager
        pre = await self.runner.emit_session_before_compact(
            {
                "type": "session_before_compact",
                "branchEntries": sm.get_branch(),
                "customInstructions": custom_instructions,
                "reason": "manual",
                "willRetry": False,
            },
            self.context,
        )
        if pre and pre.get("cancel"):
            return None
        self.is_compacting = True
        self._emit({"type": "compaction_start", "reason": "manual"})
        try:
            if pre and pre.get("compaction"):
                comp = pre["compaction"]
                eid = sm.append_compaction(
                    comp.get("summary", ""),
                    comp.get("firstKeptEntryId") or sm.get_leaf_id() or "",
                    int(comp.get("tokensBefore") or 0),
                    comp.get("details"),
                    True,
                    comp.get("usage"),
                )
                from_extension = True
            else:
                await self.engine.compact(custom_instructions)
                leaf = sm.get_leaf_id() or ""
                eid = sm.append_compaction("The engine compacted its context.", leaf, 0)
                comp = {
                    "summary": "The engine compacted its context.",
                    "firstKeptEntryId": leaf,
                    "tokensBefore": 0,
                }
                from_extension = False
        except NotSupportedError as exc:
            self._emit(
                {
                    "type": "compaction_end",
                    "reason": "manual",
                    "result": None,
                    "aborted": True,
                    "willRetry": False,
                    "errorMessage": str(exc),
                }
            )
            return None
        finally:
            self.is_compacting = False
        entry = sm.get_entry(eid)
        self._emit({"type": "entry_appended", "entry": entry})
        self._emit(
            {
                "type": "compaction_end",
                "reason": "manual",
                "result": comp,
                "aborted": False,
                "willRetry": False,
            }
        )
        await self.runner.emit(
            {
                "type": "session_compact",
                "compactionEntry": entry,
                "fromExtension": from_extension,
                "reason": "manual",
                "willRetry": False,
            },
            self.context,
        )
        return comp

    # ── tree operations ────────────────────────────────────────────────

    def get_user_messages_for_forking(self) -> list[dict[str, str]]:
        out = []
        for entry in self.session_manager.get_branch():
            if entry.get("type") == "message" and entry["message"].get("role") == "user":
                out.append({"entryId": entry["id"], "text": message_text(entry["message"])})
        return out

    async def fork(
        self, entry_id: str, position: Literal["before", "at"] = "before"
    ) -> dict[str, Any]:
        sm = self.session_manager
        entry = sm.get_entry(entry_id)
        if entry is None:
            raise KeyError(f"entry {entry_id} not found")
        if await self.runner.emit_cancellable(
            {"type": "session_before_fork", "entryId": entry_id, "position": position}, self.context
        ):
            return {"text": "", "cancelled": True}
        await self.wait_for_idle()
        text = message_text(entry["message"]) if entry.get("type") == "message" else ""
        previous_file = sm.get_session_file()
        if previous_file and _exists(previous_file):
            new_sm = SessionManager.fork_from(previous_file, self.cwd, sm.get_session_dir())
        else:
            new_sm = SessionManager.in_memory(
                self.cwd,
                entries=[*([sm.get_header()] if sm.get_header() else []), *sm.get_entries()],
            )
        target = entry_id if position == "at" else entry.get("parentId")
        if target:
            new_sm.branch(target)
        else:
            new_sm.reset_leaf()
        await self.runner.emit(
            {
                "type": "session_shutdown",
                "reason": "fork",
                "targetSessionFile": new_sm.get_session_file() or "",
            },
            self.context,
        )
        self.session_manager = new_sm
        with contextlib.suppress(NotSupportedError):
            if self.engine.capabilities.fork:
                new_engine_id = await self.engine.fork()
                new_sm.append_custom_entry(
                    ENGINE_SESSION_ENTRY, {"engine": self.engine.name, "sessionId": new_engine_id}
                )
        self._engine_desynced = True
        await self.runner.emit(
            {"type": "session_start", "reason": "fork", "previousSessionFile": previous_file or ""},
            self.context,
        )
        return {"text": text, "cancelled": False}

    async def navigate_tree(
        self, target_id: str, *, summarize: bool = True, custom_summary: str | None = None
    ) -> dict[str, Any]:
        sm = self.session_manager
        if sm.get_entry(target_id) is None:
            raise KeyError(f"entry {target_id} not found")
        if await self.runner.emit_cancellable(
            {"type": "session_before_tree", "targetId": target_id}, self.context
        ):
            return {"cancelled": True}
        await self.wait_for_idle()
        if custom_summary:
            sm.branch_with_summary(target_id, custom_summary, None, True)
        else:
            sm.branch(target_id)
        self._engine_desynced = True
        await self.runner.emit({"type": "session_tree", "targetId": target_id}, self.context)
        self._emit({"type": "tree_navigated", "leafId": sm.get_leaf_id()})
        return {"cancelled": False}

    async def new_session(self, parent_session: str | None = None) -> dict[str, Any]:
        if await self.runner.emit_cancellable(
            {"type": "session_before_switch", "reason": "new"}, self.context
        ):
            return {"cancelled": True}
        await self.abort()
        previous = self.session_manager.get_session_file()
        await self.shutdown("new")
        options = {"parentSession": parent_session} if parent_session else None
        if self.session_manager.is_persisted():
            self.session_manager = SessionManager.create(
                self.cwd, self.session_manager.get_session_dir(), options
            )
        else:
            self.session_manager = SessionManager.in_memory(self.cwd, options)
        self._engine_desynced = False
        await self.start("new", previous)
        return {"cancelled": False}

    async def switch_session(self, session_path: str) -> dict[str, Any]:
        if await self.runner.emit_cancellable(
            {
                "type": "session_before_switch",
                "reason": "resume",
                "targetSessionFile": session_path,
            },
            self.context,
        ):
            return {"cancelled": True}
        await self.abort()
        previous = self.session_manager.get_session_file()
        await self.shutdown("resume", session_path)
        self.session_manager = SessionManager.open(session_path)
        self.cwd = self.session_manager.get_cwd()
        self._engine_desynced = False
        await self.start("resume", previous)
        return {"cancelled": False}

    # ── bash ───────────────────────────────────────────────────────────

    async def execute_bash(
        self,
        command: str,
        *,
        exclude_from_context: bool = False,
        on_update: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        override = await self.runner.emit(
            {
                "type": "user_bash",
                "command": command,
                "excludeFromContext": exclude_from_context,
                "cwd": self.cwd,
            },
            self.context,
        )
        for res in override:
            if res.get("result"):
                return res["result"]
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self.cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        self._bash_proc = proc
        chunks: list[str] = []
        size = 0
        truncated = False
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.read(4096)
                if not raw:
                    break
                text = raw.decode("utf-8", "replace")
                if on_update:
                    on_update(text)
                if size < MAX_BASH_OUTPUT:
                    chunks.append(text)
                    size += len(text)
                else:
                    truncated = True
            await proc.wait()
        finally:
            self._bash_proc = None
        output = "".join(chunks)
        result: dict[str, Any] = {
            "output": output,
            "exitCode": proc.returncode,
            "cancelled": proc.returncode in (-9, -15, 130, 137, 143),
            "truncated": truncated,
        }
        if truncated:
            fd, path = tempfile.mkstemp(prefix="localcode-bash-", suffix=".log")
            with os.fdopen(fd, "w") as fh:
                fh.write(output)
            result["fullOutputPath"] = path
        message = {
            "role": "bashExecution",
            "command": command,
            "output": output,
            "exitCode": proc.returncode,
            "cancelled": result["cancelled"],
            "truncated": truncated,
            "timestamp": now_ms(),
            "excludeFromContext": exclude_from_context,
        }
        eid = self.session_manager.append_message(message)
        self._emit({"type": "entry_appended", "entry": self.session_manager.get_entry(eid)})
        if not exclude_from_context:
            self._pending_bash.append(result | {"command": command})
        return result

    def abort_bash(self) -> None:
        if self._bash_proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._bash_proc.kill()

    # ── state / stats ──────────────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "thinkingLevel": self.thinking_level,
            "isStreaming": self.is_streaming,
            "isCompacting": self.is_compacting,
            "steeringMode": self.steering_mode,
            "followUpMode": self.follow_up_mode,
            "sessionFile": self.session_file,
            "sessionId": self.session_id,
            "sessionName": self.session_name,
            "autoCompactionEnabled": self.auto_compaction_enabled,
            "messageCount": len(self.messages),
            "pendingMessageCount": self.pending_message_count,
            "engine": self.engine.name,
            "engineSessionId": self.engine.session_id,
        }

    def get_session_stats(self) -> dict[str, Any]:
        messages = self.messages
        tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
        cost = 0.0
        counts = {"user": 0, "assistant": 0, "toolResult": 0}
        tool_calls = 0
        for m in messages:
            role = m.get("role")
            if role in counts:
                counts[role] += 1  # type: ignore[index]
            if role == "assistant":
                usage = m.get("usage") or {}
                for key in ("input", "output", "cacheRead", "cacheWrite"):
                    tokens[key] += int(usage.get(key, 0) or 0)
                tokens["total"] += int(usage.get("totalTokens", 0) or 0)
                cost += float((usage.get("cost") or {}).get("total", 0) or 0)
                tool_calls += sum(
                    1
                    for c in m.get("content", [])
                    if isinstance(c, dict) and c.get("type") == "toolCall"
                )
        return {
            "sessionFile": self.session_file,
            "sessionId": self.session_id,
            "userMessages": counts["user"],
            "assistantMessages": counts["assistant"],
            "toolCalls": tool_calls,
            "toolResults": counts["toolResult"],
            "totalMessages": len(messages),
            "tokens": tokens,
            "cost": cost,
            "rateLimit": self.last_rate_limit,
        }

    def get_last_assistant_text(self) -> str | None:
        for m in reversed(self.messages):
            if m.get("role") == "assistant":
                return message_text(m)
        return None

    # ── SessionBridge (extension API) ──────────────────────────────────

    def send_custom_message(
        self, message: dict[str, Any], *, trigger_turn: bool, deliver_as: str | None
    ) -> None:
        custom = {
            "role": "custom",
            "customType": message.get("customType", "extension"),
            "content": message.get("content", ""),
            "display": bool(message.get("display", False)),
            "timestamp": now_ms(),
        }
        if message.get("details") is not None:
            custom["details"] = message["details"]
        if self.is_streaming and deliver_as in ("steer", "followUp"):
            text = custom["content"] if isinstance(custom["content"], str) else message_text(custom)
            self.spawn(
                self._queue_steer(text, None)
                if deliver_as == "steer"
                else self._queue_follow_up(text, None)
            )
            return
        self._pending_next_turn.append(custom)
        if trigger_turn and not self.is_streaming:
            self.spawn(self._run("", None))

    def send_user_message(
        self, content: Any, *, deliver_as: str | None, expand_prompt_templates: bool
    ) -> None:
        text = content if isinstance(content, str) else message_text({"content": content})
        behavior = deliver_as if self.is_streaming else None
        self.spawn(
            self.prompt(
                text,
                streaming_behavior=behavior,
                source="extension",
                expand_prompt_templates=expand_prompt_templates,
            )
        )

    def append_entry(self, custom_type: str, data: Any) -> str:
        eid = self.session_manager.append_custom_entry(custom_type, data)
        self._emit({"type": "entry_appended", "entry": self.session_manager.get_entry(eid)})
        return eid

    def set_session_name(self, name: str) -> None:
        self.session_manager.append_session_info(name)
        self._emit(
            {"type": "session_info_changed", "name": self.session_manager.get_session_name()}
        )
        self.spawn(
            self.runner.emit(
                {"type": "session_info_changed", "name": self.session_manager.get_session_name()},
                self.context,
            )
        )

    def get_session_name(self) -> str | None:
        return self.session_manager.get_session_name()

    def set_label(self, entry_id: str, label: str | None) -> None:
        self.session_manager.append_label_change(entry_id, label)

    def get_active_tools(self) -> list[str]:
        return [t.name for t in self.runner.all_tools()]

    def set_active_tools(self, names: list[str]) -> None:
        return None  # engine tool sets are fixed at start; extension tools are all active

    def get_all_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "active": True,
                "source": "extension",
            }
            for t in self.runner.all_tools()
        ]

    def get_commands(self) -> list[dict[str, Any]]:
        return [
            *self.runner.command_infos(),
            *template_infos(self.resources.prompt_templates),
            *skill_infos(self.resources.skills),
        ]


def _exists(path: str) -> bool:
    return os.path.exists(path)


def _describe_args(args: dict[str, Any]) -> str:
    parts = [f"{k}: {v}" for k, v in list(args.items())[:6]]
    text = "\n".join(parts)
    return text[:2000]


# ── factory (pi: createAgentSession) ───────────────────────────────────────


async def create_agent_session(
    *,
    engine: Engine,
    cwd: str,
    session: Literal["new", "continue"] | str = "new",
    session_dir: str | None = None,
    in_memory: bool = False,
    extension_paths: list[str] | None = None,
    discover_extensions: bool = True,
    project_trusted: bool = False,
    ui_bridge: UIBridge | None = None,
    mode: str = "rpc",
    **options: Any,
) -> AgentSession:
    """Build a session: session manager, extensions, engine, in that order."""
    from .extensions.loader import discover_extension_paths, load_extensions

    if in_memory:
        sm = SessionManager.in_memory(cwd)
    elif session == "continue":
        sm = SessionManager.continue_recent(cwd, session_dir)
    elif session == "new":
        sm = SessionManager.create(cwd, session_dir)
    else:
        sm = SessionManager.open(session, session_dir)
    runner = ExtensionRunner()
    paths = list(extension_paths or [])
    if discover_extensions:
        paths = list(
            discover_extension_paths(cwd=cwd, include_project=project_trusted, extra_paths=paths)
        )
    await load_extensions(paths, runner)
    discovered = await runner.emit_resources_discover(
        cwd, "startup", SimpleExtensionContext(cwd=cwd, mode=mode, project_trusted=project_trusted)
    )
    loader = ResourceLoader(
        cwd=cwd,
        project_trusted=project_trusted,
        extra_skill_paths=discovered.get("skillPaths", []),
        extra_prompt_paths=discovered.get("promptPaths", []),
    )
    resources = loader.load()
    if options.get("system_prompt") is None and resources.system_prompt:
        options["system_prompt"] = resources.system_prompt
    inject = options.pop("inject_context_files", False)
    appended = loader.append_prompt(inject_context_files=inject)
    if appended:
        caller = options.get("append_system_prompt")
        options["append_system_prompt"] = f"{caller}\n\n{appended}" if caller else appended
    agent = AgentSession(
        engine=engine,
        session_manager=sm,
        runner=runner,
        mode=mode,
        ui_bridge=ui_bridge,
        project_trusted=project_trusted,
        resources=resources,
        **options,
    )
    return agent


__all__ = [
    "AgentSession",
    "SessionExtensionContext",
    "create_agent_session",
    "ENGINE_SESSION_ENTRY",
]
