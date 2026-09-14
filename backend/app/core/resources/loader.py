"""ResourceLoader — where skills, prompt templates, context files and the
system-prompt overrides come from (pi: ``core/resource-loader.ts``).

Discovery, in pi's order:

    <agent_dir>/skills, <agent_dir>/prompts            user-global
    <cwd>/.localcode/skills, <cwd>/.localcode/prompts  project (trusted only)
    <cwd>/.agents/skills                               Agent Skills standard
    paths returned by extensions' ``resources_discover``

System prompt files: ``SYSTEM.md`` replaces the engine's default prompt,
``APPEND_SYSTEM.md`` is appended — project beats global for each.

Context files (``AGENTS.override.md`` / ``AGENTS.md`` / ``CLAUDE.md``) are
walked from the global dir, then each parent of ``cwd`` down to ``cwd``.
Both engines already read these natively, so by default they are *listed*
but not injected; ``inject_context_files=True`` appends them for an engine
that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import CONFIG_DIR_NAME, get_default_agent_dir
from .prompt_templates import PromptTemplate, load_prompt_templates_from_dir, merge_templates
from .skills import (
    LoadSkillsResult,
    ResourceDiagnostic,
    Skill,
    format_skills_for_prompt,
    load_skills_from_dir,
    merge_skills,
)

CONTEXT_FILE_CANDIDATES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


@dataclass
class ContextFile:
    path: str
    content: str


@dataclass
class Resources:
    skills: list[Skill] = field(default_factory=list)
    prompt_templates: list[PromptTemplate] = field(default_factory=list)
    context_files: list[ContextFile] = field(default_factory=list)
    system_prompt: str | None = None  # SYSTEM.md
    append_system_prompt: str | None = None  # APPEND_SYSTEM.md
    diagnostics: list[ResourceDiagnostic] = field(default_factory=list)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError:
        return None


def _context_file_in(directory: Path) -> ContextFile | None:
    for name in CONTEXT_FILE_CANDIDATES:
        text = _read(directory / name)
        if text is not None and text.strip():
            return ContextFile(str(directory / name), text)
    return None


def load_context_files(cwd: str | Path, agent_dir: str | Path | None = None) -> list[ContextFile]:
    base = Path(agent_dir) if agent_dir else get_default_agent_dir()
    files: list[ContextFile] = []
    global_file = _context_file_in(base)
    if global_file:
        files.append(global_file)
    root = Path(cwd).expanduser()
    chain = [*reversed(root.parents), root]
    for directory in chain:
        found = _context_file_in(directory)
        if found:
            files.append(found)
    return files


class ResourceLoader:
    def __init__(
        self,
        *,
        cwd: str | Path,
        agent_dir: str | Path | None = None,
        project_trusted: bool = False,
        extra_skill_paths: list[str] | None = None,
        extra_prompt_paths: list[str] | None = None,
        include_defaults: bool = True,
    ) -> None:
        self.cwd = Path(cwd).expanduser()
        self.agent_dir = Path(agent_dir) if agent_dir else get_default_agent_dir()
        self.project_trusted = project_trusted
        self.extra_skill_paths = list(extra_skill_paths or [])
        self.extra_prompt_paths = list(extra_prompt_paths or [])
        self.include_defaults = include_defaults
        self.resources = Resources()

    def load(self) -> Resources:
        project = self.cwd / CONFIG_DIR_NAME
        skill_results: list[LoadSkillsResult] = []
        template_groups: list[list[PromptTemplate]] = []
        if self.include_defaults:
            skill_results.append(load_skills_from_dir(self.agent_dir / "skills", "user"))
            template_groups.append(
                load_prompt_templates_from_dir(self.agent_dir / "prompts", "user")
            )
            if self.project_trusted:
                skill_results.append(load_skills_from_dir(project / "skills", "project"))
                skill_results.append(
                    load_skills_from_dir(self.cwd / ".agents" / "skills", "project")
                )
                template_groups.append(
                    load_prompt_templates_from_dir(project / "prompts", "project")
                )
        for path in self.extra_skill_paths:
            skill_results.append(load_skills_from_dir(path, "extension"))
        for path in self.extra_prompt_paths:
            template_groups.append(load_prompt_templates_from_dir(path, "extension"))
        merged = merge_skills(*skill_results)
        res = Resources(
            skills=merged.skills,
            prompt_templates=merge_templates(*template_groups),
            context_files=load_context_files(self.cwd, self.agent_dir),
            diagnostics=merged.diagnostics,
        )
        res.system_prompt = self._first(project / "SYSTEM.md", self.agent_dir / "SYSTEM.md")
        res.append_system_prompt = self._first(
            project / "APPEND_SYSTEM.md", self.agent_dir / "APPEND_SYSTEM.md"
        )
        self.resources = res
        return res

    def _first(self, *paths: Path) -> str | None:
        for i, path in enumerate(paths):
            if i == 0 and not self.project_trusted:
                continue  # project-level prompt files need trust
            text = _read(path)
            if text is not None and text.strip():
                return text.strip()
        return None

    def append_prompt(self, *, inject_context_files: bool = False) -> str | None:
        """What to hand the engine as ``append_system_prompt``."""
        parts: list[str] = []
        if self.resources.append_system_prompt:
            parts.append(self.resources.append_system_prompt)
        if inject_context_files and self.resources.context_files:
            block = ["<project_context>", "", "Project-specific instructions and guidelines:", ""]
            for cf in self.resources.context_files:
                block.append(
                    f'<project_instructions path="{cf.path}">\n{cf.content}\n</project_instructions>\n'
                )
            block.append("</project_context>")
            parts.append("\n".join(block))
        skills_block = format_skills_for_prompt(self.resources.skills)
        if skills_block:
            parts.append(skills_block.strip())
        return "\n\n".join(parts) if parts else None
