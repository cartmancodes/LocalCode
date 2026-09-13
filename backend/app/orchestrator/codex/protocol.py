"""Every `codex app-server` wire name, in one file.

The app-server is explicitly experimental: its method names, its item kinds
and the exact spelling of its JSON fields change from one CLI release to the
next. Spreading those strings across a client, a translator and a provider
would turn a schema bump into an archaeology exercise across three modules —
so every literal the wire uses lives here and nowhere else. A schema bump is
then a one-file edit plus the matching edit to
``backend/tests/fakes/fake_codex_app_server.py``.

**Caveat, and it is the important part of this docstring.** The `codex` CLI is
not installed on the machine this module was written on, so every name below
is a *best-effort reading of the documented protocol*, not something observed
against a live binary. Two consequences are baked into the design:

  * Regenerate and reconcile before trusting it::

        codex app-server generate-json-schema > docs/codex-app-server.schema.json

    (``make codex-schema``), then diff this module against the output.
  * **No translator may raise on an unknown name.** Every branch that maps a
    wire value to something of ours logs once at DEBUG and skips. A provider
    that raised on an item kind a newer CLI introduced would turn a perfectly
    good turn into an ``error`` Event, which is a far worse failure than
    silently not rendering one reasoning block. :func:`pick` exists for the
    same reason: it reads snake_case and camelCase spellings of the same field
    so a casing flip in the schema costs nothing.

Assumptions recorded explicitly (each one is a guess until the schema above is
reconciled, and each is the shape the fake app-server implements):

  * ``initialize`` takes ``{"clientInfo": {"name", "version"}}`` and is
    followed by an ``initialized`` notification, LSP-style.
  * ``thread/start`` takes ``{"cwd", "model", "additionalDirectories"}`` and
    returns ``{"threadId": ...}``; ``thread/resume`` takes ``{"threadId"}``.
  * ``turn/start`` takes ``{"threadId", "input": [{"type": "text", "text"}]}``.
  * item notifications carry ``{"threadId", "item": {"id", "type", ...}}``.
  * ``turn/completed`` may carry ``usage`` with token counts; ``turn/failed``
    carries ``error.message``.
  * the two approval requests are answered with ``{"decision": <str>}``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Bumping this is the signal to re-run `make codex-schema` and reconcile.
PROTOCOL_NOTE = (
    "Pinned to the codex app-server protocol as documented for CLI 0.5x. "
    "Regenerate with `codex app-server generate-json-schema` and reconcile "
    "this module (see `make codex-schema` and docs/codex.md)."
)

# ── client → server requests ────────────────────────────────────────────────
M_INITIALIZE = "initialize"
M_THREAD_START = "thread/start"
M_THREAD_RESUME = "thread/resume"
M_THREAD_FORK = "thread/fork"
M_TURN_START = "turn/start"
M_TURN_STEER = "turn/steer"
M_TURN_INTERRUPT = "turn/interrupt"

# ── client → server notifications ───────────────────────────────────────────
N_INITIALIZED = "initialized"

# ── server → client notifications ───────────────────────────────────────────
N_ITEM_STARTED = "item/started"
N_ITEM_UPDATED = "item/updated"
N_ITEM_COMPLETED = "item/completed"
N_TURN_COMPLETED = "turn/completed"
N_TURN_FAILED = "turn/failed"

ITEM_NOTIFICATIONS = (N_ITEM_STARTED, N_ITEM_UPDATED, N_ITEM_COMPLETED)
TURN_NOTIFICATIONS = (N_TURN_COMPLETED, N_TURN_FAILED)

# ── server → client requests (the approval callbacks) ───────────────────────
R_EXEC_APPROVAL = "execCommandApproval"
R_PATCH_APPROVAL = "applyPatchApproval"
APPROVAL_REQUESTS = (R_EXEC_APPROVAL, R_PATCH_APPROVAL)

# ── item kinds the translator handles ───────────────────────────────────────
ITEM_TYPE_AGENT_MESSAGE = "agent_message"
ITEM_TYPE_REASONING = "reasoning"
ITEM_TYPE_COMMAND_EXECUTION = "command_execution"
ITEM_TYPE_FILE_CHANGE = "file_change"
ITEM_TYPE_MCP_TOOL_CALL = "mcp_tool_call"
ITEM_TYPE_WEB_SEARCH = "web_search"
ITEM_TYPE_ERROR = "error"

KNOWN_ITEM_TYPES = frozenset(
    {
        ITEM_TYPE_AGENT_MESSAGE,
        ITEM_TYPE_REASONING,
        ITEM_TYPE_COMMAND_EXECUTION,
        ITEM_TYPE_FILE_CHANGE,
        ITEM_TYPE_MCP_TOOL_CALL,
        ITEM_TYPE_WEB_SEARCH,
        ITEM_TYPE_ERROR,
    }
)

# ── decisions we may send back on an approval request ───────────────────────
DECISION_APPROVED = "approved"
DECISION_APPROVED_FOR_SESSION = "approved_for_session"
DECISION_DENIED = "denied"
DECISION_ABORT = "abort"
APPROVAL_DECISIONS = (
    DECISION_APPROVED,
    DECISION_APPROVED_FOR_SESSION,
    DECISION_DENIED,
    DECISION_ABORT,
)

# ── JSON-RPC error codes we act on ──────────────────────────────────────────
# The app-server is overloaded: back off and retry rather than surfacing a
# transient condition to the user as a failed turn.
ERR_BUSY = -32001
# Ours, sent back when the server asks for a method we do not implement. A
# dropped server request hangs the agent forever, so every unknown one is
# answered.
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL = -32603

# Backoff schedule for ERR_BUSY, and — by its length — the number of tries.
# Three tries, then the error surfaces: an unbounded retry turns "the
# app-server is wedged" into a turn that never ends and never reports why.
# The schedule is read between tries, so with three tries the first two
# delays are slept and the third is never reached; it is the delay a fourth
# try would use, and lengthening this tuple is how you buy one.
BUSY_BACKOFF_S = (0.25, 0.5, 1.0)


def pick(payload: Mapping[str, Any] | None, *names: str, default: Any = None) -> Any:
    """First present key among ``names``, else ``default``.

    The schema has flipped between snake_case and camelCase for the same field
    more than once. Reading both spellings here means a casing change in a CLI
    release costs a line in this module, not a silently empty tool result in
    the UI.
    """
    if not isinstance(payload, Mapping):
        return default
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default
