"""Multi-agent fleet orchestrator (package facade).

A fleet is a set of agents (roles) and a fixed pipeline that runs them in
canonical order. Adding an agent means adding it to the ``roles`` dict;
removing one means deleting the key. There is no "disabled" state to keep
in sync.

Roles (canonical order):

  planner   → produces a comprehensive Markdown implementation plan
              (writing-plans-style: file paths, complete code, test commands,
              bite-sized steps). The plan is committed to
              ``<cwd>/.localcode/plans/<timestamp>-<slug>.md``.
  developer → optional design pass (used by "design + code" presets)
  coder     → executes the plan task-by-task using file/bash tools
  tester    → writes tests for the implemented code, runs them, reports
              pass/fail (does NOT modify production code)
  reviewer  → gates the work; replies LGTM or "NACK: <reason>"

Pipeline knobs:
  - ``cfg.max_review_retries``    : on reviewer NACK, re-run coder (+ tester)
                                    + reviewer up to N times
  - ``cfg.require_plan_approval`` : HITL gate after the planner; pauses the
                                    workflow until the user approves/rejects
                                    via the WS back-channel

This module was split from a single ~1k-line file into a cohesive package.
The import surface is unchanged — everything that used to be importable from
``app.orchestrator.fleet`` is re-exported here:

  constants.py  vocabulary, timing budgets, ``StepTimeoutError``
  models.py     ``RoleConfig`` / ``FleetConfig`` / ``Step`` dataclasses
  prompts.py    per-role system prompts
  presets.py    ``WORKFLOW_PRESETS``
  defaults.py   ``ROLE_LIBRARY`` + ``DEFAULT_FLEET_CONFIG``
  loader.py     locate / parse / merge / cache / serialize config
  gate.py       reviewer/tester classifier
  collect.py    sub-provider stream → reviewable text + tool digest
  provider.py   ``FleetProvider``
"""
from __future__ import annotations

from .collect import _collect_text, collect_text
from .constants import (
    DISPATCH_HARD_FAIL_CAP,
    HEARTBEAT_INTERVAL_S,
    STARTUP_GRACE_S,
    STEP_TIMEOUT_S,
    VALID_PROVIDERS,
    VALID_ROLES,
    WORKER_ROLES,
    StepRole,
    StepTimeoutError,
)
from .defaults import DEFAULT_FLEET_CONFIG, ROLE_LIBRARY
from .gate import _classify_gate, _TOOL_DIGEST_MARKER, classify_gate
from .loader import (
    _merge_config,
    _parse_config_file,
    _validate_entry_role,
    config_to_dict,
    load_fleet_config,
    role_library_dict,
)
from .models import FleetConfig, RoleConfig, Step
from .presets import WORKFLOW_PRESETS
from .prompts import (
    CODER_SYSTEM,
    DEVELOPER_SYSTEM,
    PLANNER_SYSTEM,
    REVIEWER_SYSTEM,
    TESTER_SYSTEM,
)
from .provider import FleetProvider

__all__ = [
    # vocabulary / budgets
    "VALID_PROVIDERS",
    "VALID_ROLES",
    "WORKER_ROLES",
    "StepRole",
    "HEARTBEAT_INTERVAL_S",
    "STARTUP_GRACE_S",
    "STEP_TIMEOUT_S",
    "DISPATCH_HARD_FAIL_CAP",
    "StepTimeoutError",
    # data types
    "RoleConfig",
    "FleetConfig",
    "Step",
    # defaults / presets / prompts
    "ROLE_LIBRARY",
    "DEFAULT_FLEET_CONFIG",
    "WORKFLOW_PRESETS",
    "PLANNER_SYSTEM",
    "DEVELOPER_SYSTEM",
    "CODER_SYSTEM",
    "REVIEWER_SYSTEM",
    "TESTER_SYSTEM",
    # config loading / serialization
    "load_fleet_config",
    "config_to_dict",
    "role_library_dict",
    # gate / collect
    "classify_gate",
    "collect_text",
    # provider
    "FleetProvider",
]
