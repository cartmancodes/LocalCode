"""Shared fixtures for the backend test suite.

Two failure modes these fixtures exist to prevent:

  * A test that monkeypatches an env var but reads a ``Settings`` instance
    built (and cached) by an earlier test — ``lru_cache`` on ``get_settings``
    means the env var change is silently ignored unless the cache is cleared
    around the test.
  * A test that writes to a real ``~/.localcode`` directory because nothing
    redirected ``HOME`` — polluting the developer's machine (or, in CI,
    reading whatever happens to be there).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import get_settings


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def app_root(repo_root: Path) -> Path:
    return repo_root / "backend" / "app"


@pytest.fixture
def fresh_settings():
    """Clear the ``get_settings`` cache before and after the test so a
    monkeypatched env var is actually observed, and so this test's settings
    don't leak into the next one."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_localcode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME at a throwaway directory so anything writing under
    ``~/.localcode`` (session index, cleanup sentinel, ...) is contained to
    the test and never touches the developer's real home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path
