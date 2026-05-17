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


async def _main() -> None:
    raw = sys.stdin.readline()
    try:
        req = json.loads(raw)
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
            progress=_StdoutFirstSignal(),
        )
        _emit_result({"ok": True, "text": text})
    except Exception as exc:  # noqa: BLE001 - report, never traceback to parent
        _emit_result({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    asyncio.run(_main())
