"""Fleet configuration — which roles exist, on which engine, with what limits.

``.localcode/fleet.yaml`` (or ``.yml`` / ``.json``), resolved from the session
cwd then the process cwd, falling back to built-in defaults. A workflow *is*
its roles: omit a role and it is not part of the workflow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_NAMES = ("fleet.yaml", "fleet.yml", "fleet.json")
KNOWN_ROLES = ("planner", "developer", "coder", "reviewer", "tester")
GATE_ROLES = ("reviewer", "tester")

# Engines that write files need a permissive mode; read-only roles must not
# get one. This is the enforcement the old fleet only asked for in prose.
READ_ONLY_MODES = {"claude": "plan", "codex": "read-only"}
WRITE_MODES = {"claude": "acceptEdits", "codex": "workspace-write"}


@dataclass
class RoleConfig:
    engine: str
    model: str
    can_write: bool = True
    thinking_level: str = "off"
    engine_options: dict[str, Any] = field(default_factory=dict)
    permission_mode: str | None = None
    instructions: str = ""
    # A gate's verdict decides what runs next. Read-only is a separate axis:
    # the planner writes nothing but gates nothing either.
    gate: bool = False

    def __post_init__(self) -> None:
        if self.permission_mode is None:
            table = WRITE_MODES if self.can_write else READ_ONLY_MODES
            self.permission_mode = table.get(self.engine, "default")

    @property
    def is_gate(self) -> bool:
        return self.gate


@dataclass
class FleetConfig:
    name: str = "default"
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    gate_enabled: bool = True
    always_full: bool = False
    source: str | None = None


DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    "planner": {
        "engine": "claude",
        "model": "claude-opus-4-7",
        "can_write": False,
        "thinking_level": "high",
    },
    "coder": {"engine": "claude", "model": "claude-sonnet-4-6", "can_write": True},
    "reviewer": {
        "engine": "claude",
        "model": "claude-sonnet-4-6",
        "can_write": False,
        "gate": True,
    },
    "tester": {"engine": "claude", "model": "claude-haiku-4-5", "can_write": True, "gate": True},
}


def default_config() -> FleetConfig:
    return FleetConfig(roles={name: RoleConfig(**spec) for name, spec in DEFAULT_ROLES.items()})


def find_config_file(cwd: str | Path | None) -> Path | None:
    """Look only in the session's own cwd.

    The old loader also fell back to the process cwd, which meant a server
    started in one repo silently applied that repo's fleet config to sessions
    in every other repo.
    """
    base = Path(cwd) if cwd else Path.cwd()
    for name in CONFIG_NAMES:
        candidate = base / ".localcode" / name
        if candidate.is_file():
            return candidate
    return None


def load_fleet_config(cwd: str | Path | None = None) -> FleetConfig:
    """Parse the active config. A bad field falls back to its default rather
    than failing the whole load — a typo in one role should not take the
    workflow down."""
    path = find_config_file(cwd)
    if path is None:
        return default_config()
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("fleet: ignoring %s: %s", path, exc)
        return default_config()
    if not isinstance(raw, dict):
        logger.warning("fleet: %s must be a mapping", path)
        return default_config()
    config = parse_config(raw)
    config.source = str(path)
    return config


def parse_config(raw: dict[str, Any]) -> FleetConfig:
    config = FleetConfig(name=str(raw.get("name") or "default"))
    gate = raw.get("gate")
    if isinstance(gate, dict):
        config.gate_enabled = bool(gate.get("enabled", True))
        config.always_full = bool(gate.get("always_full", False))

    raw_roles = raw.get("roles")
    if not isinstance(raw_roles, dict) or not raw_roles:
        config.roles = default_config().roles
        return config

    for name, spec in raw_roles.items():
        if name not in KNOWN_ROLES:
            logger.warning("fleet: unknown role %r (known: %s)", name, ", ".join(KNOWN_ROLES))
            continue
        if not isinstance(spec, dict):
            logger.warning("fleet: role %r must be a mapping", name)
            continue
        base = dict(DEFAULT_ROLES.get(name, {"engine": "claude", "model": "claude-sonnet-4-6"}))
        engine = str(spec.get("engine") or spec.get("provider") or base.get("engine", "claude"))
        model = str(spec.get("model") or base.get("model", ""))
        if not model:
            logger.warning("fleet: role %r has no model; skipping", name)
            continue
        can_write = bool(spec.get("can_write", base.get("can_write", name not in GATE_ROLES)))
        options = spec.get("engine_options")
        config.roles[name] = RoleConfig(
            engine=engine,
            model=model,
            can_write=can_write,
            thinking_level=str(spec.get("thinking_level") or base.get("thinking_level", "off")),
            engine_options=dict(options) if isinstance(options, dict) else {},
            permission_mode=_sandbox(spec, engine, can_write),
            instructions=str(spec.get("instructions") or ""),
            gate=bool(spec.get("gate", name in GATE_ROLES)),
        )
    if not config.roles:
        config.roles = default_config().roles
    return config


def _sandbox(spec: dict[str, Any], engine: str, can_write: bool) -> str | None:
    """An explicit ``sandbox``/``permission_mode`` wins; otherwise the mode is
    derived from whether the role is allowed to write."""
    explicit = spec.get("sandbox") or spec.get("permission_mode")
    if explicit:
        return str(explicit)
    return None  # RoleConfig.__post_init__ derives it
