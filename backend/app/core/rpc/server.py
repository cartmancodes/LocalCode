"""RPC mode — pi's command/event protocol (``modes/rpc/rpc-mode.ts``).

Commands arrive as JSON objects; every one gets a ``response`` frame that
echoes its ``id``. Session events stream out as they happen. Extensions
reach the human through ``extension_ui_request`` / ``extension_ui_response``.

The transport is pluggable: :class:`RpcServer` takes a ``send`` callable and
exposes ``handle_line``; :func:`run_rpc_mode` binds it to stdin/stdout.
The same server sits behind the WebSocket route.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from collections.abc import Callable
from typing import Any

from ..agent_session import AgentSession
from ..engines.base import NotSupportedError
from .events import to_json_event
from .jsonl import LineSplitter, serialize_json_line

Send = Callable[[dict[str, Any]], None]

ModelRef = dict[str, str]  # {"provider": ..., "modelId": ...}


class RpcUIBridge:
    """``ctx.ui`` over the wire. Dialogs block until a matching
    ``extension_ui_response`` arrives; on timeout they resolve to their
    default (pi: "auto-resolve with default on timeout")."""

    has_ui = True

    def __init__(self, send: Send) -> None:
        self._send = send
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def dialog(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        rid = str(uuid.uuid4())
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send({"type": "extension_ui_request", "id": rid, "method": method, **payload})
        timeout_ms = payload.get("timeout")
        try:
            if timeout_ms:
                async with asyncio.timeout(float(timeout_ms) / 1000):
                    return await fut
            return await fut
        except TimeoutError:
            return {"cancelled": True}
        finally:
            self._pending.pop(rid, None)

    def notify(self, method: str, payload: dict[str, Any]) -> None:
        self._send(
            {"type": "extension_ui_request", "id": str(uuid.uuid4()), "method": method, **payload}
        )

    def resolve(self, frame: dict[str, Any]) -> bool:
        fut = self._pending.get(str(frame.get("id")))
        if fut is None or fut.done():
            return False
        fut.set_result({k: v for k, v in frame.items() if k not in ("type", "id")})
        return True


class RpcServer:
    def __init__(
        self,
        session: AgentSession,
        send: Send,
        *,
        models: list[ModelRef] | None = None,
        ui_bridge: RpcUIBridge | None = None,
    ) -> None:
        self.session = session
        self._send = send
        self.models = list(models or [])
        self.ui = ui_bridge or RpcUIBridge(send)
        self._unsubscribe = session.subscribe(self._on_event)
        self._tasks: set[asyncio.Task[Any]] = set()
        self.settled = asyncio.Event()

    # ── outbound ───────────────────────────────────────────────────────

    def _on_event(self, event: dict[str, Any]) -> None:
        self._send(to_json_event(event))
        if event.get("type") == "agent_settled":
            self.settled.set()

    def _respond(self, cmd: dict[str, Any], data: Any = None, *, error: str | None = None) -> None:
        frame: dict[str, Any] = {"type": "response", "command": cmd.get("type")}
        if cmd.get("id") is not None:
            frame["id"] = cmd["id"]
        if error is not None:
            frame["success"] = False
            frame["error"] = error
        else:
            frame["success"] = True
            if data is not None:
                frame["data"] = data
        self._send(frame)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── inbound ────────────────────────────────────────────────────────

    async def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            self._send(
                {"type": "response", "command": "?", "success": False, "error": "invalid JSON"}
            )
            return
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "extension_ui_response":
            self.ui.resolve(frame)
            return
        await self.handle_command(frame)

    async def handle_command(self, cmd: dict[str, Any]) -> None:
        kind = cmd.get("type")
        s = self.session
        try:
            if kind == "prompt":
                behavior = cmd.get("streamingBehavior")
                if s.is_streaming and behavior not in ("steer", "followUp"):
                    raise RuntimeError(
                        "agent is already processing; pass streamingBehavior 'steer' or 'followUp'"
                    )
                self.settled.clear()
                self._spawn(
                    s.prompt(
                        str(cmd.get("message", "")),
                        images=cmd.get("images"),
                        streaming_behavior=behavior,
                        source="rpc",
                    )
                )
                self._respond(cmd)
            elif kind == "steer":
                await s.steer(str(cmd.get("message", "")), cmd.get("images"))
                self._respond(cmd)
            elif kind == "follow_up":
                await s.follow_up(str(cmd.get("message", "")), cmd.get("images"))
                self._respond(cmd)
            elif kind == "abort":
                await s.abort()
                self._respond(cmd)
            elif kind == "clear_queue":
                self._respond(cmd, s.clear_queue())
            elif kind == "new_session":
                self._respond(cmd, await s.new_session(cmd.get("parentSession")))
            elif kind == "get_state":
                self._respond(cmd, s.get_state())
            elif kind == "set_model":
                await s.set_model(
                    {"provider": cmd.get("provider", ""), "modelId": cmd.get("modelId", "")}
                )
                self._respond(cmd, s.model)
            elif kind == "cycle_model":
                self._respond(cmd, await self._cycle_model())
            elif kind == "get_available_models":
                self._respond(cmd, {"models": self._available_models()})
            elif kind == "set_thinking_level":
                s.set_thinking_level(str(cmd.get("level", "off")))
                self._respond(cmd)
            elif kind == "cycle_thinking_level":
                levels = s.get_available_thinking_levels()
                current = s.thinking_level
                nxt = (
                    levels[(levels.index(current) + 1) % len(levels)]
                    if current in levels
                    else levels[0]
                )
                s.set_thinking_level(nxt)
                self._respond(cmd, {"level": nxt})
            elif kind == "get_available_thinking_levels":
                self._respond(cmd, {"levels": s.get_available_thinking_levels()})
            elif kind == "set_steering_mode":
                s.steering_mode = cmd.get("mode", "one-at-a-time")
                self._respond(cmd)
            elif kind == "set_follow_up_mode":
                s.follow_up_mode = cmd.get("mode", "one-at-a-time")
                self._respond(cmd)
            elif kind == "compact":
                result = await s.compact(cmd.get("customInstructions"))
                if result is None:
                    self._respond(cmd, error="compaction cancelled or unsupported")
                else:
                    self._respond(cmd, result)
            elif kind == "set_auto_compaction":
                s.auto_compaction_enabled = bool(cmd.get("enabled", True))
                self._respond(cmd)
            elif kind in ("set_auto_retry", "abort_retry"):
                self._respond(cmd)  # retries are the engine's; accepted for protocol parity
            elif kind == "bash":
                rid = cmd.get("id")

                def update(delta: str) -> None:
                    frame: dict[str, Any] = {"type": "bash_execution_update", "delta": delta}
                    if rid is not None:
                        frame["id"] = rid
                    self._send(frame)

                result = await s.execute_bash(
                    str(cmd.get("command", "")),
                    exclude_from_context=bool(cmd.get("excludeFromContext", False)),
                    on_update=update,
                )
                self._respond(cmd, result)
            elif kind == "abort_bash":
                s.abort_bash()
                self._respond(cmd)
            elif kind == "get_session_stats":
                self._respond(cmd, s.get_session_stats())
            elif kind == "export_html":
                self._respond(cmd, error="export_html is not supported yet")
            elif kind == "switch_session":
                self._respond(cmd, await s.switch_session(str(cmd.get("sessionPath", ""))))
            elif kind == "fork":
                self._respond(cmd, await s.fork(str(cmd.get("entryId", ""))))
            elif kind == "clone":
                leaf = s.session_manager.get_leaf_id()
                if leaf is None:
                    self._respond(cmd, {"cancelled": True})
                else:
                    s.session_manager.create_branched_session(leaf)
                    self._respond(cmd, {"cancelled": False})
            elif kind == "get_fork_messages":
                self._respond(cmd, {"messages": s.get_user_messages_for_forking()})
            elif kind == "get_entries":
                entries = s.session_manager.get_entries()
                since = cmd.get("since")
                if since:
                    ids = [e["id"] for e in entries]
                    entries = entries[ids.index(since) + 1 :] if since in ids else entries
                self._respond(cmd, {"entries": entries, "leafId": s.session_manager.get_leaf_id()})
            elif kind == "get_tree":
                self._respond(
                    cmd,
                    {
                        "tree": s.session_manager.get_tree(),
                        "leafId": s.session_manager.get_leaf_id(),
                    },
                )
            elif kind == "get_last_assistant_text":
                self._respond(cmd, {"text": s.get_last_assistant_text()})
            elif kind == "set_session_name":
                s.set_session_name(str(cmd.get("name", "")))
                self._respond(cmd)
            elif kind == "get_messages":
                self._respond(cmd, {"messages": s.messages})
            elif kind == "get_commands":
                self._respond(cmd, {"commands": s.get_commands()})
            else:
                self._respond(cmd, error=f"unknown command {kind!r}")
        except NotSupportedError as exc:
            self._respond(cmd, error=str(exc))
        except KeyError as exc:
            self._respond(cmd, error=f"not found: {exc.args[0] if exc.args else exc}")
        except Exception as exc:  # noqa: BLE001 — every command gets a response
            self._respond(cmd, error=f"{type(exc).__name__}: {exc}")

    # ── models ─────────────────────────────────────────────────────────

    def _available_models(self) -> list[ModelRef]:
        mine = [m for m in self.models if m.get("provider") == self.session.engine.name]
        current = self.session.model
        if current and current not in mine:
            mine.insert(0, current)
        return mine

    async def _cycle_model(self) -> dict[str, Any] | None:
        models = self._available_models()
        if len(models) < 2:
            return None
        current = self.session.model
        idx = models.index(current) if current in models else -1
        nxt = models[(idx + 1) % len(models)]
        await self.session.set_model(nxt)
        return {"model": nxt, "thinkingLevel": self.session.thinking_level, "isScoped": False}

    async def close(self) -> None:
        self._unsubscribe()
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def run_rpc_mode(
    session: AgentSession,
    *,
    models: list[ModelRef] | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> None:
    """Serve pi's RPC protocol on stdio until stdin closes."""
    out = stdout or sys.stdout
    write_lock = asyncio.Lock()

    def send(frame: dict[str, Any]) -> None:
        out.write(serialize_json_line(frame))
        out.flush()

    _ = write_lock
    ui = RpcUIBridge(send)
    session._ui_bridge = ui
    session.context = type(session.context)(session, ui)
    server = RpcServer(session, send, models=models, ui_bridge=ui)
    await session.start()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, stdin or sys.stdin)
    lines: asyncio.Queue[str | None] = asyncio.Queue()
    splitter = LineSplitter(lambda line: lines.put_nowait(line))

    async def pump() -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                splitter.end()
                lines.put_nowait(None)
                return
            splitter.feed(chunk.decode("utf-8", "replace"))

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            line = await lines.get()
            if line is None:
                break
            await server.handle_line(line)
    finally:
        pump_task.cancel()
        await server.close()
        await session.shutdown("quit")
