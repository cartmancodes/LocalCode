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
  * A test that HANGS, taking the whole run with it and leaving nothing behind
    to diagnose. Opt-in (``LOCALCODE_TEST_TIMEOUT_S``) — see
    ``_per_test_timeout`` — because a wall clock on every local run is a flake
    generator, while a run that can hang forever is how a rare deadlock gets
    filed as "CI was slow".
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
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


# ─────────────────────────────────────────────────────────────────────────────
# A hard wall clock per test, opt-in
# ─────────────────────────────────────────────────────────────────────────────

_TIMEOUT_ENV = "LOCALCODE_TEST_TIMEOUT_S"

# How long after the dump a wedged test is given to come back before the run is
# aborted. A test that unwedges here still fails — cleanly, with a summary —
# which is the better outcome, so the grace is generous relative to the dump
# itself and tiny relative to any sane timeout.
_UNWEDGE_GRACE_S = 5.0

# Exit code for "a test wedged and the run was aborted". Distinct from
# pytest's own (1 = tests failed, 2 = interrupted) so a CI log can tell this
# apart from an ordinary failure without parsing anything.
WEDGED_EXIT_CODE = 99


@pytest.fixture(autouse=True)
def _per_test_timeout(request: pytest.FixtureRequest) -> object:
    """Bound every test by ``LOCALCODE_TEST_TIMEOUT_S``, with the stacks.

    Why this exists: during Task 5 a full-suite run under
    ``-W error::UserWarning`` hung forever at the third test in collection
    order, once, while a second pytest process ran beside it. Eight bounded
    reruns never reproduced it. A hang that rare is only ever caught by a run
    that cannot hang — and only diagnosable if the run leaves the stacks
    behind. ``make soak`` sets the variable; nothing else does, because a wall
    clock on every local run is a flake generator.

    Two outcomes, because there are two kinds of overrun and they need
    different endings:

      * the test is slow but alive — the watchdog dumps, the test finishes,
        and the fixture fails it with a message. The run continues and every
        other test still reports;
      * the test is WEDGED — nothing on the main thread will ever run again,
        so no fixture of ours can fail it and no assertion can be reached. The
        watchdog dumps, waits ``_UNWEDGE_GRACE_S``, and aborts the process with
        :data:`WEDGED_EXIT_CODE`. A run that ends in 300 s naming the test and
        carrying every thread's stack is worth incomparably more than one that
        has to be killed by hand the next morning with nothing to show.

    Why a thread and not ``pytest-timeout``: no new dependency is allowed, and
    a thread is what that plugin's "dump and fail" mode uses anyway. Why not
    ``signal.alarm``: signals reach the main thread only while it is running
    Python, which is precisely what a wedge in a blocking syscall is not doing.
    """
    raw = os.environ.get(_TIMEOUT_ENV)
    if not raw:
        yield
        return
    try:
        limit_s = float(raw)
    except ValueError:
        pytest.fail(f"{_TIMEOUT_ENV}={raw!r} is not a number of seconds")
    if limit_s <= 0:
        yield
        return

    expired = threading.Event()
    finished = threading.Event()
    node_id = request.node.nodeid

    def watch() -> None:
        if finished.wait(limit_s):
            return
        expired.set()
        print(
            f"\n[timeout] {node_id} exceeded {limit_s:g}s — dumping every "
            "thread's stack",
            file=sys.stderr,
            flush=True,
        )
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        if finished.wait(_UNWEDGE_GRACE_S):
            return  # it came back; the fixture below fails it properly
        print(
            f"[timeout] {node_id} is still wedged {_UNWEDGE_GRACE_S:g}s later — "
            f"aborting the run with exit {WEDGED_EXIT_CODE}",
            file=sys.stderr,
            flush=True,
        )
        sys.stderr.flush()
        os._exit(WEDGED_EXIT_CODE)

    watchdog = threading.Thread(target=watch, name=f"timeout:{request.node.name}")
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    finally:
        finished.set()
        watchdog.join(timeout=1.0)
    if expired.is_set():
        pytest.fail(
            f"test exceeded the {limit_s:g}s wall clock set by {_TIMEOUT_ENV} "
            "(stacks dumped to stderr above)"
        )
