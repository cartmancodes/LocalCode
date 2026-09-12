"""Module-level registry — one ``SessionRunner`` per active session.

Runners are created lazily on first WS connect and torn down when the
session is deleted (or all sessions wiped). Guarded by a lock so concurrent
WS handlers can't double-create a runner for the same id.

Two invariants the registry enforces, both about *generations* of a session:

  * **At most one live runner per session id.** Dropping a runner retires it
    before popping it, and a retired runner refuses to start turns. Without
    that, a WS handler holding a reference to a dropped runner could start a
    turn beside the turn of the runner that replaced it — two turns for one
    session, each with its own lock and its own bus, so neither can see the
    other's events or serialise against it.
  * **No resurrection.** ``get_runner`` will not build a runner for a session
    whose directory is gone; the caller reports "session not found" exactly as
    it already does for an id it has never seen. Resurrecting one hands a
    viewer a live bus for a session that can never persist anything again.

``cancel_turn`` is bounded (so ``DELETE`` cannot hang), which means a wedged
turn is *detached* rather than stopped. Detached tasks are kept here so
shutdown can make one final attempt to cancel them — each may still own a
sub-provider child process, and that process is what keeps billing the user.
"""
from __future__ import annotations

import asyncio
import logging

from ..storage.sessions import store as session_store
from .runner import SessionRunner

logger = logging.getLogger(__name__)

_runners: dict[str, SessionRunner] = {}
_runners_lock = asyncio.Lock()

# Turn tasks that outlived their grace window. Self-pruning: each task
# discards itself when it finally completes, so this cannot grow unbounded.
_detached_turns: set[asyncio.Task[None]] = set()

# How long shutdown's final attempt waits on a detached turn. Short — the
# point is to give a turn that *can* unwind its last chance to run its
# `finally` (which kills the child process), not to block the exit.
_DETACHED_REAP_S = 2.0


async def get_runner(session_id: str) -> SessionRunner | None:
    """Get-or-create a runner for ``session_id``.

    Returns None if the session no longer exists, so a connect that races a
    delete reports "session not found" instead of resurrecting the session id
    with a brand-new runner.
    """
    async with _runners_lock:
        runner = _runners.get(session_id)
        if runner is not None:
            return runner
        # Read inside the lock: it's a single small file read, and holding the
        # lock is what keeps two concurrent WS connects from each deciding the
        # session exists and building a runner for it.
        if await session_store.get_session(session_id) is None:
            return None
        runner = SessionRunner(session_id)
        _runners[session_id] = runner
        return runner


async def drop_runner(session_id: str) -> None:
    """Retire the runner, cancel any running turn, and forget it.

    Called by the session-delete route. Retiring happens before the pop so a
    caller still holding this runner cannot start a turn on a session that is
    being deleted.
    """
    async with _runners_lock:
        runner = _runners.get(session_id)
        if runner is not None:
            runner.retire()
            del _runners[session_id]
    if runner is not None:
        await _cancel_and_track(runner)


async def drop_all_runners() -> None:
    """Cancel every running turn — used by the wipe-all-sessions route and by
    the lifespan shutdown path.

    On shutdown this must run *before* the providers are closed: a turn that is
    still draining when its provider goes away never reaches the ``finally``
    that kills its sub-provider child, and that child is the orphan.
    """
    async with _runners_lock:
        runners = list(_runners.values())
        for r in runners:
            r.retire()
        _runners.clear()
    # Concurrently, not in sequence: a wedged turn costs the full grace window,
    # so cancelling ten of them one after another would add ten grace windows
    # to a Ctrl-C.
    await asyncio.gather(*(_cancel_and_track(r) for r in runners))
    await _reap_detached_turns()


async def _cancel_and_track(runner: SessionRunner) -> None:
    detached = await runner.cancel_turn()
    if detached is not None:
        _detached_turns.add(detached)
        # Self-pruning, so a long-lived backend's set of detached turns is
        # bounded by the number currently wedged, not by the number ever
        # detached.
        detached.add_done_callback(_detached_turns.discard)


async def _reap_detached_turns() -> None:
    """One last attempt at the turns that outlived their grace window.

    A turn detached minutes ago may well be unwedged by now — cancelling it
    again here lets its ``finally`` run while the loop is still alive, which is
    the last moment anything can kill its child process.
    """
    tasks = {t for t in _detached_turns if not t.done()}
    if not tasks:
        return
    for t in tasks:
        t.cancel()
    _done, pending = await asyncio.wait(tasks, timeout=_DETACHED_REAP_S)
    if pending:
        logger.warning(
            "%d detached turn task(s) survived a second cancellation; their "
            "sub-provider child processes may outlive this backend",
            len(pending),
        )
