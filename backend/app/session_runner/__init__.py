"""Per-session turn runner — decoupled from any client connection.

The WS handler used to own turn execution: opening a WS, taking a per-session
asyncio.Lock, awaiting ``provider.run()``, and forwarding events to the
socket. That meant a turn died the moment the WS closed (session switch, tab
close, network blip), even if the user wanted the pipeline to keep running.

This package flips the ownership: each session has a ``SessionRunner`` whose
lifecycle is tied to the session, not to a connection. A turn runs as an
independent ``asyncio.Task`` owned by the runner, broadcasting events to any
number of subscribers. WS handlers become viewers — they ``subscribe()`` for
live events on connect and ``unsubscribe()`` on disconnect; the turn keeps
running across reconnects, and a second tab attaching mid-turn streams the
live tail.

Robustness affordances:

  - **Replay buffer**: every broadcast event is stamped with a monotonic
    ``_id`` and stashed in a bounded ring. A reconnecting client passes
    ``?since=<id>`` and gets the tail it missed without refetching the whole
    message log.
  - **Bounded subscriber queues**: a slow consumer can't pin memory; once
    its queue fills we drop further events for that subscriber (the client
    can recover via ``loadMessages`` on its next reconnect).
  - **Synthetic tool_result on dangling tool_use**: guarantees persisted
    messages have well-formed tool_use/tool_result pairs even when a turn
    ends via cancellation.

Originally one ~415-line module; split by concern. The import surface is
unchanged — this facade re-exports everything the rest of the app imports:

  config.py       buffer / queue size constants
  bus.py          ``EventBus`` — subscribe / replay / fan-out
  accumulator.py  ``TurnAccumulator`` — message assembly + checkpoints
  turn.py         ``execute_turn`` — drain + persist + finalize pipeline
  runner.py       ``SessionRunner`` — lock, approval channel, task handle
  registry.py     ``get_runner`` / ``drop_runner`` / ``drop_all_runners``
"""
from __future__ import annotations

from .accumulator import TurnAccumulator
from .bus import EventBus
from .config import (
    _REPLAY_BUFFER_SIZE,
    _SUBSCRIBER_QUEUE_MAX,
    REPLAY_BUFFER_SIZE,
    SUBSCRIBER_QUEUE_MAX,
)
from .registry import drop_all_runners, drop_runner, get_runner
from .runner import SessionRunner
from .turn import execute_turn

__all__ = [
    "SessionRunner",
    "get_runner",
    "drop_runner",
    "drop_all_runners",
    # building blocks (exported for tests / advanced use)
    "EventBus",
    "TurnAccumulator",
    "execute_turn",
    "REPLAY_BUFFER_SIZE",
    "SUBSCRIBER_QUEUE_MAX",
    # back-compat aliases
    "_REPLAY_BUFFER_SIZE",
    "_SUBSCRIBER_QUEUE_MAX",
]
