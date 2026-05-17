"""Assembles a single assistant turn's message blocks and persists
mid-turn checkpoints.

This centralizes logic that was previously duplicated across the turn loop:
flushing the streaming-text buffer into a block (it appeared three times),
the checkpoint-persist closure, and the dangling-tool_use repair. One place,
one set of invariants.
"""
from __future__ import annotations

import logging
from typing import Any

from ..storage.sessions import store as session_store

logger = logging.getLogger(__name__)

# Stand-in result for a tool_use whose turn ended before a result arrived
# (cancellation / backend crash). Keeps persisted history with well-formed
# tool_use/tool_result pairs so a reload doesn't choke on a dangling call.
_INCOMPLETE_RESULT = (
    "Step did not complete — turn ended before a result was produced "
    "(cancellation or backend crash). Re-send the prompt to retry."
)


class TurnAccumulator:
    """Builds the assistant turn incrementally and writes idempotent
    checkpoints keyed by a stable message id.

    Text deltas accumulate in a buffer that is only promoted to a real block
    on a tool boundary or at end-of-turn — mirrors how the chat UI renders a
    contiguous text run as one block.
    """

    __slots__ = ("blocks", "_text_buf", "cost_usd", "duration_ms", "_message_id")

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self._text_buf: list[str] = []
        self.cost_usd: float | None = None
        self.duration_ms: int | None = None
        self._message_id: str | None = None

    # ── streaming text ───────────────────────────────────────────────────
    def add_text(self, text: str) -> None:
        self._text_buf.append(text)

    def flush_text(self) -> None:
        """Promote buffered text to a block (destructive). No-op if empty."""
        if self._text_buf:
            self.blocks.append({"type": "text", "text": "".join(self._text_buf)})
            self._text_buf = []

    # ── tool blocks ──────────────────────────────────────────────────────
    def add_tool_use(self, data: dict[str, Any]) -> None:
        self.blocks.append({"type": "tool_use", **data})

    def add_tool_result(self, data: dict[str, Any]) -> None:
        self.blocks.append({"type": "tool_result", **data})

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

    async def checkpoint(self, session_id: str) -> None:
        """Append an idempotent snapshot of the turn so far. Reuses a stable
        message id across checkpoints so the store dedups to the latest."""
        flushed = self._snapshot()
        if not flushed:
            return
        try:
            payload: dict[str, Any] = {
                "role": "assistant",
                "content": flushed,
                "cost_usd": self.cost_usd,
                "duration_ms": self.duration_ms,
            }
            if self._message_id is not None:
                payload["id"] = self._message_id
            stored = await session_store.append_message(session_id, payload)
            if self._message_id is None:
                self._message_id = stored["id"]
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
