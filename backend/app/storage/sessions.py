"""Filesystem-backed session store.

Replaces the Postgres ``sessions`` + ``messages`` tables with a small set of
file artifacts per session, modelled after Claude Code's
``~/.claude/projects/<project-key>/<session-uuid>.jsonl`` layout:

    <session.cwd>/.localcode/sessions/<session-uuid>/
        meta.json          ← Session metadata. Atomic-rewritten via .tmp+rename.
        messages.jsonl     ← Append-only log, one line per *finalized* message.
        current.json       ← The in-progress assistant message, overwritten by
                             each mid-turn checkpoint. Absent between turns.

A user-global index lets us list/get sessions without walking the
filesystem looking for ``.localcode/sessions/`` directories:

    ~/.localcode/sessions-index.json   {session_id: {...SessionOut fields}}

Sessions without a cwd live under ``~/.localcode/sessions/_global/`` so
they always have a stable home.

# Design notes

  * **The in-progress message lives outside the log.** Mid-turn checkpoints
    used to append a full snapshot of the growing assistant message to
    ``messages.jsonl`` and readers deduped by id, keeping the last line.
    That made write volume quadratic in the turn's size (a 200-tool turn
    ending at 5 MB wrote ~500 MB), made the log grow without bound for an
    active session, and forced every reader to scan the whole file. A
    checkpoint now atomically replaces ``current.json``, which never grows
    beyond one message; the turn's final write appends that message to the
    log **once** and removes ``current.json``.

  * **Crash recovery is unchanged in effect.** A ``current.json`` left
    behind by a killed backend is a coherent message: readers return it as
    the newest message, and the next append promotes it into the log (once)
    before writing its own line. Reads never promote — during a live turn
    ``current.json`` is the message still being written, and promoting it
    would duplicate it.

  * **fsync discipline.** The final write of a message fsyncs. Mid-turn
    checkpoints do not: a checkpoint exists so a crash does not lose the
    whole turn, not so a crash loses nothing, and that path is the hot one.

  * **Because every message now appears in the log exactly once, readers
    don't dedupe and don't scan.** ``list_messages`` reads a bounded tail
    window and widens it only if the page wasn't satisfied. A log the window
    can't serve — one written before this change, where every line is a full
    snapshot — degrades to the old line-at-a-time reader rather than holding
    multiple copies of the file (see ``_TAIL_MAX_SPAN_BYTES``). The dedupe
    likewise survives only for those older logs.

  * **Nothing here runs on the event loop.** Each public coroutine extracts
    its synchronous body into a ``_sync_*`` helper and awaits it through
    ``asyncio.to_thread``. One loop serves every session, so an inline
    ``open()``/``fsync()``/``rmtree()`` stalls all of them — cheap on a
    local SSD (0.04 ms average, measured), a freeze on a network
    filesystem. Per-session serialization is unchanged: the caller's lock
    (and the module-level index lock) is held across the offload.

  * **The index mirrors what the sidebar needs.** ``GET /api/sessions`` used
    to open and parse one ``meta.json`` per session. The index entry now
    carries every field ``SessionOut`` exposes, written on create and on
    update; ``meta.json`` stays the source of truth on disk and is read only
    for an entry written before the index was widened.

  * **Cleanup is opportunistic + bounded.** A ``.last-cleanup`` sentinel
    in the index dir lets us run the sweep at most once every 24 h.
    Triggered from FastAPI's lifespan startup — silent no-op when the
    sentinel is fresh.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

# Where the user-global sessions index + the _global session bucket live.
# Mirrors ``~/.localcode/plans/`` which the orchestrator already writes into.
USER_GLOBAL_DIR = Path.home() / ".localcode"

# Index file mapping ``session_id → mirrored meta fields``. Lets us list
# sessions across project cwds without filesystem scans. Atomic-rewritten on
# every create / update / delete via .tmp+rename.
INDEX_PATH = USER_GLOBAL_DIR / "sessions-index.json"

# Bucket for sessions without a cwd. Symmetric with how cwd-bearing sessions
# live at ``<cwd>/.localcode/sessions/<id>/``.
GLOBAL_SESSIONS_DIR = USER_GLOBAL_DIR / "sessions" / "_global"

# Sentinel file whose mtime is the last cleanup-sweep timestamp. Cleanup is
# gated on this being older than the sweep interval.
CLEANUP_SENTINEL = USER_GLOBAL_DIR / "sessions" / ".last-cleanup"

# How long before re-sweeping. 24 h matches Claude Code's cleanup cadence
# and is short enough that a long-running backend still cleans up regularly.
CLEANUP_INTERVAL_S = 24 * 60 * 60

# Filenames inside a session dir.
MESSAGES_FILE = "messages.jsonl"
CURRENT_FILE = "current.json"
META_FILE = "meta.json"

# First tail window for a bounded read of the log. 64 KiB covers tens of
# typical message lines, so one page is one read; the window doubles — reading
# *older* bytes, never the same bytes twice — until the page plus its cursor
# entry are in hand.
_TAIL_WINDOW_BYTES = 64 * 1024

# Ceiling on how much of the log a windowed read may hold. Parsing a window
# costs several times its size (the joined bytes, the decoded text, the split
# lines), so a log the window can't satisfy — a pre-``current.json`` log whose
# lines are megabytes each, or a page of unusually large messages — must not be
# windowed at all: past this point we drop the chunks and stream the file line
# by line instead, which costs one pass but holds only one line plus the
# deduped messages. Without this, a 20 MB legacy log peaked at 4x its size in
# memory on the first read after upgrading.
_TAIL_MAX_SPAN_BYTES = 1024 * 1024

# Fields the index mirrors out of meta.json. Exactly the ``SessionOut`` field
# set: the sidebar renders additional_dirs / permission_mode /
# fleet_config_override too, and `GET /api/sessions` is the only endpoint that
# ever hands the frontend a session row.
_INDEX_MIRRORED_FIELDS = (
    "id",
    "title",
    "provider",
    "model",
    "cwd",
    "additional_dirs",
    "upstream_id",
    "permission_mode",
    "fleet_config_override",
    "created_at",
    "updated_at",
)

# An entry missing any of these was written before the index was widened, so
# it can't serve a session row on its own. ``cwd`` is deliberately absent —
# None is a legitimate value (the _global bucket).
_INDEX_REQUIRED_FIELDS = ("title", "provider", "model", "created_at", "updated_at")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _new_id() -> str:
    """Match the legacy SQLAlchemy ``_uuid`` shape (32-hex no dashes) so any
    persisted references in old data still resolve."""
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _to_jsonable(obj: Any) -> Any:
    """Convert datetime / Decimal etc. into json-safe primitives.

    Mirrors what we used to do at the SQLAlchemy → JSON boundary so the
    on-wire shape consumed by the frontend is unchanged.

    The primitive pass-through below is load-bearing, not a fast path. ``bool``
    and ``int`` both implement ``__float__``, so without it the ``Decimal``
    branch caught every one of them and coerced it: a persisted
    ``"is_error": true`` came back as ``1.0``, a tool input's ``"limit": 100``
    as ``100.0``, and every ``duration_ms`` and token count in the stored
    transcript as a float. Nothing raised — JSON has one number type and
    ``1.0 == True`` in Python — which is exactly why it survived until the
    replay/matrix suites asserted on the persisted blocks by *identity* rather
    than equality. ``Decimal`` reaches the branch below unchanged.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if hasattr(obj, "__float__"):  # Decimal
        try:
            return float(obj)
        except (TypeError, ValueError):
            return str(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def _atomic_write_text(path: Path, text: str, *, fsync: bool = True) -> None:
    """Write ``text`` to ``path`` atomically — write to ``.tmp``, fsync,
    rename. A crash mid-write leaves either the old file or the new file,
    never a torn write.

    ``fsync=False`` skips the durability barrier for writes whose loss is
    acceptable (a mid-turn checkpoint): the rename is still atomic, so a
    crash can leave the previous checkpoint or an unreadable file, never a
    half-parsed one. Readers treat an unreadable ``current.json`` as absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    tmp.replace(path)  # atomic on POSIX


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("failed to read %s: %s", path, exc)
        return default


def _write_json(path: Path, payload: Any, *, fsync: bool = True) -> None:
    _atomic_write_text(
        path, json.dumps(_to_jsonable(payload), ensure_ascii=False), fsync=fsync
    )


def _project_key(cwd: str | None) -> str:
    """A filesystem-safe identifier for the cwd. Used in the user-global
    index for grouping; the actual session dir lives under the cwd directly,
    not under a project-key subdir.

    Modelled after Claude Code's ``-Users-shubhojeet-Projects-LocalCode``
    encoding (path with ``/`` → ``-``)."""
    if not cwd:
        return "_global"
    return re.sub(r"[^a-zA-Z0-9]+", "-", str(cwd)).strip("-") or "_global"


def _session_dir(session_id: str, cwd: str | None) -> Path:
    """Resolve the on-disk dir for a session.

    Sessions with a cwd live at ``<cwd>/.localcode/sessions/<id>/`` so they
    sit alongside any plans the orchestrator wrote for that project.
    Sessions without a cwd fall back to the user-global ``_global`` bucket.
    """
    if cwd:
        return Path(cwd) / ".localcode" / "sessions" / session_id
    return GLOBAL_SESSIONS_DIR / session_id


# ─────────────────────────────────────────────────────────────────────────────
# Index
# ─────────────────────────────────────────────────────────────────────────────


# Module-level lock — only one writer at a time on the index file. Read paths
# are unlocked since the file is written atomically (they always see a
# self-consistent snapshot). Held across the thread offload, which is what
# serializes writers.
_index_lock = asyncio.Lock()


def _load_index() -> dict[str, dict[str, Any]]:
    """Return ``{session_id: entry}``. Always returns a fresh dict — callers
    can mutate without affecting cached state."""
    raw = _read_json(INDEX_PATH, default={}) or {}
    if not isinstance(raw, dict):
        logger.warning("sessions-index.json is corrupt; starting fresh")
        return {}
    return raw


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    USER_GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(INDEX_PATH, index)


def _index_entry(meta: dict[str, Any]) -> dict[str, Any]:
    """Project meta.json down to the fields a session row needs."""
    return {k: meta.get(k) for k in _INDEX_MIRRORED_FIELDS}


def _session_from_index(session_id: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Build a session row straight from its index entry, or None when the
    entry predates the widening and meta.json has to be read instead."""
    if not isinstance(entry, dict):
        return None
    if any(entry.get(k) is None for k in _INDEX_REQUIRED_FIELDS):
        return None
    row = {k: entry.get(k) for k in _INDEX_MIRRORED_FIELDS}
    row["id"] = session_id
    return row


def _sync_index_upsert(session_id: str, entry: dict[str, Any]) -> None:
    idx = _load_index()
    idx[session_id] = entry
    _save_index(idx)


def _sync_index_remove(session_ids: list[str]) -> None:
    idx = _load_index()
    removed = False
    for sid in session_ids:
        removed = idx.pop(sid, None) is not None or removed
    if removed:
        _save_index(idx)


async def _index_upsert(session_id: str, entry: dict[str, Any]) -> None:
    async with _index_lock:
        await asyncio.to_thread(_sync_index_upsert, session_id, entry)


async def _index_remove(session_id: str) -> None:
    async with _index_lock:
        await asyncio.to_thread(_sync_index_remove, [session_id])


# ─────────────────────────────────────────────────────────────────────────────
# Message log reading
# ─────────────────────────────────────────────────────────────────────────────


def _ordered_messages(
    messages: Iterable[dict[str, Any]], cutoff: str | None
) -> list[dict[str, Any]]:
    """Apply the ``before`` cutoff and sort oldest first."""
    msgs = list(messages)
    if cutoff is not None:
        msgs = [m for m in msgs if str(m.get("created_at", "")) < cutoff]
    msgs.sort(key=lambda m: str(m.get("created_at", "")))
    return msgs


def _dedupe_by_id(objs: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Keep the last object per id, skipping anything unusable.

    Dedupe survives for two reasons, neither of which is the hot path: a log
    written before the in-progress message moved to ``current.json`` holds one
    line per mid-turn checkpoint, and a promotion interrupted between the
    append and the unlink can repeat one line.
    """
    latest: dict[str, dict[str, Any]] = {}
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        mid = obj.get("id")
        if mid:
            latest[mid] = obj
    return latest


def _iter_json_lines(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _parse_log_lines(blob: bytes, cutoff: str | None) -> list[dict[str, Any]]:
    """Parse a window of ``messages.jsonl`` into messages, oldest first."""
    decoded = blob.decode("utf-8", errors="replace")
    latest = _dedupe_by_id(_iter_json_lines(decoded.splitlines()))
    return _ordered_messages(latest.values(), cutoff)


def _stream_entries(path: Path, cutoff: str | None) -> list[dict[str, Any]]:
    """Read the whole log one line at a time, oldest first.

    The fallback for a log a window can't serve. Holds one line plus the
    deduped messages, never the file — which is what keeps the first read of a
    pre-``current.json`` log (one multi-MB snapshot line per checkpoint) from
    costing several times the file in memory.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            latest = _dedupe_by_id(_iter_json_lines(f))
    except OSError:
        return []
    return _ordered_messages(latest.values(), cutoff)


def _tail_entries(path: Path, *, want: int | None, cutoff: str | None) -> list[dict[str, Any]]:
    """Read back from the end of the log until ``want`` messages match.

    ``want`` is the page size plus one — the extra entry is what tells the
    caller there is an older page, so stopping as soon as ``want`` are in hand
    is enough to decide ``has_more``. ``want=None`` means no page limit, which
    is the streaming reader's job. Returns messages oldest-first. Bytes are
    read at most once: each widening seeks to an older offset and prepends,
    rather than re-reading the tail it already has.

    Falls back to ``_stream_entries`` when windowing would have to hold more
    than ``_TAIL_MAX_SPAN_BYTES`` — see that constant for why.
    """
    if want is None:
        return _stream_entries(path, cutoff)
    try:
        size = path.stat().st_size
    except OSError:
        return []

    chunks: list[bytes] = []
    pos = size
    window = _TAIL_WINDOW_BYTES
    with path.open("rb") as f:
        while size - max(0, pos - window) <= _TAIL_MAX_SPAN_BYTES:
            start = max(0, pos - window)
            f.seek(start)
            chunks.insert(0, f.read(pos - start))
            pos = start
            blob = b"".join(chunks)
            if pos > 0:
                # The window boundary lands mid-line and half a JSON object
                # is not parseable. Drop the leading fragment; the next
                # widening picks that line up whole.
                nl = blob.find(b"\n")
                blob = blob[nl + 1 :] if nl >= 0 else b""
            msgs = _parse_log_lines(blob, cutoff)
            if pos == 0 or len(msgs) >= want:
                return msgs
            window *= 2
    # Drop the accumulated window before the streaming pass allocates.
    chunks.clear()
    return _stream_entries(path, cutoff)


# ─────────────────────────────────────────────────────────────────────────────
# Synchronous bodies — every one of these runs in a worker thread
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_cwd(session_id: str) -> str | None:
    """Look up a session's cwd in the index. Used by every read path so
    we don't have to scan the filesystem.

    Returns ``None`` for sessions registered with no cwd (they live in the
    _global bucket) AND for unknown sessions. Callers that need to
    distinguish should test ``session_dir(...).exists()`` separately.
    """
    idx = _load_index()
    entry = idx.get(session_id)
    if not isinstance(entry, dict):
        return None
    return entry.get("cwd")


def _sync_create_session(meta: dict[str, Any]) -> None:
    sdir = _session_dir(meta["id"], meta.get("cwd"))
    sdir.mkdir(parents=True, exist_ok=True)
    _write_json(sdir / META_FILE, meta)
    # Touch messages.jsonl so the dir is fully provisioned even if no
    # message arrives yet (empty session looks the same as a populated
    # one to readers).
    (sdir / MESSAGES_FILE).touch(exist_ok=True)


def _sync_get_session(session_id: str) -> dict[str, Any] | None:
    cwd = _resolve_cwd(session_id)
    if cwd is None and not (GLOBAL_SESSIONS_DIR / session_id).exists():
        # Index miss AND no fallback → unknown session.
        return None
    meta = _read_json(_session_dir(session_id, cwd) / META_FILE)
    if not isinstance(meta, dict):
        return None
    return meta


def _sync_list_sessions() -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(rows sorted by updated_at desc, stale session ids)``."""
    idx = _load_index()
    out: list[dict[str, Any]] = []
    stale: list[str] = []
    for sid, entry in idx.items():
        sdir = _session_dir(sid, entry.get("cwd") if isinstance(entry, dict) else None)
        row = _session_from_index(sid, entry)
        if row is None:
            # Entry written before the index carried a full session row.
            # Fall back to meta.json so existing installs keep working; the
            # entry widens on this session's next update.
            meta = _read_json(sdir / META_FILE)
            if not isinstance(meta, dict):
                stale.append(sid)
                continue
            row = meta
        elif not (sdir / META_FILE).exists():
            # A stat, not a read: keeps the index self-healing against a
            # session deleted out-of-band without reopening N files.
            stale.append(sid)
            continue
        out.append(row)
    out.sort(key=lambda m: str(m.get("updated_at", "")), reverse=True)
    return out, stale


def _sync_update_session(session_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    meta = _sync_get_session(session_id)
    if meta is None:
        return None
    meta.update(fields)
    meta["updated_at"] = _now_iso()
    _write_json(_session_dir(session_id, meta.get("cwd")) / META_FILE, meta)
    return meta


def _sync_delete_session(session_id: str) -> bool:
    sdir = _session_dir(session_id, _resolve_cwd(session_id))
    existed = sdir.exists()
    if existed:
        shutil.rmtree(sdir, ignore_errors=True)
    return existed


def _sync_delete_all_sessions() -> int:
    idx = _load_index()
    count = 0
    for sid, entry in list(idx.items()):
        sdir = _session_dir(sid, entry.get("cwd") if isinstance(entry, dict) else None)
        if sdir.exists():
            shutil.rmtree(sdir, ignore_errors=True)
            count += 1
    _save_index({})
    return count


def _require_session_dir(session_id: str) -> Path:
    sdir = _session_dir(session_id, _resolve_cwd(session_id))
    if not sdir.exists():
        raise FileNotFoundError(f"unknown session {session_id!r}")
    return sdir


def _fill_message(message: dict[str, Any]) -> dict[str, Any]:
    msg = dict(message)
    msg.setdefault("id", _new_id())
    msg.setdefault("created_at", _now_iso())
    return msg


def _sync_write_current(session_id: str, message: dict[str, Any]) -> dict[str, Any]:
    sdir = _require_session_dir(session_id)
    msg = _fill_message(message)
    _write_json(sdir / CURRENT_FILE, msg, fsync=False)
    return msg


def _sync_append_message(
    session_id: str, message: dict[str, Any], fsync: bool
) -> dict[str, Any]:
    sdir = _require_session_dir(session_id)
    msg = _fill_message(message)
    lines = [json.dumps(_to_jsonable(msg), ensure_ascii=False)]

    current_path = sdir / CURRENT_FILE
    current = _read_json(current_path) if current_path.exists() else None
    if isinstance(current, dict) and current.get("id") and current["id"] != msg.get("id"):
        # A checkpoint from a turn that never finalized (killed backend).
        # Promote it ahead of the new line so the log stays in created_at
        # order, then drop the file so it can't be promoted twice.
        lines.insert(0, json.dumps(_to_jsonable(current), ensure_ascii=False))

    # O_APPEND on POSIX is atomic for writes up to PIPE_BUF; concurrent
    # writers within the process serialise via the per-session lock that
    # SessionRunner holds around turn execution.
    with (sdir / MESSAGES_FILE).open("a", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    if current_path.exists():
        # Either we just promoted it or this append IS its final form; either
        # way the log now owns the message.
        current_path.unlink(missing_ok=True)
    return msg


def _sync_list_messages(
    session_id: str, before: datetime | None, limit: int | None
) -> tuple[list[dict[str, Any]], datetime | None, bool]:
    sdir = _session_dir(session_id, _resolve_cwd(session_id))
    log_path = sdir / MESSAGES_FILE
    current_path = sdir / CURRENT_FILE
    if not log_path.exists() and not current_path.exists():
        return [], None, False

    cutoff = before.isoformat() if before is not None else None
    msgs: list[dict[str, Any]] = []
    if log_path.exists():
        # One extra entry beyond the page: that's what decides ``has_more``.
        msgs = _tail_entries(log_path, want=(limit + 1) if limit else None, cutoff=cutoff)

    current = _read_json(current_path) if current_path.exists() else None
    if isinstance(current, dict) and current.get("id"):
        # The in-progress message is newer than everything in the log by
        # construction — an append either promotes or supersedes it — so it is
        # overlaid at the end rather than sorted in. A reader never promotes
        # it: during a live turn this is the message still being written.
        if cutoff is None or str(current.get("created_at", "")) < cutoff:
            msgs = [m for m in msgs if m.get("id") != current["id"]]
            msgs.append(current)

    page_size = limit or len(msgs)
    # Take the trailing window: the N most recent BEFORE the cutoff.
    if len(msgs) > page_size:
        page = msgs[-page_size - 1 :]  # one extra → has_more determination
        has_more = True
        page = page[1:]  # drop the extra; page[0] is the next call's cursor
        next_before_str = page[0].get("created_at") if page else None
    else:
        page = msgs
        has_more = False
        next_before_str = None

    next_before: datetime | None = None
    if next_before_str:
        try:
            next_before = datetime.fromisoformat(str(next_before_str))
        except ValueError:
            next_before = None

    return page, next_before, has_more


def _sync_cleanup_expired(retention_days: int, force: bool) -> dict[str, int]:
    if not force and CLEANUP_SENTINEL.exists():
        age = time.time() - CLEANUP_SENTINEL.stat().st_mtime
        if age < CLEANUP_INTERVAL_S:
            logger.debug(
                "skipping session cleanup: ran %.0fs ago (interval %ds)",
                age, CLEANUP_INTERVAL_S,
            )
            return {"deleted": 0, "compacted": 0, "kept": 0, "skipped": True}

    cutoff = _now().timestamp() - retention_days * 24 * 3600
    idx = _load_index()
    deleted = 0
    compacted = 0
    kept = 0
    # The index is rewritten once at the end rather than per session: a crash
    # mid-sweep leaves entries whose dirs are gone, and list_sessions already
    # prunes those.
    survivors: dict[str, dict[str, Any]] = {}
    for sid, entry in idx.items():
        cwd = entry.get("cwd") if isinstance(entry, dict) else None
        sdir = _session_dir(sid, cwd)
        meta = _read_json(sdir / META_FILE)
        if not isinstance(meta, dict):
            continue  # orphan index entry — drop it
        updated_str = meta.get("updated_at") or meta.get("created_at") or ""
        try:
            updated_ts = datetime.fromisoformat(updated_str).timestamp()
        except ValueError:
            updated_ts = 0
        if updated_ts < cutoff:
            shutil.rmtree(sdir, ignore_errors=True)
            deleted += 1
            continue
        if _compact_messages(sdir / MESSAGES_FILE):
            compacted += 1
        kept += 1
        survivors[sid] = entry
    if len(survivors) != len(idx):
        _save_index(survivors)

    # Touch the sentinel so the 24h cooldown takes effect.
    CLEANUP_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    CLEANUP_SENTINEL.touch()

    if deleted or compacted:
        logger.info(
            "session cleanup: deleted=%d compacted=%d kept=%d (retention=%dd)",
            deleted, compacted, kept, retention_days,
        )
    return {"deleted": deleted, "compacted": compacted, "kept": kept}


def _compact_messages(path: Path) -> bool:
    """Rewrite messages.jsonl with one entry per id (latest wins), in
    chronological order. Returns True if the file shrunk.

    Only logs written before the in-progress message moved to ``current.json``
    have anything to collapse — newer turns append each message once — so this
    is a migration path for old sessions rather than ongoing maintenance.
    """
    if not path.exists():
        return False
    original_size = path.stat().st_size
    latest: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = obj.get("id")
            if mid:
                latest[mid] = obj
    rebuilt = sorted(latest.values(), key=lambda m: m.get("created_at", ""))
    new_text = "\n".join(
        json.dumps(_to_jsonable(m), ensure_ascii=False) for m in rebuilt
    )
    if new_text:
        new_text += "\n"
    _atomic_write_text(path, new_text)
    return path.stat().st_size < original_size


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


class SessionStore:
    """Filesystem-backed CRUD for sessions + their message logs.

    Every method is a coroutine that does its filesystem work in a worker
    thread. Nothing here holds the event loop.
    """

    # ───── sessions ─────────────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        provider: str,
        model: str,
        cwd: str | None = None,
        additional_dirs: list[str] | None = None,
        title: str = "New chat",
        upstream_id: str | None = None,
        permission_mode: str | None = None,
        fleet_config_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sid = _new_id()
        now = _now_iso()
        meta: dict[str, Any] = {
            "id": sid,
            "title": title,
            "provider": provider,
            "model": model,
            "cwd": cwd,
            "additional_dirs": additional_dirs,
            "upstream_id": upstream_id,
            "permission_mode": permission_mode,
            "fleet_config_override": fleet_config_override,
            "created_at": now,
            "updated_at": now,
        }
        await asyncio.to_thread(_sync_create_session, meta)
        await _index_upsert(sid, _index_entry(meta))
        return meta

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(_sync_get_session, session_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return all known sessions sorted by ``updated_at`` descending —
        matches the legacy ``ORDER BY updated_at DESC``.

        Served from the index. ``meta.json`` is opened only for an entry
        written before the index carried a full session row.
        """
        rows, stale = await asyncio.to_thread(_sync_list_sessions)
        if stale:
            # Index entries pointing at sessions that no longer exist on disk —
            # clean them out so future lists are accurate.
            async with _index_lock:
                await asyncio.to_thread(_sync_index_remove, stale)
        return rows

    async def update_session(
        self, session_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        """Atomic-rewrite meta.json with the given fields applied. Bumps
        ``updated_at`` automatically."""
        meta = await asyncio.to_thread(_sync_update_session, session_id, fields)
        if meta is None:
            return None
        # Re-mirror into the index so the sidebar can be served without
        # reopening meta.json.
        await _index_upsert(session_id, _index_entry(meta))
        return meta

    async def delete_session(self, session_id: str) -> bool:
        existed = await asyncio.to_thread(_sync_delete_session, session_id)
        await _index_remove(session_id)
        return existed

    async def delete_all_sessions(self) -> int:
        """Wipe every known session. Returns the count for the response."""
        async with _index_lock:
            return await asyncio.to_thread(_sync_delete_all_sessions)

    # ───── messages ─────────────────────────────────────────────────────────

    async def append_message(
        self,
        session_id: str,
        message: dict[str, Any],
        *,
        bump_updated_at: bool = True,
        fsync: bool = True,
    ) -> dict[str, Any]:
        """Append one finalized message to the session's messages.jsonl.

        Each message reaches the log exactly once, so readers neither dedupe
        nor scan. An in-progress assistant message belongs in
        ``write_current`` instead; this call finalizes it (and clears
        ``current.json``, promoting an orphan left by a killed backend).

        ``bump_updated_at`` controls whether we also rewrite ``meta.json``
        and the user-global index to update the session's ``updated_at``.
        The turn's ``finally`` clause does a single ``update_session`` at
        end-of-turn to keep the sidebar sorted accurately.

        ``fsync`` forces the appended line to disk before returning. Default
        True: the final form of a message is worth a ``fdatasync``.
        """
        msg = await asyncio.to_thread(_sync_append_message, session_id, message, fsync)
        if bump_updated_at:
            await self.update_session(session_id)
        return msg

    async def write_current(
        self, session_id: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Checkpoint the in-progress assistant message to ``current.json``.

        Atomic replace, no fsync, no ``updated_at`` bump — this fires on tool
        boundaries and the file never grows beyond one message. Readers treat
        it as the newest message; the turn's final ``append_message`` moves it
        into the log and removes it.
        """
        return await asyncio.to_thread(_sync_write_current, session_id, message)

    async def list_messages(
        self,
        session_id: str,
        *,
        before: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], datetime | None, bool]:
        """Return ``(messages, next_before, has_more)`` matching the legacy
        ``MessagesPage`` shape.

        Implementation:
          1. Read a bounded tail of messages.jsonl — enough for the page plus
             one entry to decide ``has_more`` — widening only if the ``before``
             filter left too few.
          2. Overlay ``current.json`` as the newest message when present.
          3. Apply ``before`` (return msgs whose created_at < before).
          4. Take the trailing ``limit`` (most recent) for pagination,
             oldest → newest within the page (frontend convention).
        """
        return await asyncio.to_thread(_sync_list_messages, session_id, before, limit)

    # ───── cleanup ──────────────────────────────────────────────────────────

    async def cleanup_expired(
        self, *, retention_days: int, force: bool = False
    ) -> dict[str, int]:
        """Sweep sessions whose ``updated_at`` is older than the retention
        window. Bounded by a 24h cooldown via ``CLEANUP_SENTINEL``.

        Set ``force=True`` to skip the cooldown (used in tests / ops).

        Returns ``{deleted: N, compacted: M, kept: K}`` for visibility.
        """
        if retention_days <= 0:
            logger.info("session retention disabled (retention_days <= 0)")
            return {"deleted": 0, "compacted": 0, "kept": 0}
        # The whole sweep runs under the index lock: it rewrites the index once
        # at the end, so a concurrent upsert would otherwise be lost.
        async with _index_lock:
            return await asyncio.to_thread(_sync_cleanup_expired, retention_days, force)


# Module-level singleton so the import-time circular check stays tidy and
# routes get a stable reference.
store = SessionStore()
