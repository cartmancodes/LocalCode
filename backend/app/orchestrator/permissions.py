"""Pure permission policy for spawned coding-agent CLIs.

Today the Claude provider silently escalates an unknown or missing permission
mode to ``acceptEdits`` so a headless run never hangs on a prompt nobody can
answer — which grants filesystem writes by default on every fleet step. That
escalation is the defect this module removes: ``normalize_permission_mode``
below folds an unknown mode to ``default`` (ask-before-write), never to a
mode that grants more.

This module makes zero I/O calls, imports nothing from the SDK, and emits no
events. That is deliberate, not incidental: a role policy (``ToolPolicy``) and
a single decision function (``decide``) that only ever look at their
arguments can be driven by a table of test cases, and the exact same table
serves both vendor providers (Claude today, Codex later) instead of each one
reinventing — and drifting from — its own gate.

``resolve()`` calls below do touch the filesystem (following symlinks) but
make no writes and hold no state; that is what "pure" means here — safe to
call speculatively, safe to call from a test, safe to call twice.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "plan", "bypassPermissions"})

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
EXEC_TOOLS = frozenset({"Bash", "BashOutput", "KillBash"})
NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})

# Tool-input keys that carry a filesystem path, in priority order: a single
# tool call uses at most one of these (they are aliases across tool
# schemas), so the first one present wins rather than every key being
# collected and de-duplicated.
PATH_ARG_KEYS = ("file_path", "path", "notebook_path", "filePath")

# Mirrors the disallowed_tools list orchestrator.py already hard-codes for
# its own "MCP dispatch only" role — the built-in catalogue a vendor CLI
# ships, named explicitly rather than relying only on an empty allow_tools
# tuple. See ToolPolicy's ``allow_tools`` vs ``deny_tools`` distinction: an
# empty allow_tools already denies everything via decide()'s branch 2, but
# orchestrator.py found in practice that allow_tools alone doesn't restrict
# the spawned CLI (user/project settings can re-grant the catalogue), so the
# role table double-covers it here too.
_ORCHESTRATOR_BUILTIN_DENY = frozenset(
    {
        "Read", "Edit", "Write", "MultiEdit",
        "Bash", "BashOutput", "KillBash",
        "Glob", "Grep",
        "WebFetch", "WebSearch",
        "Skill", "Agent", "Task",
        "TodoWrite", "ExitPlanMode", "EnterPlanMode",
        "NotebookEdit", "ToolSearch",
    }
)


@dataclass(frozen=True)
class ToolPolicy:
    """What one role (or the default session) may do with the tool surface.

    ``roots``/``denied`` are expected pre-resolved (see ``resolve_roots`` /
    ``Settings.denied_path_list``) — this dataclass just carries them.
    """

    name: str  # the role this policy belongs to
    writable: bool  # may mutate the filesystem at all
    exec_allowed: bool  # may run Bash
    roots: tuple[Path, ...]  # writable/readable roots (resolved)
    denied: tuple[Path, ...]  # always refused
    allow_tools: tuple[str, ...] | None = None  # None = vendor default set
    deny_tools: tuple[str, ...] = ()
    # Tools that need a human yes even when otherwise permitted.
    ask_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    outcome: Literal["allow", "deny", "ask"]
    reason: str


def normalize_permission_mode(mode: str | None, *, allow_bypass: bool) -> str:
    """Fold an arbitrary requested mode to one the harness will act on.

    ``None`` and anything not in ``VALID_PERMISSION_MODES`` fall back to
    ``"default"`` — never to ``"acceptEdits"``. Silently upgrading an unknown
    value to a mode that skips write approval is exactly the privilege
    escalation this module exists to remove, so an unrecognized value is
    logged at WARNING (it is very likely a typo or a stale caller) and still
    denied the upgrade.
    """
    if mode is None:
        return "default"
    if mode not in VALID_PERMISSION_MODES:
        logger.warning("Unknown permission mode %r; falling back to 'default'", mode)
        return "default"
    if mode == "bypassPermissions" and not allow_bypass:
        logger.warning(
            "bypassPermissions requested but allow_bypass_permissions is not set; "
            "falling back to 'default'"
        )
        return "default"
    return mode


def resolve_roots(cwd: str | None, additional_dirs: Sequence[str] | None) -> tuple[Path, ...]:
    """Expand and resolve ``cwd`` plus every additional dir into one ordered,
    de-duplicated tuple of absolute roots. Empty/blank entries are dropped."""
    candidates = [cwd, *(additional_dirs or [])]
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in candidates:
        if raw is None or not raw.strip():
            continue
        resolved = Path(raw).expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return tuple(roots)


def is_within(path: Path, roots: Sequence[Path]) -> bool:
    """True when ``path`` equals a root or has one as an ancestor.

    Both sides are resolved before comparing so a symlink inside an allowed
    root that points outside the tree is caught rather than trusted.
    """
    resolved = path.expanduser().resolve()
    for root in roots:
        root_resolved = root.expanduser().resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


def path_decision(path: Path, policy: ToolPolicy) -> Decision:
    """Decide one path against a policy's denied list and roots.

    Denied wins over roots: a path under both an allowed root and a denied
    subdirectory (e.g. a project root containing a ``.git`` with credential
    helpers, or simply ``~`` itself containing ``~/.ssh``) is refused.
    """
    resolved = path.expanduser().resolve()
    if is_within(resolved, policy.denied):
        return Decision("deny", f"path {resolved} is under a denied directory")
    if is_within(resolved, policy.roots):
        return Decision("allow", f"path {resolved} is within an allowed root")
    roots_desc = [str(r) for r in policy.roots]
    return Decision("deny", f"path {resolved} is outside all allowed roots {roots_desc}")


def _first_path_value(tool_input: Mapping[str, Any]) -> str | None:
    for key in PATH_ARG_KEYS:
        value = tool_input.get(key)
        if value:
            return str(value)
    return None


def extract_paths(tool_name: str, tool_input: Mapping[str, Any]) -> list[Path]:
    """Pull every filesystem path a tool call touches, so ``decide`` can run
    each one through ``path_decision``.

    ``Bash`` returns ``[]`` on purpose — a shell command line is free text,
    not a path argument, and trying to parse one out would be a false sense
    of safety. ``exec_allowed`` plus the ask-gate govern Bash instead.
    """
    if tool_name == "Bash":
        return []
    paths: list[Path] = []
    value = _first_path_value(tool_input)
    if value is not None:
        paths.append(Path(value))
    if tool_name == "MultiEdit":
        for edit in tool_input.get("edits", None) or []:
            if isinstance(edit, Mapping):
                edit_path = edit.get("file_path")
                if edit_path:
                    paths.append(Path(str(edit_path)))
    return paths


def decide(
    tool_name: str,
    tool_input: Mapping[str, Any],
    policy: ToolPolicy,
    *,
    mode: str,
) -> Decision:
    """The single decision function. Branch order is security-critical —
    see the comment on branch 6 for why the bypass check is not first."""
    # 1. An explicit per-role deny always wins, regardless of mode.
    if tool_name in policy.deny_tools:
        return Decision("deny", f"role {policy.name} may not use {tool_name}")

    # 2. An allow-list, when present, is exhaustive: anything not named is
    #    refused rather than falling through to a broader vendor default.
    if policy.allow_tools is not None and tool_name not in policy.allow_tools:
        return Decision("deny", f"role {policy.name} does not allow {tool_name}")

    # 3. Writability is a structural property of the role, not a mode.
    if tool_name in WRITE_TOOLS and not policy.writable:
        return Decision("deny", f"role {policy.name} is read-only")

    # 4. Same for exec.
    if tool_name in EXEC_TOOLS and not policy.exec_allowed:
        return Decision("deny", f"role {policy.name} may not execute commands")

    # 5. Any path argument outside the role's roots (or inside a denied
    #    directory) is refused before mode is even consulted.
    for path in extract_paths(tool_name, tool_input):
        path_result = path_decision(path, policy)
        if path_result.outcome == "deny":
            return path_result

    # 6. bypassPermissions is checked AFTER the role gates above (1-5), never
    #    before. A permission mode is a human-in-the-loop preference — skip
    #    the confirmation prompt — not a structural override of what a role
    #    may touch at all. If this branch ran first, an operator who enabled
    #    bypassPermissions would hand a read-only `planner` or `reviewer`
    #    write access, which is exactly the escalation the role table exists
    #    to prevent. Task 9 asserts this ordering directly.
    if mode == "bypassPermissions":
        return Decision("allow", "bypassPermissions")

    # 7. A role-specific ask-gate outranks acceptEdits/default's own rules.
    if tool_name in policy.ask_tools:
        return Decision("ask", f"role {policy.name} always asks before {tool_name}")

    # 8. plan mode: look, don't touch.
    if mode == "plan" and tool_name in WRITE_TOOLS | EXEC_TOOLS:
        return Decision("deny", "plan mode forbids write/exec tools")

    # 9. acceptEdits auto-approves writes (but not exec — that still asks).
    if mode == "acceptEdits" and tool_name in WRITE_TOOLS:
        return Decision("allow", "acceptEdits mode auto-approves writes")

    # 10. default mode: write/exec tools need a human yes.
    if tool_name in WRITE_TOOLS | EXEC_TOOLS:
        return Decision("ask", "default mode requires confirmation for write/exec tools")

    # 11. Everything else (reads, search, network) is allowed.
    return Decision("allow", "no restriction applies")


# Role -> (allow_tools, deny_tools, writable, exec_allowed). Anything not a
# key here gets the `writable_default` policy named "session" instead.
_ROLE_ALLOW_TOOLS: dict[str, tuple[str, ...] | None] = {
    "planner": ("Read", "Glob", "Grep", "LS", "TodoWrite"),
    "reviewer": None,
    "tester": None,
    "coder": None,
    "developer": ("Read", "Glob", "Grep", "LS"),
    "orchestrator": (),  # MCP dispatch only — no built-in tool is named
}

_ROLE_DENY_TOOLS: dict[str, frozenset[str]] = {
    "planner": WRITE_TOOLS | EXEC_TOOLS | frozenset({"Agent", "Task", "Skill"}),
    # reviewer keeps Bash (exec_allowed=True below): reading a repo honestly
    # needs `git diff` and `rg`, and denying that would make review theater
    # rather than review. The write denial below is the actual restriction
    # the spec asks for — do not "tighten" this by also denying Bash.
    "reviewer": WRITE_TOOLS | frozenset({"Agent", "Task"}),
    "tester": frozenset({"Agent", "Task"}),
    "coder": frozenset({"Agent", "Task"}),
    "developer": WRITE_TOOLS | EXEC_TOOLS,
    "orchestrator": _ORCHESTRATOR_BUILTIN_DENY,
}

_ROLE_WRITABLE: dict[str, bool] = {
    "planner": False,
    "reviewer": False,
    "tester": True,
    "coder": True,
    "developer": False,
    "orchestrator": False,
}

_ROLE_EXEC: dict[str, bool] = {
    "planner": False,
    "reviewer": True,
    "tester": True,
    "coder": True,
    "developer": False,
    "orchestrator": False,
}


def policy_for_role(
    role: str | None,
    roots: Sequence[Path],
    denied: Sequence[Path],
    *,
    writable_default: bool = True,
) -> ToolPolicy:
    """Look up the fixed role table. An unrecognized (or ``None``) role gets
    an unrestricted "session" policy — every non-fleet chat session — gated
    by ``writable_default`` and otherwise left to ``decide``'s mode branches."""
    if role not in _ROLE_WRITABLE:
        return ToolPolicy(
            name="session",
            writable=writable_default,
            exec_allowed=True,
            roots=tuple(roots),
            denied=tuple(denied),
        )
    return ToolPolicy(
        name=role,
        writable=_ROLE_WRITABLE[role],
        exec_allowed=_ROLE_EXEC[role],
        roots=tuple(roots),
        denied=tuple(denied),
        allow_tools=_ROLE_ALLOW_TOOLS[role],
        deny_tools=tuple(sorted(_ROLE_DENY_TOOLS[role])),
    )


def policy_extras(policy: ToolPolicy) -> dict[str, Any]:
    """Render a policy into the ``ctx.extras`` keys ``claude.py`` already
    reads (``option_extras`` block): ``claude_allowed_tools``,
    ``claude_disallowed_tools``, ``claude_disable_settings``,
    ``claude_disable_skills``.

    Read-only policies additionally disable settings and skills. This is the
    same belt-and-braces reasoning orchestrator.py's own comment records:
    ``allowed_tools``/``disallowed_tools`` alone doesn't restrict the spawned
    CLI, because the SDK loads user/project Claude Code settings by default
    (re-granting the built-in catalogue) and a project's own Skill
    definitions can do the same. A read-only role is not actually read-only
    while either escape hatch is open.
    """
    extras: dict[str, Any] = {}
    if policy.allow_tools is not None:
        extras["claude_allowed_tools"] = list(policy.allow_tools)
    if policy.deny_tools:
        extras["claude_disallowed_tools"] = list(policy.deny_tools)
    if not policy.writable:
        extras["claude_disable_settings"] = True
        extras["claude_disable_skills"] = True
    return extras
