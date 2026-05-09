"""Multi-agent fleet orchestrator (Proposal G).

A fleet is an ordered list of agents — the workflow IS its agents. Adding an
agent means adding it to the `roles` dict; removing one means deleting the
key. There is no "disabled" state to keep in sync, which means no class of
bug where a stale `disabled` flag could let an agent leak into execution.

Available agent roles:

  planner   → emits an ordered list of {developer | coder | reviewer} steps
  developer → produces a design / approach for a step (no code)
  coder     → implements a step (may use file/bash tools via its sub-provider)
  reviewer  → verifies the previous step's output, returns LGTM or NACK

Workflows compose these. A few presets the UI exposes one-click:

  Full crew      planner + developer + coder + reviewer
  Plan + code    planner + coder
  Design + code  planner + developer + coder
  Code + review  planner + coder + reviewer
  Code only      coder           (no planning — direct single-shot)
  Plan only      planner         (emits the JSON plan, no execution)
  Review only    reviewer        (single LGTM/NACK pass on the prompt)

The fleet itself implements the ``Provider`` protocol so the FastAPI WebSocket
treats it as just another backend. Each sub-step is surfaced as a tool_use →
tool_result pair on the unified event stream, so the existing UI renders the
workflow as expandable cards without any front-end changes.

Configuration sources (first hit wins):
  1. ``Settings.localcode_fleet_config``               (explicit absolute path)
  2. ``<cwd>/.localcode/fleet.{yaml,yml,json}``        (project-local)
  3. ``<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}``
  4. built-in defaults (full crew)

Per-session UI overrides apply on top of the resolved file config — see
``FleetProvider.run`` and the modal in ``frontend/src/components/FleetConfigEditor.tsx``.

Why so few abstractions: this is still a v1. Linear plan execution (no parallel
branches), single-shot fallback if the planner fails. Add DAG support, retries,
and richer streaming once we know what we actually need.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from ..config import get_settings
from .base import Event, RunContext


logger = logging.getLogger(__name__)


VALID_PROVIDERS = ("claude", "opencode")
VALID_ROLES = ("planner", "developer", "coder", "reviewer")
WORKER_ROLES: tuple[str, ...] = ("developer", "coder", "reviewer")
StepRole = Literal["developer", "coder", "reviewer"]

# How long the plan-approval gate blocks before treating silence as a timeout
# (and aborting the turn). 5 min strikes a balance — long enough for the user
# to read the plan, short enough that a tab left open overnight doesn't pin a
# WS forever.
APPROVAL_TIMEOUT_S = 300.0


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
        "description": "Planner decomposes; developer designs; coder implements; reviewer gates.",
        "roles": ["planner", "developer", "coder", "reviewer"],
        "entry_role": "coder",
    },
    "plan-and-code": {
        "label": "Plan + code",
        "description": "Planner breaks the task down, coder executes each step. Skips design + review.",
        "roles": ["planner", "coder"],
        "entry_role": "coder",
    },
    "design-and-code": {
        "label": "Design + code",
        "description": "Planner → developer (design) → coder (implement). No reviewer.",
        "roles": ["planner", "developer", "coder"],
        "entry_role": "coder",
    },
    "design-only": {
        "label": "Design only",
        "description": "Single developer turn — produces a technical design doc, no code.",
        "roles": ["developer"],
        "entry_role": "developer",
    },
    "design-and-review": {
        "label": "Design + review",
        "description": "Planner → developer (design) → reviewer (sanity-check the design).",
        "roles": ["planner", "developer", "reviewer"],
        "entry_role": "developer",
    },
    "code-and-review": {
        "label": "Code + review",
        "description": "Planner → coder, then reviewer gates each output.",
        "roles": ["planner", "coder", "reviewer"],
        "entry_role": "coder",
    },
    "code-only": {
        "label": "Code only",
        "description": "Single coder turn — no planning, no design, no review. Fastest.",
        "roles": ["coder"],
        "entry_role": "coder",
    },
    "plan-only": {
        "label": "Plan only",
        "description": "Just the planner — produces a JSON plan, no execution. Useful for review.",
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

_PLANNER_SYSTEM = (
    "You are the Planner in a multi-agent coding fleet. Decompose the user's "
    "request into 1–6 concrete steps. Each step must be a self-contained "
    "instruction another LLM can execute without further context.\n\n"
    "Output STRICT JSON only — no prose, no markdown fences:\n"
    '  {"steps":[{"id":"s1","role":"coder","prompt":"...","depends_on":[]}, ...]}\n\n'
    "Roles allowed:\n"
    "  - \"developer\" → designs the approach (interfaces, files, edge cases). "
    "    NO code is written. Use this when a step has architectural ambiguity, "
    "    multi-file impact, or unclear data flow.\n"
    "  - \"coder\"     → implements the change. Use directly when the step is "
    "    mechanical or already well-specified.\n"
    "  - \"reviewer\"  → gates the prior step's output (LGTM / NACK). Use "
    "    sparingly — only when correctness genuinely needs an extra check.\n\n"
    "Keep step prompts short and imperative. If the request is trivial, "
    "return a single coder step. Never include any text outside the JSON object."
)

_DEVELOPER_SYSTEM = (
    "You are the Developer in a multi-agent fleet. Produce a short technical "
    "design for the step described — files involved, interfaces and signatures, "
    "data flow, edge cases, and a brief ordered list of changes. DO NOT write "
    "code (the Coder does that next). End with a one-paragraph 'Approach:' "
    "summary the Coder can act on directly."
)

_CODER_SYSTEM = (
    "You are the Coder in a multi-agent fleet. Execute exactly the step "
    "described — nothing more. If the step's context includes a Developer's "
    "Approach, follow it. Use file-edit / bash tools as needed. Be minimal: do "
    "not add features beyond what was asked. End with a short 'Changes:' "
    "summary listing files touched and commands run."
)

_REVIEWER_SYSTEM = (
    "You are the Reviewer. The user gives you a step description and the "
    "previous step's report. Reply with one line: 'LGTM' if the work satisfies "
    "the step, or 'NACK: <one-sentence reason>' otherwise. Be terse."
)


# Built-in role definitions. The UI uses this to pre-fill a role card when
# the user adds a previously-absent role to their workflow.
ROLE_LIBRARY: dict[str, RoleConfig] = {
    "planner":   RoleConfig(provider="claude",   model="claude-sonnet-4-6",     system_prompt=_PLANNER_SYSTEM),
    "developer": RoleConfig(provider="claude",   model="claude-opus-4-7",       system_prompt=_DEVELOPER_SYSTEM),
    "coder":     RoleConfig(provider="opencode", model="openai/gpt-5.3-codex",  system_prompt=_CODER_SYSTEM),
    "reviewer":  RoleConfig(provider="claude",   model="claude-haiku-4-5",      system_prompt=_REVIEWER_SYSTEM),
}


# Default workflow when no file is found and no override is supplied: full crew.
DEFAULT_FLEET_CONFIG = FleetConfig(
    name="default",
    roles={r: ROLE_LIBRARY[r] for r in ("planner", "developer", "coder", "reviewer")},
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
# Plan parsing
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Step:
    id: str
    role: StepRole  # "developer" | "coder" | "reviewer"
    prompt: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    steps: list[Step]


def parse_plan(
    planner_output: str,
    max_steps: int,
    allowed_roles: set[str] | None = None,
) -> Plan:
    """Pull the first JSON object out of a planner reply and validate it.

    `allowed_roles` constrains which step roles are accepted — when the
    workflow doesn't include e.g. the developer, any developer-step the
    planner emits is dropped (with a warning) rather than failing the
    whole plan. Defaults to all worker roles when not supplied.
    """
    if allowed_roles is None:
        allowed_roles = set(WORKER_ROLES)

    obj = _extract_json_object(planner_output)
    if obj is None:
        raise ValueError("planner did not return JSON")
    raw_steps = obj.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("plan has no steps")

    steps: list[Step] = []
    for i, s in enumerate(raw_steps[:max_steps]):
        if not isinstance(s, dict):
            raise ValueError(f"step {i} is not an object")
        sid = str(s.get("id") or f"s{i+1}")
        role = s.get("role")
        if role not in WORKER_ROLES:
            raise ValueError(f"step {sid} has invalid role: {role!r}")
        if role not in allowed_roles:
            logger.warning(
                "plan step %s uses role %r which is not in this workflow; skipping",
                sid, role,
            )
            continue
        prompt = str(s.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"step {sid} has empty prompt")
        depends = s.get("depends_on") or []
        if not isinstance(depends, list):
            depends = []
        steps.append(
            Step(id=sid, role=role, prompt=prompt, depends_on=[str(d) for d in depends])
        )
    if not steps:
        raise ValueError("plan has no steps with workflow-available roles")
    return Plan(steps=steps)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Tolerant JSON extractor — strips ```fences``` and finds the first
    balanced ``{...}`` block, returning the parsed dict or None."""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


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

        # Branch: planner-led pipeline (planner present + at least one worker)
        # vs single-agent turn (no planner, or planner is the only role).
        if cfg.has("planner") and cfg.workers():
            async for ev in self._run_planned(ctx, cfg):
                yield ev
        else:
            async for ev in self._run_single(ctx, cfg):
                yield ev

        yield Event(
            type="assistant.done",
            data={"duration_ms": int((time.time() - t0) * 1000)},
        )

    async def _run_planned(
        self, ctx: RunContext, cfg: FleetConfig
    ) -> AsyncIterator[Event]:
        """Planner-led path: planner emits a JSON plan, steps execute
        sequentially with results threaded as context.

        Two HITL/safety features layer onto the basic loop:

        - **Plan approval gate** (``cfg.require_plan_approval``): after the
          planner emits a valid plan, the workflow blocks on an approval
          message from the WebSocket. Reject / timeout aborts the turn before
          any worker step runs.
        - **Auto-retry on reviewer NACK** (``cfg.max_review_retries``): when a
          reviewer step starts with "NACK", the upstream worker step is
          re-queued with the reviewer's feedback prepended to its prompt, then
          the reviewer runs again. Bounded so a stubbornly-failing step can't
          loop forever.
        """
        planner_base = cfg.get("planner")
        assert planner_base is not None  # caller checked cfg.has("planner")
        workers = cfg.workers()
        plan_id = "fleet.plan"

        # Tell the planner which worker roles are available in THIS workflow.
        # We append a constraint line rather than rewriting the whole prompt
        # so any user-customised planner system_prompt is preserved.
        planner_role = RoleConfig(
            provider=planner_base.provider,
            model=planner_base.model,
            system_prompt=_planner_prompt_with_constraint(
                planner_base.system_prompt, workers
            ),
        )

        yield Event(
            type="assistant.tool_use",
            data={
                "id": plan_id,
                "name": f"planner [{planner_role.provider}:{planner_role.model}]",
                "input": {"task": ctx.prompt, "available_roles": cfg.role_names()},
            },
        )
        plan_parsed_ok = False
        try:
            plan_text = await _collect_text(
                planner_role, ctx.prompt, ctx.cwd, ctx.additional_dirs
            )
            plan = parse_plan(plan_text, cfg.max_steps, allowed_roles=set(workers))
        except Exception as exc:
            logger.warning("fleet planning failed, single-shot fallback: %s", exc)
            yield Event(
                type="tool.result",
                data={
                    "tool_use_id": plan_id,
                    "content": (
                        f"planning failed ({exc}); falling back to a single "
                        f"{cfg.entry_role} step"
                    ),
                    "is_error": True,
                },
            )
            fallback = cfg.entry_role if cfg.entry_role in workers else workers[0]
            plan = Plan(
                steps=[Step(id="s1", role=fallback, prompt=ctx.prompt, depends_on=[])]  # type: ignore[arg-type]
            )
        else:
            plan_parsed_ok = True
            yield Event(
                type="tool.result",
                data={
                    "tool_use_id": plan_id,
                    "content": _summarize_plan(plan),
                    "is_error": False,
                },
            )

        # ── HITL: plan approval gate ─────────────────────────────────────────
        # Only gate when the user opted in AND the planner produced a real
        # plan — gating a single-step fallback (which IS the user's prompt) is
        # noise.
        if cfg.require_plan_approval and plan_parsed_ok:
            approval_id = "approval.plan"
            yield Event(
                type="pipeline.awaiting_approval",
                data={
                    "id": approval_id,
                    "kind": "plan",
                    "plan": _summarize_plan(plan),
                    "message": (
                        "Approve this plan to run the worker steps, or reject "
                        "with feedback to abort the turn."
                    ),
                    "timeout_s": APPROVAL_TIMEOUT_S,
                },
            )
            decision = await _await_approval(
                ctx.approval_channel, approval_id, APPROVAL_TIMEOUT_S
            )
            yield Event(type="pipeline.approval_received", data=decision)
            if decision["value"] != "yes":
                # Surface why the turn ended so chat history is self-explanatory.
                if decision["value"] == "timeout":
                    note = "Plan approval timed out — workflow aborted."
                else:
                    note = "Plan rejected by user."
                fb = decision.get("feedback") or ""
                if fb:
                    note += f"\n\nFeedback: {fb}"
                yield Event(type="assistant.text", data={"text": note})
                return

        # ── Step execution with NACK retries ────────────────────────────────
        outputs: dict[str, str] = {}
        # `pending` is a mutable queue so retries can be inserted in front of
        # the remaining plan — a NACK on step N must be fixed before step N+1
        # runs, otherwise downstream context gets stale.
        pending: list[Step] = list(plan.steps)
        # Steps that actually ran, in execution order. Used by the final
        # summary so retry outputs (which aren't in plan.steps) get picked.
        executed: list[Step] = []
        nack_attempts: dict[str, int] = {}  # base reviewer step id -> attempts so far

        while pending:
            step = pending.pop(0)
            async for ev in self._run_step(step, ctx, cfg, outputs):
                yield ev
            executed.append(step)

            if step.role != "reviewer" or cfg.max_review_retries <= 0:
                continue

            output = outputs.get(step.id, "")
            if not output.strip().upper().startswith("NACK"):
                continue

            # Retry counter is keyed by the ORIGINAL reviewer id so that a
            # second NACK on `s2.retry1` still increments the same counter.
            base_id = step.id.split(".retry", 1)[0]
            attempts = nack_attempts.get(base_id, 0)
            if attempts >= cfg.max_review_retries:
                logger.info(
                    "fleet reviewer %s NACKed %d time(s); not retrying further",
                    base_id, attempts,
                )
                continue

            upstream = _find_upstream_worker(plan.steps, base_id)
            if upstream is None:
                logger.info(
                    "fleet reviewer %s NACKed but no upstream worker to retry",
                    base_id,
                )
                continue

            attempt = attempts + 1
            nack_attempts[base_id] = attempt
            retry_step = Step(
                id=f"{upstream.id}.retry{attempt}",
                role=upstream.role,
                prompt=(
                    f"{upstream.prompt}\n\n"
                    f"## Reviewer feedback (attempt {attempt}; must address)\n"
                    f"{output}"
                ),
                depends_on=list(upstream.depends_on),
            )
            rerev_step = Step(
                id=f"{base_id}.retry{attempt}",
                role="reviewer",
                prompt=_lookup_step_prompt(plan.steps, base_id),
                depends_on=[retry_step.id],
            )
            # Run the retry pair before any remaining plan steps.
            pending.insert(0, rerev_step)
            pending.insert(0, retry_step)

        final = _final_summary(outputs, Plan(steps=executed))
        if final:
            yield Event(type="assistant.text", data={"text": final})

    async def _run_single(
        self, ctx: RunContext, cfg: FleetConfig
    ) -> AsyncIterator[Event]:
        """Single-agent path: the user prompt goes straight to one role
        (the entry role). Used by Code-only / Review-only / Plan-only
        workflows where decomposition would just add latency."""
        # _validate_entry_role guarantees entry_role is a key in cfg.roles,
        # but defend in case caller invariants ever change.
        present = cfg.role_names()
        role_name = cfg.entry_role if cfg.entry_role in present else (present[0] if present else "coder")
        role_cfg = cfg.get(role_name) or ROLE_LIBRARY[role_name]

        # Step.role expects a worker literal; for plan-only we still use the
        # planner via _run_step, but Step.role is typed as the worker union.
        # We bypass the strict typing — Step is internal to this module.
        step = Step(id="s1", role=role_name, prompt=ctx.prompt, depends_on=[])  # type: ignore[arg-type]
        outputs: dict[str, str] = {}
        async for ev in self._run_step_with_role(step, role_cfg, ctx, outputs):
            yield ev

        text = outputs.get(step.id, "")
        if text:
            yield Event(type="assistant.text", data={"text": text})

    async def _run_step(
        self,
        step: Step,
        ctx: RunContext,
        cfg: FleetConfig,
        outputs: dict[str, str],
    ) -> AsyncIterator[Event]:
        role_cfg = cfg.get(step.role)
        if role_cfg is None:
            # Defensive: parse_plan should have filtered this. Surface as error.
            yield Event(
                type="tool.result",
                data={
                    "tool_use_id": step.id,
                    "content": f"role {step.role!r} not in this workflow",
                    "is_error": True,
                },
            )
            return
        async for ev in self._run_step_with_role(step, role_cfg, ctx, outputs):
            yield ev

    async def _run_step_with_role(
        self,
        step: Step,
        role_cfg: RoleConfig,
        ctx: RunContext,
        outputs: dict[str, str],
    ) -> AsyncIterator[Event]:
        # Stitch the prompts of upstream dependencies in (or, if none declared,
        # the immediately previous step — the most useful default).
        deps = step.depends_on or ([list(outputs.keys())[-1]] if outputs else [])
        context_blocks = [
            f"## Output of {dep_id}\n{outputs[dep_id]}"
            for dep_id in deps
            if dep_id in outputs
        ]
        full_prompt = (
            "\n\n".join(context_blocks + [step.prompt]) if context_blocks else step.prompt
        )

        yield Event(
            type="assistant.tool_use",
            data={
                "id": step.id,
                "name": f"{step.role} [{role_cfg.provider}:{role_cfg.model}]",
                "input": {"prompt": step.prompt, "depends_on": deps},
            },
        )

        try:
            output = await _collect_text(
                role_cfg, full_prompt, ctx.cwd, ctx.additional_dirs
            )
        except Exception as exc:
            yield Event(
                type="tool.result",
                data={"tool_use_id": step.id, "content": str(exc), "is_error": True},
            )
            return

        outputs[step.id] = output

        is_error = step.role == "reviewer" and output.strip().upper().startswith("NACK")
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
    if text:
        return text
    if not tool_calls:
        return ""
    # Build a digest the reviewer can reason about.
    lines = [f"(no narrative text from {role.provider}:{role.model}; tool activity:)"]
    for tid, name, tinput in tool_calls:
        inp_str = json.dumps(tinput, default=str) if tinput is not None else "{}"
        if len(inp_str) > 400:
            inp_str = inp_str[:400] + "…"
        lines.append(f"- {name} input={inp_str}")
        if tid in tool_results:
            content, is_error = tool_results[tid]
            tag = "ERR" if is_error else "OK"
            snippet = content.replace("\n", " ")[:300]
            lines.append(f"    [{tag}] {snippet}")
    return "\n".join(lines)


def _planner_prompt_with_constraint(base_prompt: str, workers: list[str]) -> str:
    """Append a one-line constraint to the planner's system prompt that
    spells out which worker roles ARE in this workflow. The model already
    has its base instructions; this is just the hard guarantee.

    When all three workers are present we skip the suffix — the base prompt
    already enumerates them.
    """
    if set(workers) == set(WORKER_ROLES):
        return base_prompt
    return (
        base_prompt
        + "\n\n[Workflow constraint for this turn: ONLY emit steps with role in "
        + f"{{ {', '.join(workers) or 'none'} }}. Do NOT use any other role.]"
    )


def _summarize_plan(plan: Plan) -> str:
    return "\n".join(
        f"- [{s.id}] {s.role}: {s.prompt[:120]}{'…' if len(s.prompt) > 120 else ''}"
        for s in plan.steps
    )


def _find_upstream_worker(steps: list[Step], reviewer_id: str) -> Step | None:
    """The worker step a reviewer was reviewing — i.e. what to re-run on NACK.

    Preference order:
      1. The reviewer's own ``depends_on`` if it points to a worker.
      2. The most recent worker step before the reviewer in plan order.

    Returns None if neither is available (defensive — caller should skip retry).
    """
    reviewer = next((s for s in steps if s.id == reviewer_id), None)
    if reviewer is None:
        return None
    if reviewer.depends_on:
        by_id = {s.id: s for s in steps}
        for dep_id in reversed(reviewer.depends_on):
            dep = by_id.get(dep_id)
            if dep is not None and dep.role in WORKER_ROLES and dep.role != "reviewer":
                return dep
    idx = next((i for i, s in enumerate(steps) if s.id == reviewer_id), None)
    if idx is None:
        return None
    for s in reversed(steps[:idx]):
        if s.role in WORKER_ROLES and s.role != "reviewer":
            return s
    return None


def _lookup_step_prompt(steps: list[Step], step_id: str) -> str:
    s = next((x for x in steps if x.id == step_id), None)
    return s.prompt if s else "Re-review the previous step."


async def _await_approval(
    channel: asyncio.Queue[dict[str, Any]] | None,
    approval_id: str,
    timeout: float,
) -> dict[str, Any]:
    """Block until the user accepts/rejects this approval, or a timeout fires.

    The queue is shared across the whole turn — if the user clicks an old
    "Approve" button after a new approval is asked, the stale message has a
    different ``id`` and is dropped here rather than satisfying the wrong gate.

    When ``channel`` is None (no WS back-channel — e.g. a unit test calling
    the provider directly), default-allow so headless usage still completes.
    """
    if channel is None:
        return {"id": approval_id, "value": "yes", "feedback": None, "auto": True}

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"id": approval_id, "value": "timeout", "feedback": None}
        try:
            msg = await asyncio.wait_for(channel.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return {"id": approval_id, "value": "timeout", "feedback": None}
        msg_id = msg.get("id")
        if msg_id and msg_id != approval_id:
            # Stale message addressed to a previous approval gate. Drop and
            # keep waiting on the original deadline.
            continue
        value = "yes" if msg.get("value") == "yes" else "no"
        return {"id": approval_id, "value": value, "feedback": msg.get("feedback")}


def _final_summary(outputs: dict[str, str], plan: Plan) -> str:
    """The user-facing assistant text after all steps complete.

    Preference order: last coder output → last developer output → last output
    of any kind. Reviewer outputs are skipped — their LGTM/NACK is metadata,
    not the answer the user asked for.
    """
    for role in ("coder", "developer"):
        rolled = [outputs[s.id] for s in plan.steps if s.role == role and outputs.get(s.id)]
        if rolled:
            return rolled[-1]
    any_non_review = [
        outputs[s.id] for s in plan.steps if s.role != "reviewer" and outputs.get(s.id)
    ]
    return any_non_review[-1] if any_non_review else ""
