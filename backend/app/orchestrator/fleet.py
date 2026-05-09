"""Multi-agent fleet orchestrator (Proposal G).

A fleet decomposes a single user prompt into specialist steps:

  planner   → emits an ordered list of {developer | coder | reviewer} steps
  developer → produces a design / approach for a step (no code)
  coder     → implements a step (may use file/bash tools via its sub-provider)
  reviewer  → verifies the previous step's output, returns LGTM or NACK

The fleet itself implements the ``Provider`` protocol so the FastAPI WebSocket
treats it as just another backend. Each sub-step is surfaced as a tool_use →
tool_result pair on the unified event stream, so the existing UI renders the
workflow as an expandable card without any front-end changes.

The role assignments and models are user-configurable via a YAML or JSON file.
Lookup order:
  1. ``$LOCALCODE_FLEET_CONFIG``                       (explicit absolute path)
  2. ``<cwd>/.localcode/fleet.{yaml,yml,json}``        (project-local)
  3. ``<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}``
  4. built-in defaults

Why so few abstractions: this is still a v1. Linear plan execution (no parallel
branches), four roles, single-shot fallback if the planner fails. Add DAG
support, retries, and richer streaming once we know what we actually need.
"""
from __future__ import annotations

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
StepRole = Literal["developer", "coder", "reviewer"]


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RoleConfig:
    provider: str  # "claude" or "opencode"
    model: str
    system_prompt: str


@dataclass
class FleetConfig:
    name: str
    planner: RoleConfig
    developer: RoleConfig
    coder: RoleConfig
    reviewer: RoleConfig
    max_steps: int = 6
    # Where the active config came from — None means built-in defaults. Useful
    # for the UI to surface "Fleet config: ~/.localcode/fleet.yaml" debugging.
    config_source: str | None = None


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


DEFAULT_FLEET_CONFIG = FleetConfig(
    name="default",
    planner=RoleConfig(
        provider="claude",
        model="claude-sonnet-4-6",
        system_prompt=_PLANNER_SYSTEM,
    ),
    developer=RoleConfig(
        provider="claude",
        model="claude-opus-4-7",
        system_prompt=_DEVELOPER_SYSTEM,
    ),
    coder=RoleConfig(
        provider="opencode",
        model="openai/gpt-5.3-codex",
        system_prompt=_CODER_SYSTEM,
    ),
    reviewer=RoleConfig(
        provider="claude",
        model="claude-haiku-4-5",
        system_prompt=_REVIEWER_SYSTEM,
    ),
)


# Cache the parsed config keyed by (path, mtime). Only re-parses when the file
# changes on disk. Mtime comparison is one stat() per turn — much cheaper than
# parsing YAML and walking it every call. The lock protects the cache dict
# itself from torn writes under concurrent access.
_CFG_CACHE: dict[str, tuple[float, FleetConfig]] = {}
_CFG_CACHE_LOCK = threading.Lock()


def load_fleet_config(cwd: str | None = None) -> FleetConfig:
    """Resolve and load the active fleet config.

    Resolution order (first hit wins):
      1. ``Settings.localcode_fleet_config`` (absolute path override).
      2. ``<cwd>/.localcode/fleet.{yaml,yml,json}``.
      3. ``<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}``.

    Falls back to ``DEFAULT_FLEET_CONFIG`` when no file is found, parsing
    fails, or every override is invalid. Per-field validation is best-effort:
    invalid fields revert to the default (with a warning), so a typo in one
    role doesn't break the whole fleet.

    Cached by (path, mtime) so we don't re-parse on every turn — saves a
    YAML parse + walk on the event loop. Edit-and-retry still works because
    the cache invalidates as soon as mtime changes.
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
    """Validate and merge an override dict over the defaults. Field-by-field —
    invalid entries log a warning and inherit the default rather than failing
    the whole load."""
    roles_in = override.get("roles") or {}
    if not isinstance(roles_in, dict):
        logger.warning("fleet config: 'roles' must be a mapping; ignoring")
        roles_in = {}

    def merge_role(name: str, base_r: RoleConfig) -> RoleConfig:
        o = roles_in.get(name) or {}
        if not isinstance(o, dict):
            logger.warning("fleet config: 'roles.%s' must be a mapping; using defaults", name)
            return base_r
        provider = o.get("provider", base_r.provider)
        if provider not in VALID_PROVIDERS:
            logger.warning(
                "fleet config: 'roles.%s.provider'=%r invalid (allowed: %s); "
                "using default %r",
                name, provider, VALID_PROVIDERS, base_r.provider,
            )
            provider = base_r.provider
        model = str(o.get("model") or base_r.model).strip()
        if not model:
            logger.warning("fleet config: 'roles.%s.model' empty; using default", name)
            model = base_r.model
        system_prompt = str(o.get("system_prompt") or base_r.system_prompt).strip()
        return RoleConfig(provider=provider, model=model, system_prompt=system_prompt)

    return FleetConfig(
        name=str(override.get("name") or base.name),
        planner=merge_role("planner", base.planner),
        developer=merge_role("developer", base.developer),
        coder=merge_role("coder", base.coder),
        reviewer=merge_role("reviewer", base.reviewer),
        max_steps=max(1, int(override.get("max_steps", base.max_steps))),
    )


def config_to_dict(cfg: FleetConfig) -> dict[str, Any]:
    """Serialize a FleetConfig for the /api/fleet/config endpoint."""
    d = asdict(cfg)
    return d


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


def parse_plan(planner_output: str, max_steps: int) -> Plan:
    """Pull the first JSON object out of a planner reply and validate it."""
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
        if role not in ("developer", "coder", "reviewer"):
            raise ValueError(f"step {sid} has invalid role: {role!r}")
        prompt = str(s.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"step {sid} has empty prompt")
        depends = s.get("depends_on") or []
        if not isinstance(depends, list):
            depends = []
        steps.append(
            Step(id=sid, role=role, prompt=prompt, depends_on=[str(d) for d in depends])
        )
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
        # Per-session UI override layered on top of the file config. Same
        # validation rules apply: bad fields revert to whatever the file/built-in
        # supplied, so a typo in the modal can't sink the turn.
        ui_override = ctx.extras.get("fleet_config_override") if ctx.extras else None
        if isinstance(ui_override, dict) and ui_override:
            base_src = cfg.config_source or "<built-in defaults>"
            cfg = _merge_config(cfg, ui_override)
            cfg.config_source = f"{base_src} + UI override"

        # ── 1. Planning ─────────────────────────────────────────────────
        plan_id = "fleet.plan"
        yield Event(
            type="assistant.tool_use",
            data={
                "id": plan_id,
                "name": f"planner [{cfg.planner.provider}:{cfg.planner.model}]",
                "input": {"task": ctx.prompt},
            },
        )
        try:
            plan_text = await _collect_text(cfg.planner, ctx.prompt, ctx.cwd)
            plan = parse_plan(plan_text, cfg.max_steps)
        except Exception as exc:
            # Planner unreachable or returned garbage — fall back to a single
            # coder step on the raw user prompt so the user still gets an answer.
            logger.warning("fleet planning failed, single-shot fallback: %s", exc)
            yield Event(
                type="tool.result",
                data={
                    "tool_use_id": plan_id,
                    "content": (
                        f"planning failed ({exc}); falling back to a single coder step"
                    ),
                    "is_error": True,
                },
            )
            plan = Plan(
                steps=[Step(id="s1", role="coder", prompt=ctx.prompt, depends_on=[])]
            )
        else:
            yield Event(
                type="tool.result",
                data={
                    "tool_use_id": plan_id,
                    "content": _summarize_plan(plan),
                    "is_error": False,
                },
            )

        # ── 2. Step execution ───────────────────────────────────────────
        outputs: dict[str, str] = {}
        for step in plan.steps:
            async for ev in self._run_step(step, ctx, cfg, outputs):
                yield ev

        # ── 3. Final assistant text ─────────────────────────────────────
        final = _final_summary(outputs, plan)
        if final:
            yield Event(type="assistant.text", data={"text": final})

        yield Event(
            type="assistant.done",
            data={"duration_ms": int((time.time() - t0) * 1000)},
        )

    async def _run_step(
        self,
        step: Step,
        ctx: RunContext,
        cfg: FleetConfig,
        outputs: dict[str, str],
    ) -> AsyncIterator[Event]:
        role_cfg = {
            "developer": cfg.developer,
            "coder": cfg.coder,
            "reviewer": cfg.reviewer,
        }[step.role]

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
            output = await _collect_text(role_cfg, full_prompt, ctx.cwd)
        except Exception as exc:
            yield Event(
                type="tool.result",
                data={"tool_use_id": step.id, "content": str(exc), "is_error": True},
            )
            return

        outputs[step.id] = output

        # A reviewer's NACK is informational (not an exception), but we mark it
        # so the UI renders the card as failed.
        is_error = step.role == "reviewer" and output.strip().upper().startswith("NACK")
        yield Event(
            type="tool.result",
            data={"tool_use_id": step.id, "content": output, "is_error": is_error},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


async def _collect_text(role: RoleConfig, prompt: str, cwd: str | None) -> str:
    """Invoke a sub-provider and return its concatenated assistant text.

    Tool-use events from the sub-provider are intentionally swallowed at this
    level (file edits / bash calls still happen via the sub-provider's own
    machinery). v2 may forward them as nested events.
    """
    # Lazy import — registry imports this module's class.
    from .registry import get_provider

    sub = await get_provider(role.provider)  # type: ignore[arg-type]
    sub_ctx = RunContext(
        model=role.model,
        prompt=prompt,
        cwd=cwd,
        system_prompt=role.system_prompt,
    )
    chunks: list[str] = []
    async for ev in sub.run(sub_ctx):
        if ev.type == "assistant.text":
            chunks.append(ev.data.get("text", ""))
        elif ev.type == "error":
            raise RuntimeError(ev.data.get("message") or "sub-provider error")
    return "".join(chunks).strip()


def _summarize_plan(plan: Plan) -> str:
    return "\n".join(
        f"- [{s.id}] {s.role}: {s.prompt[:120]}{'…' if len(s.prompt) > 120 else ''}"
        for s in plan.steps
    )


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
