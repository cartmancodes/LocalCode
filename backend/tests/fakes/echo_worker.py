"""Fake v2 worker: speaks the pool's protocol with no SDK import.

The pool's contract is entirely about framing, demultiplexing and process
lifetime. Testing that against the real worker would mean importing
``claude_agent_sdk`` and spawning a vendor CLI in every test — slow, and it
would test the SDK rather than the pool. This script implements the protocol
exactly and nothing else, so the pool's behaviour under reuse, eviction,
killing and malformed input is testable in milliseconds.

It writes the same pidfile the real worker writes (through the real
``write_worker_pidfile``), because the startup sweep identifies our workers by
that file plus a command-line check, and a fake with a different file shape
would let the sweep pass while the real thing breaks.

Request knobs, read from the request's ``prompt`` field so no new protocol
fields are needed:

  ``pid``                  summary is this process's pid
  ``served``               summary is how many requests this process has served
  ``big:<n>``              summary is ``n`` bytes of payload — the D7.1 case,
                           which on v1 framing deadlocked both sides
  ``utf8:<n>``             summary is ``n`` multi-byte characters, so a
                           byte-counted length prefix is distinguishable from a
                           character-counted one
  ``slow:<seconds>``       sleep, then echo — lets a test observe @@FIRST@@
                           arriving before the result
  ``nofirst``              skip @@FIRST@@ entirely
  ``fail:<message>``       reply ``{"ok": false, "error": message}``
  ``crash:<message>``      write ``message`` to stderr and exit with no result —
                           the case where the stderr tail is the only window
                           into why a step failed
  ``hang``                 never reply (the test kills it)
  anything else            summary is the prompt verbatim
"""
from __future__ import annotations

import json
import os
import sys
import time

from backend.app.orchestrator.fleet.constants import FIRST_MARKER, RESULT_MARKER
from backend.app.orchestrator.fleet.pool import (
    remove_worker_pidfile,
    write_worker_pidfile,
)

_MODULE_NAME = __spec__.name if __spec__ is not None else __name__


def _write(data: bytes) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _emit_result(request_id: str, payload: dict) -> None:
    """Length-prefixed exactly as the real worker frames it."""
    body = json.dumps(payload).encode("utf-8")
    _write(f"{RESULT_MARKER} {request_id} {len(body)}\n".encode() + body + b"\n")


def _envelope(summary: str, served: int) -> dict:
    return {
        "summary": summary,
        "structured": None,
        "artifact_id": None,
        "artifact_path": None,
        "tool_digest": "",
        "full_bytes": len(summary.encode("utf-8")),
        # A real count, so a test can assert the per-turn token budget sums
        # something that came across the wire rather than a local constant.
        "usage": {"input_tokens": 3, "output_tokens": served},
    }


def _summary(prompt: str, served: int) -> str:
    if prompt == "pid":
        return str(os.getpid())
    if prompt == "served":
        return str(served)
    if prompt.startswith("big:"):
        return "B" * int(prompt[len("big:") :])
    if prompt.startswith("utf8:"):
        return "é" * int(prompt[len("utf8:") :])
    if prompt.startswith("slow:"):
        time.sleep(float(prompt[len("slow:") :]))
        return f"slept {prompt[len('slow:'):]}"
    return prompt


def main() -> None:
    pidfile = write_worker_pidfile(_MODULE_NAME)
    served = 0
    try:
        while True:
            line = sys.stdin.readline()
            if line == "":
                return  # EOF — exit 0, like the real worker
            line = line.strip()
            if not line:
                continue
            served += 1
            request_id = "unknown"
            try:
                request = json.loads(line)
                # The id is read BEFORE any field that can be missing: a reply
                # the pool cannot attribute is a reply it drops, and the caller
                # then waits out its whole step budget on a request that was
                # already answered.
                request_id = str(request.get("id") or "unknown")
                prompt = str(request["prompt"])
            except (ValueError, KeyError, AttributeError) as exc:
                # Malformed: answer with an error and stay alive for the next
                # request. Silence here is indistinguishable from a hang.
                _emit_result(request_id, {"ok": False, "error": f"bad request: {exc!r}"})
                continue
            if prompt.startswith("crash:"):
                # No result, a diagnostic on stderr, then gone. The pool must
                # surface that tail: it is the ONLY evidence of the cause.
                print(prompt[len("crash:") :], file=sys.stderr, flush=True)
                return
            if prompt == "hang":
                _write(f"{FIRST_MARKER} {request_id}\n".encode())
                # Long enough that a pid found dead is proof of the kill rather
                # than of a race with a timeout.
                time.sleep(600)
                return
            if prompt != "nofirst":
                _write(f"{FIRST_MARKER} {request_id}\n".encode())
            if prompt.startswith("fail:"):
                _emit_result(
                    request_id, {"ok": False, "error": prompt[len("fail:") :]}
                )
                continue
            _emit_result(
                request_id, {"ok": True, "result": _envelope(_summary(prompt, served), served)}
            )
    finally:
        remove_worker_pidfile(pidfile)


if __name__ == "__main__":
    main()
