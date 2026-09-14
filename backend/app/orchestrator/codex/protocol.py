"""Every `codex app-server` wire name, in one file.

The app-server is explicitly experimental: its method names, its item kinds
and the exact spelling of its JSON fields change from one CLI release to the
next. Spreading those strings across a client, a translator and a provider
would turn a schema bump into an archaeology exercise across three modules —
so every literal the wire uses lives here and nowhere else. A schema bump is
then a one-file edit plus the matching edit to
``backend/tests/fakes/fake_codex_app_server.py``.

"Every literal" means the **field names too**, not only the methods and item
kinds. The first pass of this module isolated the verbs and left the nouns
scattered across ``client.py`` and ``provider.py`` as inline ``pick()``
arguments, which made the one-file-edit promise false in exactly the place a
schema bump actually lands: a renamed field, not a renamed method. Write-side
spellings are the ``F_*`` constants below; read-side spellings are the
``*_FIELDS`` tuples, which are splatted straight into :func:`pick` so one
field can carry several historical spellings at no cost to the caller.

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

Assumptions recorded explicitly. **Every one is UNVERIFIED against the real
binary** — each is a reading of the documentation, and each is the shape
``backend/tests/fakes/fake_codex_app_server.py`` implements, which means the
suite agrees with these guesses by construction and cannot falsify them. They
are listed so that a reconciliation against ``make codex-schema`` has a
checklist, and so the cost of each one being wrong is written down next to it.

Framing and lifecycle:

  * **A1 (UNVERIFIED).** ``initialize`` takes
    ``{"clientInfo": {"name", "version"}}`` and is followed by an
    ``initialized`` notification, LSP-style. *Wrong ⇒* the handshake fails and
    every turn is one `error` Event naming the binary. Loud, not silent.
  * **A2 (CONFIRMED WRONG, fixed 2026-09-14 against codex-cli 0.154.0).**
    ``thread/start`` and ``thread/resume`` take
    ``{"cwd", "model", "additionalDirectories"}`` as guessed, but the id comes
    back **nested**: ``{"thread": {"id": ..., "sessionId": ..., "model": ...,
    ...}}``, not a flat ``{"threadId"}``. The flat reading meant
    ``thread_start()`` raised ``CodexUnavailable`` on every real call — no
    Codex turn ever completed against the real binary before this fix. See
    :data:`THREAD_FIELDS` and the captured real response in
    ``backend/tests/fixtures/codex_real_trace_2026-09-14.json``.
  * **A3 (UNVERIFIED), and the expensive one.** ``turn/start`` is answered as
    a **prompt acknowledgement** — the response means "the turn was accepted",
    not "the turn is finished" — and the items stream in as notifications
    afterwards. Everything else here costs an unrendered block when it is
    wrong; this one costs a turn. If the real server answers ``turn/start``
    only at turn end, ``await rpc.request(M_TURN_START, ...)`` blocks the very
    loop that drains the item queue, and any turn longer than
    ``codex_request_timeout_s`` fails with a timeout instead of an answer.
    Check this first when reconciling; the fix is to fire the request as a
    task and consume the queue while it is in flight.
  * **A4 (UNVERIFIED).** Item notifications carry
    ``{"threadId", "item": {"id", "type", ...}}``; ``turn/completed`` may
    carry ``usage``; ``turn/failed`` carries ``error.message``.
  * **A5 (UNVERIFIED).** Both approval requests are answered with
    ``{"decision": <str>}`` from :data:`APPROVAL_DECISIONS`. *Wrong ⇒* the
    agent hangs on its own callback, which is why the transport answers every
    server request no matter what.

Payload shapes (each spelling below is also read in its alternates — see the
``*_FIELDS`` tuples — so a casing flip is already covered; a *renamed* field
is not):

  * **A6 (UNVERIFIED).** ``command_execution`` items carry ``command`` (an
    argv list or a string), ``exit_code`` and ``aggregated_output``.
  * **A7 (UNVERIFIED).** ``execCommandApproval`` params carry ``command`` as
    argv. *Wrong ⇒* the approval card shows an empty command, which is a
    question the user cannot answer — so this one is worth checking early too.
  * **A8 (UNVERIFIED).** ``applyPatchApproval`` params carry ``changes`` as a
    ``{path: {"kind": ...}}`` mapping (a list of change objects is also read).
  * **A9 (UNVERIFIED).** ``agent_message`` and ``reasoning`` carry their text
    under ``text``, and ``item/updated`` carries the **accumulated** text
    rather than a delta. The translator emits only the new suffix, so it is
    correct for both readings; a genuine delta that is not a prefix of what
    was already emitted is sent whole.
  * **A10 (UNVERIFIED).** ``file_change`` items carry ``changes`` whose
    ``kind`` distinguishes a creation (``add`` / ``create`` / ``created`` →
    ``Write``) from a modification (→ ``Edit``). *Wrong ⇒* a created file is
    labelled ``Edit`` in the transcript.
  * **A11 (PARTIALLY CONFIRMED, fixed 2026-09-14).** Rate-limit state exists,
    but not where A11 guessed: it never rode on ``turn/completed``. It arrives
    as an independent ``account/rateLimits/updated`` notification —
    ``{"rateLimits": {"limitId", "primary": Window|null, "secondary":
    Window|null, "credits": {...}, "planType", ...}}`` where a non-null Window
    carries ``usedPercent`` — at any point, not scoped to a turn. Two things
    were wrong before this fix, not one: nobody registered a handler for the
    method at all (dropped at the transport layer, before ``client.py`` even
    saw it), and even a forwarded copy would have looked in the wrong place.
    The ``*_FIELDS`` tuples below still describe the payload once it is
    reached; only the wiring changed — see :data:`N_ACCOUNT_RATE_LIMITS`.
    *Was wrong ⇒* Codex headroom stayed
    locally estimated with ``confidence="unknown"``, which is what it is today
    anyway. The dangerous direction would be reading a field that means
    something else and reporting a fabricated limit, which is why a payload
    must carry a numeric utilization before anything is emitted at all.

Policy, which is not a payload shape at all:

  * **A12 (UNVERIFIED).** The approval policy is the APP-SERVER'S, and
    LocalCode does not set it. ``thread/start`` carries ``cwd``, ``model`` and
    ``additionalDirectories`` only — no approval or sandbox policy — so
    whether ``execCommandApproval`` / ``applyPatchApproval`` are sent at all is
    decided by the user's own ``~/.codex`` configuration. Every request that
    does arrive is bound by the shared ``ToolPolicy`` table; a server
    configured to ask about nothing is a server this provider never gets to
    refuse. *Wrong ⇒* nothing here breaks, but the role policy covers less
    than a reader of ``policy_for_role`` would assume, which is why it is
    written down. Sending an explicit policy is a follow-up gated on
    reconciling the real schema: a guessed field name would be a policy the
    server silently ignores, indistinguishable from one it enforces.
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

# The subcommand that puts the CLI into protocol mode. Here rather than in
# client.py for the same reason as everything else in this file: it is a name
# the vendor owns and may rename.
APP_SERVER_SUBCOMMAND = "app-server"

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
# Independent of any turn — see A11. Forwarded through an open turn's queue
# when one exists so the translator can meter it; dropped (logged) between
# turns, same as every other notification here.
N_ACCOUNT_RATE_LIMITS = "account/rateLimits/updated"

ITEM_NOTIFICATIONS = (N_ITEM_STARTED, N_ITEM_UPDATED, N_ITEM_COMPLETED)
TURN_NOTIFICATIONS = (N_TURN_COMPLETED, N_TURN_FAILED)
ACCOUNT_NOTIFICATIONS = (N_ACCOUNT_RATE_LIMITS,)

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


# ── field names we WRITE ────────────────────────────────────────────────────
# One spelling each, because we choose it. Renaming one here renames it on the
# wire; nothing else in the package spells these out.
F_CLIENT_INFO = "clientInfo"
F_NAME = "name"
F_VERSION = "version"
F_CWD = "cwd"
F_MODEL = "model"
F_ADDITIONAL_DIRECTORIES = "additionalDirectories"
F_THREAD_ID = "threadId"
F_INPUT = "input"
F_TYPE = "type"
F_TEXT = "text"
F_INPUT_TYPE_TEXT = "text"
F_DECISION = "decision"
F_ITEM = "item"
F_ERROR = "error"
F_MESSAGE = "message"

# ── field names we READ ─────────────────────────────────────────────────────
# Tuples, splatted into ``pick``: the schema has flipped between snake_case and
# camelCase (and between two different nouns) for the same field more than
# once, and reading every spelling we have seen costs nothing. Order is
# preference order — the first present, non-null key wins.
# See A2: the id is nested under this key, not flat on the response.
THREAD_FIELDS = ("thread",)
THREAD_ID_FIELDS = ("threadId", "thread_id", "id")
ITEM_FIELDS = ("item",)
ITEM_TYPE_FIELDS = ("type", "item_type")
ITEM_ID_FIELDS = ("id", "item_id")
# agent_message / reasoning. ``delta`` before ``content`` because a release
# that sends both means the delta is the new part.
TEXT_FIELDS = ("text", "delta", "content", "summary")
# command_execution, and execCommandApproval's params.
COMMAND_FIELDS = ("command", "argv")
EXIT_CODE_FIELDS = ("exit_code", "exitCode")
STATUS_FIELDS = ("status",)
OUTPUT_FIELDS = ("aggregated_output", "aggregatedOutput", "output", "result", "text")
# file_change, and applyPatchApproval's params.
CHANGES_FIELDS = ("changes", "fileChanges", "file_changes")
PATH_FIELDS = ("path", "file_path", "filePath")
KIND_FIELDS = ("kind", "type")
# mcp_tool_call / web_search.
MCP_SERVER_FIELDS = ("server", "server_name")
MCP_TOOL_FIELDS = ("tool", "tool_name", "name")
MCP_ARGUMENTS_FIELDS = ("arguments", "args", "input")
QUERY_FIELDS = ("query", "q")
# error items, and turn/failed's error object.
ERROR_FIELDS = ("error",)
ERROR_MESSAGE_FIELDS = ("message", "detail")
ITEM_ERROR_MESSAGE_FIELDS = ("message", "error", "detail")
# turn/completed.
TURN_FIELDS = ("turn",)
USAGE_FIELDS = ("usage",)
INPUT_TOKEN_FIELDS = ("input_tokens", "inputTokens")
OUTPUT_TOKEN_FIELDS = ("output_tokens", "outputTokens")
CACHE_READ_TOKEN_FIELDS = (
    "cached_input_tokens",
    "cachedInputTokens",
    "cache_read_input_tokens",
    "cacheReadInputTokens",
    "cache_read_tokens",
)
CACHE_CREATION_TOKEN_FIELDS = (
    "cache_creation_input_tokens",
    "cacheCreationInputTokens",
    "cache_creation_tokens",
)
# Vendor-reported rate limits on ``turn/completed`` — Task 11's quota governor.
# The app-server has no DOCUMENTED rate-limit field at all (see A11), so these
# are read wherever they might plausibly sit and nothing is emitted when none
# of them is present. A guess that reads nothing costs a local estimate; a
# guess that reads the wrong thing would report a fabricated limit, so every
# spelling below is a candidate, never an assertion.
RATE_LIMIT_FIELDS = ("rate_limits", "rateLimits")
# The same, nested under ``usage``.
RATE_LIMIT_NESTED_FIELDS = ("limits", "rate_limits", "rateLimits")
RATE_LIMIT_TYPE_FIELDS = ("type", "window", "rate_limit_type", "rateLimitType")
RATE_LIMIT_UTILIZATION_FIELDS = (
    "utilization",
    "used_percent",
    "usedPercent",
    "percent_used",
    "percentUsed",
)
RATE_LIMIT_RESET_FIELDS = ("resets_at", "resetsAt", "reset_at", "resetAt")
RATE_LIMIT_STATUS_FIELDS = ("status",)

# ── field VALUES with meaning ───────────────────────────────────────────────
# A file_change whose kind is one of these created the file, so the transcript
# calls it a Write rather than an Edit.
CREATE_KINDS = frozenset({"add", "create", "created"})
# A tool item that reports one of these failed, whatever its exit code says.
FAILED_STATUSES = frozenset({"failed", "error"})


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
