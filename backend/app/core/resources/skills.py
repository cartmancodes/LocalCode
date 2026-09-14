"""Skills — the Agent Skills standard, discovered the way pi does it.

A skill is a directory with ``SKILL.md`` (front matter: ``name``,
``description``, ``disable-model-invocation``) or a top-level ``.md`` file
in a skills directory. Both engines also read skills natively
(``.claude/skills``, ``.agents/skills``); what we add is pi's discovery
hierarchy, ``/skill:name`` invocation, and the ``<available_skills>`` block
appended to the system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .frontmatter import parse_frontmatter

_NAME_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass
class Skill:
    name: str
    description: str
    file_path: str
    base_dir: str
    source: str  # "user" | "project" | "package:<name>" | "extension"
    disable_model_invocation: bool = False
    content: str = ""


@dataclass
class ResourceDiagnostic:
    path: str
    message: str


@dataclass
class LoadSkillsResult:
    skills: list[Skill] = field(default_factory=list)
    diagnostics: list[ResourceDiagnostic] = field(default_factory=list)


def _skill_from_file(path: Path, source: str) -> tuple[Skill | None, ResourceDiagnostic | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, ResourceDiagnostic(str(path), f"unreadable: {exc}")
    fm, body = parse_frontmatter(text)
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, ResourceDiagnostic(str(path), "skill has no description in front matter")
    raw_name = fm.get("name") if isinstance(fm.get("name"), str) else None
    default_name = path.parent.name if path.name == "SKILL.md" else path.stem
    name = _NAME_RE.sub("-", (raw_name or default_name).strip().lower()).strip("-") or default_name
    return (
        Skill(
            name=name,
            description=description.strip(),
            file_path=str(path),
            base_dir=str(path.parent),
            source=source,
            disable_model_invocation=fm.get("disable-model-invocation") is True,
            content=body.strip(),
        ),
        None,
    )


def load_skills_from_dir(directory: str | Path, source: str) -> LoadSkillsResult:
    result = LoadSkillsResult()
    root = Path(directory)
    if not root.is_dir():
        return result
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        candidate: Path | None = None
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            candidate = entry / "SKILL.md"
        elif entry.is_file() and entry.suffix == ".md":
            candidate = entry
        if candidate is None:
            continue
        skill, diag = _skill_from_file(candidate, source)
        if skill:
            result.skills.append(skill)
        if diag:
            result.diagnostics.append(diag)
    return result


def merge_skills(*results: LoadSkillsResult) -> LoadSkillsResult:
    """Later sources override earlier ones with the same name (project beats user)."""
    merged = LoadSkillsResult()
    by_name: dict[str, Skill] = {}
    for res in results:
        merged.diagnostics.extend(res.diagnostics)
        for skill in res.skills:
            by_name[skill.name] = skill
    merged.skills = list(by_name.values())
    return merged


def format_skills_for_prompt(skills: list[Skill], file_read_tool: str = "read") -> str:
    """pi's ``formatSkillsForPrompt``, verbatim in structure."""
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "",
        "",
        "The following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description."
        if file_read_tool == "read"
        else "Use bash to load a skill's file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory "
        "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape(skill.name)}</name>")
        lines.append(f"    <description>{escape(skill.description)}</description>")
        lines.append(f"    <location>{escape(skill.file_path)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def expand_skill_command(text: str, skills: list[Skill]) -> str | None:
    """``/skill:name extra words`` → the skill's instructions plus the words.
    Returns ``None`` when the text is not a skill invocation."""
    if not text.startswith("/skill:"):
        return None
    head, _, rest = text[len("/skill:") :].partition(" ")
    skill = next((s for s in skills if s.name == head.strip()), None)
    if skill is None:
        return None
    parts = [
        f'<skill name="{escape(skill.name)}" location="{escape(skill.file_path)}">',
        skill.content,
        "</skill>",
    ]
    if rest.strip():
        parts.append(rest.strip())
    return "\n".join(parts)


def skill_infos(skills: list[Skill]) -> list[dict[str, Any]]:
    return [
        {
            "name": f"skill:{s.name}",
            "description": s.description,
            "source": "skill",
            "sourceInfo": {"path": s.file_path, "source": s.source},
        }
        for s in skills
    ]
