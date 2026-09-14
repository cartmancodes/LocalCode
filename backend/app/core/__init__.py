"""LocalCode core — a pi-shaped shell over official agent engines.

Layout mirrors pi's ``packages/coding-agent/src/core``:

- ``session_manager`` — sessions as a JSONL tree (pi session format v3).
- ``messages`` — pi-ai message shapes used inside session entries.
- ``extensions`` — the one extension API (``on`` / ``register_tool`` / …).
- ``engines`` — the agent loops. Unlike pi, the loop is never ours: each
  engine drives a vendor's official binary (``claude`` via the Agent SDK,
  ``codex`` via its app-server) so subscription auth stays with that binary.
- ``agent_session`` — ties engine + session + extensions together.
- ``rpc`` — pi's RPC vocabulary on LF-delimited JSONL.
"""

from __future__ import annotations

from .config import CONFIG_DIR_NAME, get_default_agent_dir
from .session_manager import SessionManager

__all__ = ["CONFIG_DIR_NAME", "SessionManager", "get_default_agent_dir"]
