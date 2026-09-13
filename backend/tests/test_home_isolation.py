"""Guard for conftest's autouse HOME redirect.

Task 5's provider tests (``test_claude_client_reuse.py``,
``test_approvals.py``) construct a ``ClaudeProvider`` without ever
requesting ``tmp_localcode``, and that provider's ``UsageLog`` reaches
``Path.home()`` several calls deep. Nothing about that test file *looks*
like it touches the filesystem, which is exactly how it silently appended
~30 entries per suite run to the developer's real ``~/.localcode/usage.jsonl``
before ``conftest._redirect_home`` became autouse.

This test exists so a future conftest edit that narrows or removes that
autouse fixture fails loudly here, instead of quietly reopening the same
hole.
"""
from __future__ import annotations

from pathlib import Path


def test_home_is_redirected_for_every_test(tmp_path: Path) -> None:
    assert Path.home() == tmp_path
