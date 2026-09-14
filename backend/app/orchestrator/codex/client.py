"""One `codex app-server` process per workspace, and the turn stream over it.

**The auth invariant, stated where it is actually enforced.** We spawn the
`codex` binary and let it find its own credentials, exactly as `claude` does.
Nothing here reads a credential store, and nothing here constructs an
environment for the child: ``env`` is left unset so the child inherits ours
untouched, because building one is the first step towards putting a key in it.
A binary that is not on PATH is a :class:`CodexUnavailable` naming the binary
and ``codex login`` — never a fallback to a key, which would be a terms
violation dressed up as a convenience.

**One server per workspace, held across turns.** Cold start is a process spawn
plus a handshake; paying it per dispatch would make every fleet step slower
than the one-shot design this replaces. :class:`CodexBroker` keeps one
:class:`CodexAppServer` per workspace, and a turn borrows it under
``turn_lock`` — one turn at a time, because the turn stream is demultiplexed
into a single queue and two turns sharing it would interleave their items.

Two failure modes shape the turn stream:

* **The silent turn.** A ``turn/completed`` with no ``agent_message`` item
  means the turn produced no response body. Reporting that as a success is the
  worst available answer: the UI shows an empty assistant message and the user
  has no idea anything went wrong. So a synthetic ``error`` item — built from
  whatever items *did* arrive, which is usually the actual explanation — is
  injected ahead of the completion frame.
* **The server dying mid-turn.** stdout ends and the item queue would simply
  stop producing, leaving the consumer parked forever. The reader's EOF hook
  pushes a marker instead, and the stream ends in a synthetic ``turn/failed``
  naming the exit code.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from ...config import get_settings
from .jsonrpc import StdioJsonRpc
from .protocol import (
    APP_SERVER_SUBCOMMAND,
    DECISION_DENIED,
    F_ADDITIONAL_DIRECTORIES,
    F_CLIENT_INFO,
    F_CWD,
    F_DECISION,
    F_ERROR,
    F_INPUT,
    F_INPUT_TYPE_TEXT,
    F_ITEM,
    F_MESSAGE,
    F_MODEL,
    F_NAME,
    F_TEXT,
    F_THREAD_ID,
    F_TYPE,
    F_VERSION,
    ITEM_FIELDS,
    ITEM_NOTIFICATIONS,
    ITEM_TYPE_AGENT_MESSAGE,
    ITEM_TYPE_ERROR,
    ITEM_TYPE_FIELDS,
    M_INITIALIZE,
    M_THREAD_RESUME,
    M_THREAD_START,
    M_TURN_INTERRUPT,
    M_TURN_START,
    N_INITIALIZED,
    N_ITEM_COMPLETED,
    N_TURN_COMPLETED,
    N_TURN_FAILED,
    R_EXEC_APPROVAL,
    R_PATCH_APPROVAL,
    THREAD_ID_FIELDS,
    TURN_NOTIFICATIONS,
    pick,
)

logger = logging.getLogger(__name__)

CLIENT_NAME = "localcode"
CLIENT_VERSION = "0.1.0"

# Generous ceiling for one line of the app-server's stdout, for the same reason
# ``fleet/constants.WORKER_STDOUT_LIMIT`` is generous: a large item payload (a
# whole file's diff) on one line must not be an unreadable stream. 8 MiB means
# an oversize line is a logged warning, not a dead session.
STDOUT_LIMIT = 8 * 1024 * 1024

# Internal marker pushed onto the turn queue when the child's stdout ends. Not
# a protocol name — it never crosses the wire — so it deliberately does not
# live in protocol.py.
_EOF_MARKER = "__eof__"

# The turn stream's frames are ``{"method": ..., "params": ...}`` dicts. Those
# two keys are OURS: the envelope is an internal contract between this module
# and provider.py, deliberately shaped like a JSON-RPC frame because most of
# the frames on it are one, and that is why they are not in protocol.py. The
# synthetic frames (the silent turn, the exit) use the same envelope so the
# translator needs no special case for them.

# Bound on waiting for the exit status of a server that just closed its
# stdout. Only used to put a code in an error message, so a couple of seconds
# is plenty and a hang here would be worse than an unknown code.
_EXIT_WAIT_S = 2.0

# What an approval request is answered with when nothing has registered a
# handler. Denial, never approval: an unwired gate must not become a blanket
# yes, and codex blocks until it gets an answer either way.
_UNWIRED_DECISION = DECISION_DENIED

ApprovalHandler = Callable[[str, dict[str, Any]], Awaitable[str] | str]


# N818 wants an `Error` suffix. The name is fixed by the task brief and reads
# as the condition it reports ("codex is unavailable"), which is what a caller
# matches on; renaming it would make the one documented symbol unfindable.
class CodexUnavailable(RuntimeError):  # noqa: N818
    """The `codex` app-server could not be started or handshaken.

    Raised rather than returned so no caller can mistake it for a turn that
    merely produced nothing; :class:`CodexProvider` turns it into a single
    ``error`` Event.
    """


def default_argv() -> list[str]:
    """``[<codex binary>, "app-server"]``, resolved at spawn time.

    Read from settings on every spawn, not cached at import: a test that
    repoints ``codex_binary`` (and the missing-binary case in particular) must
    be observed by the very next spawn.
    """
    return [get_settings().codex_binary, APP_SERVER_SUBCOMMAND]


def _missing_binary_message(binary: str, detail: str = "") -> str:
    return (
        f"the {binary!r} binary is not on PATH, so LocalCode could not start the "
        f"codex app-server. Install the Codex CLI and run `codex login` — "
        f"LocalCode never holds an API key of its own and will not fall back to "
        f"one.{detail}"
    )


class CodexAppServer:
    """One `codex app-server` child process, for one workspace."""

    def __init__(
        self,
        *,
        workspace: str | None,
        argv: Sequence[str] | None = None,
        startup_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
    ) -> None:
        self.workspace = workspace or ""
        # Injected by tests so no test needs the real CLI; ``None`` means "ask
        # settings at spawn time" (see ``default_argv``).
        self._argv = list(argv) if argv else None
        settings = get_settings()
        self._startup_timeout_s = (
            settings.codex_startup_timeout_s if startup_timeout_s is None else startup_timeout_s
        )
        self._request_timeout_s = (
            settings.codex_request_timeout_s if request_timeout_s is None else request_timeout_s
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc: StdioJsonRpc | None = None
        # One turn at a time on this server: the item notifications of every
        # turn arrive on one stdout, and two concurrent turns would interleave
        # into one queue with no way to tell them apart.
        self.turn_lock = asyncio.Lock()
        self._turn_queue: asyncio.Queue[dict[str, Any]] | None = None
        # Threads this process already has open. ``thread/resume`` on one of
        # them would replay a transcript into a server that is already holding
        # it, so the id is simply handed back.
        self._threads: set[str] = set()
        self._approval_handler: ApprovalHandler | None = None
        self._closed = False
        # Set when the child's stdout ends. A server that died is as unusable
        # as one we closed, and saying so is what stops the NEXT turn in this
        # workspace from writing into a dead pipe and waiting out the full
        # request timeout for an answer that can never come.
        self._died = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def closed(self) -> bool:
        """Unusable — closed by us, or dead on its own.

        Both answers have to be the same one. :class:`CodexBroker` keys a
        long-lived process per workspace, so a crashed app-server that still
        reported itself usable would be handed to every later turn in that
        workspace, each of which would write into a pipe nobody reads and
        block for ``codex_request_timeout_s``. One crash would wedge the
        workspace until the app restarted.
        """
        return self._closed or self._died

    async def start(self) -> None:
        argv = self._argv or default_argv()
        binary = argv[0]
        cwd = self.workspace or None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Its own session so one killpg reaps the helpers the
                # app-server spawns. Killing the leader alone re-parents them
                # to launchd, where they keep running with nothing attached —
                # the exact leak fleet/pool.py fixed for its workers.
                start_new_session=True,
                limit=STDOUT_LIMIT,
                # env is deliberately NOT built here. The child inherits ours
                # and finds its own credentials; constructing an environment is
                # the first step towards putting a key in one.
            )
        except FileNotFoundError as exc:
            raise CodexUnavailable(_missing_binary_message(binary)) from exc
        except (NotADirectoryError, PermissionError, OSError) as exc:
            raise CodexUnavailable(
                f"LocalCode could not spawn {binary!r}: {exc}. "
                f"Install the Codex CLI and run `codex login`."
            ) from exc

        # Captured NOW: once the child is gone its pid can no longer be
        # translated to a group, and the group is what we have to signal.
        try:
            pgid: int | None = os.getpgid(proc.pid)
        except OSError:
            # start_new_session made it the group leader, so its pid is the
            # group id either way.
            pgid = proc.pid

        self._proc = proc
        rpc = StdioJsonRpc(proc, pgid=pgid, log=logger)
        self._rpc = rpc
        self._register_handlers(rpc)
        rpc.start()
        try:
            await rpc.request(
                M_INITIALIZE,
                {F_CLIENT_INFO: {F_NAME: CLIENT_NAME, F_VERSION: CLIENT_VERSION}},
                timeout_s=self._startup_timeout_s,
            )
            await rpc.notify(N_INITIALIZED)
        except Exception as exc:
            detail = rpc.stderr_detail()
            await rpc.close("the codex app-server handshake failed")
            self._closed = True
            raise CodexUnavailable(
                f"the codex app-server ({binary!r}) did not complete its handshake: "
                f"{exc}. If this is an auth failure, run `codex login`.{detail}"
            ) from exc

    async def close(self) -> None:
        self._closed = True
        if self._rpc is not None:
            await self._rpc.close()

    def _register_handlers(self, rpc: StdioJsonRpc) -> None:
        for method in (*ITEM_NOTIFICATIONS, *TURN_NOTIFICATIONS):
            rpc.on_notification(method, self._make_notification_handler(method))
        for method in (R_EXEC_APPROVAL, R_PATCH_APPROVAL):
            rpc.on_request(method, self._make_approval_handler(method))
        rpc.on_eof(self._on_eof)

    def _make_notification_handler(self, method: str) -> Callable[[dict[str, Any]], None]:
        def handle(params: dict[str, Any]) -> None:
            queue = self._turn_queue
            if queue is None:
                # Between turns. Codex can emit trailing frames after a turn
                # ends; dropping them is correct and silent.
                logger.debug("codex %s arrived outside a turn", method)
                return
            queue.put_nowait({"method": method, "params": params})

        return handle

    def _make_approval_handler(
        self, method: str
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def handle(params: dict[str, Any]) -> dict[str, Any]:
            handler = self._approval_handler
            if handler is None:
                logger.warning(
                    "codex asked for %s with no approval handler registered — denying",
                    method,
                )
                return {F_DECISION: _UNWIRED_DECISION}
            result = handler(method, params)
            if asyncio.iscoroutine(result):
                result = await result
            return {F_DECISION: str(result)}

        return handle

    def _on_eof(self) -> None:
        # Nothing will ever arrive on this process again, so it is retired
        # here rather than on the next failed request — see ``closed``.
        self._died = True
        queue = self._turn_queue
        if queue is not None:
            queue.put_nowait({"method": _EOF_MARKER, "params": {}})

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        """Register the single callback both approval requests route through.

        Registered once per server and re-registered harmlessly: the per-turn
        state it needs lives behind the handler, not in it — see
        ``provider._TurnBinding``, which is what keeps turn 2's approval card on
        turn 2's sink when the server outlives the turn.
        """
        self._approval_handler = handler

    # ── threads ────────────────────────────────────────────────────────────

    def _rpc_or_raise(self) -> StdioJsonRpc:
        if self._rpc is None or self.closed:
            # ``closed``, not ``_closed``: a server that exited on its own must
            # fail the request now, with a message, rather than 120 seconds
            # from now with a timeout.
            raise CodexUnavailable("the codex app-server is not running")
        return self._rpc

    async def thread_start(
        self,
        cwd: str | None,
        additional_dirs: Sequence[str] | None = None,
        *,
        model: str | None = None,
    ) -> str:
        """Open a new thread rooted at ``cwd``.

        ``additional_dirs`` is forwarded, and that is half the reason this
        provider existed: OpenCode bound a session to exactly one project
        directory and exposes only a binary ``external_directory`` permission,
        so a ChatGPT-side role could never be given a sibling repo. Here it is
        just a field.
        """
        params: dict[str, Any] = {F_CWD: cwd or ""}
        if model:
            params[F_MODEL] = model
        params[F_ADDITIONAL_DIRECTORIES] = [str(d) for d in (additional_dirs or [])]
        result = await self._rpc_or_raise().request(
            M_THREAD_START, params, timeout_s=self._request_timeout_s
        )
        thread_id = str(pick(result, *THREAD_ID_FIELDS, default="") or "")
        if not thread_id:
            raise CodexUnavailable("the codex app-server returned no thread id for thread/start")
        self._threads.add(thread_id)
        return thread_id

    async def thread_resume(self, thread_id: str) -> str:
        if thread_id in self._threads:
            # Already live in THIS process. Resuming it would ask the server to
            # replay a transcript it is already holding.
            return thread_id
        result = await self._rpc_or_raise().request(
            M_THREAD_RESUME, {F_THREAD_ID: thread_id}, timeout_s=self._request_timeout_s
        )
        resumed = str(pick(result, *THREAD_ID_FIELDS, default="") or thread_id)
        self._threads.add(resumed)
        return resumed

    def forget_thread(self, thread_id: str) -> None:
        self._threads.discard(thread_id)

    # ── turns ──────────────────────────────────────────────────────────────

    async def turn_start(
        self, thread_id: str, text: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one turn, yielding raw ``{"method", "params"}`` frames.

        The queue is installed BEFORE the request goes out: the app-server is
        free to emit ``item/started`` before it answers ``turn/start``, and a
        queue installed after the response would drop those first items on the
        floor.
        """
        rpc = self._rpc_or_raise()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._turn_queue = queue
        try:
            await rpc.request(
                M_TURN_START,
                {F_THREAD_ID: thread_id, F_INPUT: [{F_TYPE: F_INPUT_TYPE_TEXT, F_TEXT: text}]},
                timeout_s=self._request_timeout_s,
            )
            saw_agent_message = False
            descriptions: list[str] = []
            while True:
                frame = await queue.get()
                method = frame.get("method")
                if method == _EOF_MARKER:
                    yield self._exit_frame(rpc, await self._exit_code())
                    return
                if method in ITEM_NOTIFICATIONS:
                    item = pick(frame.get("params"), *ITEM_FIELDS, default={}) or {}
                    item_type = str(pick(item, *ITEM_TYPE_FIELDS, default="") or "")
                    if item_type == ITEM_TYPE_AGENT_MESSAGE:
                        saw_agent_message = True
                    if method == N_ITEM_COMPLETED:
                        descriptions.append(item_type or "unknown")
                    yield frame
                    continue
                if method == N_TURN_COMPLETED and not saw_agent_message:
                    # The silent turn. Never let this reach the UI as a
                    # success — see the module docstring.
                    yield self._silent_turn_frame(descriptions)
                    yield frame
                    return
                if method in TURN_NOTIFICATIONS:
                    yield frame
                    return
                logger.debug("dropping unexpected codex turn frame %s", method)
        finally:
            self._turn_queue = None

    @staticmethod
    def _silent_turn_frame(descriptions: list[str]) -> dict[str, Any]:
        seen = ", ".join(descriptions) if descriptions else "no items at all"
        return {
            "method": N_ITEM_COMPLETED,
            "params": {
                F_ITEM: {
                    F_TYPE: ITEM_TYPE_ERROR,
                    F_MESSAGE: (
                        "the codex turn completed without producing a response "
                        f"message (items seen: {seen})"
                    ),
                }
            },
        }

    @staticmethod
    def _exit_frame(rpc: StdioJsonRpc, code: int | None) -> dict[str, Any]:
        where = f"code {code}" if code is not None else "an unknown status"
        return {
            "method": N_TURN_FAILED,
            "params": {
                F_ERROR: {
                    F_MESSAGE: (
                        f"the codex app-server exited with {where} during the turn"
                        f"{rpc.stderr_detail()}"
                    )
                }
            },
        }

    async def _exit_code(self) -> int | None:
        if self._proc is None:  # pragma: no cover - turn_start implies a proc
            return None
        if self._proc.returncode is not None:
            return self._proc.returncode
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._proc.wait(), timeout=_EXIT_WAIT_S)
        return self._proc.returncode

    async def turn_interrupt(self, thread_id: str) -> None:
        """Best effort: a cancelled turn must stop the work upstream, but a
        failure to say so must not replace the cancellation with an error."""
        if self._rpc is None or self.closed:
            return
        with contextlib.suppress(Exception):
            await self._rpc.request(
                M_TURN_INTERRUPT, {F_THREAD_ID: thread_id}, timeout_s=_EXIT_WAIT_S
            )


class CodexBroker:
    """One :class:`CodexAppServer` per workspace, built on first use.

    The per-workspace lock (rather than one global one) is the same lesson
    ``claude.py``'s ``_Builder`` records: a spawn plus a handshake can take
    tens of seconds, and holding a process-wide lock across it would queue
    every other workspace's first turn — and shutdown — behind one cold start.
    """

    def __init__(
        self,
        *,
        argv: Sequence[str] | None = None,
        startup_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
    ) -> None:
        self._argv = list(argv) if argv else None
        self._startup_timeout_s = startup_timeout_s
        self._request_timeout_s = request_timeout_s
        self._servers: dict[str, CodexAppServer] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards the two dicts and nothing else — never held across a spawn.
        self._guard = asyncio.Lock()

    async def get(self, workspace: str | None) -> CodexAppServer:
        key = workspace or ""
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            server = self._servers.get(key)
            if server is not None and not server.closed:
                return server
            if server is not None:
                # It closed, or it died mid-turn. Forget it and reap it before
                # spawning its replacement: handing the dead one back is how a
                # single crash turns into every later turn in this workspace
                # timing out, and dropping it without closing it leaks the
                # process group its helpers live in.
                async with self._guard:
                    if self._servers.get(key) is server:
                        del self._servers[key]
                with contextlib.suppress(Exception):
                    await server.close()
            server = CodexAppServer(
                workspace=key,
                argv=self._argv,
                startup_timeout_s=self._startup_timeout_s,
                request_timeout_s=self._request_timeout_s,
            )
            await server.start()
            async with self._guard:
                self._servers[key] = server
            return server

    async def drop(self, workspace: str | None) -> None:
        """Close and forget one workspace's server (it crashed, or its last
        session went away). The next ``get`` spawns a fresh one."""
        key = workspace or ""
        async with self._guard:
            server = self._servers.pop(key, None)
        if server is not None:
            await server.close()

    async def aclose_all(self) -> None:
        async with self._guard:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            with contextlib.suppress(Exception):
                await server.close()
