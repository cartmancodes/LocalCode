from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from backend.app.core.agent_session import AgentSession
from backend.app.core.engines import FakeEngine
from backend.app.core.extensions import ExtensionRunner, load_extension
from backend.app.core.rpc import (
    LineSplitter,
    RpcServer,
    RpcUIBridge,
    serialize_json_line,
    to_json_event,
)
from backend.app.core.session_manager import SessionManager


class Wire:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def __call__(self, frame: dict[str, Any]) -> None:
        # round-trip through JSON so anything non-serialisable fails here
        self.frames.append(json.loads(serialize_json_line(frame)))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == kind]

    def response(self, cmd_id: str) -> dict[str, Any]:
        return next(f for f in self.frames if f.get("type") == "response" and f.get("id") == cmd_id)


async def make(
    project: Path, runs: list | None = None, runner: ExtensionRunner | None = None, **kw: Any
):
    engine = FakeEngine(runs)
    wire = Wire()
    ui = RpcUIBridge(wire)
    session = AgentSession(
        engine=engine,
        session_manager=SessionManager.create(project),
        runner=runner,
        ui_bridge=ui,
        mode="rpc",
        **kw,
    )
    server = RpcServer(
        session,
        wire,
        models=[
            {"provider": "fake", "modelId": "fake-1"},
            {"provider": "fake", "modelId": "fake-2"},
            {"provider": "claude", "modelId": "x"},
        ],
        ui_bridge=ui,
    )
    await session.start()
    return server, wire, session, engine


async def send(server: RpcServer, **cmd: Any) -> None:
    await server.handle_line(serialize_json_line(cmd))


async def settle(server: RpcServer) -> None:
    await asyncio.wait_for(server.settled.wait(), timeout=5)


def test_line_splitter_and_serialize() -> None:
    seen: list[str] = []
    s = LineSplitter(seen.append)
    s.feed('{"a":1}\r\n{"b": ')
    s.feed('2}\n{"c"')
    s.end()
    assert seen == ['{"a":1}', '{"b": 2}', '{"c"']
    assert serialize_json_line({"x": "a\nb"}) == '{"x":"a\\nb"}\n'


def test_to_json_event_thins_message_update() -> None:
    partial = {
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {}}],
        "usage": {"input": 1},
    }
    ev = {
        "type": "message_update",
        "message": partial,
        "assistantMessageEvent": {"type": "toolcall_start", "contentIndex": 0, "partial": partial},
    }
    out = to_json_event(ev)
    assert out == {
        "type": "message_update",
        "usage": {"input": 1},
        "assistantMessageEvent": {
            "type": "toolcall_start",
            "contentIndex": 0,
            "id": "c1",
            "toolName": "bash",
        },
    }
    text = to_json_event(
        {
            "type": "message_update",
            "message": partial,
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "x",
                "partial": partial,
            },
        }
    )
    assert (
        "partial" not in text["assistantMessageEvent"]
        and text["assistantMessageEvent"]["delta"] == "x"
    )
    assert to_json_event({"type": "agent_start"}) == {"type": "agent_start"}


async def test_prompt_streams_events_and_responds(agent_dir: Path, project: Path) -> None:
    server, wire, session, engine = await make(
        project,
        [
            [
                {
                    "text": "hi from fake",
                    "tools": [{"name": "read", "args": {"p": 1}, "result": "R"}],
                },
                {"text": "bye"},
            ]
        ],
        default_permission="allow",
    )
    await send(server, id="1", type="prompt", message="hello")
    assert wire.response("1") == {
        "type": "response",
        "command": "prompt",
        "id": "1",
        "success": True,
    }
    await settle(server)
    kinds = [f["type"] for f in wire.frames]
    assert kinds.count("turn_start") == 2 and kinds[-1] == "agent_settled"
    updates = wire.of("message_update")
    assert all("partial" not in u["assistantMessageEvent"] for u in updates)
    assert any(u["assistantMessageEvent"].get("toolName") == "read" for u in updates)
    assert wire.of("tool_execution_end")[0]["result"]["content"][0]["text"] == "R"
    assert wire.of("entry_appended")
    # second prompt while idle works; while streaming it must specify behaviour
    session.is_streaming = True
    await send(server, id="2", type="prompt", message="again")
    assert (
        wire.response("2")["success"] is False
        and "streamingBehavior" in wire.response("2")["error"]
    )
    await send(server, id="3", type="prompt", message="later", streamingBehavior="followUp")
    assert wire.response("3")["success"] is True
    await asyncio.sleep(0)  # let the spawned prompt task queue the follow-up
    session.is_streaming = False
    assert wire.of("queue_update")[-1]["followUp"] == ["later"]
    await send(server, id="4", type="clear_queue")
    assert wire.response("4")["data"] == {"steering": [], "followUp": ["later"]}


async def test_state_models_thinking_and_session_commands(agent_dir: Path, project: Path) -> None:
    server, wire, session, engine = await make(project, [[{"text": "one"}], [{"text": "two"}]])
    await send(server, id="s", type="get_state")
    state = wire.response("s")["data"]
    assert (
        state["model"] == {"provider": "fake", "modelId": "fake-1"}
        and state["isStreaming"] is False
    )
    await send(server, id="m", type="get_available_models")
    assert [m["modelId"] for m in wire.response("m")["data"]["models"]] == ["fake-1", "fake-2"]
    await send(server, id="c", type="cycle_model")
    assert wire.response("c")["data"]["model"] == {"provider": "fake", "modelId": "fake-2"}
    await send(server, id="sm", type="set_model", provider="claude", modelId="x")
    assert wire.response("sm")["success"] is False
    await send(server, id="t", type="get_available_thinking_levels")
    assert wire.response("t")["data"]["levels"][0] == "off"
    await send(server, id="ct", type="cycle_thinking_level")
    assert wire.response("ct")["data"] == {"level": "low"}
    await send(server, id="n", type="set_session_name", name="demo")
    assert wire.response("n")["success"] and session.session_name == "demo"
    assert wire.of("session_info_changed")[0]["name"] == "demo"
    await send(server, id="p", type="prompt", message="q1")
    await settle(server)
    await send(server, id="e", type="get_entries")
    entries = wire.response("e")["data"]
    assert entries["leafId"] and [x["type"] for x in entries["entries"]][:2] == [
        "model_change",
        "thinking_level_change",
    ]
    await send(server, id="e2", type="get_entries", since=entries["entries"][1]["id"])
    assert len(wire.response("e2")["data"]["entries"]) == len(entries["entries"]) - 2
    await send(server, id="tr", type="get_tree")
    assert wire.response("tr")["data"]["tree"][0]["entry"]["type"] == "model_change"
    await send(server, id="f", type="get_fork_messages")
    forkable = wire.response("f")["data"]["messages"]
    assert forkable[0]["text"] == "q1"
    await send(server, id="fk", type="fork", entryId=forkable[0]["entryId"])
    assert wire.response("fk")["data"] == {"text": "q1", "cancelled": False}
    await send(server, id="st", type="get_session_stats")
    assert wire.response("st")["data"]["assistantMessages"] == 0  # forked before q1's answer
    await send(server, id="la", type="get_last_assistant_text")
    assert wire.response("la")["data"] == {"text": None}
    await send(server, id="msgs", type="get_messages")
    assert wire.response("msgs")["data"]["messages"] == []
    await send(server, id="cm", type="get_commands")
    assert wire.response("cm")["data"] == {"commands": []}
    await send(server, id="x", type="export_html")
    assert wire.response("x")["success"] is False
    await send(server, id="u", type="nope")
    assert "unknown command" in wire.response("u")["error"]
    await send(server, id="ns", type="new_session")
    assert wire.response("ns")["data"] == {"cancelled": False}


async def test_bash_streams_updates(agent_dir: Path, project: Path) -> None:
    server, wire, session, engine = await make(project)
    await send(server, id="b", type="bash", command="printf 'a'; printf 'b'")
    resp = wire.response("b")
    assert resp["data"]["output"] == "ab" and resp["data"]["exitCode"] == 0
    assert "".join(u["delta"] for u in wire.of("bash_execution_update")) == "ab"
    assert all(u["id"] == "b" for u in wire.of("bash_execution_update"))


async def test_extension_ui_roundtrip(agent_dir: Path, project: Path) -> None:
    ext = project / "e" / "confirm.py"
    ext.parent.mkdir(parents=True)
    ext.write_text(
        "def setup(api):\n"
        "    async def h(event, ctx):\n"
        "        ok = await ctx.ui.confirm('Run tool?', event['toolName'])\n"
        "        ctx.ui.notify('decided', 'info')\n"
        "        return None if ok else {'block': True, 'reason': 'user said no'}\n"
        "    api.on('tool_call', h)\n"
    )
    runner = ExtensionRunner()
    await load_extension(ext, runner)
    server, wire, session, engine = await make(
        project,
        [[{"text": "t", "tools": [{"name": "bash", "args": {}, "result": "ran"}]}]],
        runner=runner,
        default_permission="allow",
    )
    await send(server, id="p", type="prompt", message="go")
    for _ in range(50):
        if wire.of("extension_ui_request"):
            break
        await asyncio.sleep(0.01)
    req = wire.of("extension_ui_request")[0]
    assert req["method"] == "confirm" and req["title"] == "Run tool?" and req["message"] == "bash"
    await send(server, type="extension_ui_response", id=req["id"], confirmed=False)
    await settle(server)
    assert wire.of("tool_execution_end")[0]["isError"] is True
    notify = [r for r in wire.of("extension_ui_request") if r["method"] == "notify"]
    assert notify and notify[0]["message"] == "decided"


async def test_ui_dialog_timeout_resolves_default() -> None:
    frames: list[dict] = []
    ui = RpcUIBridge(frames.append)
    result = await ui.dialog("select", {"title": "t", "options": ["a"], "timeout": 10})
    assert result == {"cancelled": True}
    assert frames[0]["method"] == "select" and frames[0]["timeout"] == 10
    assert ui.resolve({"type": "extension_ui_response", "id": "unknown", "value": "x"}) is False


async def test_run_modes_and_cli_parser(agent_dir: Path, project: Path) -> None:
    import io

    from backend.app.core.cli import build_parser
    from backend.app.core.modes import run_json_mode, run_print_mode

    args = build_parser().parse_args(["--engine", "fake", "--mode", "json", "hi"])
    assert args.engine == "fake" and args.mode == "json" and args.prompt == "hi"
    out = io.StringIO()
    session = AgentSession(
        engine=FakeEngine([[{"text": "json answer"}]]),
        session_manager=SessionManager.in_memory(project),
        mode="json",
    )
    assert await run_json_mode(session, "q", stdout=out) == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[-1]["type"] == "agent_settled" and any(
        line["type"] == "message_update" for line in lines
    )
    out = io.StringIO()
    session = AgentSession(
        engine=FakeEngine([[{"text": "printed"}]]),
        session_manager=SessionManager.in_memory(project),
        mode="print",
    )
    assert await run_print_mode(session, "q", stdout=out) == 0
    assert out.getvalue() == "printed\n"


def test_cli_main_print_mode_end_to_end(agent_dir: Path, project: Path, capsys) -> None:
    """Sync on purpose: main() owns its own event loop."""
    from backend.app.core.cli import main

    code = main(
        ["--engine", "fake", "--in-memory", "--no-extensions", "--cwd", str(project), "-p", "hello"]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "echo: hello"
