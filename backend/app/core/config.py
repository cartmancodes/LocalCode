"""Paths and names shared by the core.

pi keeps user-global state in ``~/.pi/agent`` and project-local state in
``<cwd>/.pi``. We keep the same split under ``~/.localcode/agent`` and
``<cwd>/.localcode``.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "localcode"

# Project-local config dir (pi: ".pi"). Holds extensions/, skills/, prompts/,
# settings.json, SYSTEM.md, APPEND_SYSTEM.md.
CONFIG_DIR_NAME = ".localcode"

# Env override for the user-global agent dir (pi: PI_CODING_AGENT_DIR).
AGENT_DIR_ENV = "LOCALCODE_AGENT_DIR"


def get_default_agent_dir() -> Path:
    """User-global agent dir (pi: ``~/.pi/agent``)."""
    override = os.environ.get(AGENT_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".localcode" / "agent").resolve()


def get_project_config_dir(cwd: str | Path) -> Path:
    return Path(cwd).expanduser().resolve() / CONFIG_DIR_NAME
