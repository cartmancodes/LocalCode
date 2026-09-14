"""The fleet package: config, complexity gate, verdicts, and dispatch."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.app.core.agent_session import AgentSession, create_agent_session
from backend.app.core.engines import FakeEngine
from backend.app.core.extensions import ExtensionRunner, load_extension
from backend.app.core.packages import install_package
from backend.app.core.session_manager import SessionManager

FLEET_DIR = Path(__file__).resolve().parents[2] / "packages" / "fleet"
FLEET_INDEX = FLEET_DIR / "extensions" / "index.py"


def _import_fleet() -> Any:
    """Import the fleet extension the way the loader does — as a package, so
    its relative imports resolve — and hand back the module."""
    import importlib.util

    name = "localcode_fleet_ext"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, FLEET_INDEX, submodule_search_locations=[str(FLEET_INDEX.parent)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fleet_index = _import_fleet()
fleet_config = sys.modules["localcode_fleet_ext.config"]
fleet_gate = sys.modules["localcode_fleet_ext.gate"]
fleet_verdict = sys.modules["localcode_fleet_ext.verdict"]

DEFAULT_ROLES = fleet_config.DEFAULT_ROLES
RoleConfig = fleet_config.RoleConfig
load_fleet_config = fleet_config.load_fleet_config
parse_config = fleet_config.parse_config
Complexity = fleet_gate.Complexity
classify_complexity = fleet_gate.classify_complexity
parse_verdict = fleet_verdict.parse_verdict


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# ── config ─────────────────────────────────────────────────────────────────


def test_default_config_when_no_file(project: Path) -> None:
    config = load_fleet_config(project)
    assert config.name == "default" and config.source is None
    assert sorted(config.roles) == sorted(DEFAULT_ROLES)
    assert config.gate_enabled is True and config.always_full is False


def test_role_sandbox_derives_from_write_access() -> None:
    """Write access picks the sandbox; being a gate is a separate axis."""
    writer = RoleConfig(engine="claude", model="m", can_write=True)
    assert writer.permission_mode == "acceptEdits" and writer.is_gate is False
    reader = RoleConfig(engine="claude", model="m", can_write=False)
    assert reader.permission_mode == "plan"
    assert reader.is_gate is False  # a read-only planner gates nothing
    gate = RoleConfig(engine="claude", model="m", can_write=False, gate=True)
    assert gate.is_gate is True
    codex_reader = RoleConfig(engine="codex", model="m", can_write=False)
    assert codex_reader.permission_mode == "read-only"
    explicit = RoleConfig(
        engine="claude", model="m", can_write=False, permission_mode="bypassPermissions"
    )
    assert explicit.permission_mode == "bypassPermissions"


def test_yaml_config_roundtrip(project: Path) -> None:
    write(
        project / ".localcode" / "fleet.yaml",
        "name: mine\n"
        "gate: { enabled: false, always_full: true }\n"
        "roles:\n"
        "  planner: { engine: claude, model: claude-opus-4-7, thinking_level: high }\n"
        "  coder:   { engine: codex, model: gpt-5.5 }\n"
        "  reviewer: { engine: claude, model: claude-sonnet-4-6, sandbox: read-only }\n",
    )
    config = load_fleet_config(project)
    assert config.name == "mine" and config.source.endswith("fleet.yaml")
    assert sorted(config.roles) == ["coder", "planner", "reviewer"]  # tester dropped
    assert config.roles["coder"].engine == "codex" and config.roles["coder"].can_write is True
    assert (
        config.roles["planner"].thinking_level == "high"
        and config.roles["planner"].can_write is False
    )
    assert config.roles["reviewer"].permission_mode == "read-only"
    assert config.gate_enabled is False and config.always_full is True


def test_json_config_and_legacy_provider_key(project: Path) -> None:
    write(
        project / ".localcode" / "fleet.json",
        json.dumps({"name": "j", "roles": {"coder": {"provider": "codex", "model": "gpt-5.5"}}}),
    )
    config = load_fleet_config(project)
    assert config.roles["coder"].engine == "codex"  # 'provider' still accepted


def test_malformed_config_falls_back(project: Path, caplog: pytest.LogCaptureFixture) -> None:
    write(project / ".localcode" / "fleet.yaml", "roles: [not, a, mapping]\n")
    assert sorted(load_fleet_config(project).roles) == sorted(DEFAULT_ROLES)
    assert parse_config({"roles": {"nope": {"model": "m"}}}).roles.keys() == DEFAULT_ROLES.keys()
    assert parse_config({"roles": {"coder": {"engine": "x"}}}).roles["coder"].engine == "x"
    # a role with no model at all is skipped, not guessed
    partial = parse_config({"roles": {"coder": {"model": ""}, "planner": {"model": "p"}}})
    assert sorted(partial.roles) == ["coder", "planner"]  # coder falls back to its default model


# ── complexity gate ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("how many files are in this repo?", Complexity.SIMPLE),
        ("what does setup.sh do", Complexity.SIMPLE),
        ("list the routes", Complexity.SIMPLE),
        ("fix the typo in README", Complexity.SIMPLE),
        ("hello", Complexity.SIMPLE),
        ("implement the quota governor and then wire it into the RPC layer", Complexity.COMPLEX),
        ("plan the migration", Complexity.COMPLEX),
        ("review this and test it", Complexity.COMPLEX),
        ("refactor every provider across the whole backend", Complexity.COMPLEX),
        ("", Complexity.UNKNOWN),
    ],
)
def test_classify_complexity(prompt: str, expected: Complexity) -> None:
    assert classify_complexity(prompt) is expected


def test_long_mutating_request_is_complex() -> None:
    prompt = "implement " + " ".join(f"thing{i}" for i in range(70))
    assert classify_complexity(prompt) is Complexity.COMPLEX


def test_medium_mutating_request_defers_to_the_model() -> None:
    """Too long to be one edit, too short to obviously need a crew."""
    prompt = (
        "update the session manager so it records the engine identifier alongside "
        "each stored entry and keeps the existing branch semantics intact"
    )
    assert classify_complexity(prompt) is Complexity.UNKNOWN


def test_short_mutating_request_is_one_edit() -> None:
    assert classify_complexity("update the readme to mention the new flag") is Complexity.SIMPLE


def test_json_verdict_is_preferred() -> None:
    v = parse_verdict(
        'Looks good.\n\n```json\n{"verdict": "pass", "reason": "all tasks present"}\n```'
    )
    assert v.passed and v.source == "json" and v.reason == "all tasks present"
    failed = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "task 6 missing", "blame": "code"}\n```'
    )
    assert not failed.passed and failed.blame == "code"
    bare = parse_verdict('result: {"verdict": "pass", "reason": "ok"}')
    assert bare.passed and bare.source == "json"
    last_wins = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "first"}\n```\nOn reflection:\n'
        '```json\n{"verdict": "pass", "reason": "second"}\n```'
    )
    assert last_wins.passed and last_wins.reason == "second"


def test_classifier_fallback_still_works() -> None:
    assert parse_verdict("Everything checks out.\n\nLGTM").passed
    nack = parse_verdict("Task 6 is missing.\n\nNACK: src/cli.py absent")
    assert not nack.passed and nack.blame == "code" and "cli.py" in nack.reason
    tests = parse_verdict("NACK_TESTS: my fixture was wrong")
    assert tests.blame == "tests" and tests.source == "classifier"
    decorated = parse_verdict("**LGTM**")
    assert decorated.passed


def test_chatty_reply_no_longer_breaks_the_gate() -> None:
    """The old last-line parser turned a closing pleasantry into a NACK."""
    chatty = 'All tasks present.\n\n```json\n{"verdict": "pass", "reason": "matches the plan"}\n```\n\nThanks for the review!'
    assert parse_verdict(chatty).passed


def test_missing_verdict_fails_safe() -> None:
    v = parse_verdict("I had a look and it seems fine to me.")
    assert not v.passed and v.source == "missing"
    assert parse_verdict("").verdict == "fail"


def test_tool_digest_is_stripped_before_parsing() -> None:
    text = "LGTM\n---\n(tool activity from claude:sonnet)\n- Bash input={}\n    [OK] NACK: noise"
    assert parse_verdict(text).passed


# ── the extension, end to end ──────────────────────────────────────────────


async def load_fleet(runner: ExtensionRunner) -> Any:
    ext = await load_extension(FLEET_DIR / "extensions" / "index.py", runner)
    assert ext is not None, runner.errors
    return ext


async def test_extension_registers_dispatch_and_command(agent_dir: Path, project: Path) -> None:
    runner = ExtensionRunner()
    await load_fleet(runner)
    found = runner.get_tool("dispatch")
    assert found is not None
    _ext, tool = found
    assert tool.parameters["required"] == ["name", "task"]
    assert runner.get_command("fleet") is not None


async def test_gate_annotates_simple_turns_only(agent_dir: Path, project: Path) -> None:
    runner = ExtensionRunner()
    await load_fleet(runner)
    session = AgentSession(
        engine=FakeEngine([[{"text": "ok"}], [{"text": "ok"}]]),
        session_manager=SessionManager.in_memory(project),
        runner=runner,
    )
    await session.start()
    simple = await runner.emit_before_agent_start(
        "how many files are here?", None, "", session.context
    )
    assert [m["customType"] for m in simple["messages"]] == ["fleet_gate"]
    assert "do not dispatch subagents" in simple["messages"][0]["content"]
    assert simple["messages"][0]["display"] is False  # the note is for the model, not the reader

    involved = await runner.emit_before_agent_start(
        "implement the governor and then wire it in", None, "", session.context
    )
    assert involved["messages"] == []


async def test_gate_off_when_configured(agent_dir: Path, project: Path) -> None:
    write(
        project / ".localcode" / "fleet.yaml",
        "gate: { enabled: false }\nroles:\n  coder: { engine: fake, model: f }\n",
    )
    runner = ExtensionRunner()
    await load_fleet(runner)
    session = AgentSession(
        engine=FakeEngine(), session_manager=SessionManager.in_memory(project), runner=runner
    )
    await session.start()
    result = await runner.emit_before_agent_start("what is this?", None, "", session.context)
    assert result["messages"] == []


async def test_dispatch_runs_a_subagent_and_returns_its_text(
    agent_dir: Path, project: Path
) -> None:
    write(
        project / ".localcode" / "fleet.yaml",
        "roles:\n"
        "  planner:  { engine: fake, model: fake-plan, can_write: false }\n"
        "  coder:    { engine: fake, model: fake-code }\n"
        "  reviewer: { engine: fake, model: fake-review, can_write: false }\n",
    )
    runner = ExtensionRunner()
    await load_fleet(runner)
    session = AgentSession(
        engine=FakeEngine(), session_manager=SessionManager.in_memory(project), runner=runner
    )
    await session.start()
    _ext, tool = runner.get_tool("dispatch")

    updates: list[dict] = []
    result = await tool.execute(
        "c1", {"name": "planner", "task": "plan it"}, None, updates.append, session.context
    )
    assert not result.get("isError")
    text = result["content"][0]["text"]
    assert text.startswith("[planner · fake:fake-plan ·") and "echo: plan it" in text
    assert result["details"]["engine"] == "fake" and result["details"]["verdict"] is None
    assert updates and "dispatching planner" in updates[0]["content"][0]["text"]
    # the plan is persisted as an artifact
    plans = list((project / ".localcode" / "plans").glob("*.md"))
    assert len(plans) == 1 and "echo: plan it" in plans[0].read_text()


async def test_dispatch_passes_the_plan_to_the_coder(agent_dir: Path, project: Path) -> None:
    write(
        project / ".localcode" / "fleet.yaml",
        "roles:\n  planner: { engine: fake, model: p, can_write: false }\n  coder: { engine: fake, model: c }\n",
    )
    runner = ExtensionRunner()
    await load_fleet(runner)
    session = AgentSession(
        engine=FakeEngine(), session_manager=SessionManager.in_memory(project), runner=runner
    )
    await session.start()
    _ext, tool = runner.get_tool("dispatch")
    await tool.execute(
        "c1", {"name": "planner", "task": "plan the thing"}, None, None, session.context
    )
    result = await tool.execute(
        "c2", {"name": "coder", "task": "build it"}, None, None, session.context
    )
    # the fake engine echoes its prompt, so the coder's reply shows what it received
    assert "The plan you are working from" in result["content"][0]["text"]
    assert "echo: plan the thing" in result["content"][0]["text"]


async def test_gate_role_verdict_is_parsed(agent_dir: Path, project: Path) -> None:
    write(
        project / ".localcode" / "fleet.yaml",
        "roles:\n  reviewer: { engine: fake, model: r, can_write: false }\n",
    )
    runner = ExtensionRunner()
    await load_fleet(runner)
    session = AgentSession(
        engine=FakeEngine(), session_manager=SessionManager.in_memory(project), runner=runner
    )
    await session.start()
    _ext, tool = runner.get_tool("dispatch")
    result = await tool.execute(
        "c1", {"name": "reviewer", "task": "review"}, None, None, session.context
    )
    # the echo engine returns no verdict → fail-safe
    assert result["details"]["verdict"]["verdict"] == "fail"
    assert result["details"]["verdict"]["source"] == "missing"
    assert "verdict=fail" in result["content"][0]["text"]


async def test_dispatch_rejects_unknown_roles_and_budgets(agent_dir: Path, project: Path) -> None:
    write(project / ".localcode" / "fleet.yaml", "roles:\n  coder: { engine: fake, model: c }\n")
    runner = ExtensionRunner()
    await load_fleet(runner)
    session = AgentSession(
        engine=FakeEngine(), session_manager=SessionManager.in_memory(project), runner=runner
    )
    await session.start()
    _ext, tool = runner.get_tool("dispatch")

    unknown = await tool.execute("c", {"name": "nope", "task": "x"}, None, None, session.context)
    assert unknown["isError"] and "Registered: coder" in unknown["content"][0]["text"]
    empty = await tool.execute("c", {"name": "coder", "task": "  "}, None, None, session.context)
    assert empty["isError"] and "missing 'task'" in empty["content"][0]["text"]

    max_dispatches = fleet_index.MAX_DISPATCHES_PER_TURN

    for i in range(max_dispatches):
        await tool.execute(f"c{i}", {"name": "coder", "task": "work"}, None, None, session.context)
    spent = await tool.execute(
        "last", {"name": "coder", "task": "more"}, None, None, session.context
    )
    assert spent["isError"] and "budget for this turn is spent" in spent["content"][0]["text"]


async def test_failing_engine_is_capped(agent_dir: Path, project: Path) -> None:
    write(
        project / ".localcode" / "fleet.yaml", "roles:\n  coder: { engine: exploding, model: c }\n"
    )
    runner = ExtensionRunner()
    await load_fleet(runner)

    def exploding(**options: Any) -> Any:
        raise RuntimeError("engine is down")

    runner.engine_factories["exploding"] = exploding
    session = AgentSession(
        engine=FakeEngine(), session_manager=SessionManager.in_memory(project), runner=runner
    )
    await session.start()
    _ext, tool = runner.get_tool("dispatch")

    first = await tool.execute("c1", {"name": "coder", "task": "x"}, None, None, session.context)
    assert first["isError"] and "engine is down" in first["content"][0]["text"]
    await tool.execute("c2", {"name": "coder", "task": "x"}, None, None, session.context)
    third = await tool.execute("c3", {"name": "coder", "task": "x"}, None, None, session.context)
    assert "Do not dispatch it again" in third["content"][0]["text"]


async def test_fleet_installs_as_a_package_and_registers_its_tool(
    agent_dir: Path, project: Path
) -> None:
    """The end state of phase 5: the fleet arrives through `localcode install`."""
    install_package(str(FLEET_DIR), cwd=project, agent_dir=agent_dir)
    session = await create_agent_session(
        engine=FakeEngine(), cwd=str(project), in_memory=True, project_trusted=True
    )
    assert [t.name for t in session.runner.all_tools()] == ["dispatch"]
    assert any(c["name"] == "fleet" for c in session.get_commands())
