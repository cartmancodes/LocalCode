from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.app.core.extensions import (
    ExtensionRunner,
    ExtensionUIContext,
    NoUIBridge,
    SimpleExtensionContext,
    discover_extension_paths,
    load_extension,
    load_extensions,
)


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def ctx(project: Path, **kw: Any) -> SimpleExtensionContext:
    return SimpleExtensionContext(cwd=str(project), **kw)


# ── discovery ──────────────────────────────────────────────────────────────


def test_discovery_order_global_then_project_then_extra(agent_dir: Path, project: Path) -> None:
    g1 = write(agent_dir / "extensions" / "b.py", "def setup(api): pass\n")
    g2 = write(agent_dir / "extensions" / "a" / "index.py", "def setup(api): pass\n")
    write(agent_dir / "extensions" / "_private.py", "def setup(api): pass\n")
    write(agent_dir / "extensions" / "notes.md", "not an extension")
    p1 = write(project / ".localcode" / "extensions" / "proj.py", "def setup(api): pass\n")
    extra = write(project / "somewhere" / "x.py", "def setup(api): pass\n")
    paths = discover_extension_paths(cwd=project, extra_paths=[extra, extra])
    assert paths == [g2, g1, p1, extra.resolve()]
    assert discover_extension_paths(cwd=project, include_project=False) == [g2, g1]


# ── loading ────────────────────────────────────────────────────────────────


async def test_load_sync_and_async_setup_and_bad_modules(agent_dir: Path, project: Path) -> None:
    good_sync = write(
        project / "e" / "one.py",
        "def setup(api):\n    api.register_flag('x', type='boolean', default=True)\n",
    )
    good_async = write(
        project / "e" / "two.py",
        "async def setup(api):\n    api.register_command('hi', handler=lambda a, c: None, description='says hi')\n",
    )
    no_setup = write(project / "e" / "three.py", "VALUE = 1\n")
    broken = write(project / "e" / "four.py", "def setup(api):\n    raise RuntimeError('boom')\n")
    uses_context = write(
        project / "e" / "five.py", "def setup(api):\n    api.on('context', lambda e, c: None)\n"
    )
    runner = ExtensionRunner()
    result = await load_extensions([good_sync, good_async, no_setup, broken, uses_context], runner)
    assert [e.name for e in result.extensions] == ["one", "two"]
    errors = {Path(e.extension_path).name: e.error for e in result.errors}
    assert "no setup(api) function" in errors["three.py"]
    assert "boom" in errors["four.py"]
    assert "'context' is not available" in errors["five.py"]
    assert runner.get_flag("x") is True
    assert [c["name"] for c in runner.command_infos()] == ["hi"]
    assert runner.command_infos()[0]["source"] == "extension"


async def test_unknown_event_is_rejected(project: Path) -> None:
    ext = write(
        project / "e" / "bad.py", "def setup(api):\n    api.on('tool_cal', lambda e, c: None)\n"
    )
    runner = ExtensionRunner()
    assert await load_extension(ext, runner) is None
    assert "unknown extension event" in runner.errors[0].error


# ── merge semantics ────────────────────────────────────────────────────────


async def test_tool_call_first_block_wins_and_input_edits_persist(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "def setup(api):\n"
        "    def h(event, ctx):\n"
        "        event['input']['command'] = 'echo safe'\n"
        "    api.on('tool_call', h)\n",
    )
    write(
        project / "e" / "b.py",
        "def setup(api):\n"
        "    async def h(event, ctx):\n"
        "        if 'rm' in event['input'].get('original', ''):\n"
        "            return {'block': True, 'reason': 'destructive', 'terminate': True}\n"
        "    api.on('tool_call', h)\n",
    )
    write(
        project / "e" / "c.py",
        "calls = []\n"
        "def setup(api):\n"
        "    api.on('tool_call', lambda e, c: calls.append(e['toolCallId']))\n",
    )
    runner = ExtensionRunner()
    await load_extensions(
        discover_extension_paths(cwd=project, include_project=False, extra_paths=[project / "e"]),
        runner,
    )
    tool_input = {"command": "rm -rf /", "original": "rm -rf /"}
    result = await runner.emit_tool_call("bash", "t1", tool_input, ctx(project))
    assert result == {"block": True, "reason": "destructive", "terminate": True}
    assert tool_input["command"] == "echo safe"
    # c.py never ran because b.py blocked
    import sys

    c_mod = next(
        m for n, m in sys.modules.items() if n.startswith("localcode_ext_") and hasattr(m, "calls")
    )
    assert c_mod.calls == []
    # a non-blocking call reaches everyone and reports terminate-only when nothing blocks
    result = await runner.emit_tool_call("bash", "t2", {"command": "ls"}, ctx(project))
    assert result is None
    assert c_mod.calls == ["t2"]


async def test_tool_result_chains(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "def setup(api):\n    api.on('tool_result', lambda e, c: {'content': [{'type': 'text', 'text': e['content'][0]['text'] + ' +a'}]})\n",
    )
    write(
        project / "e" / "b.py",
        "def setup(api):\n    api.on('tool_result', lambda e, c: {'content': [{'type': 'text', 'text': e['content'][0]['text'] + ' +b'}], 'isError': True})\n",
    )
    runner = ExtensionRunner()
    await load_extensions(sorted((project / "e").glob("*.py")), runner)
    out = await runner.emit_tool_result(
        "read", "t", {}, [{"type": "text", "text": "x"}], None, False, ctx(project)
    )
    assert out == {
        "content": [{"type": "text", "text": "x +a +b"}],
        "details": None,
        "isError": True,
    }


async def test_input_transform_chains_and_handled_stops(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "def setup(api):\n    api.on('input', lambda e, c: {'action': 'transform', 'text': e['text'].upper()})\n",
    )
    write(
        project / "e" / "b.py",
        "def setup(api):\n    api.on('input', lambda e, c: {'action': 'transform', 'text': e['text'] + '!'})\n",
    )
    runner = ExtensionRunner()
    await load_extensions(sorted((project / "e").glob("*.py")), runner)
    out = await runner.emit_input("hi", None, "rpc", None, ctx(project))
    assert out == {"action": "continue", "text": "HI!", "images": None}
    write(
        project / "e" / "0handled.py",
        "def setup(api):\n    api.on('input', lambda e, c: {'action': 'handled'} if e['text'].startswith('/x') else None)\n",
    )
    runner = ExtensionRunner()
    await load_extensions(sorted((project / "e").glob("*.py")), runner)
    assert await runner.emit_input("/x", None, "rpc", None, ctx(project)) == {"action": "handled"}
    assert (await runner.emit_input("y", None, "rpc", None, ctx(project)))["text"] == "Y!"


async def test_before_agent_start_collects_messages_last_prompt_wins(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "def setup(api):\n    api.on('before_agent_start', lambda e, c: {'message': {'customType': 'a', 'content': 'A', 'display': False}, 'systemPrompt': e['systemPrompt'] + ' A'})\n",
    )
    write(
        project / "e" / "b.py",
        "def setup(api):\n    api.on('before_agent_start', lambda e, c: {'message': {'customType': 'b', 'content': 'B', 'display': True}, 'systemPrompt': 'B only'})\n",
    )
    runner = ExtensionRunner()
    await load_extensions(sorted((project / "e").glob("*.py")), runner)
    out = await runner.emit_before_agent_start("p", None, "base", ctx(project))
    assert [m["customType"] for m in out["messages"]] == ["a", "b"]
    assert out["systemPrompt"] == "B only"


async def test_handler_errors_are_isolated_and_reported(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "def setup(api):\n    def h(e, c):\n        raise ValueError('nope')\n    api.on('agent_start', h)\n",
    )
    write(
        project / "e" / "b.py",
        "seen = []\ndef setup(api):\n    api.on('agent_start', lambda e, c: seen.append(1))\n",
    )
    reported = []
    runner = ExtensionRunner(on_error=reported.append)
    await load_extensions(sorted((project / "e").glob("*.py")), runner)
    await runner.emit({"type": "agent_start"}, ctx(project))
    assert len(runner.errors) == 1 and runner.errors[0].event == "agent_start"
    assert "ValueError: nope" in runner.errors[0].error
    assert reported == runner.errors
    import sys

    b_mod = next(
        m for n, m in sys.modules.items() if n.startswith("localcode_ext_") and hasattr(m, "seen")
    )
    assert b_mod.seen == [1]


async def test_project_trust_resources_and_cancellable(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "def setup(api):\n    api.on('project_trust', lambda e, c: {'trusted': 'undecided'})\n    api.on('resources_discover', lambda e, c: {'skillPaths': ['/s1']})\n    api.on('session_before_fork', lambda e, c: None)\n",
    )
    write(
        project / "e" / "b.py",
        "def setup(api):\n    api.on('project_trust', lambda e, c: {'trusted': 'yes', 'remember': True})\n    api.on('resources_discover', lambda e, c: {'skillPaths': ['/s2'], 'promptPaths': ['/p']})\n    api.on('session_before_fork', lambda e, c: {'cancel': True})\n",
    )
    runner = ExtensionRunner()
    await load_extensions(sorted((project / "e").glob("*.py")), runner)
    c = ctx(project)
    assert await runner.emit_project_trust(str(project), c) == {"trusted": "yes", "remember": True}
    assert await runner.emit_resources_discover(str(project), "startup", c) == {
        "skillPaths": ["/s1", "/s2"],
        "promptPaths": ["/p"],
        "themePaths": [],
    }
    assert await runner.emit_cancellable(
        {"type": "session_before_fork", "entryId": "x", "position": "before"}, c
    )
    assert not await runner.emit_cancellable({"type": "session_before_switch", "reason": "new"}, c)


# ── api surface ────────────────────────────────────────────────────────────


async def test_session_facing_methods_need_a_bound_session(project: Path) -> None:
    write(project / "e" / "a.py", "def setup(api):\n    api.append_entry('x', {})\n")
    runner = ExtensionRunner()
    assert await load_extension(project / "e" / "a.py", runner) is None
    assert "no live session yet" in runner.errors[0].error

    write(
        project / "e" / "b.py",
        "def setup(api):\n    api.register_command('mark', handler=lambda args, c: api.append_entry('mark', {'args': args}), description='mark')\n",
    )
    runner = ExtensionRunner()
    await load_extension(project / "e" / "b.py", runner)

    class Bridge:
        entries: list = []

        def append_entry(self, custom_type, data):
            self.entries.append((custom_type, data))
            return "id1"

    runner.bind(Bridge())
    cmd = runner.get_command("mark")
    assert cmd is not None and cmd.description == "mark"
    cmd.handler("now", ctx(project))
    assert Bridge.entries == [("mark", {"args": "now"})]


async def test_register_tool_requires_async_execute_and_exec_runs(project: Path) -> None:
    write(
        project / "e" / "a.py",
        "from backend.app.core.extensions import ToolDefinition\n"
        "def setup(api):\n"
        "    async def run(tool_call_id, params, signal, on_update, ctx):\n"
        "        return {'content': [{'type': 'text', 'text': params['x']}], 'details': {}}\n"
        "    api.register_tool(ToolDefinition(name='echo', description='echo', execute=run))\n"
        "    try:\n"
        "        api.register_tool(ToolDefinition(name='bad', description='bad', execute=lambda *a: None))\n"
        "    except TypeError as e:\n"
        "        api.register_flag('caught', type='string', default=str(e))\n",
    )
    runner = ExtensionRunner()
    ext = await load_extension(project / "e" / "a.py", runner)
    assert ext is not None
    found = runner.get_tool("echo")
    assert found is not None and found[1].label == "echo"
    assert "must be an async function" in str(runner.get_flag("caught"))
    from backend.app.core.extensions.api import ExtensionAPI

    api = ExtensionAPI(ext, runner)
    res = await api.exec("sh", ["-c", "echo out; echo err 1>&2; exit 3"])
    assert res["stdout"].strip() == "out" and res["stderr"].strip() == "err"
    assert res["code"] == 3 and res["killed"] is False
    res = await api.exec("sleep", ["5"], timeout_s=0.1)
    assert res["killed"] is True


# ── ui ─────────────────────────────────────────────────────────────────────


async def test_ui_context_over_bridges() -> None:
    ui = ExtensionUIContext(NoUIBridge())
    assert await ui.confirm("t", "m") is False
    assert await ui.select("t", ["first", "second"]) == "first"
    assert await ui.input("t") is None

    class Recording:
        has_ui = True
        requests: list = []

        async def dialog(self, method, payload):
            self.requests.append((method, payload))
            return {"value": "chosen", "confirmed": True}

        def notify(self, method, payload):
            self.requests.append((method, payload))

    rec = Recording()
    ui = ExtensionUIContext(rec)
    assert await ui.select("Allow?", ["Allow", "Block"], {"timeout": 2.5}) == "chosen"
    assert await ui.confirm("Clear?", "All lost.") is True
    ui.notify("hi", "warning")
    ui.set_status("k", "running")
    assert rec.requests[0] == (
        "select",
        {"title": "Allow?", "options": ["Allow", "Block"], "timeout": 2500},
    )
    assert rec.requests[1] == ("confirm", {"title": "Clear?", "message": "All lost."})
    assert rec.requests[2] == ("notify", {"message": "hi", "notifyType": "warning"})
    assert rec.requests[3] == ("setStatus", {"statusKey": "k", "statusText": "running"})


@pytest.mark.parametrize("mode", ["print", "rpc"])
def test_simple_context_defaults(project: Path, mode: str) -> None:
    c = ctx(project, mode=mode)
    assert c.mode == mode and c.has_ui is False and c.is_idle() and not c.is_project_trusted()
    assert c.get_system_prompt() == "" and c.get_context_usage() is None
