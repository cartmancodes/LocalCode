"""Prompt templates (pi: ``core/prompt-templates.ts``).

A template is a Markdown file; front matter may give ``description`` and
``argument-hint``. Invoked as ``/name arg1 "arg two"``; ``$1``…``$9``, ``$@``
and ``$ARGUMENTS`` are substituted, ``{{args}}`` is accepted as an alias.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter


@dataclass
class PromptTemplate:
    name: str
    description: str
    content: str
    file_path: str
    source: str
    argument_hint: str | None = None


def parse_command_args(args_string: str) -> list[str]:
    try:
        return shlex.split(args_string)
    except ValueError:
        return args_string.split()


def substitute_args(content: str, args: list[str]) -> str:
    joined = " ".join(args)
    out = content.replace("$ARGUMENTS", joined).replace("$@", joined).replace("{{args}}", joined)

    def positional(match: re.Match[str]) -> str:
        idx = int(match.group(1)) - 1
        return args[idx] if 0 <= idx < len(args) else ""

    return re.sub(r"\$(\d)", positional, out)


def load_prompt_templates_from_dir(directory: str | Path, source: str) -> list[PromptTemplate]:
    root = Path(directory)
    if not root.is_dir():
        return []
    out: list[PromptTemplate] = []
    for path in sorted(root.glob("*.md"), key=lambda p: p.name):
        if path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        description = fm.get("description") if isinstance(fm.get("description"), str) else ""
        hint = fm.get("argument-hint") if isinstance(fm.get("argument-hint"), str) else None
        out.append(
            PromptTemplate(
                name=path.stem,
                description=description or "",
                content=body.strip(),
                file_path=str(path),
                source=source,
                argument_hint=hint,
            )
        )
    return out


def merge_templates(*groups: list[PromptTemplate]) -> list[PromptTemplate]:
    by_name: dict[str, PromptTemplate] = {}
    for group in groups:
        for template in group:
            by_name[template.name] = template
    return list(by_name.values())


def expand_prompt_template(text: str, templates: list[PromptTemplate]) -> str:
    """``/name args`` → the template body with arguments substituted; other
    text is returned unchanged."""
    if not text.startswith("/"):
        return text
    head, _, rest = text[1:].partition(" ")
    template = next((t for t in templates if t.name == head), None)
    if template is None:
        return text
    return substitute_args(template.content, parse_command_args(rest))


def template_infos(templates: list[PromptTemplate]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "source": "prompt",
            "sourceInfo": {"path": t.file_path, "source": t.source},
        }
        for t in templates
    ]
