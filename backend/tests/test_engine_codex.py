from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from backend.app.core.engines import EngineConfig, EngineHooks
from backend.app.core.engines.codex import CodexEngine
from backend.app.core.engines.codex_rpc import CodexAppServerClient, CodexRpcError
from backend.app.core.messages import user_message

FAKE = [sys.executable, str(Path(__file__).with_name("fake_codex_server.py"))]


async def run(engine: CodexEngine, text: str, hooks: EngineHooks | None = None) -> list[dict]:
    return [ev async for ev in engine.prompt(user_message(text), hooks or EngineHooks())]


async def make_engine(**cfg: Any) -> CodexEngine:
    eng = CodexEngine(command=FAKE)
    await eng.start(EngineConfig(cwd="/tmp", **cfg))
    return eng


async def test_start_resume_and_rate_limits() -> None:
    eng = await make_engine(model="gpt-5.5-mini")
    assert eng.session_id == "thr_1" and eng.model == "gpt-5.5-mini"
    evs = await run(eng, "hello")
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "engine_session" and evs[0]["sessionId"] == "thr_1"
    assert "rate_limit" in kinds
    rl = next(e for e in evs if e["type"] == "rate_limit")["info"]
    assert rl["plan"] == "plus" and rl["primary"]["usedPercent"] == 42
    await eng.close()
    resumed = await make_engine(session_id="thr_1")
    assert resumed.session_id == "thr_1"
    await resumed.close()


async def test_turn_projection_and_message_stream() -> None:
    eng = await make_engine()
    evs = await run(eng, "hello")
    kinds = [e["type"] for e in evs]
    assert kinds[-3:] == ["message_end", "turn_end", "agent_end"]
    assert kinds.count("turn_start") == 1 and kinds.count("message_start") == 1
    deltas = [e["assistantMessageEvent"] for e in evs if e["type"] == "message_update"]
    assert "".join(d["delta"] for d in deltas if d["type"] == "thinking_delta") == "thinking…"
    assert "".join(d["delta"] for d in deltas if d["type"] == "text_delta") == "Hello from codex"
    final = evs[-1]["messages"][-1]
    assert final["role"] == "assistant" and final["stopReason"] == "stop"
    assert final["usage"]["input"] == 12 and final["usage"]["cacheRead"] == 3
    assert final["provider"] == "openai"
    await eng.close()


async def test_approval_routes_through_hooks() -> None:
    eng = await make_engine()
    seen: list[Any] = []

    async def before(name: str, call_id: str, args: dict) -> dict | None:
        seen.append(("before", name, call_id, args["command"]))
        return None

    async def permission(name: str, args: dict, ctx: dict) -> dict | None:
        seen.append(("permission", name, ctx["tool_use_id"]))
        return {"allow": True}

    evs = await run(
        eng, "use a tool", EngineHooks(before_tool=before, permission_request=permission)
    )
    assert seen == [("before", "bash", "c1", "ls -la"), ("permission", "bash", "c1")]
    start = next(e for e in evs if e["type"] == "tool_execution_start")
    assert start["toolName"] == "bash" and start["args"]["command"] == "ls -la"
    update = next(e for e in evs if e["type"] == "tool_execution_update")
    assert update["partialResult"]["content"][0]["text"] == "file1\n"
    end = next(e for e in evs if e["type"] == "tool_execution_end")
    assert end["isError"] is False and end["result"]["content"][0]["text"] == "file1\n"
    turn_end = next(e for e in evs if e["type"] == "turn_end")
    assert [c["type"] for c in turn_end["message"]["content"]] == ["thinking", "toolCall", "text"]
    assert turn_end["toolResults"][0]["toolName"] == "bash"
    roles = [m["role"] for m in evs[-1]["messages"]]
    assert roles == ["toolResult", "assistant"]
    await eng.close()


async def test_blocked_and_unapproved_tools_are_declined() -> None:
    eng = await make_engine()

    async def before(name: str, call_id: str, args: dict) -> dict | None:
        return {"block": True, "reason": "no"}

    evs = await run(eng, "use a tool", EngineHooks(before_tool=before))
    end = next(e for e in evs if e["type"] == "tool_execution_end")
    assert end["isError"] is True
    # no permission handler at all → declined, and the engine says why
    evs = await run(eng, "use a tool", EngineHooks())
    assert any("no approval handler" in e.get("message", "") for e in evs if e["type"] == "error")
    assert next(e for e in evs if e["type"] == "tool_execution_end")["isError"] is True
    await eng.close()


async def test_interrupt_steer_fork_compact() -> None:
    eng = await make_engine()
    seen: list[str] = []

    async def permission(name: str, args: dict, ctx: dict) -> dict:
        # Steer + interrupt while the server is blocked on this approval, so
        # both reach it before our answer does.
        seen.append("steer:%s" % await eng.steer(user_message("hurry")))
        await eng.interrupt()
        return {"allow": True}

    events = await run(eng, "use a tool", EngineHooks(permission_request=permission))
    assert seen == ["steer:True"]
    assert events[-1]["type"] == "agent_end" and events[-1]["aborted"] is True
    assert not any(
        e["type"] == "message_end" and "codex" in str(e["message"]["content"]) for e in events
    )
    assert await eng.steer(user_message("late")) is False  # not running
    new_id = await eng.fork()
    assert new_id == "thr_fork" and eng.session_id == "thr_fork"
    await eng.compact()
    evs = await run(eng, "hello")
    assert evs[0]["type"] == "engine_session" and evs[0]["sessionId"] == "thr_fork"
    assert any(e["type"] == "compaction" for e in evs)
    await eng.close()


async def _allow(name: str, args: dict, ctx: dict) -> dict:
    return {"allow": True}


async def test_a_duplicate_error_message_is_not_reported_twice() -> None:
    """The real app-server can send BOTH a standalone "error" notification
    and turn/completed's own turn.error for the exact same failure (see
    backend/tests/fixtures/codex_real_trace_2026-09-14.json, captured live
    against codex-cli 0.154.0). The engine must not surface that as two
    separate error events to a client."""
    eng = await make_engine()
    evs = await run(eng, "rate limited")
    errors = [e for e in evs if e["type"] == "error"]
    assert len(errors) == 1, [e["message"] for e in errors]
    assert "usage limit" in errors[0]["message"].lower()
    assert evs[-1]["type"] == "agent_end"
    await eng.close()


async def test_rpc_client_busy_retry_and_unknown_method() -> None:
    client = CodexAppServerClient(FAKE)
    await client.start()
    await client.initialize()
    with pytest.raises(CodexRpcError) as info:
        await client.request_with_retry("busy/test", attempts=2)
    assert info.value.code == -32001
    with pytest.raises(CodexRpcError):
        await client.request("nope")
    await client.close()
    assert client.closed.is_set()


async def test_token_refresh_request_is_refused() -> None:
    """The invariant: this client never holds ChatGPT tokens."""
    eng = CodexEngine(command=FAKE)
    with pytest.raises(Exception, match="never holds ChatGPT tokens"):
        await eng._on_server_request("account/chatgptAuthTokens/refresh", {})
