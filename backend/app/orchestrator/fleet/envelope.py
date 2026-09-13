"""The bounded envelope one fleet step hands back to the orchestrator.

A step used to return its WHOLE transcript: every narrative chunk plus an
uncapped tool digest, straight into the orchestrator's context. Two failures
followed from that. A coder that cats a 2 MB test log spends the rest of the
turn's context window on it (and changes the prompt prefix, so the cache the
rest of the harness keeps warm goes cold for every later turn — see
``artifacts.py``). And a reviewer's verdict had to be re-derived by
string-matching the transcript at every consumer that cared.

``StepResult`` separates the two concerns the transcript conflated:

  * what the orchestrator *reads* — ``summary`` plus a capped tool digest plus
    a pointer to the evicted full output, assembled by ``context_text()``;
  * what the orchestrator *routes on* — ``structured``, the already-parsed
    verdict, so no consumer has to re-parse prose.

``context_text()`` is deliberately the only place that decides what an
orchestrator sees. When eviction is implemented per-caller it drifts: one
caller caps the digest, another forgets, and the forgotten one is the path
that blows the window. Everything that needs step text calls this.

The envelope crosses a process boundary (``subproc.py`` runs each step in its
own OS process), so ``to_wire``/``from_wire`` speak plain JSON-able types
only — no ``Path``, no ``ArtifactRef``. ``from_wire`` tolerates missing keys
rather than raising: a worker that dies mid-protocol should surface as a
degraded envelope the parent can still report, never as a ``KeyError`` inside
the parent's stdout pump.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Ceiling on the tool-activity digest, in characters. The digest is a
# debugging aid for the gates ("did the coder actually write the files?"), so
# it has to be present — but a step with 400 tool calls would otherwise carry
# a digest larger than the narrative it annotates. ``collect_step`` trims to
# this budget call-by-call (keeping whole entries) and ``context_text`` keeps
# a blunt backstop for an envelope built anywhere else.
MAX_TOOL_DIGEST_CHARS = 4000


@dataclass
class StepResult:
    """One sub-agent step's bounded result.

    Fields are ordered as the wire format documents them; ``usage`` is last
    because it is the only optional one.
    """

    # What goes into the orchestrator's context: either the step's full
    # narrative (when it was small enough) or the artifact store's head/tail
    # summary of it.
    summary: str
    # Parsed verdict / metrics, when this role produces any. A gate role
    # carries ``Verdict.to_dict()`` here so consumers route on a value
    # instead of re-parsing prose.
    structured: dict[str, Any] | None
    # Set only when the full output was evicted to the artifact store.
    artifact_id: str | None
    artifact_path: str | None
    # Capped digest of tool activity (see ``MAX_TOOL_DIGEST_CHARS``).
    tool_digest: str
    # Size of the output before summarizing, in UTF-8 bytes. Kept even when
    # nothing was evicted so a caller can see how close a step came to the
    # threshold.
    full_bytes: int
    # Token counts the sub-provider reported on its ``assistant.done``, when
    # it reported any. Task 7's per-turn budget and Task 11's quota governor
    # both read this, so the field exists from the start rather than being
    # bolted on after the wire format has shipped twice.
    usage: dict[str, int] | None = None

    def to_wire(self) -> dict[str, Any]:
        """A JSON-able dict — this crosses the worker process boundary."""
        return {
            "summary": self.summary,
            "structured": self.structured,
            "artifact_id": self.artifact_id,
            "artifact_path": self.artifact_path,
            "tool_digest": self.tool_digest,
            "full_bytes": self.full_bytes,
            "usage": self.usage,
        }

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> StepResult:
        """Rebuild from ``to_wire``'s output.

        Every field is read with a default. A half-written or
        version-skewed payload must degrade to a usable envelope, because
        the alternative is an exception in the parent's stdout pump — which
        the parent reports as the useless "worker exited without a result"
        rather than as the partial result it actually received.
        """
        structured = data.get("structured")
        usage = data.get("usage")
        return cls(
            summary=str(data.get("summary") or ""),
            structured=structured if isinstance(structured, dict) else None,
            artifact_id=_opt_str(data.get("artifact_id")),
            artifact_path=_opt_str(data.get("artifact_path")),
            tool_digest=str(data.get("tool_digest") or ""),
            full_bytes=_as_int(data.get("full_bytes")),
            usage=usage if isinstance(usage, dict) else None,
        )

    def context_text(self) -> str:
        """Exactly what an orchestrator (or a UI card) gets to read.

        Layout: the summary, then the tool digest under the marker
        ``gate.TOOL_DIGEST_MARKER`` matches, then one pointer line when the
        full output was evicted. The marker shape is load-bearing — the gate
        classifier strips from it onward, so a reviewer that emitted ``LGTM``
        above a Bash log still classifies on its own verdict line and not on
        the log's last line.
        """
        digest = self.tool_digest.strip()
        if len(digest) > MAX_TOOL_DIGEST_CHARS:
            # Backstop only: ``collect_step`` trims entry-by-entry. An
            # envelope built elsewhere still cannot smuggle an unbounded
            # digest into context through this method.
            digest = digest[:MAX_TOOL_DIGEST_CHARS] + "… (digest truncated)"

        summary = self.summary.strip()
        if summary and digest:
            body = f"{summary}\n\n---\n{digest}"
        else:
            body = summary or digest

        if self.artifact_id:
            pointer = (
                f"[full output evicted: {self.full_bytes} bytes, artifact "
                f"{self.artifact_id[:12]} at {self.artifact_path} — read that "
                f"file if you need the detail]"
            )
            body = f"{body}\n\n{pointer}" if body else pointer
        return body


def _opt_str(value: Any) -> str | None:
    """``None`` stays ``None``; anything else becomes a string. Keeps a JSON
    ``null`` from turning into the string ``"None"`` on the way back in."""
    return None if value is None else str(value)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
