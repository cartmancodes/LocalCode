"""Minimal JSON-RPC client for ``codex app-server`` over stdio.

Wire format (from ``codex app-server generate-json-schema``): one JSON
object per LF-terminated line, *without* the ``"jsonrpc":"2.0"`` member.
Three message kinds flow both ways:

- request:      ``{"id": N, "method": "...", "params": {...}}``
- response:     ``{"id": N, "result": {...}}`` or ``{"id": N, "error": {...}}``
- notification: ``{"method": "...", "params": {...}}``

The server also *initiates* requests (approvals, user input, dynamic tool
calls); ``on_server_request`` must answer every one of them — replying
``-32601`` to all of them would make every approval fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Awaitable, Callable
from typing import Any

ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

BUSY_ERROR = -32001


class CodexRpcError(Exception):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__(f"{method} failed: {error}")
        self.method = method
        self.error = error
        self.code = error.get("code") if isinstance(error, dict) else None


class CodexAppServerClient:
    def __init__(
        self,
        command: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_server_request: ServerRequestHandler | None = None,
    ) -> None:
        self._command = command or ["codex", "app-server"]
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.on_server_request = on_server_request
        self._reader: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self.stderr_tail: list[str] = []
        self.closed = asyncio.Event()

    # ── process ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._stderr_loop())

    async def close(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        if proc.stdin:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        if self._reader:
            self._reader.cancel()
        self.closed.set()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ── io ─────────────────────────────────────────────────────────────

    async def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        line = json.dumps(message, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._route(msg)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("codex app-server exited"))
            self._pending.clear()
            self.notifications.put_nowait({"method": "__closed__", "params": {}})
            self.closed.set()

    async def _stderr_loop(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                return
            self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            del self.stderr_tail[:-50]

    def _route(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg["id"] is not None
        if has_id and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif has_id and "method" in msg:
            task = asyncio.create_task(self._answer(msg))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        else:
            self.notifications.put_nowait(msg)

    async def _answer(self, msg: dict[str, Any]) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]
        if self.on_server_request is None:
            await self._send(
                {"id": rid, "error": {"code": -32601, "message": f"unsupported: {method}"}}
            )
            return
        try:
            result = await self.on_server_request(method, params)
        except Exception as exc:  # noqa: BLE001 — must always answer
            await self._send(
                {"id": rid, "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}
            )
            return
        await self._send({"id": rid, "result": result})

    # ── api ────────────────────────────────────────────────────────────

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"id": rid, "method": method, "params": params or {}})
        async with asyncio.timeout(timeout_s):
            msg = await fut
        if "error" in msg:
            raise CodexRpcError(method, msg["error"] or {})
        return msg.get("result") or {}

    async def request_with_retry(
        self, method: str, params: dict[str, Any] | None = None, *, attempts: int = 5
    ) -> dict[str, Any]:
        """Back off on ``-32001`` (server busy) instead of failing the turn."""
        delay = 0.2
        for attempt in range(attempts):
            try:
                return await self.request(method, params)
            except CodexRpcError as exc:
                if exc.code != BUSY_ERROR or attempt == attempts - 1:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 3.0)
        raise AssertionError("unreachable")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def initialize(
        self,
        *,
        name: str = "localcode",
        version: str = "0.1.0",
        experimental_api: bool = False,
        opt_out: list[str] | None = None,
    ) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": name, "title": "LocalCode", "version": version},
                "capabilities": {
                    "experimentalApi": experimental_api,
                    "optOutNotificationMethods": opt_out or [],
                },
            },
        )
        await self.notify("initialized")
        return result
