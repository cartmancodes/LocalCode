"""The fleet — an orchestrator that dispatches specialist subagents.

Registers one tool, ``dispatch``. Each subagent is a nested
:class:`AgentSession` on its own engine, so it gets its own context window,
its own model, and its own enforced tool limits. Engines are already
out-of-process, so no subprocess dance is needed — the reason the old fleet
spawned one per step was an SDK re-entrancy deadlock that no longer exists.

Three things the old fleet got wrong, fixed here:

1. **Tool limits were prose.** "You do not have Bash" in a system prompt is a
   request. Now the reviewer runs under a read-only sandbox and the
   orchestrator's own engine session is never given file tools at all.
2. **Verdicts were string-matched** on the last line, so a chatty reviewer
   broke the gate. Now subagents return a fenced JSON verdict; the last-line
   form is kept only as a fallback.
3. **Every turn paid for every role.** A complexity gate routes trivial and
   read-only turns to a single agent; the 15x multiplier is reserved for work
   that earns it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from backend.app.core.agent_session import AgentSession
from backend.app.core.engines import create_engine
from backend.app.core.extensions.types import ToolDefinition
from backend.app.core.session_manager import SessionManager

from .config import FleetConfig, RoleConfig, load_fleet_config
from .gate import Complexity, classify_complexity
from .prompts import ORCHESTRATOR_GUIDANCE, role_prompt
from .verdict import Verdict, parse_verdict

MAX_DISPATCHES_PER_TURN = 12
HARD_FAIL_CAP = 2


def setup(api: Any) -> None:
    state = FleetState(api)

    api.register_tool(
        ToolDefinition(
            name="dispatch",
            label="Dispatch subagent",
            description=(
                "Hand a focused task to a specialist subagent. The subagent runs in "
                "its own context window on its own model with its own tool limits, and "
                "returns its result. Use this instead of doing planning, coding, "
                "reviewing or testing work yourself. Arguments: name (one of the "
                "registered roles), task (a self-contained description of what that "
                "agent should do)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "the subagent to run"},
                    "task": {"type": "string", "description": "self-contained task description"},
                },
                "required": ["name", "task"],
                "additionalProperties": False,
            },
            prompt_snippet="delegate a focused task to a specialist subagent",
            prompt_guidelines=[ORCHESTRATOR_GUIDANCE],
            execute=state.dispatch,
        )
    )

    api.register_command(
        "fleet", handler=state.command, description="show or force the fleet workflow"
    )

    async def on_session_start(event: dict[str, Any], ctx: Any) -> None:
        state.reset(ctx.cwd)

    async def on_before_agent_start(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """The complexity gate. A turn that does not need a crew does not get one.

        The verdict rides along as a context message rather than being edited
        into the user's prompt: what they typed is what the transcript should
        show, and what the model reads is their prompt plus our note.
        """
        state.reset(ctx.cwd)
        if state.forced_full:
            state.forced_full = False
            state.complexity = Complexity.COMPLEX
            return None
        config = state.config
        if not config.gate_enabled or config.always_full:
            return None
        complexity = classify_complexity(event.get("prompt", ""))
        state.complexity = complexity
        if complexity is not Complexity.SIMPLE:
            return None
        return {
            "message": {
                "customType": "fleet_gate",
                "content": (
                    "This looks like a direct request rather than a project. "
                    "Answer it yourself; do not dispatch subagents."
                ),
                "display": False,
            }
        }

    async def on_agent_end(event: dict[str, Any], ctx: Any) -> None:
        if state.dispatches:
            summary = ", ".join(f"{name} x{n}" for name, n in sorted(state.dispatches.items()))
            api.append_entry(
                "fleet_run", {"dispatches": summary, "complexity": state.complexity.value}
            )

    api.on("session_start", on_session_start)
    api.on("before_agent_start", on_before_agent_start)
    api.on("agent_end", on_agent_end)


class FleetState:
    """Per-session fleet bookkeeping: config, dispatch counts, failure caps."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.cwd: str | None = None
        self._config: FleetConfig | None = None
        self.dispatches: dict[str, int] = {}
        self.hard_fails: dict[str, int] = {}
        self.outputs: dict[str, str] = {}
        self.complexity: Complexity = Complexity.UNKNOWN
        self.forced_full = False

    @property
    def config(self) -> FleetConfig:
        if self._config is None:
            self._config = load_fleet_config(self.cwd)
        return self._config

    def reset(self, cwd: str | None) -> None:
        if cwd != self.cwd:
            self.cwd = cwd
            self._config = None
        self.dispatches.clear()
        self.outputs.clear()

    # ── the tool ───────────────────────────────────────────────────────

    async def dispatch(
        self, call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        name = str(params.get("name", "")).strip()
        task = str(params.get("task", "")).strip()
        config = self.config

        if not name or name not in config.roles:
            return _error(
                f"unknown subagent {name!r}. Registered: "
                f"{', '.join(sorted(config.roles)) or '(none — check .localcode/fleet.yaml)'}"
            )
        if not task:
            return _error("missing 'task' — give the subagent something to do")
        if sum(self.dispatches.values()) >= MAX_DISPATCHES_PER_TURN:
            return _error(
                f"dispatch budget for this turn is spent ({MAX_DISPATCHES_PER_TURN}). "
                "Summarise what you have and stop."
            )
        if self.hard_fails.get(name, 0) >= HARD_FAIL_CAP:
            return _error(
                f"{name} has failed {self.hard_fails[name]}x this turn — its engine is "
                f"unavailable. Do not dispatch it again. Stop and tell the user which "
                f"engine failed."
            )

        role = config.roles[name]
        self.dispatches[name] = self.dispatches.get(name, 0) + 1
        prompt = self._build_prompt(name, task, role)

        if on_update is not None:
            on_update(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"dispatching {name} ({role.engine}:{role.model})…",
                        }
                    ]
                }
            )

        started = time.monotonic()
        try:
            text = await self._run_subagent(name, role, prompt, ctx)
        except Exception as exc:  # noqa: BLE001 — report to the model, never crash the turn
            self.hard_fails[name] = self.hard_fails.get(name, 0) + 1
            return _error(f"{name} failed on {role.engine}: {type(exc).__name__}: {exc}")

        elapsed = time.monotonic() - started
        if not text.strip():
            self.hard_fails[name] = self.hard_fails.get(name, 0) + 1
            return _error(
                f"{name} produced no output after {elapsed:.0f}s. Do not retry it blindly."
            )

        self.outputs[name] = text
        if name == "planner":
            saved = _save_plan(text, ctx.cwd)
            if saved:
                text = f"{text}\n\n_Plan saved to_ `{saved}`"

        verdict = parse_verdict(text) if role.is_gate else None
        summary = _summarise(name, role, text, verdict, elapsed)
        return {
            "content": [{"type": "text", "text": summary}],
            "details": {
                "role": name,
                "engine": role.engine,
                "model": role.model,
                "verdict": verdict.to_dict() if verdict else None,
                "elapsedSeconds": round(elapsed, 1),
            },
        }

    # ── subagent execution ─────────────────────────────────────────────

    async def _run_subagent(self, name: str, role: RoleConfig, prompt: str, ctx: Any) -> str:
        """One nested AgentSession, torn down when it is done."""
        engine = create_engine(
            role.engine, factories=self.api.engine_factories, **role.engine_options
        )
        session = AgentSession(
            engine=engine,
            session_manager=SessionManager.in_memory(ctx.cwd),
            mode="print",
            model=role.model,
            thinking_level=role.thinking_level,
            append_system_prompt=role_prompt(name, role),
            permission_mode=role.permission_mode,
            default_permission="allow" if role.can_write else "deny",
        )
        try:
            await session.start()
            await session.prompt(prompt, source="extension", expand_prompt_templates=False)
            await session.wait_for_idle()
            return session.get_last_assistant_text() or ""
        finally:
            await session.shutdown("quit")

    def _build_prompt(self, name: str, task: str, role: RoleConfig) -> str:
        """Give a subagent the artifacts it needs, not the whole transcript."""
        parts = [task]
        if name in ("coder", "reviewer", "tester") and self.outputs.get("planner"):
            parts.append(f"# The plan you are working from\n\n{self.outputs['planner']}")
        if name in ("reviewer", "tester") and self.outputs.get("coder"):
            parts.append(f"# What the coder reported\n\n{self.outputs['coder']}")
        if role.is_gate:
            parts.append(
                "End your reply with a fenced JSON verdict:\n\n"
                "```json\n"
                '{"verdict": "pass" | "fail", "reason": "<one sentence>", '
                '"blame": "code" | "tests" | null}\n'
                "```"
            )
        return "\n\n".join(parts)

    # ── /fleet ─────────────────────────────────────────────────────────

    async def command(self, args: str, ctx: Any) -> None:
        self.reset(ctx.cwd)
        config = self.config
        argument = args.strip()
        if argument.startswith("full"):
            self.forced_full = True
            rest = argument[4:].strip()
            ctx.ui.notify("fleet: full crew forced for the next turn", "info")
            if rest:
                self.api.send_user_message(rest)
            return
        lines = [f"fleet '{config.name}' — {config.source or 'built-in defaults'}"]
        for role_name, role in config.roles.items():
            flags = []
            if role.is_gate:
                flags.append("gate")
            if not role.can_write:
                flags.append("read-only")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {role_name}: {role.engine}:{role.model}{suffix}")
        lines.append(f"  complexity gate: {'on' if config.gate_enabled else 'off'}")
        ctx.ui.notify("\n".join(lines), "info")


# ── helpers ────────────────────────────────────────────────────────────────


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True, "details": {}}


def _summarise(
    name: str, role: RoleConfig, text: str, verdict: Verdict | None, elapsed: float
) -> str:
    header = f"[{name} · {role.engine}:{role.model} · {elapsed:.0f}s]"
    if verdict is not None:
        header += f" verdict={verdict.verdict}"
        if verdict.blame:
            header += f" blame={verdict.blame}"
    return f"{header}\n\n{text}"


def _save_plan(plan: str, cwd: str | None) -> Path | None:
    """Persist the planner's markdown so it is an inspectable artifact."""
    base = Path(cwd) if cwd else Path.cwd()
    plans = base / ".localcode" / "plans"
    match = re.search(r"^\s*#\s+(.+?)\s*$", plan, re.MULTILINE)
    slug = re.sub(r"[^a-z0-9]+", "-", (match.group(1) if match else "plan").lower()).strip("-")
    path = plans / f"{time.strftime('%Y%m%d-%H%M%S')}-{(slug or 'plan')[:60]}.md"
    try:
        plans.mkdir(parents=True, exist_ok=True)
        path.write_text(plan, encoding="utf-8")
    except OSError:
        return None
    return path


__all__ = ["setup", "FleetState", "json"]
