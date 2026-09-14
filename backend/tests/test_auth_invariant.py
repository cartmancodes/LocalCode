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
    # against claude.py / base.py / codex/, which legitimately document the
    # auth model (including the literal path a vendor CLI reads).
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
        '    """Reads its own OAuth credentials from ~/.codex/auth.json."""\n\n'
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


def test_decoy_invariants_module_is_not_exempt(tmp_path: Path) -> None:
    """A file merely *named* invariants.py elsewhere in the tree must not get
    a free pass. Only specific AST nodes (the pattern-table literals) are
    exempt — never a whole file by filename, which would be a trivial
    escape hatch for a real leak (just name your file invariants.py)."""
    decoy_dir = tmp_path / "sub"
    decoy_dir.mkdir()
    decoy = decoy_dir / "invariants.py"
    decoy.write_text(
        'import os\n\nos.environ["ANTHROPIC_API_KEY"] = "leaked"\n',
        encoding="utf-8",
    )
    violations = scan_tree(tmp_path)
    assert any(v.rule == "key-assignment" for v in violations)


def test_pattern_table_exemption_is_node_scoped_not_file_scoped() -> None:
    """Exempting CREDENTIAL_STORE_MARKERS's own literal elements must not
    exempt the rest of the module those literals live in. A real credential
    leak added anywhere else in the same file — even reusing the exact same
    string value — is still caught, proving the exemption is scoped to
    specific AST node identities, not to a filename or a string value."""
    source = (
        "from __future__ import annotations\n\n"
        "import os\n\n"
        "CREDENTIAL_STORE_MARKERS: tuple[str, ...] = (\n"
        '    "auth.json",\n'
        '    "ANTHROPIC_API_KEY",\n'
        ")\n\n"
        "def leak(token: str) -> None:\n"
        '    os.environ["ANTHROPIC_API_KEY"] = token\n'
    )
    violations = scan_source(source, "invariants.py")

    # The pattern table's own elements (lines 6-7) are exempt.
    assert not any(v.line in (6, 7) for v in violations)

    # The identical string, used for a real assignment elsewhere in the same
    # file (line 11), is still caught on both rules.
    assert any(v.rule == "credential-store" and v.line == 11 for v in violations)
    assert any(v.rule == "key-assignment" and v.line == 11 for v in violations)
