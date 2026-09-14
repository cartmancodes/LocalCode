from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.app.core.agent_session import ENGINE_SESSION_ENTRY, AgentSession, create_agent_session
from backend.app.core.engines import EngineCapabilities, FakeEngine
from backend.app.core.extensions import ExtensionRunner, load_extension
from backend.app.core.session_manager import SessionManager


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


class UI:
    has_ui = True

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict]] = []

    async def dialog(self, method: str, payload: dict) -> dict:
        self.asked.append((method, payload))
        return {"confirmed": self.answer, "value": (payload.get("options") or [None])[0]}

    def notify(self, method: str, payload: dict) -> None:
        self.asked.append((method, payload))


async def make(
    project: Path, runs: list | None = None, **kw: Any
) -> tuple[AgentSession, FakeEngine, Recorder]:
    engine = FakeEngine(runs)
    sm = SessionManager.create(project)
    session = AgentSession(engine=engine, session_manager=sm, **kw)
    rec = Recorder()
    session.subscribe(rec)
    await session.start()
    return session, engine, rec


async def test_prompt_records_entries_and_emits_pi_events(agent_dir: Path, project: Path) -> None:
    session, engine, rec = await make(project, [[{"text": "hello back"}]])
    await session.prompt("hello")
    types = rec.types()
    assert types[0] == "entry_appended"  # user message
    assert "agent_start" in types and "message_update" in types
    assert types[-2:] == ["agent_end", "agent_settled"]
    entries = session.session_manager.get_entries()
    kinds = [(e["type"], e.get("customType") or e.get("message", {}).get("role")) for e in entries]
    assert kinds == [
        ("message", "user"),
        ("custom", ENGINE_SESSION_ENTRY),
        ("message", "assistant"),
    ]
    assert session.get_last_assistant_text() == "hello back"
    assert session.get_state()["messageCount"] == 2 and session.get_state()["engine"] == "fake"
    stats = session.get_session_stats()
    assert stats["assistantMessages"] == 1 and stats["tokens"]["input"] == 10
    assert engine.prompts[0]["content"][0]["text"] == "hello"


async def test_engine_session_id_is_resumed_on_restart(agent_dir: Path, project: Path) -> None:
    session, engine, _ = await make(project)
    await session.prompt("x")
    sid = engine.session_id
    assert sid
    path = session.session_manager.get_session_file()
    engine2 = FakeEngine()
    session2 = AgentSession(engine=engine2, session_manager=SessionManager.open(path))
    await session2.start()
    assert engine2.session_id == sid


async def test_tool_flow_records_tool_results_and_extension_blocks(
    agent_dir: Path, project: Path
) -> None:
    ext = write(
        project / "e" / "guard.py",
        "def setup(api):\n"
        "    api.on('tool_call', lambda e, c: {'block': True, 'reason': 'nope'} if e['input'].get('command') == 'rm' else None)\n",
    )
    runner = ExtensionRunner()
    await load_extension(ext, runner)
    runs = [
        [
            {
                "text": "try",
                "tools": [
                    {"name": "bash", "args": {"command": "rm"}, "result": "x"},
                    {"name": "bash", "args": {"command": "ls"}, "result": "ok"},
                ],
            },
            {"text": "done"},
        ]
    ]
    session, engine, rec = await make(project, runs, runner=runner, default_permission="allow")
    await session.prompt("go")
    ends = [e for e in rec.events if e["type"] == "tool_execution_end"]
    assert [e["isError"] for e in ends] == [True, False]
    roles = [
        e["message"]["role"]
        for e in session.session_manager.get_entries()
        if e["type"] == "message"
    ]
    assert roles == ["user", "assistant", "toolResult", "toolResult", "assistant"]
    tr = [
        e["message"]
        for e in session.session_manager.get_entries()
        if e["type"] == "message" and e["message"]["role"] == "toolResult"
    ]
    assert tr[0]["isError"] and "nope" in tr[0]["content"][0]["text"]
    assert tr[1]["content"][0]["text"] == "ok"


async def test_permission_defaults_ask_ui_then_deny_without_ui(
    agent_dir: Path, project: Path
) -> None:
    ui = UI(answer=True)
    session, _, _ = await make(project, mode="rpc", ui_bridge=ui)
    assert (await session._permission_request("Write", {"file_path": "a"}, {"tool_use_id": "t1"}))[
        "allow"
    ] is True
    assert ui.asked[0][0] == "confirm" and "Write" in ui.asked[0][1]["title"]
    session_no_ui, _, _ = await make(project, mode="print")
    denied = await session_no_ui._permission_request("Write", {}, {"tool_use_id": "t2"})
    assert denied["allow"] is False and "no approval UI" in denied["message"]
    session_no_ui.permission_policy = lambda name, args, ctx: name == "Read"
    assert (await session_no_ui._permission_request("Read", {}, {}))["allow"] is True
    assert (await session_no_ui._permission_request("Bash", {}, {}))["allow"] is False


async def test_follow_up_queue_drains_and_steer_is_native(agent_dir: Path, project: Path) -> None:
    session, engine, rec = await make(project, [[{"text": "one"}], [{"text": "two"}]])
    await session.prompt("first")
    # queue while "streaming": simulate by flagging
    session.is_streaming = True
    await session.prompt("second", streaming_behavior="followUp")
    await session.prompt("hurry", streaming_behavior="steer")
    session.is_streaming = False
    assert session.pending_message_count == 1  # follow-up queued; steer went to the engine natively
    assert engine.steers and engine.steers[0]["content"][0]["text"] == "hurry"
    queue_events = [e for e in rec.events if e["type"] == "queue_update"]
    assert queue_events[0]["followUp"] == ["second"]
    await session.prompt("third")
    assert [p["content"][0]["text"] for p in engine.prompts] == ["first", "third", "second"]
    assert session.pending_message_count == 0
    assert session.clear_queue() == {"steering": [], "followUp": []}


async def test_steer_falls_back_to_interrupt_when_engine_cannot(
    agent_dir: Path, project: Path
) -> None:
    engine = FakeEngine(
        [[{"text": "a" * 40}, {"text": "b"}], [{"text": "steered"}]],
        delay_s=0.001,
        capabilities=EngineCapabilities(steer=False, fork=True, compact=True),
    )
    session = AgentSession(engine=engine, session_manager=SessionManager.in_memory(project))
    rec = Recorder()
    session.subscribe(rec)
    await session.start()

    async def steer_once(ev: dict) -> None:
        pass

    fired = []

    def listener(ev: dict) -> None:
        if ev["type"] == "message_update" and not fired:
            fired.append(1)
            session.spawn(session.prompt("stop and do this", streaming_behavior="steer"))

    session.subscribe(listener)
    await session.prompt("start")
    assert [p["content"][0]["text"] for p in engine.prompts] == ["start", "stop and do this"]
    ends = [e for e in rec.events if e["type"] == "agent_end"]
    assert len(ends) == 2 and rec.types()[-1] == "agent_settled"


async def test_set_model_thinking_and_compact(agent_dir: Path, project: Path) -> None:
    session, engine, rec = await make(project, [[{"text": "x"}]])
    await session.set_model("fake/fake-9")
    assert engine.model == "fake-9" and session.model == {"provider": "fake", "modelId": "fake-9"}
    session.set_thinking_level("high")
    await session.prompt("q")
    assert engine.thinking_level == "high"
    comp = await session.compact("shorter please")
    assert comp and engine.compactions == ["shorter please"]
    kinds = [e["type"] for e in session.session_manager.get_entries()]
    assert kinds[:2] == ["model_change", "thinking_level_change"] and kinds[-1] == "compaction"
    assert "compaction_end" in rec.types()
    try:
        await session.set_model("other/model")
    except Exception as exc:  # noqa: BLE001
        assert "start a new session" in str(exc)
    else:
        raise AssertionError("cross-engine model switch should be refused")


async def test_fork_and_navigate(agent_dir: Path, project: Path) -> None:
    session, engine, rec = await make(
        project, [[{"text": "one"}], [{"text": "two"}], [{"text": "three"}]]
    )
    await session.prompt("first")
    await session.prompt("second")
    forkable = session.get_user_messages_for_forking()
    assert [m["text"] for m in forkable] == ["first", "second"]
    old_file = session.session_file
    old_engine_id = engine.session_id
    result = await session.fork(forkable[1]["entryId"])
    assert result == {"text": "second", "cancelled": False}
    assert session.session_file != old_file
    assert session.session_manager.get_header()["parentSession"] == old_file
    assert engine.session_id != old_engine_id
    # leaf is before "second": the branch has first/assistant/engine entries only
    branch_roles = [
        e.get("message", {}).get("role") or e.get("customType")
        for e in session.session_manager.get_branch()
    ]
    assert "second" not in [
        str(e.get("message", {}).get("content")) for e in session.session_manager.get_branch()
    ]
    assert branch_roles[-1] == ENGINE_SESSION_ENTRY
    await session.prompt("third")
    assert "rewound" in engine.prompts[-1]["content"][0]["text"]
    first_entry = forkable[0]["entryId"]
    nav = await session.navigate_tree(first_entry, custom_summary="went back")
    assert nav == {"cancelled": False}
    assert session.session_manager.get_leaf_entry()["type"] == "branch_summary"


async def test_execute_bash_and_new_session(agent_dir: Path, project: Path) -> None:
    session, engine, rec = await make(project, [[{"text": "ok"}]])
    chunks: list[str] = []
    res = await session.execute_bash("printf 'hi'; exit 2", on_update=chunks.append)
    assert res["output"] == "hi" and res["exitCode"] == 2 and chunks == ["hi"]
    assert session.session_manager.get_entries()[-1]["message"]["role"] == "bashExecution"
    await session.prompt("what happened?")
    assert "<bash_execution" in engine.prompts[-1]["content"][0]["text"]
    old_id = session.session_id
    assert (await session.new_session())["cancelled"] is False
    assert session.session_id != old_id and session.session_manager.get_entries() == []
    assert "session_shutdown" not in rec.types()  # extension events are not subscriber events


async def test_extension_commands_and_bridge(agent_dir: Path, project: Path) -> None:
    ext = write(
        project / "e" / "cmd.py",
        "def setup(api):\n"
        "    api.register_command('name', handler=lambda args, ctx: api.set_session_name(args), description='rename')\n"
        "    api.on('before_agent_start', lambda e, c: {'message': {'customType': 'memo', 'content': 'remember X', 'display': False}})\n",
    )
    runner = ExtensionRunner()
    await load_extension(ext, runner)
    session, engine, rec = await make(project, [[{"text": "ok"}]], runner=runner)
    await session.prompt("/name my session")
    assert session.session_name == "my session"
    assert engine.prompts == []
    await session.prompt("hello")
    sent = engine.prompts[0]["content"][0]["text"]
    assert sent.startswith('<context source="memo">') and sent.endswith("hello")
    kinds = [e["type"] for e in session.session_manager.get_entries()]
    assert "custom_message" in kinds
    assert session.get_commands()[0]["name"] == "name"


async def test_create_agent_session_factory(agent_dir: Path, project: Path) -> None:
    write(
        agent_dir / "extensions" / "g.py",
        "def setup(api):\n    api.register_flag('g', type='boolean', default=True)\n",
    )
    write(
        project / ".localcode" / "extensions" / "p.py",
        "def setup(api):\n    api.register_flag('p', type='boolean', default=True)\n",
    )
    untrusted = await create_agent_session(
        engine=FakeEngine(), cwd=str(project), in_memory=True, project_trusted=False
    )
    assert untrusted.runner.get_flag("g") is True and untrusted.runner.get_flag("p") is None
    trusted = await create_agent_session(
        engine=FakeEngine(), cwd=str(project), project_trusted=True
    )
    assert trusted.runner.get_flag("p") is True and trusted.session_manager.is_persisted()
    await trusted.prompt("hi")
    resumed = await create_agent_session(
        engine=FakeEngine(), cwd=str(project), session="continue", project_trusted=True
    )
    assert resumed.session_id == trusted.session_id
