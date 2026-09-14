"""ClaudeEngine translation tests with a scripted stand-in for ClaudeSDKClient.

The real client needs the `claude` CLI and a login; these tests feed the
engine the SDK's own message dataclasses in the order the CLI emits them.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from backend.app.core.engines import EngineConfig, EngineHooks
from backend.app.core.engines.claude import ClaudeEngine
from backend.app.core.messages import user_message


class FakeClient:
    """Replays scripted SDK messages; records control calls."""

    scripts: list[list[Any]] = []
    instances: list[FakeClient] = []

    def __init__(self, options: Any) -> None:
        self.options = options
        self.queries: list[Any] = []
        self.interrupted = False
        self.models: list[str] = []
        self.connected = False
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_server_info(self) -> dict[str, Any]:
        return {"session_id": "sess-1"}

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        script = FakeClient.scripts.pop(0) if FakeClient.scripts else []
        for msg in script:
            if callable(msg):
                msg = await msg(self)
            yield msg

    async def interrupt(self) -> None:
        self.interrupted = True

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model or "")


def stream(ev: dict[str, Any]) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="sess-1", event=ev)


def result(**kw: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
    )
    base.update(kw)
    return ResultMessage(**base)


async def make(script: list[Any], **cfg: Any) -> tuple[ClaudeEngine, list[dict]]:
    FakeClient.scripts = [script]
    FakeClient.instances = []
    eng = ClaudeEngine(client_factory=FakeClient)
    await eng.start(EngineConfig(cwd="/tmp", model="claude-sonnet-4-6", **cfg))
    return eng, []


async def run(eng: ClaudeEngine, text: str, hooks: EngineHooks | None = None) -> list[dict]:
    return [ev async for ev in eng.prompt(user_message(text), hooks or EngineHooks())]


async def test_options_carry_hooks_permissions_and_resume() -> None:
    eng, _ = await make(
        [result()], session_id="old", thinking_level="high", append_system_prompt="Be terse."
    )
    await run(eng, "x")
    opts = FakeClient.instances[0].options
    assert opts.resume == "old" and opts.include_partial_messages is True
    assert opts.permission_mode == "default" and opts.can_use_tool is not None
    assert set(opts.hooks) == {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "Stop",
        "PreCompact",
    }
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": "Be terse."}
    assert opts.effort == "high"
    assert eng.session_id == "sess-1"


async def test_streamed_text_then_tool_use_and_result() -> None:
    script = [
        SystemMessage(subtype="init", data={"session_id": "sess-1"}),
        stream({"type": "message_start"}),
        stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me "}}),
        stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "look."}}),
        AssistantMessage(
            content=[
                TextBlock(text="Let me look."),
                ToolUseBlock(id="tu1", name="Read", input={"file_path": "a.py"}),
            ],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 100, "output_tokens": 7, "cache_read_input_tokens": 50},
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="tu1", content="print('hi')", is_error=False)]
        ),
        stream({"type": "message_start"}),
        stream(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "It prints hi."},
            }
        ),
        AssistantMessage(
            content=[TextBlock(text="It prints hi.")],
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
        result(total_cost_usd=0.01),
    ]
    eng, _ = await make(script)
    evs = await run(eng, "what does a.py do?")
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "engine_session" and kinds[1] == "agent_start"
    assert kinds.count("turn_start") == 2 and kinds[-1] == "agent_end"
    first_turn_end = [e for e in evs if e["type"] == "turn_end"][0]
    assert first_turn_end["message"]["stopReason"] == "toolUse"
    assert first_turn_end["message"]["usage"]["cacheRead"] == 50
    assert first_turn_end["toolResults"][0] == {
        "role": "toolResult",
        "toolCallId": "tu1",
        "toolName": "Read",
        "content": [{"type": "text", "text": "print('hi')"}],
        "isError": False,
        "timestamp": first_turn_end["toolResults"][0]["timestamp"],
    }
    text = "".join(
        e["assistantMessageEvent"]["delta"]
        for e in evs
        if e["type"] == "message_update" and e["assistantMessageEvent"]["type"] == "text_delta"
    )
    assert text == "Let me look.It prints hi."
    start = next(e for e in evs if e["type"] == "tool_execution_start")
    assert start == {
        "type": "tool_execution_start",
        "toolCallId": "tu1",
        "toolName": "Read",
        "args": {"file_path": "a.py"},
    }
    assert [m["role"] for m in evs[-1]["messages"]] == ["assistant", "toolResult", "assistant"]


async def test_hooks_compile_down() -> None:
    eng, _ = await make([result()])
    calls: list[Any] = []

    async def before(name: str, call_id: str, args: dict) -> dict | None:
        calls.append(("before", name, call_id))
        if args.get("command") == "rm -rf /":
            return {"block": True, "reason": "destructive"}
        if name == "Edit":
            return {"updated_input": {**args, "file_path": "/safe/" + args["file_path"]}}
        return None

    async def after(
        name: str, call_id: str, args: dict, content: list, is_error: bool
    ) -> dict | None:
        calls.append(("after", name, content[0]["text"]))
        return {"content": [{"type": "text", "text": "note: reviewed"}]}

    async def before_prompt(text: str) -> dict | None:
        return {"additional_context": f"ctx for {text}"}

    async def permission(name: str, args: dict, ctx: dict) -> dict | None:
        calls.append(("permission", name, ctx["tool_use_id"]))
        return {"allow": name == "Write"}

    stopped = []

    async def on_stop() -> None:
        stopped.append(1)

    hooks = EngineHooks(
        before_tool=before,
        after_tool=after,
        before_prompt=before_prompt,
        on_stop=on_stop,
        permission_request=permission,
    )
    await run(eng, "x", hooks)

    deny = await eng._pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "tool_use_id": "t1"}, "t1", {}
    )
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert deny["hookSpecificOutput"]["permissionDecisionReason"] == "destructive"
    rewrite = await eng._pre_tool_use(
        {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}, "tool_use_id": "t2"}, "t2", {}
    )
    assert rewrite["hookSpecificOutput"]["updatedInput"] == {"file_path": "/safe/x.py"}
    assert (
        await eng._pre_tool_use(
            {"tool_name": "Read", "tool_input": {}, "tool_use_id": "t3"}, "t3", {}
        )
        == {}
    )
    post = await eng._post_tool_use(
        {"tool_name": "Read", "tool_input": {}, "tool_response": {"stdout": "out"}}, "t3", {}
    )
    assert post["hookSpecificOutput"]["additionalContext"] == "note: reviewed"
    ups = await eng._user_prompt_submit({"prompt": "hi"}, None, {})
    assert ups["hookSpecificOutput"]["additionalContext"] == "ctx for hi"
    await eng._stop({}, None, {})
    assert stopped == [1]

    class Ctx:
        tool_use_id = "t9"
        title = description = decision_reason = None

    allow = await eng._can_use_tool("Write", {"file_path": "f"}, Ctx())
    assert allow.behavior == "allow"
    deny2 = await eng._can_use_tool("Bash", {"command": "ls"}, Ctx())
    assert deny2.behavior == "deny"
    assert ("after", "Read", "out") in calls and ("permission", "Write", "t9") in calls

    eng2, _ = await make([result()])
    await run(eng2, "x")
    no_handler = await eng2._can_use_tool("Bash", {}, Ctx())
    assert no_handler.behavior == "deny" and "approval extension" in no_handler.message


async def test_interrupt_error_and_fork() -> None:
    eng, _ = await make(
        [
            stream({"type": "message_start"}),
            stream(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}
            ),
            result(terminal_reason="aborted_streaming"),
        ]
    )
    events: list[dict] = []
    async for ev in eng.prompt(user_message("go"), EngineHooks()):
        events.append(ev)
        if ev["type"] == "message_update":
            await eng.interrupt()
    assert FakeClient.instances[0].interrupted is True
    assert events[-1]["aborted"] is True
    assert (
        next(e for e in events if e["type"] == "message_end")["message"]["stopReason"] == "aborted"
    )

    FakeClient.scripts = [[result(is_error=True, subtype="error_max_turns", result="max turns")]]
    evs = await run(eng, "again")
    assert any(e["type"] == "error" and e["message"] == "max turns" for e in evs)

    FakeClient.scripts = [[result()]]
    forked = await eng.fork()
    assert forked == "sess-1"
    opts = FakeClient.instances[-1].options
    assert opts.fork_session is True and opts.resume == "sess-1"
    await eng.set_model("claude-opus-4-7")
    assert FakeClient.instances[-1].models == ["claude-opus-4-7"]
    await eng.close()
    assert FakeClient.instances[-1].connected is False
