"""Out-of-process sub-provider worker.

Run as ``python -m backend.app.orchestrator.fleet.subproc``. Reads a single
JSON request line from stdin, runs :func:`collect_text` for one sub-provider
step, and writes the result back on stdout.

**Why a subprocess.** The orchestrator is itself a ``claude_agent_sdk``
session. Driving a second ``query()`` from inside its MCP tool callback
deadlocks the SDK — its internal ``_process_query_inner`` async generator
cannot be closed while another is interleaved on the same process
(``RuntimeError: aclose(): asynchronous generator is already running``). A
thread with its own loop is not enough; the SDK has process-global state.
A separate OS process is fully isolated — verified: two concurrent claude
sessions in separate processes both stream fine — and it gives the parent
true cancellation (kill the process reclaims a wedged ``claude`` CLI).

Stdout protocol (line-oriented, so the parent can react before completion):

  ``@@FIRST@@``            — emitted once, the instant the sub-provider yields
                             its first event (drives the parent's honest
                             heartbeat / fast-fail logic).
  ``@@RESULT@@ <json>``    — terminal. ``{"ok": true, "text": "..."}`` or
                             ``{"ok": false, "error": "..."}``.

Anything else on stdout/stderr is diagnostic noise and ignored by the parent.
"""
from __future__ import annotations

import asyncio
import json
import sys

from .collect import collect_text
from .models import RoleConfig

_FIRST_MARKER = "@@FIRST@@"
_RESULT_PREFIX = "@@RESULT@@ "


class _StdoutFirstSignal:
    """Duck-typed stand-in for ``threading.Event`` that ``collect_text``
    pokes on the first sub-provider event. Instead of flipping an in-memory
    flag (useless across a process boundary) it writes the FIRST marker so
    the parent can stop guessing whether the backend is alive."""

    __slots__ = ("_set",)

    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        if not self._set:
            self._set = True
            sys.stdout.write(_FIRST_MARKER + "\n")
            sys.stdout.flush()


def _emit_result(payload: dict) -> None:
    sys.stdout.write(_RESULT_PREFIX + json.dumps(payload) + "\n")
    sys.stdout.flush()


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


async def _main() -> None:
    try:
        req = json.loads(sys.stdin.readline())
        role = RoleConfig(
            provider=req["provider"],
            model=req["model"],
            system_prompt=req.get("system_prompt", ""),
        )
        text = await collect_text(
            role,
            req["prompt"],
            req.get("cwd"),
            req.get("additional_dirs") or [],
            permission_mode=req.get("permission_mode"),
            role_name=req.get("role_name"),
            progress=_StdoutFirstSignal(),
        )
        _emit_result({"ok": True, "text": text})
    # BaseException (not just Exception): anyio TaskGroups surface failures as
    # BaseExceptionGroup, and a cancelled/torn-down SDK generator raises
    # CancelledError — both must still produce a STRUCTURED result so the
    # parent never sees an opaque "exited without a result".
    except BaseException as exc:  # noqa: BLE001
        _emit_result({"ok": False, "error": _describe(exc)})


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except BaseException as exc:  # noqa: BLE001
        # _main almost always emits its own structured result; this only
        # catches a failure in asyncio.run itself (loop teardown, the SDK's
        # "aclose(): asynchronous generator is already running"). Emit a
        # last-resort result so the parent still gets a reason, and echo to
        # the (captured) stderr for the backend log.
        try:
            _emit_result({"ok": False, "error": f"worker crashed: {_describe(exc)}"})
        except Exception:
            pass
        print(f"subproc fatal: {_describe(exc)}", file=sys.stderr, flush=True)
