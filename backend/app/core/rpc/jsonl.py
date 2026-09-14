"""Strict LF-delimited JSONL framing (pi: ``modes/rpc/jsonl.ts``).

Records are split on ``\\n`` only — never on U+2028/U+2029 or other Unicode
separators — and a trailing ``\\r`` is stripped. ``serialize`` never emits a
bare newline inside a record because ``json.dumps`` escapes them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def serialize_json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


class LineSplitter:
    """Feed arbitrary chunks; get complete records back."""

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._buffer = ""
        self._on_line = on_line

    def feed(self, chunk: str) -> None:
        self._buffer += chunk
        while True:
            idx = self._buffer.find("\n")
            if idx < 0:
                return
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1 :]
            self._emit(line)

    def end(self) -> None:
        if self._buffer:
            line, self._buffer = self._buffer, ""
            self._emit(line)

    def _emit(self, line: str) -> None:
        if line.endswith("\r"):
            line = line[:-1]
        self._on_line(line)
