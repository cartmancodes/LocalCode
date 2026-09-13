"""Out-of-process sub-provider worker — a request **loop**, not one shot.

Run as ``python -m backend.app.orchestrator.fleet.subproc``. Reads JSON request
lines from stdin, runs :func:`collect_step` for each one, and writes the framed
result back on stdout. Exits 0 on EOF.

**Why a subprocess.** The orchestrator is itself a ``claude_agent_sdk``
session. Driving a second ``query()`` from inside its MCP tool callback
deadlocks the SDK — its internal ``_process_query_inner`` async generator
cannot be closed while another is interleaved on the same process
(``RuntimeError: aclose(): asynchronous generator is already running``). A
thread with its own loop is not enough; the SDK has process-global state.
A separate OS process is fully isolated — verified: two concurrent claude
sessions in separate processes both stream fine — and it gives the parent
true cancellation (kill the process reclaims a wedged ``claude`` CLI).

**Why a loop.** Spawning this process per step paid interpreter start + SDK
import + CLI spawn every time: 0.7 to 1.0 s before any model token. Serving
many requests from one process pays it once. What is *not* reused is the
conversation — each request builds a fresh sub-provider via ``collect_step``,
so one role's context can never leak into the next role's step.

Stdout protocol (v2), multiplexed by request id so one process can serve many:

  ``@@FIRST@@ <id>``                  — emitted once per request, the instant the
                                        sub-provider yields its first event
                                        (drives the parent's honest heartbeat /
                                        fast-fail logic).
  ``@@RESULT@@ <id> <byte-length>``   — terminal for that request, followed by
                                        EXACTLY ``<byte-length>`` bytes of JSON
                                        and a newline. ``{"ok": true, "result":
                                        <StepResult wire dict>}`` or
                                        ``{"ok": false, "error": "..."}``.

The length prefix is load-bearing. v1 put the whole result on one
newline-terminated line, so a plan over the reader's 64 KiB line limit did not
merely truncate — the parent raised on the line while this process was still
blocked writing the rest into a pipe nobody was draining, and both sides hung.
Framing by length removes the dependency on any reader limit at all.

The success payload carries the whole :class:`StepResult` envelope rather than
the step's text, because the parent needs more than text: the parsed verdict it
routes on, the artifact pointer for the output that was too big to inline, and
the token usage the per-turn budget spends. Sending text alone forced the
parent to re-derive all three from prose.

Anything else on stdout/stderr is diagnostic noise and ignored by the parent.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

from .collect import collect_step
from .constants import FIRST_MARKER, RESULT_MARKER
from .models import RoleConfig
from .pool import remove_worker_pidfile, write_worker_pidfile

# When a request line is too broken to name itself. The parent logs a result
# for an unknown id and drops it, which is strictly better than this process
# staying silent: silence is indistinguishable from a wedged backend.
_UNKNOWN_ID = "unknown"

# Under ``-m`` this module's ``__name__`` is ``"__main__"``, which would record
# a useless label in the pidfile. ``__spec__.name`` is the dotted path the pool
# actually spawned.
_MODULE_NAME = __spec__.name if __spec__ is not None else __name__


def _write(data: bytes) -> None:
    """One byte-level writer for everything this worker emits.

    Goes through ``sys.stdout.buffer`` because the framed result is counted in
    *bytes*: a text-layer write of a str whose UTF-8 encoding is longer than
    its character count would make the parent's ``readexactly`` read into the
    next line. The text layer is flushed first so interleaved ``print``
    diagnostics cannot land inside a framed payload.
    """
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


class _StdoutFirstSignal:
    """Duck-typed stand-in for ``threading.Event`` that ``collect_step``
    pokes on the first sub-provider event. Instead of flipping an in-memory
    flag (useless across a process boundary) it writes the FIRST marker so
    the parent can stop guessing whether the backend is alive.

    Carries the request id: one process now serves many requests, so an
    unattributed marker would set the first-signal of whichever request the
    parent happened to be waiting on."""

    __slots__ = ("_request_id", "_set")

    def __init__(self, request_id: str) -> None:
        self._request_id = request_id
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        if not self._set:
            self._set = True
            _write(f"{FIRST_MARKER} {self._request_id}\n".encode())


def _emit_result(request_id: str, payload: dict) -> None:
    """Write one length-prefixed result for ``request_id``."""
    body = json.dumps(payload).encode("utf-8")
    _write(f"{RESULT_MARKER} {request_id} {len(body)}\n".encode() + body + b"\n")


def _describe(exc: BaseException) -> str:
    """Flatten ExceptionGroup/BaseExceptionGroup (anyio task groups in
    claude-agent-sdk raise these on Python 3.11) so the real cause survives
    instead of a useless 'unhandled errors in a TaskGroup'."""
    parts: list[str] = []
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        subs = getattr(e, "exceptions", None)
        if subs:
            stack.extend(subs)
        else:
            parts.append(f"{type(e).__name__}: {e}")
    return " | ".join(dict.fromkeys(parts)) or f"{type(exc).__name__}: {exc}"


async def handle_request(line: str) -> None:
    """Run one request to completion and emit exactly one framed result.

    Always emits — a malformed line, a dead backend and a cancelled SDK
    generator all produce a structured error, because the parent's only
    alternative reading of silence is "worker exited without a result", the
    least useful message available.
    """
    request_id = _UNKNOWN_ID
    try:
        request = json.loads(line)
        request_id = str(request.get("id") or _UNKNOWN_ID)
        role = RoleConfig(
            provider=request["provider"],
            model=request["model"],
            system_prompt=request.get("system_prompt", ""),
        )
        result = await collect_step(
            role,
            request["prompt"],
            request.get("cwd"),
            request.get("additional_dirs") or [],
            permission_mode=request.get("permission_mode"),
            role_name=request.get("role_name"),
            progress=_StdoutFirstSignal(request_id),
            session_id=request.get("session_id"),
        )
        _emit_result(request_id, {"ok": True, "result": result.to_wire()})
    # BaseException (not just Exception): anyio TaskGroups surface failures as
    # BaseExceptionGroup, and a cancelled/torn-down SDK generator raises
    # CancelledError — both must still produce a STRUCTURED result so the
    # parent never sees an opaque "exited without a result".
    except BaseException as exc:  # noqa: BLE001
        _emit_result(request_id, {"ok": False, "error": _describe(exc)})
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            # A structured error for the request in flight, then out — these
            # two mean "stop", and swallowing them would make the loop
            # unkillable by anything short of SIGKILL.
            raise


async def serve(read_line: Callable[[], Awaitable[str]]) -> None:
    """The request loop.

    One request is handled to completion before the next line is read, so a
    worker is never running two sub-providers at once — the pool provides
    concurrency by running several workers instead. A blank line is ignored, a
    malformed one answers with an error result and the loop stays alive, and EOF
    returns so the process can exit 0.

    ``read_line`` is injected so the loop is testable without a real stdin.
    """
    while True:
        line = await read_line()
        if line == "":
            return  # EOF
        line = line.strip()
        if not line:
            continue
        await handle_request(line)


async def _read_stdin_line() -> str:
    """One line from stdin, off the event loop.

    A thread rather than asyncio's stdin reader: a blocking ``readline`` in a
    thread keeps this module's ``BaseException`` → structured-result guarantee
    intact (``connect_read_pipe`` failures on a non-pipe stdin would raise
    during *setup*, before any request exists to report them against), and
    nothing else runs on this loop while we wait for work.
    """
    return await asyncio.to_thread(sys.stdin.readline)


async def _main() -> None:
    await serve(_read_stdin_line)


if __name__ == "__main__":
    # Recorded before the first request so the pool's startup sweep can reclaim
    # this process even if the backend is SIGKILLed immediately after spawning
    # it — nothing gets to run a handler in that case, and a worker in its own
    # session never sees the terminal's signals either.
    _pidfile = write_worker_pidfile(_MODULE_NAME)
    try:
        asyncio.run(_main())
    except BaseException as exc:  # noqa: BLE001
        # ``handle_request`` almost always emits its own structured result;
        # this only catches a failure in asyncio.run itself (loop teardown, the
        # SDK's "aclose(): asynchronous generator is already running"). Emit a
        # last-resort result so the parent still gets a reason, and echo to the
        # (captured) stderr for the backend log.
        try:
            _emit_result(_UNKNOWN_ID, {"ok": False, "error": f"worker crashed: {_describe(exc)}"})
        except Exception:
            pass
        print(f"subproc fatal: {_describe(exc)}", file=sys.stderr, flush=True)
    finally:
        remove_worker_pidfile(_pidfile)
