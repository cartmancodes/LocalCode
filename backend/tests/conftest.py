"""Shared fixtures for the backend test suite.

Failure modes these fixtures exist to prevent:

  * A test that monkeypatches an env var but reads a ``Settings`` instance
    built (and cached) by an earlier test — ``lru_cache`` on ``get_settings``
    means the env var change is silently ignored unless the cache is cleared
    around the test.
  * A test that writes to a real ``~/.localcode`` directory because nothing
    redirected ``HOME`` — polluting the developer's machine (or, in CI,
    reading whatever happens to be there). This used to be opt-in (a test
    had to request ``tmp_localcode``), and Task 5's provider tests proved
    opt-in isn't good enough: a test can construct a production object (a
    ``ClaudeProvider``, and behind it a ``UsageLog``) without knowing that
    object reaches ``Path.home()`` internally, and never request the
    fixture that would have protected it. ``_redirect_home`` below is
    autouse — HOME is redirected for every test in this suite, full stop,
    so forgetting a fixture is no longer a way to reach the real home. See
    ``test_home_isolation.py`` for the guard that keeps this honest.
  * A test that redirects ``HOME`` and still writes to the real
    ``~/.localcode`` because ``storage.sessions`` resolved its paths from
    ``Path.home()`` at *import* time. ``isolated_store`` repoints those
    module constants, which is the only thing that actually contains it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import get_settings
from backend.app.storage import sessions as sessions_mod


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


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME at a throwaway directory for every test, unconditionally.

    Not opt-in: a test that constructs a production object which reaches
    ``Path.home()`` several calls deep (``ClaudeProvider`` -> ``UsageLog``,
    for instance) has no way to know it needs to and may never request
    ``tmp_localcode``. Making the redirect autouse means forgetting a
    fixture is no longer a way to append to, or read, the developer's real
    ``~/.localcode``. See ``test_home_isolation.py`` for the guard that
    keeps this fixture honest.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def tmp_localcode(_redirect_home: Path) -> Path:
    """Name kept for existing tests/readability: the throwaway HOME that
    ``_redirect_home`` (autouse) already set up for this test."""
    return _redirect_home


@pytest.fixture
def isolated_store(tmp_localcode: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the session store's user-global paths at a throwaway directory.

    ``storage.sessions`` computes ``USER_GLOBAL_DIR`` and friends from
    ``Path.home()`` when the module is imported, so a ``HOME`` monkeypatch
    alone leaves the index, the ``_global`` bucket and the cleanup sentinel
    pointing at the developer's real home. Returns the fake home; session
    dirs land under ``<returned>/proj/.localcode/sessions/<id>/`` when a test
    passes ``cwd=str(home / "proj")``.
    """
    root = tmp_localcode / ".localcode"
    monkeypatch.setattr(sessions_mod, "USER_GLOBAL_DIR", root)
    monkeypatch.setattr(sessions_mod, "INDEX_PATH", root / "sessions-index.json")
    monkeypatch.setattr(sessions_mod, "GLOBAL_SESSIONS_DIR", root / "sessions" / "_global")
    monkeypatch.setattr(sessions_mod, "CLEANUP_SENTINEL", root / "sessions" / ".last-cleanup")
    return tmp_localcode
