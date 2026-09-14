from __future__ import annotations

from pathlib import Path

from backend.app.core.agent_session import create_agent_session
from backend.app.core.engines import FakeEngine
from backend.app.core.resources import (
    ResourceLoader,
    expand_prompt_template,
    expand_skill_command,
    format_skills_for_prompt,
    load_context_files,
    parse_command_args,
    substitute_args,
)
from backend.app.core.resources.frontmatter import parse_frontmatter
from backend.app.core.resources.prompt_templates import load_prompt_templates_from_dir
from backend.app.core.resources.skills import load_skills_from_dir, merge_skills


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_frontmatter() -> None:
    assert parse_frontmatter("no front matter") == ({}, "no front matter")
    fm, body = parse_frontmatter("---\nname: X\ndescription: does x\n---\nbody here\n")
    assert fm == {"name": "X", "description": "does x"} and body == "body here\n"
    assert parse_frontmatter("---\n: bad: [yaml\n---\nb")[0] == {}
    assert parse_frontmatter("---\nunterminated")[1] == "---\nunterminated"


def test_skills_discovery_names_and_diagnostics(project: Path) -> None:
    d = project / "skills"
    write(d / "Deploy Thing" / "SKILL.md", "---\ndescription: deploy it\n---\nrun deploy.sh\n")
    write(
        d / "review.md",
        "---\nname: Code Review\ndescription: review code\ndisable-model-invocation: true\n---\nlook carefully\n",
    )
    write(d / "nodesc" / "SKILL.md", "---\nname: x\n---\nno description\n")
    write(d / ".hidden.md", "---\ndescription: hidden\n---\n")
    result = load_skills_from_dir(d, "user")
    assert [s.name for s in result.skills] == ["deploy-thing", "code-review"]
    assert result.skills[0].content == "run deploy.sh" and result.skills[0].base_dir == str(
        d / "Deploy Thing"
    )
    assert result.skills[1].disable_model_invocation is True
    assert len(result.diagnostics) == 1 and "no description" in result.diagnostics[0].message
    block = format_skills_for_prompt(result.skills)
    assert (
        "<name>deploy-thing</name>" in block and "code-review" not in block
    )  # disabled skills stay hidden
    assert format_skills_for_prompt([]) == ""
    expanded = expand_skill_command("/skill:deploy-thing to prod", result.skills)
    assert expanded.startswith('<skill name="deploy-thing"') and expanded.endswith("to prod")
    assert expand_skill_command("/skill:nope", result.skills) is None
    assert expand_skill_command("plain", result.skills) is None
    # project overrides user on the same name
    d2 = project / "skills2"
    write(d2 / "deploy-thing" / "SKILL.md", "---\ndescription: project deploy\n---\nproject way\n")
    merged = merge_skills(result, load_skills_from_dir(d2, "project"))
    assert {s.name: s.source for s in merged.skills} == {
        "deploy-thing": "project",
        "code-review": "user",
    }


def test_prompt_templates(project: Path) -> None:
    d = project / "prompts"
    write(
        d / "fix.md",
        "---\ndescription: fix a bug\nargument-hint: <file> <issue>\n---\nFix $2 in $1. All: $@ / $ARGUMENTS / {{args}}\n",
    )
    write(d / "plain.md", "Just do it\n")
    templates = load_prompt_templates_from_dir(d, "user")
    assert [t.name for t in templates] == ["fix", "plain"]
    assert templates[0].argument_hint == "<file> <issue>" and templates[1].description == ""
    assert parse_command_args('a.py "null deref" tail') == ["a.py", "null deref", "tail"]
    assert parse_command_args('unbalanced "quote') == ["unbalanced", '"quote']
    assert substitute_args("$1-$2-$3", ["x", "y"]) == "x-y-"
    out = expand_prompt_template('/fix a.py "null deref"', templates)
    assert out == "Fix null deref in a.py. All: a.py null deref / a.py null deref / a.py null deref"
    assert expand_prompt_template("/unknown x", templates) == "/unknown x"
    assert expand_prompt_template("not a command", templates) == "not a command"


def test_context_files_walk_global_then_parents(agent_dir: Path, project: Path) -> None:
    write(agent_dir / "AGENTS.md", "global rules")
    write(project.parent / "CLAUDE.md", "parent rules")
    write(project / "AGENTS.override.md", "override wins")
    write(project / "AGENTS.md", "ignored because override exists")
    files = load_context_files(project, agent_dir)
    assert [f.content for f in files] == ["global rules", "parent rules", "override wins"]


def test_resource_loader_trust_gating_and_append_prompt(agent_dir: Path, project: Path) -> None:
    write(agent_dir / "skills" / "g" / "SKILL.md", "---\ndescription: global skill\n---\ng\n")
    write(agent_dir / "APPEND_SYSTEM.md", "global append")
    write(project / ".localcode" / "SYSTEM.md", "project system prompt")
    write(project / ".localcode" / "APPEND_SYSTEM.md", "project append")
    write(
        project / ".localcode" / "skills" / "p" / "SKILL.md",
        "---\ndescription: project skill\n---\np\n",
    )
    write(
        project / ".agents" / "skills" / "std" / "SKILL.md",
        "---\ndescription: standard skill\n---\ns\n",
    )
    write(project / ".localcode" / "prompts" / "t.md", "template $1\n")
    write(project / "AGENTS.md", "agents rules")

    untrusted = ResourceLoader(cwd=project, agent_dir=agent_dir, project_trusted=False).load()
    assert [s.name for s in untrusted.skills] == ["g"] and untrusted.prompt_templates == []
    assert untrusted.system_prompt is None and untrusted.append_system_prompt == "global append"

    loader = ResourceLoader(cwd=project, agent_dir=agent_dir, project_trusted=True)
    trusted = loader.load()
    assert sorted(s.name for s in trusted.skills) == ["g", "p", "std"]
    assert [t.name for t in trusted.prompt_templates] == ["t"]
    assert trusted.system_prompt == "project system prompt"
    assert trusted.append_system_prompt == "project append"
    assert [Path(c.path).name for c in trusted.context_files] == ["AGENTS.md"]
    appended = loader.append_prompt()
    assert (
        appended.startswith("project append")
        and "<available_skills>" in appended
        and "AGENTS" not in appended
    )
    with_ctx = loader.append_prompt(inject_context_files=True)
    assert '<project_instructions path="' in with_ctx and "agents rules" in with_ctx
    extra = ResourceLoader(
        cwd=project, agent_dir=agent_dir, extra_skill_paths=[str(project / ".localcode" / "skills")]
    ).load()
    assert {s.name: s.source for s in extra.skills} == {"g": "user", "p": "extension"}


async def test_session_expands_skills_and_templates(agent_dir: Path, project: Path) -> None:
    write(
        project / ".localcode" / "skills" / "tidy" / "SKILL.md",
        "---\ndescription: tidy up\n---\nremove clutter\n",
    )
    write(
        project / ".localcode" / "prompts" / "greet.md",
        "---\ndescription: greet\n---\nSay hello to $1\n",
    )
    write(project / ".localcode" / "APPEND_SYSTEM.md", "be brief")
    write(
        project / ".localcode" / "extensions" / "disc.py",
        "def setup(api):\n    api.on('resources_discover', lambda e, c: {'promptPaths': [e['cwd'] + '/more']})\n",
    )
    write(project / "more" / "extra.md", "Extra $@\n")
    engine = FakeEngine([[{"text": "a"}], [{"text": "b"}], [{"text": "c"}]])
    session = await create_agent_session(
        engine=engine,
        cwd=str(project),
        in_memory=True,
        project_trusted=True,
        append_system_prompt="caller first",
    )
    assert engine.config is None
    await session.prompt("/skill:tidy the garage")
    sent = engine.prompts[0]["content"][0]["text"]
    assert (
        sent.startswith('<skill name="tidy"')
        and "remove clutter" in sent
        and sent.endswith("the garage")
    )
    assert (
        engine.config.append_system_prompt.startswith("caller first\n\nbe brief")
        and "<available_skills>" in engine.config.append_system_prompt
    )
    await session.prompt("/greet Ada")
    assert engine.prompts[1]["content"][0]["text"] == "Say hello to Ada"
    await session.prompt("/extra one two")
    assert engine.prompts[2]["content"][0]["text"] == "Extra one two"
    names = {(c["name"], c["source"]) for c in session.get_commands()}
    assert names == {("greet", "prompt"), ("extra", "prompt"), ("skill:tidy", "skill")}
    assert session.get_commands()[0]["sourceInfo"]["source"] == "project"
