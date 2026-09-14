"""Identifier helpers — same shapes pi uses.

Entry ids are the first 8 hex chars of a UUID (pi: ``randomUUID().slice(0, 8)``),
checked against the ids already in the session so a collision within one file
is impossible. Session ids are full UUIDs.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Container

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def generate_id(existing: Container[str]) -> str:
    for _ in range(100):
        candidate = str(uuid.uuid4())[:8]
        if candidate not in existing:
            return candidate
    # Fallback to a full UUID if we somehow keep colliding.
    return str(uuid.uuid4())


def create_session_id() -> str:
    return str(uuid.uuid4())


def assert_valid_session_id(session_id: str) -> None:
    """Session ids become file-name fragments; reject anything that could
    escape the session directory or break the ``<timestamp>_<id>.jsonl``
    naming."""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
