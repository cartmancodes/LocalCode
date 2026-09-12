"""Static gate for the one invariant this whole project depends on.

LocalCode drives Claude Code and Codex by spawning each vendor's own CLI as a
subprocess and letting that CLI authenticate itself (``claude login`` /
``codex login``, refreshed OAuth tokens on disk, the macOS keychain). The
orchestrator is never supposed to read those credential stores or hold a raw
API key / OAuth token / session key itself — Anthropic's February 2026
terms make automating a Claude subscription through anything but the vendor
CLI a violation, and enforcement of that clause has been live since April
2026. A provider that quietly grew a credential read (to "optimize away" a
subprocess spawn, say) would put every user's subscription at risk, and
that kind of change is easy to miss in code review because it often looks
like an innocuous refactor.

This module is a source-text scanner, not a runtime check: it has to catch
the violation at CI time, before the code ever runs. It is deliberately
built from pure functions over source text (via ``ast``) so the same logic
serves as both the CI gate (``scan_tree``) and a set of unit-testable rules
(``scan_source``).
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    filename: str
    line: int
    rule: str  # "credential-store" | "key-assignment" | "keychain"
    detail: str


# Substrings that must never appear in a string literal under `backend/app/`.
# Each one names a place a subscription credential actually lives on disk,
# or a keychain lookup, or an env var name a vendor CLI reads its own key
# from — any of these appearing as a literal is a strong signal the code is
# about to read or fabricate a credential itself instead of leaving that to
# the CLI subprocess.
CREDENTIAL_STORE_MARKERS: tuple[str, ...] = (
    ".credentials.json",
    "credentials.json",
    "/auth.json",
    "auth.json",
    ".claude/.credentials",
    ".codex/auth",
    "find-generic-password",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_SESSION_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

# Catches env var names this codebase must never assign, even ones the
# marker list above doesn't name outright (e.g. a hypothetical
# `MY_VENDOR_API_KEY`) — anything that looks like a secret by its suffix.
SECRET_KEY_PATTERN = re.compile(r"(API_KEY|OAUTH_TOKEN|SESSION_KEY|AUTH_TOKEN)$")


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Return the ``id()`` of every ``ast.Constant`` that is a docstring.

    Docstrings are the first statement of a module/class/function body,
    represented as ``ast.Expr(value=ast.Constant(str))``. They are exempt
    from the credential-store scan because modules like ``claude.py`` and
    ``base.py`` legitimately *describe* the auth model (where the CLI finds
    its token) in prose — that's documentation, not a credential read.
    Comments never reach the AST at all, so they need no separate handling.
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))
    return docstring_ids


def _environ_base(node: ast.expr) -> bool:
    """True for the ``os.environ`` / bare ``environ`` receiver of a subscript
    or attribute access (covers both ``import os`` and
    ``from os import environ`` call sites)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _dotted_call_name(func: ast.expr) -> str | None:
    """Best-effort dotted name for a ``Call.func`` (e.g. ``"os.environ.setdefault"``,
    ``"subprocess.run"``). Returns ``None`` for anything more complex than a
    plain chain of attribute/name lookups — such calls aren't the patterns
    this gate is looking for."""
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    parts.append(func.id)
    return ".".join(reversed(parts))


def _string_constant(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _scan_environ_subscript_assign(node: ast.Assign | ast.AugAssign) -> list[tuple[int, str]]:
    """``os.environ["ANTHROPIC_API_KEY"] = token`` and its ``environ[...]``
    spelling — the most direct way this codebase could smuggle a
    subscription key into the process environment."""
    hits: list[tuple[int, str]] = []
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if not isinstance(target, ast.Subscript) or not _environ_base(target.value):
            continue
        key = _string_constant(target.slice)
        if key is not None and SECRET_KEY_PATTERN.search(key):
            hits.append((node.lineno, f'os.environ[{key!r}] assignment matches a secret key name'))
    return hits


def _scan_call(node: ast.Call) -> list[tuple[int, str, str]]:
    """Returns ``(line, rule, detail)`` triples for the call-shaped patterns:
    ``os.environ.setdefault(...)``, ``os.putenv(...)``, an ``env={...}``
    keyword dict with a secret-shaped key, and a keychain lookup via
    ``subprocess``/``asyncio.create_subprocess_*``."""
    hits: list[tuple[int, str, str]] = []
    name = _dotted_call_name(node.func)

    if name in ("os.environ.setdefault", "environ.setdefault", "os.putenv") and node.args:
        key = _string_constant(node.args[0])
        if key is not None and SECRET_KEY_PATTERN.search(key):
            detail = f"{name}({key!r}, ...) matches a secret key name"
            hits.append((node.lineno, "key-assignment", detail))

    for kw in node.keywords:
        if kw.arg != "env" or not isinstance(kw.value, ast.Dict):
            continue
        for key_node in kw.value.keys:
            key = _string_constant(key_node) if key_node is not None else None
            if key is not None and SECRET_KEY_PATTERN.search(key):
                detail = f"env={{{key!r}: ...}} matches a secret key name"
                hits.append((node.lineno, "key-assignment", detail))

    is_subprocess_call = name is not None and (
        name.startswith("subprocess.") or name.startswith("asyncio.create_subprocess_")
    )
    if is_subprocess_call:
        literals = {_string_constant(a) for a in node.args}
        if "security" in literals and "find-generic-password" in literals:
            detail = f"{name}(...) reads the macOS keychain directly"
            hits.append((node.lineno, "keychain", detail))

    return hits


def scan_source(source: str, filename: str) -> list[Violation]:
    """Scan one file's source text for invariant violations.

    Raises ``SyntaxError`` if ``source`` doesn't parse — a file the gate
    can't read is a failure, not a silent pass.
    """
    tree = ast.parse(source, filename=filename)
    docstring_ids = _docstring_constants(tree)
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            for marker in CREDENTIAL_STORE_MARKERS:
                if marker in node.value:
                    violations.append(
                        Violation(
                            filename=filename,
                            line=node.lineno,
                            rule="credential-store",
                            detail=f"string literal contains {marker!r}",
                        )
                    )
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            for line, detail in _scan_environ_subscript_assign(node):
                violations.append(
                    Violation(filename=filename, line=line, rule="key-assignment", detail=detail)
                )
        elif isinstance(node, ast.Call):
            for line, rule, detail in _scan_call(node):
                violations.append(Violation(filename=filename, line=line, rule=rule, detail=detail))

    violations.sort(key=lambda v: (v.line, v.rule, v.detail))
    return violations


# This module's own definitions necessarily contain the forbidden substrings
# as data: CREDENTIAL_STORE_MARKERS enumerates them, and `_scan_call` compares
# call arguments against the literal pair "security" / "find-generic-password".
# A substring is always "in" itself, so scanning this file would flag its own
# pattern table. It is excluded from `scan_tree` by name for that reason —
# it never touches `os.environ` or spawns a subprocess, so the exclusion
# doesn't open a gap anywhere the invariant actually matters.
_GATE_MODULE_NAME = "invariants.py"


def scan_tree(root: Path, *, skip_dirs: Iterable[str] = ("tests",)) -> list[Violation]:
    """Scan every ``*.py`` file under ``root``, skipping directories named in
    ``skip_dirs`` (and always ``__pycache__``) at any depth below ``root``,
    and this gate's own defining module (see ``_GATE_MODULE_NAME`` above)."""
    skip = set(skip_dirs) | {"__pycache__"}
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == _GATE_MODULE_NAME:
            continue
        if skip & set(path.relative_to(root).parts[:-1]):
            continue
        violations.extend(scan_source(path.read_text(encoding="utf-8"), str(path)))
    return violations


def format_violations(violations: Iterable[Violation]) -> str:
    """Render violations as one line each, followed by a paragraph explaining
    the invariant — this string is what shows up in a failed assertion, so
    it has to be self-contained for whoever reads the CI log."""
    lines = [f"{v.filename}:{v.line} [{v.rule}] {v.detail}" for v in violations]
    lines.append("")
    lines.append(
        "This gate enforces LocalCode's core invariant: the orchestrator spawns "
        "each vendor's own CLI (claude, codex) and never reads a credential "
        "store or holds a subscription API key / OAuth token / session key "
        "itself. Anthropic's February 2026 terms prohibit automating a Claude "
        "subscription through anything but the vendor CLI, and enforcement of "
        "that clause has been live since April 2026 — a violation here risks "
        "every user's subscription, not just a lint failure."
    )
    return "\n".join(lines)
