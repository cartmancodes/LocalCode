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


def test_the_user_global_files_resolve_under_the_redirected_home(
    tmp_path: Path,
) -> None:
    """Every ``~/.localcode`` file the app writes, named here so adding one
    that resolves its path at import time fails in this file rather than by
    quietly appearing in the developer's real home.

    ``quota.json`` is the one this most recently caught out: the governor is
    built from settings on first use, and an ``lru_cache``d instance created
    before the redirect would keep the real path for the whole session.
    """
    from backend.app.quota import Governor, default_quota_path
    from backend.app.usage import UsageLog, default_usage_log_path

    localcode = tmp_path / ".localcode"
    assert default_usage_log_path() == localcode / "usage.jsonl"
    assert UsageLog().path == localcode / "usage.jsonl"
    assert default_quota_path() == localcode / "quota.json"
    assert Governor().path == localcode / "quota.json"
