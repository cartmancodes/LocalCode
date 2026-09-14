from __future__ import annotations

from typing import Any

from backend.app.core.engines import (
    AssistantMessageBuilder,
    EngineConfig,
    EngineHooks,
    FakeEngine,
    create_engine,
)
from backend.app.core.messages import user_message


async def collect(engine: FakeEngine, text: str, hooks: EngineHooks | None = None) -> list[dict]:
    return [ev async for ev in engine.prompt(user_message(text), hooks or EngineHooks())]


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def test_builder_emits_pi_shaped_stream() -> None:
    b = AssistantMessageBuilder(api="fake", provider="fake", model="m")
    evs = b.text_delta("hel") + b.text_delta("lo") + b.thinking_delta("hmm")
    evs += b.tool_call("c1", "bash", {"command": "ls"}) + b.finish("toolUse")
    assert [e["type"] for e in evs] == [
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "toolcall_start",
        "toolcall_end",
        "done",
    ]
    msg = b.message
    assert msg["content"][0] == {"type": "text", "text": "hello"}
    assert msg["content"][1]["thinking"] == "hmm"
    assert msg["content"][2]["name"] == "bash" and msg["stopReason"] == "toolUse"
    assert evs[3]["content"] == "hello" and evs[-1]["message"] is msg
    assert b.finish("aborted")[-1]["type"] == "error"


async def test_fake_engine_echo_run_sequence() -> None:
    eng = FakeEngine()
    await eng.start(EngineConfig(cwd="/tmp"))
    evs = await collect(eng, "hi there")
    assert types(evs)[:4] == ["engine_session", "agent_start", "turn_start", "message_start"]
    assert types(evs)[-3:] == ["message_end", "turn_end", "agent_end"]
    deltas = [e["assistantMessageEvent"] for e in evs if e["type"] == "message_update"]
    text = "".join(d["delta"] for d in deltas if d["type"] == "text_delta")
    assert text == "echo: hi there"
    assert deltas[-1]["type"] == "done" and deltas[-1]["reason"] == "stop"
    final = evs[-1]
    assert [m["role"] for m in final["messages"]] == ["assistant"] and final["aborted"] is False
    assert eng.session_id and evs[0]["sessionId"] == eng.session_id


async def test_fake_engine_scripted_tools_and_hooks() -> None:
    eng = FakeEngine(
        [
            [
                {
                    "text": "I'll look.",
                    "tools": [{"name": "read", "args": {"path": "a"}, "result": "A"}],
                },
                {"text": "done"},
            ]
        ]
    )
    await eng.start(EngineConfig(cwd="/tmp"))
    seen: dict[str, Any] = {"before": [], "after": [], "prompt": [], "stop": 0}

    async def before(name: str, call_id: str, args: dict) -> dict | None:
        seen["before"].append((name, dict(args)))
        return {"updated_input": {"path": "b"}}

    async def after(
        name: str, call_id: str, args: dict, content: list, is_error: bool
    ) -> dict | None:
        seen["after"].append((name, args, content, is_error))
        return {"content": [{"type": "text", "text": "B!"}]}

    async def before_prompt(text: str) -> dict | None:
        seen["prompt"].append(text)
        return None

    async def on_stop() -> None:
        seen["stop"] += 1

    hooks = EngineHooks(
        before_tool=before, after_tool=after, before_prompt=before_prompt, on_stop=on_stop
    )
    evs = await collect(eng, "go", hooks)
    assert seen["prompt"] == ["go"] and seen["stop"] == 1
    assert seen["before"] == [("read", {"path": "a"})]
    assert seen["after"][0][1] == {"path": "b"}  # rewritten input reached execution
    start = next(e for e in evs if e["type"] == "tool_execution_start")
    end = next(e for e in evs if e["type"] == "tool_execution_end")
    assert start["args"] == {"path": "b"}
    assert end["result"]["content"] == [{"type": "text", "text": "B!"}] and end["isError"] is False
    turns = [e for e in evs if e["type"] == "turn_end"]
    assert len(turns) == 2
    assert turns[0]["message"]["stopReason"] == "toolUse"
    assert turns[0]["toolResults"][0]["content"] == [{"type": "text", "text": "B!"}]
    assert turns[1]["message"]["stopReason"] == "stop"
    roles = [m["role"] for m in evs[-1]["messages"]]
    assert roles == ["assistant", "toolResult", "assistant"]


async def test_fake_engine_blocked_tool() -> None:
    eng = FakeEngine(
        [
            [
                {
                    "text": "rm time",
                    "tools": [{"name": "bash", "args": {"command": "rm -rf /"}, "result": "gone"}],
                }
            ]
        ]
    )
    await eng.start(EngineConfig(cwd="/tmp"))

    async def before(name: str, call_id: str, args: dict) -> dict | None:
        return {"block": True, "reason": "destructive"}

    after_called = []

    async def after(*a: Any) -> dict | None:
        after_called.append(a)
        return None

    evs = await collect(eng, "x", EngineHooks(before_tool=before, after_tool=after))
    end = next(e for e in evs if e["type"] == "tool_execution_end")
    assert end["isError"] is True and "destructive" in end["result"]["content"][0]["text"]
    assert after_called == []


async def test_fake_engine_interrupt_mid_run() -> None:
    eng = FakeEngine([[{"text": "a" * 50}, {"text": "never"}]], delay_s=0.001)
    await eng.start(EngineConfig(cwd="/tmp"))
    events: list[dict] = []
    async for ev in eng.prompt(user_message("x"), EngineHooks()):
        events.append(ev)
        if ev["type"] == "message_update" and ev["assistantMessageEvent"]["type"] == "text_delta":
            await eng.interrupt()
    assert events[-1]["type"] == "agent_end" and events[-1]["aborted"] is True
    assert sum(1 for e in events if e["type"] == "turn_start") == 1
    msg = next(e for e in events if e["type"] == "message_end")["message"]
    assert msg["stopReason"] == "aborted"


async def test_fake_engine_queues_fork_compact_and_mounted_tools() -> None:
    eng = FakeEngine(
        [[{"text": "using tool", "tools": [{"name": "greet", "args": {"who": "pi"}}]}]]
    )
    await eng.start(EngineConfig(cwd="/tmp", session_id="resume-me", model="fake-2"))
    assert eng.session_id == "resume-me" and eng.model == "fake-2"
    assert await eng.steer(user_message("stop")) is True
    await eng.follow_up(user_message("later"))
    from backend.app.core.extensions import ToolDefinition

    async def run(call_id: str, params: dict, signal: Any, on_update: Any, ctx: Any) -> dict:
        return {"content": [{"type": "text", "text": f"hello {params['who']}"}], "details": {}}

    calls = []

    async def executor(name: str, call_id: str, args: dict) -> dict:
        calls.append((name, args))
        return await run(call_id, args, None, None, None)

    await eng.mount_tools([ToolDefinition(name="greet", description="g", execute=run)], executor)
    evs = await collect(eng, "x")
    end = next(e for e in evs if e["type"] == "tool_execution_end")
    assert end["result"]["content"][0]["text"] == "hello pi" and calls == [("greet", {"who": "pi"})]
    # the steer was consumed after the first turn and lands in the produced messages
    assert any(m.get("role") == "user" for m in evs[-1]["messages"])
    old = eng.session_id
    assert await eng.fork() != old and eng.session_id != old
    await eng.compact("shorter")
    assert eng.compactions == ["shorter"]
    await eng.close()
    assert eng.closed


def test_registry() -> None:
    eng = create_engine("fake", model="m")
    assert isinstance(eng, FakeEngine) and eng.model == "m"
    custom = create_engine("mine", factories={"mine": lambda **o: FakeEngine(model="custom")})
    assert custom.model == "custom"
    try:
        create_engine("nope")
    except Exception as exc:  # noqa: BLE001
        assert "unknown engine" in str(exc)
    else:
        raise AssertionError("expected EngineError")
