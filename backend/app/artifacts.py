"""Content-addressed storage for large step/tool output.

A fleet step or a tool call can legitimately produce megabytes of text (a
build log, a full test run). Handing that to the model on every turn would
blow the context window and, worse, poison the prompt cache the rest of the
harness (Task 4) works hard to keep warm — a single oversized turn changes
the transcript prefix for every turn after it. So large output is written
once to disk, keyed by the sha256 of its bytes, and only a small head/tail
summary plus a pointer back to the artifact goes into context. Identical
content (a retried step that fails the same way twice) dedupes to one file
instead of accumulating copies.

Writes use the tmp-then-``os.replace`` idiom from
``storage/sessions.py._atomic_write_text``: a crash mid-write must never leave
a half-written artifact for ``get_text`` to hand back as if it were complete.
Failures are allowed to raise ``OSError`` — a store that silently drops data
a caller believes was saved is worse than one that surfaces the failure.

The default root is resolved from ``Path.home()`` *inside* ``ArtifactStore``'s
constructor, not as a module-level constant or a mutable default-argument
expression, both of which are evaluated once at import time. A test that
redirects ``HOME`` (see ``conftest.tmp_localcode``) constructs its store after
that redirect, so the store has to compute the default the first time it is
actually asked for one.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings


def default_artifact_root() -> Path:
    """Where artifacts live when nothing overrides it.

    Reads the (possibly test-overridden) settings for an explicit root,
    falling back to ``~/.localcode/artifacts``. Called lazily by
    ``ArtifactStore.__init__``, never at import time.
    """
    override = get_settings().artifact_root
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".localcode" / "artifacts"


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer to one stored artifact — what a caller keeps instead of the
    full text."""

    id: str  # sha256 hex of the UTF-8 bytes
    path: Path
    size_bytes: int
    kind: str  # "step-output" | "tool-result" | ...


class ArtifactStore:
    """Content-addressed text blobs under ``root/<sha[:2]>/<sha>.txt``."""

    def __init__(self, root: Path | None = None) -> None:
        # ``None`` is the only thing safe to evaluate at def time; the actual
        # ``Path.home()`` (or settings) lookup happens here, when the store is
        # constructed — not when this module is imported.
        self.root = root if root is not None else default_artifact_root()

    def _path_for(self, artifact_id: str) -> Path:
        return self.root / artifact_id[:2] / f"{artifact_id}.txt"

    def put_text(self, text: str, *, kind: str) -> ArtifactRef:
        """Store ``text``, deduping on content. Writing an id that already
        exists on disk is a no-op — same bytes, same file."""
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest)
        if not path.exists():
            self._atomic_write(path, data)
        return ArtifactRef(id=digest, path=path, size_bytes=len(data), kind=kind)

    def get_text(self, artifact_id: str) -> str | None:
        try:
            return self._path_for(artifact_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def summarize_for_context(self, text: str, ref: ArtifactRef, *, max_bytes: int) -> str:
        """A short stand-in for ``text`` safe to put back in the model's context.

        Under the threshold, ``text`` is returned unchanged. Over it, a head
        and a tail are kept (2:1, so the more informative start gets more
        room) with a marker line in between pointing at the full artifact.

        Sliced on *characters*, not raw bytes: a byte-offset slice can land
        inside a multi-byte UTF-8 sequence (a CJK character, an emoji) and
        either raise on decode or silently produce mojibake. Python string
        indexing is code-point based, so slicing here can never split one.
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        head_chars = 2 * max_bytes // 3
        tail_chars = max_bytes // 3
        head = text[:head_chars]
        tail = text[-tail_chars:] if tail_chars > 0 else ""
        marker = (
            f"\n… [truncated {len(encoded)} bytes — full output at "
            f"{ref.path} (artifact {ref.id[:12]})]\n"
        )
        return f"{head}{marker}{tail}"

    def store_if_large(
        self, text: str, *, kind: str, max_bytes: int
    ) -> tuple[str, ArtifactRef | None]:
        """What callers actually use: store ``text`` only if it would not fit
        in context, returning ``(text_or_summary, ref_or_None)``."""
        if len(text.encode("utf-8")) <= max_bytes:
            return text, None
        ref = self.put_text(text, kind=kind)
        return self.summarize_for_context(text, ref, max_bytes=max_bytes), ref

    def _atomic_write(self, path: Path, data: bytes) -> None:
        """tmp + ``os.replace`` — see ``storage/sessions.py._atomic_write_text``.
        A crash mid-write leaves either nothing or the old tmp file, never a
        truncated artifact that ``get_text`` would hand back as complete."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX
