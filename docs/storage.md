# Storage — Filesystem-backed session store

LocalCode persists session metadata and message history as plain files on
disk, modelled after Claude Code's `~/.claude/projects/<project>/<uuid>.jsonl`
layout. There is no database — no Postgres to provision, no schema to
migrate, no docker stack to run.

> **TL;DR**
> ```
> <session.cwd>/.localcode/sessions/<session-uuid>/
>     meta.json          ← session metadata, atomic-rewritten
>     messages.jsonl     ← append-only event log, deduped on read
>
> ~/.localcode/sessions-index.json   ← user-global {session_id: {cwd}} index
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
- **Append-heavy writes.** Mid-turn checkpoints fire on every tool_use
  and tool_result. POSIX `O_APPEND` is atomic and lock-free; SQL UPDATE
  takes a transaction round-trip per checkpoint.
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
        └── messages.jsonl
```

This is the user's project — sessions follow the project naturally,
mirror the plans they generated, and disappear if the project does.

Sessions without a `cwd` (e.g. a chat opened before any project was
selected) fall back to the user-global `~/.localcode/sessions/_global/`
bucket so they always have a stable home.

### User-global index

A single JSON file maps every known session id to its cwd:

```
~/.localcode/sessions-index.json
```

```json
{
  "397b395ffba949d88cde1b93755c0e4e": {
    "cwd": "/Users/shubhojeet/Projects/scrapers",
    "created_at": "2026-05-10T11:12:52.435206+00:00"
  },
  "0b6b08f6d89241fbb81824750939d507": {
    "cwd": "/Users/shubhojeet/neural-city-utils",
    "created_at": "2026-05-10T00:00:45.000000+00:00"
  }
}
```

Why an index: `list_sessions` (the sidebar) needs to enumerate sessions
across all the projects the user has touched. Without an index we'd have
to walk the entire home directory looking for `.localcode/sessions/`
subdirs. The index is small, atomic-rewritten via `.tmp` + rename, and
self-healing — `list_sessions` drops entries whose on-disk dir is missing.

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
| `provider`               | `"claude" \| "opencode" \| "fleet"` | Pinned at session creation.                                       |
| `model`                  | string                        | Provider-native model id.                                            |
| `cwd`                    | string \| null                | Where the agent operates. Determines on-disk session dir.            |
| `additional_dirs`        | list[string] \| null          | Extra dirs the agent's tools may read/write under.                   |
| `upstream_id`            | string \| null                | Provider-native session id (e.g. opencode session) for resume.       |
| `fleet_config_override`  | dict \| null                  | Per-session override merged on top of the file-level fleet config.   |
| `created_at`             | ISO 8601 + tz                 | Set once.                                                            |
| `updated_at`             | ISO 8601 + tz                 | Bumped on every message append. Drives sort order in the sidebar.    |

Writes go through `_atomic_write_text(path, text)` — write to `.tmp`,
fsync, rename. A crash mid-write leaves either the old file intact or
the fully-written new file; never a torn write.

### `messages.jsonl` (append-only)

One JSON object per line, in append order:

```jsonl
{"role":"user","content":[{"type":"text","text":"Write a scraper..."}],"id":"5f5ad59c...","created_at":"2026-05-09T23:37:19.278Z"}
{"id":"abc123...","role":"assistant","content":[{"type":"tool_use","name":"planner [...]"}],"cost_usd":null,"duration_ms":null,"created_at":"2026-05-09T23:37:25Z"}
{"id":"abc123...","role":"assistant","content":[{"type":"tool_use","name":"planner [...]"},{"type":"tool_result","content":"<plan>"}],"cost_usd":null,"duration_ms":null,"created_at":"2026-05-09T23:39:00Z"}
{"id":"abc123...","role":"assistant","content":[{"type":"tool_use","name":"planner [...]"},{"type":"tool_result","content":"<plan>"},{"type":"tool_use","name":"coder [...]"}],"cost_usd":0.0234,"duration_ms":12345,"created_at":"2026-05-09T23:42:11Z"}
```

Note that the second, third, and fourth lines all share the same `id` —
they're **mid-turn checkpoints** of the same assistant message, written
on each `tool_use` and `tool_result` event. The reader (`list_messages`)
dedups by `id`, keeping the **latest** entry. So the chat user sees one
coherent assistant turn, while the persistence layer is durable to crashes
mid-way through a long workflow.

#### Line shape

| Field         | Type                | Notes                                                  |
| :------------ | :------------------ | :----------------------------------------------------- |
| `id`          | 32-hex              | Auto-generated if omitted on first append.             |
| `role`        | `"user" \| "assistant" \| "system"` | Standard chat roles.                  |
| `content`     | list[block]         | Each block is `{"type": "text" \| "tool_use" \| "tool_result", ...}`. Same shape the WebSocket emits. |
| `cost_usd`    | float \| null       | Set by the provider's `assistant.done` event.          |
| `duration_ms` | int \| null         | Set by the provider's `assistant.done` event.          |
| `created_at`  | ISO 8601 + tz       | When this checkpoint was written.                      |

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
| Session delete           | ✅                | `shutil.rmtree` then atomic index update.                        |
| Cleanup sweep            | ✅ per-session    | Each session is deleted independently. A crash mid-sweep leaves the rest intact; the sentinel only updates after the loop completes. |

Mid-turn checkpoints are intentionally non-atomic across the whole turn —
that's the whole point. We append cheap, idempotent snapshots throughout
the workflow so the user can refresh at any point and see partial
progress. End-of-turn doesn't need a special "finalize" step; the LAST
append IS the final state, dedup picks it up.

## Mid-turn checkpoints

The lifecycle of an assistant turn:

```
WS event sequence                           messages.jsonl appends
─────────────────────────────────────────  ──────────────────────────────────
user prompt arrives                        ┐ user message line   (id: u1)
                                           │
session.started                             │
assistant.tool_use   (planner step)        ├─ assistant line     (id: a1, content: [tool_use(planner)])
…heartbeats during planner work…           │  (heartbeats are NOT persisted)
tool.result          (planner output)      ├─ assistant line     (id: a1, content: [tool_use(planner), tool_result(planner)])
assistant.text       ("Plan saved to…")    │
assistant.tool_use   (coder step)          ├─ assistant line     (id: a1, content: [..., tool_use(coder)])
…heartbeats during coder work…             │
tool.result          (coder output)        ├─ assistant line     (id: a1, content: [..., tool_use(coder), tool_result(coder)])
… continues for reviewer + tester …        │
assistant.done       (cost, duration)      ┘ final assistant line (id: a1, content: [...all blocks...], cost_usd: ..., duration_ms: ...)
```

The trailing assistant line wins on read — the user sees exactly one
assistant message in the chat with all its tool blocks.

This also handles WS disconnect cleanly: if the user reloads the page
during the coder step, the messages endpoint returns the partial state
(everything up to the latest checkpoint), and the frontend's
`loadMessages` helper marks the in-progress turn so live events from
the new WS continue extending the same turn.

## Reading: dedup + pagination

`SessionStore.list_messages(session_id, before=None, limit=N)` follows
this algorithm:

1. Read `messages.jsonl` line by line; parse each as JSON. Skip blank or
   malformed lines silently.
2. Build `latest: dict[id, msg]` — last write wins per id. This collapses
   mid-turn checkpoints to their final state.
3. Sort the values by `created_at` ascending.
4. Apply the `before` cursor: drop messages with `created_at >= before`.
5. Take the trailing `limit` messages (the most recent in the window).
6. Compute `has_more` and `next_before` (the timestamp of the next-older
   message, used as the cursor for the previous page).

The shape of the response matches the legacy `MessagesPage` so the
frontend doesn't notice the migration. Default page size is 50, capped
at 500 (configurable via `MESSAGES_PAGE_DEFAULT` / `MESSAGES_PAGE_MAX`).

## Cleanup + compaction

Sessions are swept on a 24-hour cadence. The sweep does two things:

1. **Delete expired sessions.** Any session whose `meta.updated_at` is
   older than `SESSION_RETENTION_DAYS` (default `7`, configurable via
   `.env` / Settings) gets `shutil.rmtree`'d and its index entry removed.
2. **Compact kept sessions.** For each surviving session, rewrite
   `messages.jsonl` to one entry per id. This reclaims the disk used by
   mid-turn-checkpoint duplicates over the session's lifetime.

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

async def append_message(session_id: str, message: dict[str, Any]) -> dict[str, Any]:
    """Atomic JSONL append. Returns the stored message (with id+created_at filled in)."""

async def list_messages(
    session_id: str, *, before: datetime | None = None, limit: int | None = None
) -> tuple[list[dict[str, Any]], datetime | None, bool]:
    """Returns (messages, next_before, has_more). Messages are deduped by id."""

async def cleanup_expired(*, retention_days: int, force: bool = False) -> dict[str, int]:
    """Sweep stale sessions + compact the rest. Returns {deleted, compacted, kept}."""
```

All methods are async even when their bodies are mostly synchronous — the
`asyncio.Lock` on the global index is the only place we genuinely need
async, but the consistent signature lets callers `await` everything.

## Comparison with upstream models

| Property                    | Claude Code (`~/.claude/`)            | OpenCode (`~/.local/share/opencode/`)        | LocalCode (`<cwd>/.localcode/sessions/`)            |
| :-------------------------- | :------------------------------------ | :------------------------------------------- | :-------------------------------------------------- |
| Storage backend             | JSONL files only                      | SQLite (WAL) + JSON sidecar files            | JSONL files only                                    |
| Per-project namespacing     | `~/.claude/projects/<url-encoded-cwd>/` | Single DB; project_id column                | Sessions live IN the cwd; user-global index for cross-project queries |
| Append model                | Append to `<uuid>.jsonl`              | INSERT row per event                         | Append to `messages.jsonl`                          |
| Mid-turn durability         | Each event is a separate line         | Each event is a separate row                 | Each event is a separate line; deduped by id on read |
| Cleanup                     | `cleanupPeriodDays` setting + sentinel | Manual / not documented                      | `SESSION_RETENTION_DAYS` + `.last-cleanup` sentinel |
| Compaction                  | None — each line is the truth         | None                                         | Yes — cleanup sweep collapses checkpoint duplicates |
| Subagent transcripts        | Separate files under `subagents/agent-<id>` | DB rows                                  | Not yet — orchestrator subagent events are inlined into the parent session's log; could split if it becomes noisy |
| Pluggable remote storage    | `SessionStore` adapter (S3/Redis/PG)  | Not pluggable                                | Not yet — single class; could add an adapter protocol later |
| Lock granularity            | None documented (single CLI process)  | DB-level                                     | Per-session asyncio `Lock` in routes; `O_APPEND` for messages; index-level asyncio `Lock` |

We've got the simpler core (JSONL only, no SQLite at all) plus one feature
both upstream models lack: **explicit compaction** of checkpoint
duplicates. The rest is essentially Claude Code's pattern with a
user-global index added because we needed cross-project listing.

## Migration from Postgres

If you had data in the old Postgres-backed schema, here's a one-shot
migration script. Run it once, then drop the database.

```python
# tools/migrate_pg_to_filestore.py
import asyncio
import os

import asyncpg

from backend.app.storage.sessions import store


async def main():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(db_url)
    try:
        sessions = await conn.fetch("SELECT * FROM sessions ORDER BY created_at")
        for s in sessions:
            new = await store.create_session(
                provider=s["provider"],
                model=s["model"],
                cwd=s["cwd"],
                additional_dirs=s["additional_dirs"],
                title=s["title"],
                upstream_id=s["upstream_id"],
                fleet_config_override=s["fleet_config_override"],
            )
            # Preserve the original id so any in-flight references keep working
            await store.update_session(new["id"], id=s["id"])
            msgs = await conn.fetch(
                "SELECT * FROM messages WHERE session_id=$1 ORDER BY created_at", s["id"]
            )
            for m in msgs:
                await store.append_message(s["id"], {
                    "id": m["id"],
                    "role": m["role"],
                    "content": m["content"],
                    "cost_usd": float(m["cost_usd"]) if m["cost_usd"] is not None else None,
                    "duration_ms": m["duration_ms"],
                    "created_at": m["created_at"].isoformat(),
                })
            print(f"migrated {s['id']}: {len(msgs)} messages")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

This is **not shipped** — copy/paste it if you need it. The current
codebase has no Postgres dependencies, so you'd need to add `asyncpg`
to your local environment temporarily.

## Operational tips

### Inspecting a session manually

```bash
# What sessions exist?
cat ~/.localcode/sessions-index.json | jq

# Read a session's meta
cat /path/to/project/.localcode/sessions/<uuid>/meta.json | jq

# Read its message history (raw, unsorted, with checkpoints)
cat /path/to/project/.localcode/sessions/<uuid>/messages.jsonl | jq -c '{role, id: .id[:8], blocks: (.content|length)}'

# Read deduped (latest per id), oldest first
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
  (`~/.claude/`, `~/.local/share/opencode/auth.json`). LocalCode never
  reads or copies them.
- **In-flight per-turn state** (asyncio locks, approval-channel queues,
  WS connections) is in-memory only. A backend bounce drops this state;
  the persisted JSONL state is what's recovered on restart.

## Source

- [backend/app/storage/sessions.py](../backend/app/storage/sessions.py) — `SessionStore` class, atomic write helpers, cleanup logic.
- [backend/app/routes/sessions.py](../backend/app/routes/sessions.py) — REST + WebSocket consumers of the store.
- [backend/app/main.py](../backend/app/main.py) — wires `cleanup_expired` into the FastAPI lifespan startup hook.
- [backend/app/config.py](../backend/app/config.py) — `Settings.session_retention_days`.
