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


def _is_utf8_continuation_byte(b: int) -> bool:
    """True for a UTF-8 continuation byte (``10xxxxxx``) — a byte offset
    that lands on one is *inside* a multi-byte sequence, never a valid
    place to cut a string."""
    return (b & 0xC0) == 0x80


def _byte_safe_head(data: bytes, budget: int) -> str:
    """The longest prefix of ``data`` that both fits in ``budget`` bytes and
    ends on a UTF-8 character boundary.

    Only ever shrinks toward 0 from ``budget``, so the result is always
    ``<= budget`` bytes — the property the byte-count budget promises and a
    plain character-count slice does not (1 CJK character is 3 bytes, 1
    emoji is 4). Backing off to the nearest earlier boundary rather than
    just cutting at ``budget`` is what keeps a 3-byte or 4-byte sequence
    from being split in half, which would otherwise raise on ``.decode()``.
    """
    end = min(max(budget, 0), len(data))
    while end > 0 and end < len(data) and _is_utf8_continuation_byte(data[end]):
        end -= 1
    return data[:end].decode("utf-8")


def _byte_safe_tail(data: bytes, budget: int) -> str:
    """The shortest suffix of ``data`` that both fits in ``budget`` bytes and
    starts on a UTF-8 character boundary. Mirror of ``_byte_safe_head``:
    here the cut can only move *forward* (shrinking the suffix further),
    which keeps the result ``<= budget`` bytes while never starting
    mid-character."""
    start = max(len(data) - max(budget, 0), 0)
    while start < len(data) and _is_utf8_continuation_byte(data[start]):
        start += 1
    return data[start:].decode("utf-8")


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

        Both are trimmed to a *byte* budget, not a character count: a
        budget expressed as "this many characters" silently assumes 1
        byte/char, so CJK (3 bytes/char) or emoji (4 bytes/char) content
        blows the stated ``max_bytes`` by 2-3x — exactly the failure this
        module exists to prevent (a 2 MB tool output must never land back
        in context near-whole). ``_byte_safe_head``/``_byte_safe_tail``
        additionally never split a multi-byte UTF-8 sequence: they trim to
        the nearest character boundary rather than a raw byte offset, which
        can land mid-sequence and either raise on decode or produce
        mojibake.
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        head_budget = 2 * max_bytes // 3
        tail_budget = max_bytes // 3
        head = _byte_safe_head(encoded, head_budget)
        tail = _byte_safe_tail(encoded, tail_budget)
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
