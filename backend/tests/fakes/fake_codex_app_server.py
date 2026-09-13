"""A stand-in for `codex app-server`, driven by a scenario name.

The `codex` CLI is not installed on the machine this was written on, and even
where it is, running it in the test suite would mean a network call, a paid
subscription and a several-second cold start per test. What the provider's
contract is actually *about* — framing, request/response correlation, the
approval round-trip, the silent turn, backpressure, a server that dies
mid-turn — is entirely protocol-shaped, so this script implements the protocol
and nothing else.

**No project imports.** It runs as a child process spawned by the code under
test; importing the app would mean the fake and the thing it is testing share
a module graph, and a broken import in the app would look like a broken fake.
That also means the wire names below are DUPLICATED from
``backend/app/orchestrator/codex/protocol.py`` on purpose — the duplication is
the test: if a schema bump edits protocol.py and not this file, the tests fail,
which is exactly the signal the reconciliation note in protocol.py asks for.

Usage::

    python fake_codex_app_server.py <scenario> [record-path]

Scenarios:

  ``happy``           reasoning, a command_execution pair, an agent_message,
                      then turn/completed with token counts.
  ``approval``        an ``execCommandApproval`` server request; the command
                      only runs on an approving decision. Serves any number of
                      turns, so one process can be driven twice.
  ``busy``            answers the first ``turn/start`` with ``-32001``, then
                      succeeds.
  ``silent``          a turn that completes with no ``agent_message``.
  ``crash``           exits non-zero mid-turn.
  ``patch_approval``  an ``applyPatchApproval`` request.
  ``file_create``     a ``file_change`` item whose kind is ``add``. Its own
                      scenario because it is the only thing that exercises the
                      translator's Write branch: every other scenario that
                      touches a file reports ``update``, so without this one
                      "a created file is a Write, not an Edit" was an
                      unasserted claim.
  ``child``           like ``happy``, but spawns a sleeping grandchild first
                      and records both pids. Not one of the protocol
                      scenarios — it exists so a test can prove the process
                      GROUP is reaped, which is the leak that actually bites
                      (the app-server's own helpers outlive a kill aimed at
                      the leader alone).
  ``rpc``             not a protocol scenario either: a peer for the
                      transport tests, answering ``test/*`` methods that put
                      ``StdioJsonRpc`` in the states a real app-server
                      reaches only by luck — two requests answered out of
                      order, a chosen error code, N busy replies, a
                      server→client round-trip whose answer it reports back,
                      a non-JSON line before a good frame, and a request it
                      never answers at all.

``record-path`` is a JSONL file the fake appends to: the environment it was
handed, every request it received, and (for ``child``) the pids. A file is
used rather than stdout because the code under test owns stdout and parses it
as protocol — the same reason ``tree_worker.py`` reports through a file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque

# ── wire vocabulary (deliberately duplicated — see the module docstring) ────
M_INITIALIZE = "initialize"
M_THREAD_START = "thread/start"
M_THREAD_RESUME = "thread/resume"
M_TURN_START = "turn/start"
M_TURN_INTERRUPT = "turn/interrupt"

N_ITEM_STARTED = "item/started"
N_ITEM_UPDATED = "item/updated"
N_ITEM_COMPLETED = "item/completed"
N_TURN_COMPLETED = "turn/completed"

R_EXEC_APPROVAL = "execCommandApproval"
R_PATCH_APPROVAL = "applyPatchApproval"

ERR_BUSY = -32001

APPROVING = ("approved", "approved_for_session")

THREAD_ID = "thread-fake-1"

# Long enough that a pid found dead is proof of a kill rather than of a race
# with a timeout — the same constant, for the same reason, as tree_worker.py.
_HANG_S = 600

_pending: deque = deque()
_next_request_id = 1000
_record_path: str | None = None


# ── plumbing ───────────────────────────────────────────────────────────────


def record(entry: dict) -> None:
    if _record_path is None:
        return
    with open(_record_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def respond(request_id, result) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def respond_error(request_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def notify(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def read_message():
    """Next inbound message, or ``None`` at EOF."""
    if _pending:
        return _pending.popleft()
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except ValueError:
        return {}


def ask(method: str, params: dict) -> str:
    """Send a server→client request and block until its response.

    Anything else that arrives while waiting is queued, not dropped: the whole
    point of this round-trip is that the client keeps its reader running while
    a human answers the card.
    """
    global _next_request_id
    _next_request_id += 1
    request_id = _next_request_id
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        msg = read_message()
        if msg is None:
            sys.exit(0)
        if msg.get("id") == request_id and ("result" in msg or "error" in msg):
            result = msg.get("result") or {}
            return str(result.get("decision") or "denied")
        _pending.append(msg)


def item(method: str, payload: dict) -> None:
    notify(method, {"threadId": THREAD_ID, "item": payload})


# ── scenario pieces ────────────────────────────────────────────────────────


def emit_reasoning(text: str) -> None:
    item(N_ITEM_STARTED, {"id": "r1", "type": "reasoning", "text": ""})
    item(N_ITEM_COMPLETED, {"id": "r1", "type": "reasoning", "text": text})


def emit_command(item_id: str, command, exit_code: int, output: str, *, started: bool = True):
    if started:
        item(N_ITEM_STARTED, {"id": item_id, "type": "command_execution", "command": command})
    item(
        N_ITEM_COMPLETED,
        {
            "id": item_id,
            "type": "command_execution",
            "command": command,
            "exit_code": exit_code,
            "aggregated_output": output,
        },
    )


def emit_agent_message(final: str, *, stream: str | None = None) -> None:
    item(N_ITEM_STARTED, {"id": "m1", "type": "agent_message", "text": ""})
    if stream is not None:
        item(N_ITEM_UPDATED, {"id": "m1", "type": "agent_message", "text": stream})
    item(N_ITEM_COMPLETED, {"id": "m1", "type": "agent_message", "text": final})


def emit_turn_completed() -> None:
    notify(
        N_TURN_COMPLETED,
        {
            "threadId": THREAD_ID,
            "usage": {
                "input_tokens": 11,
                "cached_input_tokens": 4,
                "output_tokens": 7,
            },
        },
    )


def run_turn(scenario: str, request_id, turn_index: int) -> None:
    if scenario == "busy" and turn_index == 0:
        # Backpressure, not failure: the client is expected to back off and
        # send the request again rather than surfacing it.
        respond_error(request_id, ERR_BUSY, "the app-server is busy")
        return

    respond(request_id, {"turnId": f"turn-{turn_index}"})

    if scenario == "crash":
        item(N_ITEM_STARTED, {"id": "c1", "type": "command_execution", "command": ["sleep", "1"]})
        sys.stdout.flush()
        # No result, no turn/completed, no clean exit — the case where the
        # provider has to notice EOF and say so.
        os._exit(3)

    if scenario == "silent":
        # Completes with no agent_message: the turn produced no response body.
        item(N_ITEM_COMPLETED, {"id": "r1", "type": "reasoning", "text": ""})
        emit_turn_completed()
        return

    if scenario == "approval":
        decision = ask(
            R_EXEC_APPROVAL,
            {
                "threadId": THREAD_ID,
                "callId": f"call-{turn_index}",
                "command": ["rm", "-rf", "build"],
                "cwd": os.getcwd(),
                "reason": "the command writes outside the sandbox",
            },
        )
        record({"kind": "decision", "request": R_EXEC_APPROVAL, "decision": decision})
        if decision in APPROVING:
            emit_command("c1", ["rm", "-rf", "build"], 0, "removed build/\n")
            emit_agent_message("Removed the build directory.")
        else:
            # The rejection comes back to the agent as a failed tool call. No
            # item/started was ever sent for it, which is also the case the
            # provider has to synthesize a tool_use for.
            emit_command(
                "c1",
                ["rm", "-rf", "build"],
                1,
                "command rejected by LocalCode",
                started=False,
            )
            emit_agent_message("The command was denied, so I stopped.")
        emit_turn_completed()
        return

    if scenario == "file_create":
        created = os.path.join(os.getcwd(), "new_module.py")
        item(
            N_ITEM_COMPLETED,
            {
                "id": "f1",
                "type": "file_change",
                "changes": {created: {"kind": "add"}},
            },
        )
        emit_agent_message("Created the module.")
        emit_turn_completed()
        return

    if scenario == "patch_approval":
        target = os.path.join(os.getcwd(), "notes.md")
        decision = ask(
            R_PATCH_APPROVAL,
            {
                "threadId": THREAD_ID,
                "callId": f"patch-{turn_index}",
                "changes": {target: {"kind": "update"}},
                "reason": "the patch touches a tracked file",
            },
        )
        record({"kind": "decision", "request": R_PATCH_APPROVAL, "decision": decision})
        if decision in APPROVING:
            item(
                N_ITEM_COMPLETED,
                {
                    "id": "f1",
                    "type": "file_change",
                    "changes": {target: {"kind": "update"}},
                },
            )
            emit_agent_message("Applied the patch.")
        else:
            emit_agent_message("The patch was denied, so nothing changed.")
        emit_turn_completed()
        return

    # happy / busy-after-retry / child
    emit_reasoning("Considering the request.")
    emit_command("c1", ["echo", "hi"], 0, "hi\n")
    emit_agent_message("Hello, world.", stream="Hello")
    emit_turn_completed()


# ── transport-test support (the ``rpc`` scenario) ──────────────────────────

_deferred: list = []
_attempts: dict = {}


def ask_raw(method: str, params: dict) -> dict:
    """Send a server→client request and return the whole response frame.

    The transport tests care about the *shape* of the answer — a result, or
    an error with a code — because "a handler that raises still answers" is
    only observable from here.
    """
    global _next_request_id
    _next_request_id += 1
    request_id = _next_request_id
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        msg = read_message()
        if msg is None:
            sys.exit(0)
        if msg.get("id") == request_id and ("result" in msg or "error" in msg):
            return msg
        _pending.append(msg)


def handle_rpc_method(method: str, request_id, params: dict) -> bool:
    """Answer one ``test/*`` method. False means "not mine"."""
    if method == "test/deferred":
        # Parked, not answered: the test sends two of these and then a flush,
        # which answers them backwards. The acknowledgement is a NOTIFICATION
        # so the test can wait for it instead of sleeping — an ordering test
        # that races is worse than no ordering test.
        _deferred.append((request_id, params.get("tag")))
        notify("test/queued", {"tag": params.get("tag")})
        return True
    if method == "test/flush":
        for deferred_id, tag in reversed(_deferred):
            respond(deferred_id, {"tag": tag})
        count = len(_deferred)
        _deferred.clear()
        respond(request_id, {"flushed": count})
        return True
    if method == "test/error":
        respond_error(request_id, int(params.get("code", -32000)), str(params.get("message", "")))
        return True
    if method == "test/busy":
        _attempts[method] = _attempts.get(method, 0) + 1
        if _attempts[method] <= int(params.get("times", 1)):
            respond_error(request_id, ERR_BUSY, "the app-server is busy")
        else:
            respond(request_id, {"attempts": _attempts[method]})
        return True
    if method == "test/attempts":
        # Reads the counter WITHOUT touching it, so a test can pin the exact
        # number of tries rather than "at least this many".
        respond(request_id, {"attempts": _attempts.get(str(params.get("of") or ""), 0)})
        return True
    if method == "test/ask":
        response = ask_raw(
            str(params.get("method") or R_EXEC_APPROVAL), params.get("payload") or {}
        )
        respond(request_id, {"response": response})
        return True
    if method == "test/noise":
        # Exactly what the real binary interleaves with protocol frames.
        sys.stdout.write("codex app-server starting\n")
        sys.stdout.write("{ this is not json\n")
        sys.stdout.flush()
        respond(request_id, {"ok": True})
        return True
    if method == "test/hang":
        # Deliberately unanswered: close() has to fail this one itself.
        return True
    return False


# ── main loop ──────────────────────────────────────────────────────────────


def main() -> None:
    global _record_path
    scenario = sys.argv[1] if len(sys.argv) > 1 else "happy"
    _record_path = sys.argv[2] if len(sys.argv) > 2 else None

    # Proves the spawn environment carries no credential the harness invented.
    record(
        {
            "kind": "env",
            "secretish": sorted(
                key
                for key in os.environ
                if key.endswith(("_API_KEY", "_OAUTH_TOKEN", "_SESSION_KEY", "_AUTH_TOKEN"))
            ),
            "cwd": os.getcwd(),
        }
    )

    if scenario == "child":
        # A grandchild of the backend, exactly like the helpers the real
        # app-server spawns. It survives a kill aimed at this process alone.
        grandchild = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", f"import time; time.sleep({_HANG_S})"]
        )
        record({"kind": "pids", "server": os.getpid(), "child": grandchild.pid})

    turn_index = 0
    while True:
        msg = read_message()
        if msg is None:
            return
        method = msg.get("method")
        if method is None:
            continue  # a response to one of our requests, already consumed
        request_id = msg.get("id")
        record({"kind": "request", "method": method, "params": msg.get("params")})

        if scenario == "rpc" and handle_rpc_method(method, request_id, msg.get("params") or {}):
            continue

        if method == M_INITIALIZE:
            respond(
                request_id,
                {
                    "serverInfo": {"name": "fake-codex-app-server", "version": "0.0.0"},
                    "capabilities": {},
                },
            )
        elif method in (M_THREAD_START, M_THREAD_RESUME):
            respond(request_id, {"threadId": THREAD_ID})
        elif method == M_TURN_START:
            run_turn(scenario, request_id, turn_index)
            turn_index += 1
        elif method == M_TURN_INTERRUPT:
            respond(request_id, {"ok": True})
        elif request_id is not None:
            # Never silently drop a request: an unanswered one wedges the
            # caller for its whole timeout.
            respond_error(request_id, -32601, f"fake app-server has no {method}")

        # Give a "hang" style scenario somewhere to live without a busy loop.
        if scenario == "hang":
            time.sleep(_HANG_S)


if __name__ == "__main__":
    main()
