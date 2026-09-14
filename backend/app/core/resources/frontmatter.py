"""YAML front matter for SKILL.md and prompt templates (pi: utils/frontmatter.ts)."""

from __future__ import annotations

from typing import Any

import yaml


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter, body)``. Missing or malformed front matter →
    ``({}, text)``; never raises."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1 :])
            try:
                data = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                return {}, text
            return (data if isinstance(data, dict) else {}), body
    return {}, text
