"""Module-level registry — one ``SessionRunner`` per active session.

Runners are created lazily on first WS connect and torn down when the
session is deleted (or all sessions wiped). Guarded by a lock so concurrent
WS handlers can't double-create a runner for the same id.
"""
from __future__ import annotations

import asyncio

from .runner import SessionRunner

_runners: dict[str, SessionRunner] = {}
_runners_lock = asyncio.Lock()


async def get_runner(session_id: str) -> SessionRunner:
    """Get-or-create a runner for ``session_id``."""
    async with _runners_lock:
        runner = _runners.get(session_id)
        if runner is None:
            runner = SessionRunner(session_id)
            _runners[session_id] = runner
        return runner


async def drop_runner(session_id: str) -> None:
    """Cancel any running turn and forget the runner.

    Called by the session-delete route so a fresh runner is created if a new
    session reuses the same id (shouldn't happen with UUIDs, but the
    invariant is cheap to maintain).
    """
    async with _runners_lock:
        runner = _runners.pop(session_id, None)
    if runner is not None:
        await runner.cancel_turn()


async def drop_all_runners() -> None:
    """Cancel every running turn — used by the wipe-all-sessions route."""
    async with _runners_lock:
        runners = list(_runners.values())
        _runners.clear()
    for r in runners:
        await r.cancel_turn()
