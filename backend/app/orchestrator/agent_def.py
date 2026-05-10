"""Agent definitions — the registry entries the orchestrator dispatches to.

This is the v2 shape, modelled after Claude Code's ``AgentDefinition`` and
OpenCode's agent frontmatter. It's deliberately a superset of the existing
``RoleConfig`` so we can convert legacy fleet configs into a registry without
losing information.

An ``AgentDef`` is pure data — no provider calls happen here. The
``dispatch.py`` MCP tool consumes the registry and routes invocations to the
right ``Provider`` implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentDef:
    """One subagent in the orchestrator's registry.

    Mirrors Claude Code's ``AgentDefinition`` / OpenCode's agent frontmatter:

      - ``name`` is the unique identifier the orchestrator passes to the
        ``dispatch_subagent`` tool.
      - ``description`` is what the orchestrator reads to decide *when* to
        dispatch this agent. Keep it imperative and outcome-focused
        ("produces a Markdown plan"), not behavioural ("you are a helpful…").
      - ``provider`` + ``model`` route the dispatch to a concrete sub-provider
        (``claude`` → claude-agent-sdk, ``opencode`` → opencode HTTP).
      - ``system_prompt`` is the agent's own instructions, rendered when the
        sub-provider is invoked.
      - ``permission_mode`` and ``max_turns`` shape the inner ReAct loop the
        sub-provider runs (``acceptEdits``/``default``/``bypassPermissions``;
        max iterations before the SDK forces a final response).
    """

    name: str
    description: str
    provider: str
    model: str
    system_prompt: str
    permission_mode: str | None = None
    max_turns: int | None = None
    # Free-form metadata for forward compat (e.g. tags, color hints, future
    # tool allowlists). Never participates in dispatch routing.
    metadata: dict[str, Any] = field(default_factory=dict)


def registry_from_role_library(role_library: dict[str, Any]) -> dict[str, AgentDef]:
    """Convert the legacy ``ROLE_LIBRARY`` (``dict[role_name, RoleConfig]``)
    into an orchestrator registry. Used as the default registry seed when
    a fleet config doesn't supply one explicitly.

    Descriptions are baked from the role name so the orchestrator has
    something concrete to read; users can override per agent in fleet.yaml.
    """
    descriptions = {
        "planner": (
            "Produces a comprehensive Markdown implementation plan with "
            "exact file paths, complete code blocks, test commands, and "
            "bite-sized steps. Plan is committed to .localcode/plans/. "
            "Dispatch this FIRST for any non-trivial task."
        ),
        "developer": (
            "Optional design pass before the coder. Produces a short "
            "technical design doc — no code. Use only when the plan needs "
            "extra architectural detail."
        ),
        "coder": (
            "Executes a plan task-by-task using file-edit and bash tools. "
            "Pass the FULL plan in the prompt. The coder MUST use tools, "
            "not just describe intent."
        ),
        "reviewer": (
            "Verifies plan compliance and code quality on disk (read-only). "
            "Returns LGTM or NACK: <reason> on its last line. Dispatch "
            "AFTER coder."
        ),
        "tester": (
            "Writes executable tests for the implementation, runs them, "
            "and returns LGTM, NACK_CODE: <reason> (impl bug → re-dispatch "
            "coder), or NACK_TESTS: <reason> (test bug → re-dispatch "
            "tester). Dispatch AFTER reviewer LGTM."
        ),
    }
    out: dict[str, AgentDef] = {}
    for name, role_cfg in role_library.items():
        # role_cfg is a fleet.RoleConfig but we only depend on its public
        # attributes — duck-typed to avoid a circular import.
        out[name] = AgentDef(
            name=name,
            description=descriptions.get(name, f"{name} agent"),
            provider=role_cfg.provider,
            model=role_cfg.model,
            system_prompt=role_cfg.system_prompt,
        )
    return out


def render_registry_for_prompt(registry: dict[str, AgentDef]) -> str:
    """Render the agent registry as a Markdown bullet list for the
    orchestrator's system prompt.

    Format mirrors what Claude Code puts in front of an orchestrator —
    one line per agent with name + description + (provider:model) hint so
    the orchestrator can reason about cost/latency tradeoffs when it has a
    choice between agents that overlap.
    """
    lines: list[str] = []
    for name, agent in registry.items():
        lines.append(
            f"  - `{name}` ({agent.provider}:{agent.model}) — {agent.description}"
        )
    return "\n".join(lines)
