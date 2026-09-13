"""Newline-delimited JSON-RPC 2.0 over a child process's stdio.

`codex app-server` is a long-lived subprocess that speaks JSON-RPC in **both
directions**: we send it requests (``thread/start``, ``turn/start``), it sends
us notifications (``item/completed``) *and* requests of its own (the two
approval callbacks). That bidirectionality is what makes this a module rather
than three lines of ``json.dumps``:

* **A server request that is never answered hangs the agent forever.** Codex
  blocks on ``execCommandApproval`` until a response with the matching id comes
  back. So an unknown method gets an error response (``-32601``) rather than a
  DEBUG log and a shrug, and a handler that *raises* gets an error response
  too. The one thing we must never do is drop it.
* **A response must find the request that is waiting for it.** Ids are a
  monotonic counter and every in-flight request owns a future keyed by its id,
  so two requests in flight can resolve in either order.
* **The child prints diagnostics on stdout.** The real binary interleaves
  human-readable lines with protocol frames, so a line that is not JSON is a
  DEBUG log and a skip — never an exception that kills the reader and with it
  every future still waiting.
* **``-32001`` is backpressure, not failure.** The app-server reports overload
  with it; retrying with a short backoff turns a transient condition into a
  slower turn instead of a failed one. Three tries and it surfaces, because an
  unbounded retry is a turn that never ends and never says why.

Process lifetime lives here too, because this object owns the pipes. ``close()``
kills the **process group**, not the process: the app-server spawns children of
its own (the sandbox helper, the tools it runs), and signalling only the leader
re-parents them to ``launchd`` where they keep running with nothing attached.
That is the same failure ``fleet/pool.py`` fixed for its workers, and the same
remedy — ``start_new_session=True`` at spawn (the caller's job, see
``client.py``), the pgid captured *then* because a dead process's pid can no
longer be translated to a group, and ``os.killpg`` here.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import BUSY_BACKOFF_S, ERR_BUSY, ERR_INTERNAL, ERR_METHOD_NOT_FOUND

logger = logging.getLogger(__name__)

# How many stderr lines to keep. When a turn dies the tail is usually the only
# evidence of why ("not logged in", "unknown flag"), so it is captured rather
# than discarded — the same reasoning as the fleet pool's ``_STDERR_TAIL_LINES``.
_STDERR_TAIL_LINES = 40

NotificationHandler = Callable[[dict[str, Any]], Any]
RequestHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class JsonRpcError(Exception):
    """A JSON-RPC ``error`` response, carrying the code the peer sent.

    The code is the whole point: ``ERR_BUSY`` is retried, everything else is
    surfaced, and a caller cannot tell those apart from a message string.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class StdioJsonRpc:
    """One JSON-RPC peer over one child process's stdin/stdout."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        pgid: int | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._proc = proc
        # Captured at spawn by the caller, never derived here: once the child
        # is gone ``os.getpgid`` can no longer translate its pid, and the
        # children we are trying to reach would be unreachable.
        self._pgid = pgid
        self._log = log or logger
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notification_handlers: dict[str, NotificationHandler] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._eof_handlers: list[Callable[[], Any]] = []
        self._reader: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        # Serialises writes: two coroutines interleaving halves of two frames
        # on one pipe produces JSON neither side can parse.
        self._write_lock = asyncio.Lock()
        # Server→client requests are handled in their own tasks so the reader
        # keeps draining while an approval card waits for a human. Tracked so
        # close() can cancel them instead of leaving them parked on a dead pipe.
        self._inflight: set[asyncio.Task[None]] = set()
        self._closed = False
        self._unknown_logged: set[str] = set()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())
        if self._stderr_task is None and self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def stderr_detail(self) -> str:
        tail = [line for line in self._stderr_tail if line.strip()][-6:]
        return (" | stderr: " + " ⏎ ".join(tail)) if tail else ""

    async def close(self, reason: str = "the codex app-server was closed") -> None:
        """Stop reading, reap the whole process group, fail every waiter.

        Idempotent: several paths reach it (a failed handshake, a session
        delete, app shutdown) and a second close must not raise.
        """
        if self._closed:
            return
        self._closed = True

        for task in (self._reader, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in list(self._inflight):
            if not task.done():
                task.cancel()

        if self._proc.stdin is not None:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()

        self._kill_group()
        # Reaped, not just signalled: an un-awaited child stays a zombie for
        # the life of the process, and the test suite asserts none are left.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)

        for task in (self._reader, self._stderr_task, *self._inflight):
            if task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._inflight.clear()

        self._fail_pending(reason)

    def _kill_group(self) -> None:
        """SIGKILL the group, falling back to the process alone.

        The app-server spawns helpers; killing only the leader leaves them
        running with no interface attached. ``ProcessLookupError`` means the
        group is already gone, which is success, not an error.
        """
        if self._pgid is not None:
            try:
                os.killpg(self._pgid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return  # already gone
            except OSError:
                self._log.warning(
                    "killpg(%s) failed; killing the codex app-server process only",
                    self._pgid,
                )
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._proc.kill()

    def _fail_pending(self, reason: str) -> None:
        detail = self.stderr_detail()
        while self._pending:
            _, fut = self._pending.popitem()
            if not fut.done():
                fut.set_exception(ConnectionError(f"{reason}{detail}"))

    # ── registration ───────────────────────────────────────────────────────

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers[method] = handler

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    def on_eof(self, handler: Callable[[], Any]) -> None:
        """Called once when the child's stdout ends — normally, or because it
        died. The client turns that into a ``turn/failed`` so a crashed
        app-server surfaces as an ``error`` Event instead of a turn that simply
        stops producing."""
        self._eof_handlers.append(handler)

    # ── sending ────────────────────────────────────────────────────────────

    async def _write(self, payload: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None or self._closed:
            raise ConnectionError(
                f"the codex app-server is not accepting input{self.stderr_detail()}"
            )
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            stdin.write(line)
            await stdin.drain()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float,
    ) -> Any:
        """Send one request and await its response.

        ``ERR_BUSY`` is retried on a fresh id (a retried request is a *new*
        request to the peer, so reusing the id would collide with the one it
        already answered) with the :data:`BUSY_BACKOFF_S` schedule. Every other
        error — and the last busy one — surfaces.
        """
        attempts = len(BUSY_BACKOFF_S)
        for attempt in range(attempts):
            try:
                return await self._request_once(method, params, timeout_s=timeout_s)
            except JsonRpcError as exc:
                if exc.code != ERR_BUSY or attempt == attempts - 1:
                    raise
                delay = BUSY_BACKOFF_S[attempt]
                self._log.info(
                    "codex app-server is busy on %s; retrying in %.2fs (attempt %d/%d)",
                    method,
                    delay,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request_once(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_s: float,
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError as exc:
            raise TimeoutError(
                f"the codex app-server did not answer {method} within "
                f"{timeout_s:.0f}s{self.stderr_detail()}"
            ) from exc
        finally:
            # Always dropped, including on timeout: a future left in the map is
            # a slow leak, and a late response would resolve a future nobody
            # reads.
            self._pending.pop(request_id, None)

    # ── receiving ──────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:  # pragma: no cover - always a pipe in practice
            return
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except ValueError:
                    # One oversize line. The stream resumes mid-line, so the
                    # next frame is garbage and gets skipped as non-JSON —
                    # better than tearing down every pending future.
                    self._log.warning("oversize line on the codex app-server stdout")
                    continue
                if not raw:
                    break
                self._handle_line(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("the codex app-server reader failed")
        finally:
            self._on_eof()

    def _on_eof(self) -> None:
        for handler in list(self._eof_handlers):
            try:
                handler()
            except Exception:
                self._log.debug("a codex eof handler raised", exc_info=True)
        # Nothing more will ever arrive, so anything still waiting must be told
        # now rather than waiting out its full timeout.
        self._fail_pending("the codex app-server exited")

    def _handle_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except ValueError:
            # The real binary prints human-readable diagnostics here.
            self._log.debug("non-JSON line from the codex app-server: %s", text[:200])
            return
        if not isinstance(msg, dict):
            self._log.debug("non-object JSON from the codex app-server: %s", text[:200])
            return

        if "method" in msg and "id" in msg:
            self._dispatch_request(msg)
        elif "method" in msg:
            self._dispatch_notification(msg)
        elif "id" in msg:
            self._resolve(msg)
        else:
            self._log.debug("unrecognised JSON-RPC frame: %s", text[:200])

    def _resolve(self, msg: dict[str, Any]) -> None:
        try:
            request_id = int(msg["id"])
        except (TypeError, ValueError):
            self._log.debug("response with a non-integer id: %s", msg.get("id"))
            return
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            self._log.debug("response for unknown request id %s", request_id)
            return
        error = msg.get("error")
        if isinstance(error, dict):
            fut.set_exception(
                JsonRpcError(
                    int(error.get("code", ERR_INTERNAL)),
                    str(error.get("message", "")),
                    error.get("data"),
                )
            )
        else:
            fut.set_result(msg.get("result"))

    def _dispatch_notification(self, msg: dict[str, Any]) -> None:
        method = str(msg.get("method"))
        handler = self._notification_handlers.get(method)
        if handler is None:
            if method not in self._unknown_logged:
                self._unknown_logged.add(method)
                self._log.debug("dropping unknown codex notification %s", method)
            return
        params = msg.get("params")
        try:
            result = handler(params if isinstance(params, dict) else {})
        except Exception:
            self._log.exception("a codex notification handler for %s raised", method)
            return
        if asyncio.iscoroutine(result):
            self._spawn(self._await_quietly(result, method))

    def _dispatch_request(self, msg: dict[str, Any]) -> None:
        self._spawn(self._serve_request(msg))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _await_quietly(self, coro: Awaitable[Any], method: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("a codex notification handler for %s raised", method)

    async def _serve_request(self, msg: dict[str, Any]) -> None:
        """Answer one server→client request. Always answers."""
        request_id = msg.get("id")
        method = str(msg.get("method"))
        params = msg.get("params")
        handler = self._request_handlers.get(method)
        if handler is None:
            await self._respond_error(
                request_id, ERR_METHOD_NOT_FOUND, f"LocalCode does not implement {method}"
            )
            return
        try:
            result = handler(params if isinstance(params, dict) else {})
            if asyncio.iscoroutine(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An error response, not a dropped request: codex blocks on the
            # approval callbacks, so a silent drop wedges the turn forever.
            self._log.exception("the codex request handler for %s raised", method)
            await self._respond_error(request_id, ERR_INTERNAL, f"{method} failed: {exc}")
            return
        await self._respond_result(request_id, result)

    async def _respond_result(self, request_id: Any, result: Any) -> None:
        with contextlib.suppress(Exception):
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        with contextlib.suppress(Exception):
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )

    async def _drain_stderr(self) -> None:
        stderr = self._proc.stderr
        if stderr is None:  # pragma: no cover
            return
        try:
            while True:
                raw = await stderr.readline()
                if not raw:
                    return
                self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.debug("the codex stderr drain stopped", exc_info=True)
