"""Fleet config resolution: locate, parse, merge, cache, and serialize.

Configuration sources (first hit wins):
  1. ``Settings.localcode_fleet_config``               (explicit absolute path)
  2. ``<cwd>/.localcode/fleet.{yaml,yml,json}``        (project-local)
  3. ``<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}``
  4. built-in defaults
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from ...config import get_settings
from .constants import VALID_PROVIDERS, VALID_ROLES
from .defaults import DEFAULT_FLEET_CONFIG, ROLE_LIBRARY
from .models import FleetConfig, RoleConfig

logger = logging.getLogger(__name__)


# Cache the parsed config keyed by (path, mtime). Only re-parses when the file
# changes on disk. Mtime comparison is one stat() per turn — much cheaper than
# parsing YAML and walking it every call. The lock protects the cache dict
# itself from torn writes under concurrent access. Bounded so a session that
# rotates through many cwds can't grow the cache without limit.
_CFG_CACHE_MAX = 16
_CFG_CACHE: dict[str, tuple[float, FleetConfig]] = {}
_CFG_CACHE_LOCK = threading.Lock()


def load_fleet_config(cwd: str | None = None) -> FleetConfig:
    """Resolve and load the active fleet config. Falls back to defaults on
    any failure. Cached by (path, mtime) for cheap repeat reads.

    Validation is best-effort: invalid fields revert to the default rather
    than failing the whole load, so a typo in one role doesn't break the
    workflow.
    """
    candidates: list[Path] = []
    env_path = get_settings().localcode_fleet_config
    if env_path:
        candidates.append(Path(env_path).expanduser())
    for base in (cwd, str(Path.cwd())):
        if not base:
            continue
        bdir = Path(base) / ".localcode"
        candidates.extend([bdir / "fleet.yaml", bdir / "fleet.yml", bdir / "fleet.json"])

    seen: set[Path] = set()
    for p in candidates:
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            stat = resolved.stat()
        except OSError:
            continue
        if not resolved.is_file():
            continue

        cached = _CFG_CACHE.get(str(resolved))
        if cached is not None and cached[0] == stat.st_mtime:
            return cached[1]

        raw = _parse_config_file(resolved)
        if raw is None:
            continue
        merged = _merge_config(DEFAULT_FLEET_CONFIG, raw)
        merged.config_source = str(resolved)
        with _CFG_CACHE_LOCK:
            if len(_CFG_CACHE) >= _CFG_CACHE_MAX:
                # Drop the oldest insertion — dict preserves order, so a cheap
                # FIFO eviction keeps the cache bounded without a real LRU.
                _CFG_CACHE.pop(next(iter(_CFG_CACHE)), None)
            _CFG_CACHE[str(resolved)] = (stat.st_mtime, merged)
        return merged

    return DEFAULT_FLEET_CONFIG


def _parse_config_file(path: Path) -> dict[str, Any] | None:
    """Parse YAML or JSON based on extension. Returns None on parse error."""
    text = path.read_text()
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            obj = json.loads(text)
        else:
            obj = yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        logger.warning("ignoring fleet config %s: %s", path, exc)
        return None
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        logger.warning("fleet config %s: top-level must be a mapping; got %s", path, type(obj))
        return None
    return obj


def _merge_config(base: FleetConfig, override: dict[str, Any]) -> FleetConfig:
    """Merge an override on top of a base config.

    Semantics: when ``override["roles"]`` is supplied, it REPLACES the workflow
    membership entirely (the workflow is exactly those agents). For each role
    in the override, fields fall back to the corresponding ROLE_LIBRARY entry,
    so writing ``coder: {model: gpt-5.4}`` doesn't require re-specifying the
    system prompt.

    When ``override["roles"]`` is NOT supplied, base's role membership is
    preserved and each role's per-field override (if any) merges over the
    base's RoleConfig.

    Validation is permissive: invalid fields are dropped with a warning and
    inherit defaults rather than failing the whole config load.
    """
    has_role_override = isinstance(override.get("roles"), dict)
    raw_roles = override.get("roles") if has_role_override else {}

    # Normalize the role list: dropping unknown keys, replacement vs merge.
    if has_role_override:
        # The override defines the workflow. Use override keys verbatim.
        role_names = [r for r in VALID_ROLES if r in raw_roles]  # canonical order
        # Warn on unknown roles in the override.
        for k in raw_roles:
            if k not in VALID_ROLES:
                logger.warning(
                    "fleet config: 'roles.%s' unknown (allowed: %s); ignoring",
                    k, list(VALID_ROLES),
                )
    else:
        role_names = base.role_names()

    if not role_names:
        logger.warning("fleet config: no valid roles after merge; falling back to defaults")
        role_names = list(DEFAULT_FLEET_CONFIG.role_names())

    # Build each RoleConfig.
    resolved_roles: dict[str, RoleConfig] = {}
    for name in role_names:
        # Field-level base for this role: prefer the existing base config's
        # entry, then the role library, so users can tweak just one field.
        role_base = base.roles.get(name) or ROLE_LIBRARY.get(name)
        if role_base is None:  # pragma: no cover — name is in VALID_ROLES so library has it
            continue

        per_role = (raw_roles.get(name) or {}) if isinstance(raw_roles, dict) else {}
        if not isinstance(per_role, dict):
            logger.warning(
                "fleet config: 'roles.%s' must be a mapping; using defaults", name
            )
            per_role = {}

        provider = per_role.get("provider", role_base.provider)
        if provider not in VALID_PROVIDERS:
            logger.warning(
                "fleet config: 'roles.%s.provider'=%r invalid (allowed: %s); using default %r",
                name, provider, VALID_PROVIDERS, role_base.provider,
            )
            provider = role_base.provider
        model = str(per_role.get("model") or role_base.model).strip()
        if not model:
            logger.warning("fleet config: 'roles.%s.model' empty; using default", name)
            model = role_base.model
        system_prompt = str(per_role.get("system_prompt") or role_base.system_prompt).strip()
        resolved_roles[name] = RoleConfig(provider=provider, model=model, system_prompt=system_prompt)

    entry_role = _validate_entry_role(
        override.get("entry_role", base.entry_role),
        present=list(resolved_roles.keys()),
    )

    return FleetConfig(
        name=str(override.get("name") or base.name),
        roles=resolved_roles,
        entry_role=entry_role,
        max_steps=max(1, int(override.get("max_steps", base.max_steps))),
        max_review_retries=max(
            0, int(override.get("max_review_retries", base.max_review_retries))
        ),
        require_plan_approval=bool(
            override.get("require_plan_approval", base.require_plan_approval)
        ),
    )


def _validate_entry_role(value: Any, present: list[str]) -> str:
    """Resolve `entry_role`. Must be one of the present roles. Falls back
    to first non-planner role, then to first present role."""
    sv = str(value).strip().lower() if value is not None else ""
    if sv in present:
        return sv
    for r in present:
        if r != "planner":
            return r
    return present[0] if present else "coder"


def config_to_dict(cfg: FleetConfig) -> dict[str, Any]:
    """Serialize a FleetConfig for the /api/fleet/config endpoint."""
    return asdict(cfg)


def role_library_dict() -> dict[str, dict[str, Any]]:
    """Serialize ROLE_LIBRARY for the API. Used by the UI to populate a
    newly-added role card with sensible defaults."""
    return {name: asdict(rc) for name, rc in ROLE_LIBRARY.items()}
