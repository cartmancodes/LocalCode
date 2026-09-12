"""Tests for the auth-invariant scanner (`backend/app/invariants.py`).

Each rule gets a unit test against a minimal bad snippet — these are the
specification of what the gate catches. The final test is the gate itself:
`scan_tree` must return no violations against today's `backend/app/`, or the
whole plan built on top of this task is unverifiable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.invariants import (
    CREDENTIAL_STORE_MARKERS,
    Violation,
    format_violations,
    scan_source,
    scan_tree,
)


def test_credential_store_marker_fires_on_bad_read() -> None:
    source = 'from pathlib import Path\n\ntoken_path = Path("~/.claude/.credentials.json")\n'
    violations = scan_source(source, "bad.py")
    assert any(v.rule == "credential-store" for v in violations)


def test_key_assignment_fires_on_environ_subscript() -> None:
    source = (
        "import os\n\n"
        'def leak(token: str) -> None:\n    os.environ["ANTHROPIC_API_KEY"] = token\n'
    )
    violations = scan_source(source, "bad.py")
    assert any(v.rule == "key-assignment" for v in violations)


def test_key_assignment_fires_on_environ_setdefault() -> None:
    source = 'import os\n\nos.environ.setdefault("OPENAI_API_KEY", "sk-test")\n'
    violations = scan_source(source, "bad.py")
    assert any(v.rule == "key-assignment" for v in violations)


def test_key_assignment_fires_on_env_kwarg_dict() -> None:
    source = (
        "import subprocess\n\n"
        'subprocess.run(["echo"], env={"ANTHROPIC_API_KEY": "sk-test"})\n'
    )
    violations = scan_source(source, "bad.py")
    assert any(v.rule == "key-assignment" for v in violations)


def test_keychain_fires_on_security_find_generic_password() -> None:
    source = (
        "import asyncio\n\n"
        "async def read_keychain() -> None:\n"
        '    await asyncio.create_subprocess_exec("security", "find-generic-password", "-w")\n'
    )
    violations = scan_source(source, "bad.py")
    assert any(v.rule == "keychain" for v in violations)


def test_clean_source_has_no_violations() -> None:
    source = (
        "from __future__ import annotations\n\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
    )
    assert scan_source(source, "clean.py") == []


def test_docstring_mentioning_credentials_json_is_exempt() -> None:
    # Guards the false positive that would otherwise make the gate unusable
    # against claude.py / base.py / opencode.py, which legitimately document
    # the auth model (including the literal path a vendor CLI reads).
    source = (
        '"""The CLI reads its token from ~/.claude/.credentials.json."""\n'
        "from __future__ import annotations\n\n"
        "def noop() -> None:\n"
        "    pass\n"
    )
    assert scan_source(source, "documented.py") == []


def test_class_docstring_mentioning_auth_json_is_exempt() -> None:
    source = (
        "from __future__ import annotations\n\n"
        "class Provider:\n"
        '    """Reads its own OAuth credentials from ~/.local/share/opencode/auth.json."""\n\n'
        "    name: str\n"
    )
    assert scan_source(source, "documented.py") == []


def test_unparseable_source_raises_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        scan_source("def broken(:\n", "broken.py")


def test_format_violations_includes_each_line_and_a_why_paragraph() -> None:
    violations = [Violation(filename="f.py", line=3, rule="credential-store", detail="detail")]
    rendered = format_violations(violations)
    assert "f.py:3 [credential-store] detail" in rendered
    assert "Anthropic" in rendered


def test_credential_store_markers_cover_the_documented_set() -> None:
    # Sanity check on the fixture itself rather than the scanner: if this
    # list drifts from the brief, every test above silently tests less.
    assert "ANTHROPIC_API_KEY" in CREDENTIAL_STORE_MARKERS
    assert "find-generic-password" in CREDENTIAL_STORE_MARKERS


def test_gate_passes_against_the_real_app_tree(app_root: Path) -> None:
    violations = scan_tree(app_root)
    assert violations == [], format_violations(violations)


def test_scan_tree_excludes_its_own_gate_module(app_root: Path) -> None:
    # invariants.py necessarily lists every forbidden substring as data (that
    # IS the marker table); scanning it would flag its own definitions. It's
    # excluded from the walk by name, not silently dropped by accident.
    scanned = {str(v.filename) for v in scan_tree(app_root)}
    assert not any(name.endswith("invariants.py") for name in scanned)
    # And scan_source on it directly, unfiltered, does self-flag — proving
    # the exclusion in scan_tree is doing real work, not a no-op.
    source = (app_root / "invariants.py").read_text(encoding="utf-8")
    assert any(
        v.rule == "credential-store" for v in scan_source(source, "invariants.py")
    )
