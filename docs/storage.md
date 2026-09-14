# Storage — Filesystem-backed session store

LocalCode persists session metadata and message history as plain files on
disk, modelled after Claude Code's `~/.claude/projects/<project>/<uuid>.jsonl`
layout. There is no database — no Postgres to provision, no schema to
migrate, no docker stack to run.

> **TL;DR**
> ```
> <session.cwd>/.localcode/sessions/<session-uuid>/
>     meta.json          ← session metadata, atomic-rewritten
>     messages.jsonl     ← append-only log, one line per finalized message
>     current.json       ← the in-progress assistant message, overwritten per
>                          checkpoint; absent between turns
>
> ~/.localcode/sessions-index.json   ← user-global session rows, keyed by id
> ~/.localcode/sessions/_global/     ← sessions with no cwd
> ~/.localcode/sessions/.last-cleanup ← 24h cooldown sentinel
> ```

## Why files, not a database

We previously shipped a Postgres-backed session store using SQLAlchemy
(`sessions` and `messages` tables). It was overkill for what is, in
practice, a per-user log of conversations:

- **Single-user, single-process.** No multi-tenant concurrency to worry
  about. The orchestrator runs as one uvicorn process; turns within a
  session are serialised by an asyncio `Lock` already.
- **No relational queries.** Every read path is "give me this session"
  or "give me this session's messages." No joins, no analytics.
- **Append-heavy writes.** A finalized message is one `O_APPEND` write —
  atomic and lock-free on POSIX, where a SQL UPDATE takes a transaction
  round-trip. The in-progress message is one atomic file replace per
  checkpoint, which no database gives you more cheaply.
- **Auditable.** A `cat ~/.localcode/sessions/.../messages.jsonl` shows
  exactly what was persisted. Easier to grep / inspect / diff than rows
  in `pg`.

Both Claude Code ([docs](https://code.claude.com/docs/en/agent-sdk/session-storage),
[deep dive](https://databunny.medium.com/inside-claude-code-the-session-file-format-and-how-to-inspect-it-b9998e66d56b))
and OpenCode ([architecture](https://deepwiki.com/sst/opencode/2.9-storage-and-database))
adopted this pattern. We follow Claude Code's specific shape — JSONL event
streams scoped by project — because our access patterns mirror theirs:
read on session open, append on every turn event, occasionally truncate
on cleanup.

## Path layout

### Per-project session dir

Sessions live next to the implementation plans the orchestrator writes,
inside the agent's working directory:

```
<session.cwd>/.localcode/
├── plans/
│   └── 20260510-053336-feature-implementation-plan.md
└── sessions/
    └── <session-uuid>/
        ├── meta.json
        ├── messages.jsonl
        └── current.json        # only while a turn is in flight
```

This is the user's project — sessions follow the project naturally,
mirror the plans they generated, and disappear if the project does.

Sessions without a `cwd` (e.g. a chat opened before any project was
selected) fall back to the user-global `~/.localcode/sessions/_global/`
bucket so they always have a stable home.

### User-global index

A single JSON file holds a row per known session — every field `SessionOut`
exposes, mirrored out of that session's `meta.json`:

```
~/.localcode/sessions-index.json
```

```json
{
  "397b395ffba949d88cde1b93755c0e4e": {
    "id": "397b395ffba949d88cde1b93755c0e4e",
    "title": "Times of India scraper",
    "provider": "fleet",
    "model": "default",
    "cwd": "/Users/shubhojeet/Projects/scrapers",
    "additional_dirs": null,
    "upstream_id": null,
    "permission_mode": null,
    "fleet_config_override": { "max_review_retries": 3 },
    "created_at": "2026-05-10T11:12:52.435206+00:00",
    "updated_at": "2026-05-10T11:34:18.872004+00:00"
  }
}
```

Why an index: `list_sessions` (the sidebar) needs to enumerate sessions
across all the projects the user has touched. Without an index we'd have
to walk the entire home directory looking for `.localcode/sessions/`
subdirs. It is atomic-rewritten via `.tmp` + rename on every create,
update and delete, and self-healing — `list_sessions` drops entries whose
`meta.json` is gone (a `stat`, not a read).

Why a full row rather than just `{cwd, created_at}`: `GET /api/sessions` is
the only endpoint that ever hands the frontend a session row, and it used to
open and parse one `meta.json` per session to build the list. `meta.json`
remains the source of truth on disk; the index is a denormalised read cache
of it, rewritten from the same data whenever a session changes. An entry
written before the index was widened (an install predating this change) is
detected by its missing fields and resolved by reading `meta.json`, and
widens on that session's next update.

## File formats

### `meta.json` (one per session)

```json
{
  "id": "397b395ffba949d88cde1b93755c0e4e",
  "title": "Times of India scraper",
  "provider": "fleet",
  "model": "default",
  "cwd": "/Users/shubhojeet/Projects/scrapers",
  "additional_dirs": null,
  "upstream_id": null,
  "fleet_config_override": {
    "max_review_retries": 3,
    "roles": { "...": "..." }
  },
  "created_at": "2026-05-10T11:12:52.435206+00:00",
  "updated_at": "2026-05-10T11:34:18.872004+00:00"
}
```

| Field                    | Type                          | Notes                                                                |
| :----------------------- | :---------------------------- | :------------------------------------------------------------------- |
| `id`                     | 32-hex string                 | Matches the legacy SQLAlchemy `_uuid()` shape so old DB references resolve. |
| `title`                  | string                        | Default `"New chat"`. Updateable.                                    |
| `provider`               | `"claude" \| "codex" \| "fleet"` | Pinned at session creation.                                          |
| `model`                  | string                        | Provider-native model id.                                            |
| `cwd`                    | string \| null                | Where the agent operates. Determines on-disk session dir.            |
| `additional_dirs`        | list[string] \| null          | Extra dirs the agent's tools may read/write under.                   |
| `upstream_id`            | string \| null                | Provider-native session id (Claude Code `session_id` or Codex thread id) for resume. |
| `fleet_config_override`  | dict \| null                  | Per-session override merged on top of the file-level fleet config.   |
| `created_at`             | ISO 8601 + tz                 | Set once.                                                            |
| `updated_at`             | ISO 8601 + tz                 | Bumped on every message append. Drives sort order in the sidebar.    |

Writes go through `_atomic_write_text(path, text)` — write to `.tmp`,
fsync, rename. A crash mid-write leaves either the old file intact or
the fully-written new file; never a torn write.

### `messages.jsonl` (append-only)

One JSON object per line, in append order — **one line per finalized
message**, ids unique:

```jsonl
{"role":"user","content":[{"type":"text","text":"Write a scraper..."}],"id":"5f5ad59c...","created_at":"2026-05-09T23:37:19.278Z"}
{"id":"abc123...","role":"assistant","content":[{"type":"tool_use","name":"planner [...]"},{"type":"tool_result","content":"<plan>"},{"type":"tool_use","name":"coder [...]"}],"cost_usd":0.0234,"duration_ms":12345,"created_at":"2026-05-09T23:42:11Z"}
```

The assistant's line is written once, when the turn finalizes. Everything
that happened before that lived in `current.json` (see below). Because ids
are unique, the log is append-order = `created_at` order and readers neither
dedupe nor scan it.

Logs written before this layout hold one line *per mid-turn checkpoint*, all
sharing one id. Readers still collapse duplicate ids within the window they
read (last line wins), and the cleanup sweep's compaction pass rewrites such
a log to one line per id. A duplicate whose copies fall in different pages of
a very long legacy log can still show up twice until that sweep runs.

### `current.json` (the in-progress message)

A single JSON object — the same line shape as a `messages.jsonl` entry —
holding the assistant message the current turn is still building:

```json
{
  "id": "abc123...",
  "role": "assistant",
  "content": [
    { "type": "tool_use", "name": "planner [...]" },
    { "type": "tool_result", "content": "<plan>" }
  ],
  "cost_usd": null,
  "duration_ms": null,
  "created_at": "2026-05-09T23:39:00Z"
}
```

Each checkpoint **overwrites** it by atomic replace, so it never grows
beyond one message, and it is deleted when the turn finalizes and the
message is appended to the log. Readers treat it as the newest message,
which is what makes a mid-turn page reload show partial progress.

Why it is a separate file: appending a full snapshot of the growing message
on every tool boundary made write volume quadratic in the turn's size — a
200-tool turn ending at 2 MB wrote ~200 MB across 200 fsynced appends, left
all 200 copies in the log, and forced every reader to parse them back out.
One overwritten file fixes all three.

#### Line shape

| Field         | Type                | Notes                                                  |
| :------------ | :------------------ | :----------------------------------------------------- |
| `id`          | 32-hex              | Auto-generated if omitted on first append.             |
| `role`        | `"user" \| "assistant" \| "system"` | Standard chat roles.                  |
| `content`     | list[block]         | Each block is `{"type": "text" \| "tool_use" \| "tool_result", ...}`. Same shape the WebSocket emits. |
| `cost_usd`    | float \| null       | Set by the provider's `assistant.done` event.          |
| `duration_ms` | int \| null         | Set by the provider's `assistant.done` event.          |
| `created_at`  | ISO 8601 + tz       | When the line (or the checkpoint it came from) was written. |

#### Why JSONL vs. one big JSON array

- **Append is one syscall.** `O_APPEND` on POSIX is atomic for writes up
  to `PIPE_BUF` (4 KB on macOS, 8 KB on Linux). Concurrent appends from
  separate fds in the same file don't interleave bytes.
- **A crash loses at most one line.** A partially-written line is dropped
  by the JSONL parser (`json.JSONDecodeError` → skip). We never lose the
  whole turn.
- **Streamable.** We could `tail -f messages.jsonl` and reconstruct the
  conversation as it grows — useful for debugging, log forwarding, etc.

## Atomicity guarantees

| Operation                | Atomic?           | Mechanism                                                        |
| :----------------------- | :---------------- | :--------------------------------------------------------------- |
| `meta.json` rewrite      | ✅                | `_atomic_write_text` → `.tmp` + fsync + rename                   |
| `sessions-index.json` update | ✅            | Same mechanism, guarded by an asyncio `Lock` to serialise concurrent writers in the single-process backend. |
| `messages.jsonl` append (≤ PIPE_BUF) | ✅      | `O_APPEND` syscall is atomic vs other appenders, no fragmentation. |
| `messages.jsonl` append (> PIPE_BUF) | ⚠️       | Larger lines may interleave under concurrent writers. The per-session asyncio `Lock` in `routes/sessions.py` already serialises turns within a session, so this case doesn't arise in practice. |
| `current.json` checkpoint | ✅ rename, ⚠️ durability | `.tmp` + rename, **without** fsync. The rename is atomic, so a reader never sees half a message; an OS-level crash can leave the previous checkpoint, or a file that doesn't parse — which readers treat as absent. |
| Session delete           | ✅                | `shutil.rmtree` then atomic index update.                        |
| Cleanup sweep            | ✅ per-session    | Each session is deleted independently. A crash mid-sweep leaves the rest intact; the sentinel only updates after the loop completes, and the index is rewritten once at the end (entries whose dirs are gone are pruned by the next `list_sessions`). |

Mid-turn checkpoints don't fsync, on purpose. A checkpoint exists so a crash
does not lose the whole turn, not so a crash loses nothing — and it is the
hot path. The final write of a message does fsync.

## Mid-turn checkpoints

The lifecycle of an assistant turn:

```
WS event sequence                           on-disk writes
─────────────────────────────────────────  ──────────────────────────────────
user prompt arrives                        ┐ messages.jsonl += user line (id: u1)
                                           │
session.started                            │
assistant.tool_use   (planner step)        ├─ current.json = {id: a1, content: [tool_use(planner)]}
…heartbeats during planner work…           │  (heartbeats are NOT persisted)
tool.result          (planner output)      ├─ current.json = {id: a1, content: [tool_use(planner), tool_result(planner)]}
assistant.text       ("Plan saved to…")    │  …if the throttle allows; see below
assistant.tool_use   (coder step)          ├─ current.json = {id: a1, content: [..., tool_use(coder)]}
…heartbeats during coder work…             │
tool.result          (coder output)        ├─ current.json = {id: a1, content: [..., tool_use(coder), tool_result(coder)]}
… continues for reviewer + tester …        │
assistant.done       (cost, duration)      ┘ messages.jsonl += assistant line (id: a1, all blocks,
                                             cost_usd, duration_ms); current.json removed;
                                             meta.updated_at + index bumped once
```

The user sees exactly one assistant message in the chat with all its tool
blocks — mid-turn from `current.json`, afterwards from the log.

This also handles WS disconnect cleanly: if the user reloads the page
during the coder step, the messages endpoint returns the partial state
(everything up to the latest checkpoint), and the frontend's
`loadMessages` helper marks the in-progress turn so live events from
the new WS continue extending the same turn.

### Checkpoint throttle

A checkpoint rewrites the *whole* message, so one per tool boundary costs
`boundaries × final size`. `TurnAccumulator` skips a mid-turn checkpoint
unless either:

- the message has grown by at least `CHECKPOINT_MIN_GROWTH_BYTES` (64 KiB)
  **and** by at least as much as the last checkpoint wrote — amortizing the
  write volume to roughly 2× the final message however long the turn runs; or
- `CHECKPOINT_MIN_INTERVAL_S` (2 s) has passed and the message is still
  smaller than that growth floor, so a slow turn that emits very little is
  still recoverable and the write is cheap by construction.

The final checkpoint always writes. Both knobs are `Settings` fields
(`backend/app/config.py`), overridable via `.env`.

What the throttle costs — this is the bound to tune on, and it is **not** the
2 s interval:

- While the message is under 64 KiB, a crash or a mid-turn page reload is at
  most one interval (2 s) of work behind.
- Above 64 KiB the time arm no longer applies at all. The next checkpoint waits
  for the message to grow by as much as the last one wrote, so the unsaved tail
  is bounded by the last checkpoint's size — roughly half the message so far —
  with **no time ceiling**. A turn sitting at 1 MB that grows slowly can go many
  minutes without writing, and a reload then shows ~1 MB-stale content.

So the guarantee is "you never lose more than you have already saved", not "you
never lose more than 2 s". Above the growth floor that is inherent to amortizing
a whole-message rewrite: tightening it means rewriting more often, which is the
quadratic write volume this replaced. The live WebSocket stream is unaffected —
it keeps extending the same turn, so the gap closes on the next event.

### Crash recovery

A backend killed mid-turn leaves `current.json` behind. It is a coherent
message, so:

- `list_messages` returns it as the newest message — the chat is not missing
  the partial turn.
- The next `append_message` (the next prompt, say) promotes it into
  `messages.jsonl` ahead of its own line and deletes it, so it is promoted
  exactly once.
- Reads never promote. During a live turn `current.json` is the message still
  being written; promoting it on read would duplicate it.

## Reading: bounded tail + pagination

`SessionStore.list_messages(session_id, before=None, limit=N)` follows
this algorithm:

1. Read the **tail** of `messages.jsonl` — a 64 KiB window, doubling
   backwards (never re-reading bytes) until it yields `limit + 1` messages
   matching the cursor, or reaches the start of the file. A window boundary
   lands mid-line, so the leading fragment is dropped; the next widening
   picks that line up whole. Blank and malformed lines are skipped silently.
   The extra message beyond the page is what decides `has_more`.
2. Overlay `current.json` when present, as the newest message (replacing the
   log entry with the same id, if the log already has one).
3. Apply the `before` cursor: drop messages with `created_at >= before`.
4. Take the trailing `limit` messages (the most recent in the window),
   oldest-first within the page.
5. Compute `has_more` and `next_before` (the `created_at` of the oldest
   message in the page, used as the cursor for the previous page).

Serving one 50-message page out of a 1 MB log reads ~192 KiB, not the file.

**Fallback for logs a window can't serve.** Parsing a window costs several
times its size, so the read gives up on windowing once it would have to hold
more than 1 MiB and streams the file line by line instead — holding one line
plus the deduped messages, never the file. This is what a pre-`current.json`
log hits on the first read after upgrading, where every line is a full
snapshot: a 20 MB log of 100 KB lines peaks at ~4 MB of allocation that way
versus ~82 MB if the windows were joined. It costs one full pass, which is
what the old reader always did. A deep `before` cursor — paging back past the
start of the window — ends up there too.

The shape of the response matches the legacy `MessagesPage` so the
frontend doesn't notice the migration. Default page size is 50, capped
at 500 (configurable via `MESSAGES_PAGE_DEFAULT` / `MESSAGES_PAGE_MAX`).

## Cleanup + compaction

Sessions are swept on a 24-hour cadence. The sweep does two things:

1. **Delete expired sessions.** Any session whose `meta.updated_at` is
   older than `SESSION_RETENTION_DAYS` (default `7`, configurable via
   `.env` / Settings) gets `shutil.rmtree`'d and its index entry removed.
2. **Compact kept sessions.** For each surviving session, rewrite
   `messages.jsonl` to one entry per id. Only logs written before the
   in-progress message moved into `current.json` have anything to collapse —
   newer turns append each message once — so this is a migration path for old
   sessions rather than ongoing maintenance.

The sweep is gated by a sentinel file:

```
~/.localcode/sessions/.last-cleanup
```

Whose mtime is the timestamp of the last completed sweep. On startup the
SessionStore checks `now - mtime < CLEANUP_INTERVAL_S` (24 h) — if true,
the sweep is a no-op. After the sweep completes the sentinel is touched
to reset the cooldown.

This means:
- **A bouncing backend** triggers at most one sweep per day.
- **A long-running backend** triggers a sweep every 24 hours (we don't
  have a periodic timer; the sweep runs lazily on the next startup that
  crosses the threshold). For our use case — a single-user dev orchestrator
  that gets restarted regularly — this is plenty.

### Disabling auto-cleanup

Set `SESSION_RETENTION_DAYS=0` in `.env`. The sweep early-returns; no
sessions are ever deleted. Disk usage grows over time; you'll need to
manually clear via the UI's `/clear-all` (which calls
`DELETE /api/sessions`) or via `rm -rf` on the on-disk dirs.

### Forcing a sweep

```bash
python -c "
import asyncio
from backend.app.storage.sessions import store
asyncio.run(store.cleanup_expired(retention_days=7, force=True))
"
```

`force=True` skips the 24-hour cooldown.

## SessionStore API

The Python module [backend/app/storage/sessions.py](../backend/app/storage/sessions.py)
exports a single instance `store: SessionStore` with these methods:

```python
async def create_session(
    *,
    provider: str, model: str,
    cwd: str | None = None,
    additional_dirs: list[str] | None = None,
    title: str = "New chat",
    upstream_id: str | None = None,
    fleet_config_override: dict[str, Any] | None = None,
) -> dict[str, Any]: ...

async def get_session(session_id: str) -> dict[str, Any] | None: ...

async def list_sessions() -> list[dict[str, Any]]:
    """Sorted by updated_at desc — drives the sidebar."""

async def update_session(session_id: str, **fields: Any) -> dict[str, Any] | None:
    """Atomic-rewrite meta.json. Bumps updated_at."""

async def delete_session(session_id: str) -> bool:
    """rmtree the session dir + remove index entry. Returns True if existed."""

async def delete_all_sessions() -> int:
    """Wipe everything. Returns count deleted."""

async def append_message(
    session_id: str, message: dict[str, Any],
    *, bump_updated_at: bool = True, fsync: bool = True,
) -> dict[str, Any]:
    """Append one finalized message. Clears current.json (promoting an orphan
    left by a killed backend). Returns the stored message (id+created_at filled in)."""

async def write_current(session_id: str, message: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint the in-progress assistant message to current.json — atomic
    replace, no fsync, no updated_at bump."""

async def list_messages(
    session_id: str, *, before: datetime | None = None, limit: int | None = None
) -> tuple[list[dict[str, Any]], datetime | None, bool]:
    """Returns (messages, next_before, has_more) from a bounded tail read plus
    current.json as the newest message."""

async def cleanup_expired(*, retention_days: int, force: bool = False) -> dict[str, int]:
    """Sweep stale sessions + compact the rest. Returns {deleted, compacted, kept}."""
```

Every method is a coroutine that does its filesystem work in a worker thread
(`asyncio.to_thread` around a `_sync_*` body). One event loop serves every
session, so an inline `open()` / `fsync()` / `rmtree()` stalls all of them —
0.04 ms on a local SSD, far worse on a network filesystem. Serialization is
unchanged: the per-session lock in `SessionRunner` and the module-level index
`asyncio.Lock` are both held *across* the offload.

## Comparison with upstream models

| Property                    | Claude Code (`~/.claude/`)            | OpenCode (`~/.local/share/opencode/`)        | LocalCode (`<cwd>/.localcode/sessions/`)            |
| :-------------------------- | :------------------------------------ | :------------------------------------------- | :-------------------------------------------------- |
| Storage backend             | JSONL files only                      | SQLite (WAL) + JSON sidecar files            | JSONL files only                                    |
| Per-project namespacing     | `~/.claude/projects/<url-encoded-cwd>/` | Single DB; project_id column                | Sessions live IN the cwd; user-global index for cross-project queries |
| Append model                | Append to `<uuid>.jsonl`              | INSERT row per event                         | Append to `messages.jsonl`                          |
| Mid-turn durability         | Each event is a separate line         | Each event is a separate row                 | One `current.json` overwritten per checkpoint, promoted into the log when the turn finalizes |
| Cleanup                     | `cleanupPeriodDays` setting + sentinel | Manual / not documented                      | `SESSION_RETENTION_DAYS` + `.last-cleanup` sentinel |
| Compaction                  | None — each line is the truth         | None                                         | Not needed — one line per message; the cleanup sweep collapses pre-`current.json` logs |
| Subagent transcripts        | Separate files under `subagents/agent-<id>` | DB rows                                  | Not yet — orchestrator subagent events are inlined into the parent session's log; could split if it becomes noisy |
| Pluggable remote storage    | `SessionStore` adapter (S3/Redis/PG)  | Not pluggable                                | Not yet — single class; could add an adapter protocol later |
| Lock granularity            | None documented (single CLI process)  | DB-level                                     | Per-session asyncio `Lock` in routes; `O_APPEND` for messages; index-level asyncio `Lock` |

We've got the simpler core (JSONL only, no SQLite at all). Where Claude Code
writes every event as its own line, we keep the log to one line per message
and park the in-progress one in a file of its own — the same durability for a
fraction of the bytes. The rest is essentially Claude Code's pattern with a
user-global index added because we needed cross-project listing.

## Operational tips

### Inspecting a session manually

```bash
# What sessions exist?
cat ~/.localcode/sessions-index.json | jq

# Read a session's meta
cat /path/to/project/.localcode/sessions/<uuid>/meta.json | jq

# Read its message history (one line per message, oldest first)
cat /path/to/project/.localcode/sessions/<uuid>/messages.jsonl | jq -c '{role, id: .id[:8], blocks: (.content|length), created_at}'

# Is a turn in flight (or was one interrupted)? What's in it?
cat /path/to/project/.localcode/sessions/<uuid>/current.json | jq '{role, id: .id[:8], blocks: (.content|length)}'

# A log written before current.json existed: collapse the checkpoint duplicates
cat /path/to/project/.localcode/sessions/<uuid>/messages.jsonl \
  | jq -c -s 'group_by(.id)|map(max_by(.created_at))|sort_by(.created_at)|.[]' \
  | jq '{role, blocks: (.content|length), created_at}'
```

### Force-deleting a stuck session

```bash
SID=<session-uuid>
CWD=$(jq -r ".[\"$SID\"].cwd" ~/.localcode/sessions-index.json)
rm -rf "$CWD/.localcode/sessions/$SID"
jq "del(.\"$SID\")" ~/.localcode/sessions-index.json > /tmp/i && mv /tmp/i ~/.localcode/sessions-index.json
```

### Disk-usage check

```bash
du -sh ~/.localcode/sessions/_global/
find ~ -path '*/.localcode/sessions/*' -name 'messages.jsonl' -exec du -sh {} + | sort -h | tail -10
```

The latter shows the 10 chattiest sessions across all your projects.

## What's not stored on disk

- **Plans** are persisted under `<cwd>/.localcode/plans/<timestamp>-<slug>.md`
  by the planner subagent (the dispatch MCP tool writes them when
  `name == "planner"` — see the dispatch MCP server in
  [docs/architecture.md](architecture.md)). These are user-readable
  artifacts (markdown, no checkpoint duplication) and are NOT swept by
  session cleanup. Manage their retention separately.
- **Provider OAuth tokens** stay where the providers put them
  (`~/.claude/`, `~/.codex/auth.json`). LocalCode never reads or copies
  them.
- **Provider transcripts** stay in the providers' own stores. LocalCode
  keeps only the opaque `upstream_id` needed to resume Claude Code via
  `ClaudeAgentOptions.resume` or a Codex thread via `thread/resume`.
- **In-flight per-turn state** (asyncio locks, approval-channel queues,
  WS connections) is in-memory only. A backend bounce drops this state;
  the persisted JSONL state is what's recovered on restart.

## Source

- [backend/app/storage/sessions.py](../backend/app/storage/sessions.py) — `SessionStore` class, atomic write helpers, cleanup logic.
- [backend/app/routes/sessions.py](../backend/app/routes/sessions.py) — REST + WebSocket consumers of the store.
- [backend/app/main.py](../backend/app/main.py) — wires `cleanup_expired` into the FastAPI lifespan startup hook.
- [backend/app/config.py](../backend/app/config.py) — `Settings.session_retention_days`.
