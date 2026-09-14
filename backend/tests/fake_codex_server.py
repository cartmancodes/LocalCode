"""A stand-in for ``codex app-server`` speaking its JSON-RPC line protocol.

Scripted turn: reasoning → command execution (asks for approval) → agent
message → token usage → turn/completed. Handles interrupt, fork, compact and
rate-limit reads so the engine's control surface can be tested without the
real binary or any auth.
"""

from __future__ import annotations

import json
import sys
import threading

_lock = threading.Lock()
_ids = iter(range(1000, 100000))
_pending: dict[int, dict] = {}
_state = {"thread": "thr_1", "turn": None, "interrupted": False, "turns": 0}


def send(msg: dict) -> None:
    with _lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def notify(method: str, params: dict) -> None:
    send({"method": method, "params": params})


def server_request(method: str, params: dict) -> dict:
    """Send a server→client request and block until the client answers."""
    rid = next(_ids)
    ev = threading.Event()
    _pending[rid] = {"event": ev}
    send({"id": rid, "method": method, "params": params})
    ev.wait(timeout=10)
    return _pending.pop(rid, {}).get("response") or {}


def run_turn(turn_id: str, text: str) -> None:
    tid = _state["thread"]
    notify(
        "turn/started",
        {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress", "items": []}},
    )
    notify(
        "item/started",
        {
            "threadId": tid,
            "turnId": turn_id,
            "startedAtMs": 1,
            "item": {"type": "reasoning", "id": "r1", "summary": [], "content": []},
        },
    )
    notify(
        "item/reasoning/textDelta",
        {
            "threadId": tid,
            "turnId": turn_id,
            "itemId": "r1",
            "contentIndex": 0,
            "delta": "thinking…",
        },
    )
    notify(
        "item/completed",
        {
            "threadId": tid,
            "turnId": turn_id,
            "completedAtMs": 2,
            "item": {"type": "reasoning", "id": "r1", "summary": [], "content": ["thinking…"]},
        },
    )
    if "tool" in text:
        cmd = {
            "type": "commandExecution",
            "id": "c1",
            "command": "ls -la",
            "cwd": "/tmp",
            "commandActions": [],
            "status": "inProgress",
        }
        notify("item/started", {"threadId": tid, "turnId": turn_id, "startedAtMs": 3, "item": cmd})
        resp = server_request(
            "item/commandExecution/requestApproval",
            {
                "threadId": tid,
                "turnId": turn_id,
                "itemId": "c1",
                "command": "ls -la",
                "cwd": "/tmp",
                "startedAtMs": 3,
            },
        )
        decision = (resp.get("result") or {}).get("decision")
        if decision == "accept":
            notify(
                "item/commandExecution/outputDelta",
                {"threadId": tid, "turnId": turn_id, "itemId": "c1", "delta": "file1\n"},
            )
            done = {**cmd, "status": "completed", "aggregatedOutput": "file1\n", "exitCode": 0}
        else:
            done = {**cmd, "status": "declined", "aggregatedOutput": "", "exitCode": None}
        notify(
            "item/completed", {"threadId": tid, "turnId": turn_id, "completedAtMs": 4, "item": done}
        )
    if _state["interrupted"]:
        _state["interrupted"] = False
        notify(
            "turn/completed",
            {"threadId": tid, "turn": {"id": turn_id, "status": "interrupted", "items": []}},
        )
        return
    notify(
        "item/started",
        {
            "threadId": tid,
            "turnId": turn_id,
            "startedAtMs": 5,
            "item": {"type": "agentMessage", "id": "m1", "text": ""},
        },
    )
    for piece in ("Hello ", "from ", "codex"):
        notify(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "m1", "delta": piece},
        )
    notify(
        "item/completed",
        {
            "threadId": tid,
            "turnId": turn_id,
            "completedAtMs": 6,
            "item": {"type": "agentMessage", "id": "m1", "text": "Hello from codex"},
        },
    )
    notify(
        "thread/tokenUsage/updated",
        {
            "threadId": tid,
            "turnId": turn_id,
            "tokenUsage": {
                "last": {"inputTokens": 12, "outputTokens": 5, "cachedInputTokens": 3},
                "total": {"inputTokens": 12, "outputTokens": 5},
            },
        },
    )
    notify(
        "turn/completed",
        {"threadId": tid, "turn": {"id": turn_id, "status": "completed", "items": []}},
    )


def handle(msg: dict) -> None:
    if "id" in msg and "method" not in msg:  # client answering our request
        entry = _pending.get(msg["id"])
        if entry:
            entry["response"] = msg
            entry["event"].set()
        return
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method == "initialize":
        send(
            {
                "id": rid,
                "result": {
                    "userAgent": "fake-codex/0.0",
                    "platformOs": "test",
                    "platformFamily": "unix",
                    "codexHome": "/tmp",
                },
            }
        )
    elif method == "initialized":
        return
    elif method in ("thread/start", "thread/resume"):
        _state["thread"] = params.get("threadId") or "thr_1"
        send(
            {
                "id": rid,
                "result": {
                    "thread": {
                        "id": _state["thread"],
                        "cwd": params.get("cwd", "/"),
                        "cliVersion": "fake",
                        "createdAt": 0,
                        "ephemeral": False,
                        "modelProvider": "openai",
                    },
                    "model": params.get("model") or "gpt-5.5",
                    "modelProvider": "openai",
                    "cwd": params.get("cwd", "/"),
                    "approvalPolicy": "on-request",
                    "sandbox": params.get("sandbox"),
                },
            }
        )
        notify("thread/started", {"thread": {"id": _state["thread"]}})
    elif method == "thread/fork":
        _state["thread"] = "thr_fork"
        send(
            {
                "id": rid,
                "result": {
                    "thread": {"id": "thr_fork"},
                    "model": "gpt-5.5",
                    "modelProvider": "openai",
                    "cwd": "/",
                    "approvalPolicy": "on-request",
                },
            }
        )
    elif method == "turn/start":
        _state["turns"] += 1
        turn_id = f"turn_{_state['turns']}"
        _state["turn"] = turn_id
        send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        text = "".join(
            i.get("text", "") for i in params.get("input", []) if i.get("type") == "text"
        )
        threading.Thread(target=run_turn, args=(turn_id, text), daemon=True).start()
    elif method == "turn/interrupt":
        _state["interrupted"] = True
        send({"id": rid, "result": {}})
    elif method == "turn/steer":
        send({"id": rid, "result": {"turnId": params.get("expectedTurnId")}})
    elif method == "thread/compact/start":
        send({"id": rid, "result": {}})
        notify("thread/compacted", {"threadId": _state["thread"], "turnId": "compact"})
    elif method == "account/rateLimits/read":
        send(
            {
                "id": rid,
                "result": {
                    "rateLimits": {
                        "planType": "plus",
                        "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1},
                        "secondary": None,
                    }
                },
            }
        )
    elif method == "busy/test":
        send({"id": rid, "error": {"code": -32001, "message": "busy"}})
    else:
        send({"id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"fake server error: {exc}\n")


if __name__ == "__main__":
    main()
