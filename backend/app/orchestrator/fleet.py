"""Multi-agent fleet orchestrator.

A fleet is a set of agents (roles) and a fixed pipeline that runs them in
canonical order. Adding an agent means adding it to the `roles` dict;
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
  - ``cfg.max_review_retries``   : on reviewer NACK, re-run coder (+ tester)
                                    + reviewer up to N times
  - ``cfg.require_plan_approval`` : HITL gate after the planner; pauses the
                                    workflow until the user approves/rejects
                                    via the WS back-channel

Configuration sources (first hit wins):
  1. ``Settings.localcode_fleet_config``               (explicit absolute path)
  2. ``<cwd>/.localcode/fleet.{yaml,yml,json}``        (project-local)
  3. ``<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}``
  4. built-in defaults

Each per-role invocation is surfaced as a tool_use → tool_result pair on the
unified event stream, so the existing chat UI renders the workflow as
expandable cards without front-end changes.

Prompt design borrows directly from the obra/superpowers skill set:
  writing-plans, executing-plans, test-driven-development, requesting-code-review.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from ..config import get_settings
from .base import Event, RunContext

logger = logging.getLogger(__name__)


VALID_PROVIDERS = ("claude", "opencode")
# Canonical execution order:
#   planner  → produces the markdown plan (committed to disk)
#   developer→ optional design step (legacy "design + code" presets)
#   coder    → implements the plan
#   reviewer → gates the implementation against the plan; on NACK, the coder
#              is re-run with the feedback and the reviewer runs again
#              (bounded by ``cfg.max_review_retries``)
#   tester   → final smoke test — writes and runs tests against whatever
#              implementation survived the reviewer gate. Tester results are
#              reported but do NOT trigger further retries; the user acts on
#              the report.
#
# Why tester is last: the reviewer is a code-review pass (catches plan-
# compliance + obvious issues), the tester is a behaviour-verification pass.
# In that order, the tester gives the final word: "the code that the reviewer
# signed off actually works under tests" (or doesn't).
VALID_ROLES = ("planner", "developer", "coder", "reviewer", "tester")
WORKER_ROLES: tuple[str, ...] = ("developer", "coder", "reviewer", "tester")
StepRole = Literal["developer", "coder", "reviewer", "tester"]


# Cadence at which a long-running sub-provider step (e.g. opus thinking for 3
# minutes on a complex plan) emits a chat heartbeat so the UI doesn't look
# frozen. Heartbeats carry ``heartbeat: True`` in their data and are filtered
# out of the persisted message blocks — they're chrome for the live UI only.
HEARTBEAT_INTERVAL_S = 30.0

# Maximum wall-clock time we'll wait on a single sub-provider step before
# treating it as hung and aborting the turn. 10 min is generous (claude-opus
# on a complex markdown plan can legitimately take 3-4 min) but bounded so a
# stuck CLI / network blackhole can't pin a session forever. Surfaces as a
# tool_result with is_error=True followed by a ``StepTimeoutError`` that
# propagates up through ``_safe_run`` so the WS gets a clean ``error`` +
# ``assistant.done`` close-out instead of silently waiting.
STEP_TIMEOUT_S = 600.0


class StepTimeoutError(RuntimeError):
    """Raised by ``_run_step_with_role`` when a sub-provider exceeds the
    per-step budget. Distinct from generic exceptions so the outer pipeline
    can recognise "step abandoned" vs "step errored mid-flight"."""


# ─────────────────────────────────────────────────────────────────────────────
# Workflow presets — name → role membership + entry role.
#
# These are starting points the UI exposes as one-click buttons. When the user
# picks a preset, the modal pre-fills the role cards using ROLE_LIBRARY for
# any role that wasn't already present in their existing config; from there
# they can tune per-role provider/model freely.
# ─────────────────────────────────────────────────────────────────────────────

WORKFLOW_PRESETS: dict[str, dict[str, Any]] = {
    "full": {
        "label": "Full crew",
        "description": "Planner writes a detailed plan; coder implements; reviewer gates plan compliance; tester writes & runs tests as the final smoke check.",
        "roles": ["planner", "coder", "reviewer", "tester"],
        "entry_role": "coder",
    },
    "plan-code-review-test": {
        "label": "Plan + code + review + test",
        "description": "Same as Full crew. Explicit name for clarity on the canonical order.",
        "roles": ["planner", "coder", "reviewer", "tester"],
        "entry_role": "coder",
    },
    "plan-code-test": {
        "label": "Plan + code + test",
        "description": "Planner → coder → tester. Skips review gate; tester is the only check.",
        "roles": ["planner", "coder", "tester"],
        "entry_role": "coder",
    },
    "plan-and-code": {
        "label": "Plan + code",
        "description": "Planner breaks the task down, coder executes. Skips testing + review.",
        "roles": ["planner", "coder"],
        "entry_role": "coder",
    },
    "design-and-code": {
        "label": "Design + code",
        "description": "Planner → developer (extra design pass) → coder. No reviewer.",
        "roles": ["planner", "developer", "coder"],
        "entry_role": "coder",
    },
    "design-only": {
        "label": "Design only",
        "description": "Single developer turn — produces a technical design doc, no code.",
        "roles": ["developer"],
        "entry_role": "developer",
    },
    "code-and-review": {
        "label": "Code + review",
        "description": "Planner → coder → reviewer. No tester.",
        "roles": ["planner", "coder", "reviewer"],
        "entry_role": "coder",
    },
    "code-only": {
        "label": "Code only",
        "description": "Single coder turn — no planning, testing, or review. Fastest.",
        "roles": ["coder"],
        "entry_role": "coder",
    },
    "plan-only": {
        "label": "Plan only",
        "description": "Just the planner — produces the markdown plan, no execution. Useful for review.",
        "roles": ["planner"],
        "entry_role": "planner",
    },
    "review-only": {
        "label": "Review only",
        "description": "Single reviewer turn — paste content into the prompt for an LGTM/NACK pass.",
        "roles": ["reviewer"],
        "entry_role": "reviewer",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Config types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RoleConfig:
    provider: str  # "claude" or "opencode"
    model: str
    system_prompt: str


@dataclass
class FleetConfig:
    """A workflow, defined by which agents it contains.

    Invariants (enforced at construction by ``_merge_config``):
      - ``roles`` is non-empty
      - every key in ``roles`` is in ``VALID_ROLES``
      - ``entry_role`` is one of ``roles``' keys
    """

    name: str
    # Presence = membership. The workflow is exactly these agents.
    roles: dict[str, RoleConfig]
    # Which role runs first if there is no planner (or the planner fails).
    # Always one of the keys in ``roles``.
    entry_role: str
    max_steps: int = 6
    # Auto-retry on reviewer NACK. After a reviewer step that begins with
    # "NACK", the upstream worker step is re-run with the reviewer's feedback
    # appended to its prompt, then the reviewer runs again. Bounded so a
    # consistently-failing step can't loop forever. 0 disables retries.
    max_review_retries: int = 1
    # HITL: when true and the workflow has a planner, the pipeline pauses
    # after the planner emits its plan and waits for the user to approve.
    # Reject ends the turn with the user's feedback recorded as the assistant
    # message. Has no effect on planner-less workflows.
    require_plan_approval: bool = False
    # Where the active config came from — None means built-in defaults. Useful
    # for the UI to surface "Fleet config: ~/.localcode/fleet.yaml".
    config_source: str | None = None

    # Convenience accessors — keep callers from poking `cfg.roles[...]` directly.
    def has(self, role: str) -> bool:
        return role in self.roles

    def role_names(self) -> list[str]:
        """Roles in canonical execution order (planner → dev → coder → reviewer)."""
        return [r for r in VALID_ROLES if r in self.roles]

    def workers(self) -> list[str]:
        """Non-planner roles present, in canonical order."""
        return [r for r in WORKER_ROLES if r in self.roles]

    def get(self, role: str) -> RoleConfig | None:
        return self.roles.get(role)


# ─────────────────────────────────────────────────────────────────────────────
# Built-in defaults — the role library and the default full-crew config.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# System prompts.
#
# Inspired by the obra/superpowers skill set:
#   - writing-plans       → planner produces a comprehensive markdown plan
#   - executing-plans     → coder follows the plan task-by-task, doesn't guess
#   - test-driven-development → tester writes real tests and runs them
#   - requesting-code-review  → reviewer gives a terse LGTM/NACK gate
#
# Economic intent: the planner and reviewer are the "expensive thinking" roles
# (default to bigger Claude models); the coder and tester are the "do the work"
# roles (default to cheaper opencode-routed models). All defaults are
# overridable per-role in fleet.yaml.
# ─────────────────────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are the Planner in a multi-agent coding fleet. You produce ONE artifact:
a comprehensive Markdown implementation plan that the Coder will execute
without further context. Assume the Coder has zero familiarity with this
codebase and questionable taste — write everything they need, exactly.

Your output is plain Markdown. No JSON. No outer code fences around the whole
plan — just the plan itself. The plan MUST follow this structure:

# <Feature> Implementation Plan

**Goal:** <one sentence>

**Architecture:** <2–3 sentences on approach, key trade-offs, tech stack>

## File Structure

List every file to create or modify, one per line, with a one-line
responsibility. Group files that change together.

## Tasks

For each task use this exact shape (checkbox-style steps so the Coder can
track progress):

### Task N: <Component>

**Files:**
- Create: `exact/path/to/file.ext`
- Modify: `exact/path/to/existing.ext`

- [ ] **Step 1: <imperative action>**
```<lang>
<the FULL code — not pseudocode, not "similar to Task N">
```

- [ ] **Step 2: Verify**
Run: `<exact shell command>`
Expected: <expected stdout / exit status>

- [ ] **Step 3: Commit**
```bash
git add <files>
git commit -m "<conventional message>"
```

Repeat for every task. Each step is 2–5 minutes of work.

## Forbidden in plans

- "TBD", "TODO", "implement later", "fill in details"
- "Add appropriate error handling" without showing the code
- "Similar to Task N" — repeat the code; the Coder may read tasks out of order
- Pseudocode where real code is needed
- Undefined types, methods, or imports

## Self-review before emitting

Before you finish, walk back through:
1. Every requirement in the user's request maps to at least one task.
2. No placeholder phrases anywhere.
3. Type names, function signatures, and identifiers are consistent across
   tasks (a function called `clearLayers()` in Task 3 must be `clearLayers()`
   in Task 7 — not `clearFullLayers()`).

Fix issues inline, then emit the plan. The plan is your only output — no
preamble, no commentary outside the document.
"""

_DEVELOPER_SYSTEM = """\
You are the Developer in a multi-agent fleet. You receive the user's request
and (when present) the Planner's plan. Produce a short technical design that
fills any architectural gaps the plan left open: file layout, interfaces and
signatures, data flow, edge cases, and a brief ordered list of changes.

Do NOT write production code — the Coder does that next. End with a one-
paragraph "Approach:" summary the Coder can act on directly.

This role is optional in most workflows; the Planner's plan typically already
contains the design. Use this role when the plan punts on architectural
decisions or when a sub-system needs a separate sketch before implementation.
"""

_CODER_SYSTEM = """\
You are the Coder in a multi-agent fleet. The Planner has produced an
implementation plan (committed to `.localcode/plans/<timestamp>-<slug>.md`
and included verbatim in your context). Your job: EXECUTE the plan
task-by-task using file-edit and bash tools.

# Execute, don't announce

You have file-edit and bash tools. Use them. Do NOT reply with text like
"Starting with the scaffold…" or "I'll implement task 1 first…" without
following through with actual tool calls. Announcement-only responses are
the #1 failure mode of this role and will cause the Reviewer to NACK.

Concretely, for every task in the plan:
1. Call the file-edit / write tool to create or modify each `Files:` entry.
2. Call the bash tool to run the verification command exactly as the plan
   specified, then confirm the expected output.
3. Move to the next task.

Don't paraphrase the plan into prose; perform the steps.

# Discipline

- Follow the plan steps exactly. Don't add features the plan doesn't list.
- Don't refactor adjacent code. YAGNI.
- After each task, run its verification command and confirm the expected
  output. Don't skip verifications.
- If a step is unclear or its verification fails repeatedly, stop and report
  the blocker plainly with which step you stopped at. Do not guess.
- If the plan refers to types/functions/files defined in earlier tasks, use
  the names exactly as the plan defined them. Don't rename mid-flight.

# Output

End with a 'Changes:' summary listing:
- Files touched (paths)
- Commands run (exact, with exit codes)
- Which tasks from the plan are complete, in-progress, or skipped

If you wrote no files and ran no commands, your `Changes:` section is
empty — and that's a self-NACK. The Reviewer will see the empty diff on
disk and route the work back to you.
"""

_REVIEWER_SYSTEM = """\
You are the Reviewer in a multi-agent fleet. You run BEFORE the Tester —
your gate is plan compliance and code quality, not test results (the Tester
hasn't run yet). You receive the implementation plan and the Coder's report.

You may use file-read / bash tools to inspect what the Coder actually
shipped (e.g. `ls`, `git status`, `cat <file>`) — don't take the Coder's
narrative at face value when it's easy to verify.

Verify:

1. Every task in the plan is complete on disk (files exist, code matches).
   If the Coder's narrative says "done" but the files aren't there, that's
   an automatic NACK.
2. No placeholders ("TODO", "TBD", "implement later", "fill in details")
   survived in the implementation.
3. Type and function names match what the plan defined — no drift.
4. The Coder didn't add scope the plan didn't ask for.

# Output protocol — STRICT

You MUST end your reply with EXACTLY ONE classifier line, alone on the
final line, no trailing whitespace, no surrounding markdown. Above the
classifier you may write up to ~10 lines of reasoning / findings — that's
fine, the orchestrator parses only the LAST non-empty line.

Allowed classifiers (pick ONE):

  LGTM
  NACK: <one-sentence specific reason naming the failing task or file>

Examples of valid output:

  ----
  Walked the plan task-by-task. Tasks 1-5 present and match. Task 6 (cli.py)
  is missing — `ls src/scraper/` shows no cli.py.

  NACK: task 6 is unimplemented (src/scraper/cli.py missing)
  ----

  ----
  All 8 tasks present. Names align with the plan. No placeholders.

  LGTM
  ----

If you produce an unclassified ending, the orchestrator treats it as NACK
to be safe — so always include the classifier line. Don't restate the plan
or echo the code; the Coder already has both.
"""

_TESTER_SYSTEM = """\
You are the Tester in a multi-agent fleet. You run LAST — the Coder has
implemented the plan and the Reviewer has signed off on plan compliance.
Your job: write executable tests that exercise the new behavior and run
them. You are the final gate; your verdict decides whether the workflow
ships or loops back.

Process per behavior the plan specifies:
1. Write a test in the project's existing test directory (typically
   `tests/` or `__tests__/`). Use real inputs/outputs — avoid mocks unless
   the dependency is genuinely unavoidable (network, time, randomness).
2. Run it with the project's test runner.
3. Record pass/fail and the assertion that fired on failure.

You do NOT modify production code — that is the Coder's job. You MAY
modify your own test files on retry if the test itself was wrong.

Above your classifier line, write the full 'Tests:' summary:
- Each test file you created (with path)
- Each test case inside (one line each)
- pass/fail for each
- For each failure: plan-task id + the actual assertion that fired

# Output protocol — STRICT

You MUST end your reply with EXACTLY ONE classifier line, alone on the
final line, no trailing whitespace, no surrounding markdown. The
orchestrator parses only the LAST non-empty line; reasoning above is fine.

Allowed classifiers (pick ONE):

  LGTM                        — all tests passed; workflow ships
  NACK_CODE: <one-sentence>   — at least one test failed; the IMPLEMENTATION
                                is at fault → coder retries with your feedback
  NACK_TESTS: <one-sentence>  — at least one test failed; the TEST itself is
                                wrong → only you retry (you may edit tests)

Examples of valid output:

  ----
  Tests:
  - tests/test_scraper.py::test_fetch_for_date — PASS
  - tests/test_scraper.py::test_handles_404 — PASS

  LGTM
  ----

  ----
  Tests:
  - tests/test_scraper.py::test_fetch_for_date — FAIL (AssertionError: expected 12 articles, got 0)

  NACK_CODE: scraper.fetch returns empty list because date filter is wrong
  ----

  ----
  Tests:
  - tests/test_scraper.py::test_fetch_for_date — FAIL (TypeError: unhashable list)

  NACK_TESTS: my fixture passed a list where a tuple was needed
  ----

If you produce an unclassified ending, the orchestrator treats it as
NACK_CODE (the more common failure mode) — so always include the classifier
line. Picking the wrong classifier wastes retries: NACK_TESTS when the impl
is broken hides bugs; NACK_CODE when the test is broken churns the Coder.
"""


# Built-in role definitions. The UI uses this to pre-fill a role card when
# the user adds a previously-absent role to their workflow.
#
# Default model picks reflect the economic intent of the fleet:
#   - planner  → biggest Claude (deep decomposition is worth the cost)
#   - developer→ mid-tier Claude (used only when the plan needs more design)
#   - coder    → cheap opencode-routed codex (mechanical execution)
#   - tester   → cheap Claude haiku (writes + runs tests; doesn't reason much)
#   - reviewer → mid-tier Claude (LGTM/NACK gate needs real judgement)
ROLE_LIBRARY: dict[str, RoleConfig] = {
    "planner":   RoleConfig(provider="claude",   model="claude-opus-4-7",       system_prompt=_PLANNER_SYSTEM),
    "developer": RoleConfig(provider="claude",   model="claude-sonnet-4-6",     system_prompt=_DEVELOPER_SYSTEM),
    "coder":     RoleConfig(provider="opencode", model="openai/gpt-5.3-codex",  system_prompt=_CODER_SYSTEM),
    "tester":    RoleConfig(provider="claude",   model="claude-haiku-4-5",      system_prompt=_TESTER_SYSTEM),
    "reviewer":  RoleConfig(provider="claude",   model="claude-sonnet-4-6",     system_prompt=_REVIEWER_SYSTEM),
}


# Default workflow: planner produces the plan, coder implements, reviewer
# gates plan compliance, tester writes & runs tests as the final smoke check.
# Developer is omitted from the default — most tasks don't need a separate
# design step now that the plan IS the design.
DEFAULT_FLEET_CONFIG = FleetConfig(
    name="default",
    roles={r: ROLE_LIBRARY[r] for r in ("planner", "coder", "reviewer", "tester")},
    entry_role="coder",
)


# ─────────────────────────────────────────────────────────────────────────────
# Loading + parsing
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Step record + plan persistence
#
# The planner now produces a Markdown document — we don't parse it into
# discrete steps. The plan is the artifact; the Coder reads it whole and
# iterates through its tasks with file/bash tools. Step is just a per-role
# invocation record so we can label tool_use events and track execution order.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Step:
    id: str
    role: str  # any role in VALID_ROLES — typed loose so planner-only single-shot fits too
    prompt: str
# ─────────────────────────────────────────────────────────────────────────────
# Provider
# ─────────────────────────────────────────────────────────────────────────────


class FleetProvider:
    """Composes Claude + OpenCode into a planner/coder/reviewer workflow.

    Stateless across turns — every per-turn datum is a local in `run()` so
    concurrent turns don't clobber each other's state.
    """

    name = "fleet"

    async def open_session(self, ctx: RunContext) -> str:
        return ctx.upstream_session_id or ""

    async def aclose(self) -> None:
        return None

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        # ALL per-turn state must live as locals here, not as instance attrs —
        # FleetProvider is a singleton and concurrent turns share `self`.
        t0 = time.time()
        cfg = load_fleet_config(ctx.cwd)
        # Per-session UI override layered on top of the file config.
        ui_override = ctx.extras.get("fleet_config_override") if ctx.extras else None
        if isinstance(ui_override, dict) and ui_override:
            base_src = cfg.config_source or "<built-in defaults>"
            cfg = _merge_config(cfg, ui_override)
            cfg.config_source = f"{base_src} + UI override"

        if not cfg.role_names():
            yield Event(
                type="error",
                data={"message": "fleet has no agents configured"},
            )
        else:
            # Single path now: the LLM-driven orchestrator. It handles
            # planner / coder / reviewer / tester registries equally well as
            # single-role registries (it just dispatches the one available
            # agent), so we don't need separate single-agent or linear-
            # pipeline branches.
            async for ev in self._run_orchestrated(ctx, cfg):
                yield ev

        yield Event(
            type="assistant.done",
            data={"duration_ms": int((time.time() - t0) * 1000)},
        )

    async def _run_orchestrated(
        self, ctx: RunContext, cfg: FleetConfig
    ) -> AsyncIterator[Event]:
        """LLM-driven dispatch path (Tier-4).

        Builds a per-turn registry from ``cfg.roles``, instantiates an
        ``OrchestratorAgent`` with a fresh MCP dispatch server, and yields
        the merged event stream (orchestrator narrative + sub-agent cards).
        """
        # Lazy imports — these modules import RoleConfig/Step from this
        # module, so we keep the dependency one-way at import time.
        from .agent_def import registry_from_role_library
        from .orchestrator import OrchestratorAgent

        registry = registry_from_role_library(cfg.roles)
        orchestrator = OrchestratorAgent(
            registry=registry,
            run_step_fn=self._run_step_with_role,
            require_plan_approval=cfg.require_plan_approval,
        )
        async for ev in orchestrator.run(ctx):
            yield ev

    async def _run_step_with_role(
        self,
        step: Step,
        role_cfg: RoleConfig,
        ctx: RunContext,
        outputs: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Invoke ``role_cfg`` with ``step.prompt`` exactly. The caller is
        responsible for stitching plan + prior-step context into ``prompt`` —
        we just delegate to the sub-provider and emit tool_use/tool_result.

        Two safeguards keep this robust:

        - **Heartbeats** every ``HEARTBEAT_INTERVAL_S`` so the UI doesn't
          look frozen during a multi-minute opus turn. Marked
          ``heartbeat: True`` so the WS handler keeps them out of persisted
          history.
        - **Per-step timeout** ``STEP_TIMEOUT_S``. On exceeding the budget
          we yield a ``tool.result`` with ``is_error=True`` and raise
          ``StepTimeoutError``, which propagates up through ``_safe_run``
          and surfaces as a clean ``error`` + ``assistant.done`` to the
          frontend. Without this a hung sub-provider would pin the WS
          forever.
        """
        display = step.prompt if len(step.prompt) <= 600 else step.prompt[:600] + "…"
        yield Event(
            type="assistant.tool_use",
            data={
                "id": step.id,
                "name": f"{step.role} [{role_cfg.provider}:{role_cfg.model}]",
                "input": {"prompt": display},
            },
        )

        # Run _collect_text concurrently with a heartbeat ticker. shield()
        # protects the inner task from wait_for's cancel-on-timeout — we want
        # the timeout to fire the heartbeat, not abort the inner work.
        collect = asyncio.create_task(
            _collect_text(role_cfg, step.prompt, ctx.cwd, ctx.additional_dirs)
        )
        elapsed_s = 0
        output: str | None = None
        error_text: str | None = None
        timed_out = False
        try:
            while True:
                try:
                    output = await asyncio.wait_for(
                        asyncio.shield(collect), timeout=HEARTBEAT_INTERVAL_S
                    )
                    break
                except TimeoutError:
                    elapsed_s += int(HEARTBEAT_INTERVAL_S)
                    if elapsed_s >= STEP_TIMEOUT_S:
                        timed_out = True
                        error_text = (
                            f"{step.role} step exceeded {int(STEP_TIMEOUT_S)}s budget "
                            f"with no response — aborting turn. Check that the "
                            f"{role_cfg.provider} backend is healthy."
                        )
                        break
                    yield Event(
                        type="assistant.text",
                        data={
                            "text": f"_…{step.role} still working ({elapsed_s}s)…_\n",
                            "heartbeat": True,
                        },
                    )
        except Exception as exc:
            # Sub-provider raised (or our own cancellation cascaded). Capture
            # the message for the tool_result; the finally block will cancel
            # the inner task.
            error_text = str(exc) or repr(exc)
        finally:
            # Belt-and-braces: cancel the inner task on every exit path so
            # it doesn't leak past this scope (the outer generator may be
            # aclose()'d on WS disconnect, in which case the try blocks above
            # don't catch it).
            if not collect.done():
                collect.cancel()
                try:
                    await collect
                except (asyncio.CancelledError, Exception):
                    pass

        if error_text is not None:
            yield Event(
                type="tool.result",
                data={"tool_use_id": step.id, "content": error_text, "is_error": True},
            )
            if timed_out:
                # Bubble up so the outer pipeline aborts cleanly rather than
                # racing on with no output for this step. _safe_run will
                # surface this as an `error` event + `assistant.done`.
                raise StepTimeoutError(error_text)
            return

        # Successful step — record output and emit the result card.
        assert output is not None  # if no error_text, we broke out with output set
        outputs[step.id] = output
        # Mark gate failures as errored tool results so the UI shows them red.
        # We use the canonical _classify_gate (last-line parse, fail-safe to
        # NACK) so a reviewer that buries its verdict under prose still gets
        # routed correctly.
        is_error = step.role in ("reviewer", "tester") and _classify_gate(
            output, step.role
        ) != "lgtm"
        yield Event(
            type="tool.result",
            data={"tool_use_id": step.id, "content": output, "is_error": is_error},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


async def _collect_text(
    role: RoleConfig,
    prompt: str,
    cwd: str | None,
    additional_dirs: list[str] | None = None,
) -> str:
    """Invoke a sub-provider and return its concatenated assistant text.

    If the sub-provider produced only tool calls (no narrative text), fall back
    to a structured digest of those tool calls so downstream steps — and
    especially the reviewer — have something to act on. Without this, a coder
    that edits files but doesn't summarise produces "" and the reviewer
    correctly NACKs with "no output to review".
    """
    # Lazy import — registry imports this module's class.
    from .registry import get_provider

    sub = await get_provider(role.provider)  # type: ignore[arg-type]
    sub_ctx = RunContext(
        model=role.model,
        prompt=prompt,
        cwd=cwd,
        additional_dirs=list(additional_dirs or []),
        system_prompt=role.system_prompt,
    )
    chunks: list[str] = []
    tool_calls: list[tuple[str, str, Any]] = []  # (id, name, input)
    tool_results: dict[str, tuple[str, bool]] = {}  # id -> (content, is_error)
    async for ev in sub.run(sub_ctx):
        if ev.type == "assistant.text":
            chunks.append(ev.data.get("text", ""))
        elif ev.type == "assistant.tool_use":
            tool_calls.append(
                (ev.data.get("id", ""), ev.data.get("name", ""), ev.data.get("input"))
            )
        elif ev.type == "tool.result":
            content = ev.data.get("content")
            if isinstance(content, list):
                # Anthropic tool_result blocks come as a list of {type:text, text:...}
                content = "\n".join(
                    str(b.get("text", b)) if isinstance(b, dict) else str(b)
                    for b in content
                )
            tool_results[ev.data.get("tool_use_id", "")] = (
                str(content or ""),
                bool(ev.data.get("is_error")),
            )
        elif ev.type == "error":
            raise RuntimeError(ev.data.get("message") or "sub-provider error")

    text = "".join(chunks).strip()

    # Build a tool-activity digest. We always include it (when tools fired)
    # so downstream gates can verify what the worker actually did rather
    # than trusting its narrative summary. Previously, ANY non-empty text
    # caused tool activity to be dropped — which let the coder say "Starting
    # with the scaffold…" while having made zero edits, and the Reviewer had
    # no way to detect the gap from the prompt alone.
    digest_lines: list[str] = []
    if tool_calls:
        digest_lines.append(f"(tool activity from {role.provider}:{role.model})")
        for tid, name, tinput in tool_calls:
            inp_str = json.dumps(tinput, default=str) if tinput is not None else "{}"
            if len(inp_str) > 400:
                inp_str = inp_str[:400] + "…"
            digest_lines.append(f"- {name} input={inp_str}")
            if tid in tool_results:
                content, is_error = tool_results[tid]
                tag = "ERR" if is_error else "OK"
                snippet = content.replace("\n", " ")[:300]
                digest_lines.append(f"    [{tag}] {snippet}")

    if text and digest_lines:
        return text + "\n\n---\n" + "\n".join(digest_lines)
    if text:
        return text
    if digest_lines:
        return "\n".join(digest_lines)
    return ""


def _classify_gate(output: str, role: str) -> str:
    """Parse the LAST non-empty line of a gate's output for its classifier.

    Returns one of:
      - ``"lgtm"``        — explicit pass
      - ``"nack"``        — reviewer NACK (or unclassified reviewer output —
                            fail-safe so we retry rather than silently shipping
                            work the gate didn't bless)
      - ``"nack_code"``   — tester says implementation is buggy
      - ``"nack_tests"``  — tester says tests themselves are buggy

    Both system prompts instruct the model to put the classifier on the LAST
    line. Earlier we used ``output.startswith("NACK")`` which only ever
    examined the FIRST line — so a reviewer that prefaced its verdict with
    descriptive prose ("The project directory is empty…") looked like an
    LGTM and the workflow advanced past clear failures. This function is the
    canonical classifier; ``_run_step_with_role`` and the retry loop both
    consume it.
    """
    lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
    if not lines:
        # Empty output → fail-safe NACK. Retry rather than silently advance.
        return "nack" if role != "tester" else "nack_code"
    last = lines[-1].upper()

    if role == "tester":
        if last.startswith("LGTM") or last.startswith("TESTS_OK"):
            return "lgtm"
        if last.startswith("NACK_TESTS"):
            return "nack_tests"
        if last.startswith("NACK_CODE") or last.startswith("NACK"):
            return "nack_code"
        # Unclassified output → assume implementation bug (the more common
        # cause of test failure). Better to retry the coder than to ship.
        return "nack_code"

    # Reviewer (or any other gate that uses the LGTM / NACK protocol).
    if last.startswith("LGTM"):
        return "lgtm"
    if last.startswith("NACK"):
        return "nack"
    return "nack"  # unclassified → fail-safe NACK
