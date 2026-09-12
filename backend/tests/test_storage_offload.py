"""D13.1 — the session store's filesystem work must not run on the loop.

Every method on ``SessionStore`` is ``async def``, which says nothing about
where the work happens: the pre-fix module called ``open()``, ``json.load()``,
``os.fsync()`` and ``shutil.rmtree()`` inline, so one session's append stalled
*every* session's event delivery. Measured at 0.04 ms average on a local SSD,
so this is a latent defect rather than a live stall — but the same code on a
network filesystem freezes the whole backend for as long as the server takes
to answer.

The test is therefore behavioural rather than a search for ``to_thread`` in
the source: make one filesystem call take 200 ms, run the operation, and watch
whether the loop kept ticking. A stall detector that passes on the pre-fix
module would be measuring nothing, so the RED run is part of the evidence.
"""
from __future__ import annotations

import shutil
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import get_settings
from backend.app.storage.sessions import store

from .fakes.stall_detector import detect_stalls

# How long the patched filesystem call blocks. Large enough that a single
# inline call dwarfs the 50 ms verdict threshold, small enough that the suite
# still runs in seconds.
BLOCK_S = 0.2

# A stalled loop shows gaps at least as long as BLOCK_S; a healthy one shows
# the 5 ms sample period plus scheduler jitter. 50 ms sits between the two by
# an order of magnitude in both directions.
MAX_GAP_MS = 50.0


@contextmanager
def slow_filesystem() -> Iterator[None]:
    """Make every ``Path.open`` and ``shutil.rmtree`` take ``BLOCK_S``.

    ``time.sleep`` is the right stand-in: it holds the calling thread and
    releases the GIL, exactly like a real blocking syscall, so it stalls the
    loop if and only if the call runs on the loop's thread.
    """
    real_open = Path.open
    real_rmtree = shutil.rmtree

    def patched_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        time.sleep(BLOCK_S)
        return real_open(self, *args, **kwargs)

    def patched_rmtree(*args: Any, **kwargs: Any) -> Any:
        time.sleep(BLOCK_S)
        return real_rmtree(*args, **kwargs)

    Path.open = patched_open  # type: ignore[method-assign]
    shutil.rmtree = patched_rmtree
    try:
        yield
    finally:
        Path.open = real_open  # type: ignore[method-assign]
        shutil.rmtree = real_rmtree


async def _assert_off_loop(op: Callable[[], Awaitable[Any]]) -> None:
    async with detect_stalls() as report:
        with slow_filesystem():
            await op()
    assert report.samples > 1, f"watchdog never sampled: {report}"
    assert report.worst_gap_ms < MAX_GAP_MS, f"event loop blocked: {report}"


async def _prepare(home: Path) -> str:
    meta = await store.create_session(
        provider="claude", model="claude-sonnet-4-6", cwd=str(home / "proj")
    )
    await store.append_message(
        meta["id"], {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    )
    return meta["id"]


# Every public coroutine on the store that touches the filesystem. Parametrized
# rather than one test each so a method added later without an offload has an
# obvious place to be covered.
OPERATIONS: dict[str, Callable[[str, Path], Awaitable[Any]]] = {
    "create_session": lambda sid, home: store.create_session(
        provider="claude", model="m", cwd=str(home / "proj2")
    ),
    "get_session": lambda sid, home: store.get_session(sid),
    "list_sessions": lambda sid, home: store.list_sessions(),
    "update_session": lambda sid, home: store.update_session(sid, title="renamed"),
    "append_message": lambda sid, home: store.append_message(
        sid, {"role": "user", "content": [{"type": "text", "text": "x"}]}
    ),
    "write_current": lambda sid, home: store.write_current(
        sid, {"role": "assistant", "content": [{"type": "text", "text": "partial"}]}
    ),
    "list_messages": lambda sid, home: store.list_messages(sid, limit=10),
    "cleanup_expired": lambda sid, home: store.cleanup_expired(retention_days=7, force=True),
    "delete_session": lambda sid, home: store.delete_session(sid),
    "delete_all_sessions": lambda sid, home: store.delete_all_sessions(),
}


@pytest.mark.parametrize("name", sorted(OPERATIONS))
async def test_store_operation_runs_off_the_event_loop(
    name: str, isolated_store: Path
) -> None:
    sid = await _prepare(isolated_store)
    op = OPERATIONS[name]
    await _assert_off_loop(lambda: op(sid, isolated_store))


async def test_fleet_config_parse_runs_off_the_event_loop(
    isolated_store: Path, fresh_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13.6: the cache-miss path reads and parses YAML. Low severity — the
    mtime cache makes it rare — but it is the same defect in kind."""
    from backend.app.orchestrator.fleet.loader import load_fleet_config_async

    cfg_dir = isolated_store / "proj" / ".localcode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "fleet.yaml").write_text("name: offload-test\nmax_steps: 5\n")
    monkeypatch.setenv("LOCALCODE_FLEET_CONFIG", str(cfg_dir / "fleet.yaml"))
    # Warm the settings cache outside the slow-filesystem window: building
    # Settings reads .env, which is not the call under test.
    get_settings()

    async with detect_stalls() as report:
        with slow_filesystem():
            cfg = await load_fleet_config_async(str(isolated_store / "proj"))
    assert cfg.name == "offload-test"
    assert report.samples > 1, f"watchdog never sampled: {report}"
    assert report.worst_gap_ms < MAX_GAP_MS, f"event loop blocked: {report}"
