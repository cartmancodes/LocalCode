"""Assembles a single assistant turn's message blocks and persists
mid-turn checkpoints.

This centralizes logic that was previously duplicated across the turn loop:
flushing the streaming-text buffer into a block (it appeared three times),
the checkpoint-persist closure, and the dangling-tool_use repair. One place,
one set of invariants.

Checkpoints are throttled. A checkpoint writes the *whole* message
accumulated so far, so writing one per tool boundary costs
O(boundaries x final size) — a 200-tool turn ending at 2 MB wrote ~200 MB.
The throttle keeps total checkpoint volume proportional to the final message
instead, while still bounding how much work a crash can discard.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..config import get_settings
from ..storage.sessions import store as session_store

logger = logging.getLogger(__name__)

# Stand-in result for a tool_use whose turn ended before a result arrived
# (cancellation / backend crash). Keeps persisted history with well-formed
# tool_use/tool_result pairs so a reload doesn't choke on a dangling call.
_INCOMPLETE_RESULT = (
    "Step did not complete — turn ended before a result was produced "
    "(cancellation or backend crash). Re-send the prompt to retry."
)


def _approx_size(value: Any) -> int:
    """Cheap stand-in for the serialized size of one block.

    The throttle only needs to know when the message has grown by tens of
    kilobytes, so counting the characters in the strings involved is close
    enough — and it runs once per block added, not once per checkpoint over
    the whole snapshot, which is the cost it exists to avoid.
    """
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(len(str(k)) + _approx_size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_approx_size(v) for v in value)
    return 16  # numbers, bools, None — small and fixed-ish


class TurnAccumulator:
    """Builds the assistant turn incrementally and writes idempotent
    checkpoints keyed by a stable message id.

    Text deltas accumulate in a buffer that is only promoted to a real block
    on a tool boundary or at end-of-turn — mirrors how the chat UI renders a
    contiguous text run as one block.
    """

    __slots__ = (
        "blocks",
        "_text_buf",
        "cost_usd",
        "duration_ms",
        "_message_id",
        "_size",
        "_last_write_at",
        "_last_write_size",
    )

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self._text_buf: list[str] = []
        self.cost_usd: float | None = None
        self.duration_ms: int | None = None
        self._message_id: str | None = None
        # Running size estimate of the message, plus what it was the last time
        # we wrote. Maintained as blocks arrive so the throttle never has to
        # serialize the snapshot just to decide whether to skip it.
        self._size = 0
        self._last_write_at: float | None = None
        self._last_write_size = 0

    # ── streaming text ───────────────────────────────────────────────────
    def add_text(self, text: str) -> None:
        self._text_buf.append(text)
        self._size += len(text)

    def flush_text(self) -> None:
        """Promote buffered text to a block (destructive). No-op if empty."""
        if self._text_buf:
            self.blocks.append({"type": "text", "text": "".join(self._text_buf)})
            self._text_buf = []

    # ── tool blocks ──────────────────────────────────────────────────────
    def add_tool_use(self, data: dict[str, Any]) -> None:
        self.blocks.append({"type": "tool_use", **data})
        self._size += _approx_size(data)

    def add_tool_result(self, data: dict[str, Any]) -> None:
        self.blocks.append({"type": "tool_result", **data})
        self._size += _approx_size(data)

    def set_done(self, *, cost_usd: float | None, duration_ms: int | None) -> None:
        self.cost_usd = cost_usd
        self.duration_ms = duration_ms

    # ── persistence ──────────────────────────────────────────────────────
    def _snapshot(self) -> list[dict[str, Any]]:
        """Non-destructive view = committed blocks + any buffered text as a
        trailing block. Used for checkpoints so text keeps accumulating."""
        snap = list(self.blocks)
        if self._text_buf:
            snap.append({"type": "text", "text": "".join(self._text_buf)})
        return snap

    def _should_write(self) -> bool:
        """Is this mid-turn checkpoint worth its bytes?

        Two arms, either of which is enough:

          * **Growth**, amortized against the last checkpoint's size. A fixed
            byte threshold on its own still rewrites a growing message
            ``size / threshold`` times — that is the quadratic volume we are
            removing. Requiring the message to have grown by at least as much
            as the last write caps total checkpoint bytes at roughly twice the
            final message, however long the turn runs.
          * **Time**, while the message is still smaller than the growth
            floor. A slow turn that emits very little must still be
            recoverable, and rewriting something under 64 KiB every couple of
            seconds costs nothing. Above that floor the growth arm governs, so
            the time arm can never dominate the write volume.
        """
        s = get_settings()
        growth = self._size - self._last_write_size
        if growth <= 0:
            return False  # nothing new to protect
        if growth >= max(s.checkpoint_min_growth_bytes, self._last_write_size):
            return True
        elapsed = (
            float("inf")
            if self._last_write_at is None
            else time.monotonic() - self._last_write_at
        )
        return (
            elapsed >= s.checkpoint_min_interval_s
            and self._size <= s.checkpoint_min_growth_bytes
        )

    async def checkpoint(self, session_id: str, *, final: bool = False) -> None:
        """Persist the turn so far under a stable message id.

        Mid-turn checkpoints overwrite ``current.json`` (atomic replace, no
        fsync, no ``updated_at`` bump) and are throttled by
        ``_should_write``. ``final=True`` — used by the turn's ``finally``
        clause — always writes: it appends the message to ``messages.jsonl``
        once, fsyncs, clears ``current.json`` and bumps ``updated_at`` so the
        sidebar reflects the activity.
        """
        # Cheap emptiness/throttle checks BEFORE building the snapshot: a
        # skipped write must not pay for the copy it isn't going to use.
        # ``not self.blocks and not self._text_buf`` is exactly the condition
        # under which ``_snapshot()`` would come back empty.
        if not self.blocks and not self._text_buf:
            return
        if not final and not self._should_write():
            return
        flushed = self._snapshot()
        try:
            payload: dict[str, Any] = {
                "role": "assistant",
                "content": flushed,
                "cost_usd": self.cost_usd,
                "duration_ms": self.duration_ms,
            }
            if self._message_id is not None:
                payload["id"] = self._message_id
            if final:
                stored = await session_store.append_message(
                    session_id, payload, bump_updated_at=True
                )
            else:
                stored = await session_store.write_current(session_id, payload)
            if self._message_id is None:
                self._message_id = stored["id"]
            self._last_write_at = time.monotonic()
            self._last_write_size = self._size
        except FileNotFoundError:
            # Session was deleted while the turn was still draining
            # (drop_runner cancelled us). Persistence is moot at this point —
            # skip silently rather than spewing a traceback.
            pass
        except Exception:
            logger.exception("checkpoint persist failed for %s", session_id)

    def synthesize_missing_results(self) -> None:
        """Append an error tool_result for every tool_use that never got one
        (turn ended mid-step). Keeps persisted pairs well-formed."""
        fulfilled = {
            b.get("tool_use_id")
            for b in self.blocks
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        for b in list(self.blocks):
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("id")
                and b["id"] not in fulfilled
            ):
                self.blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": _INCOMPLETE_RESULT,
                        "is_error": True,
                    }
                )
                fulfilled.add(b["id"])
