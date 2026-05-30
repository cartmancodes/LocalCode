"""Filesystem-backed session store.

Replaces the Postgres ``sessions`` + ``messages`` tables with two file
artifacts per session, modelled after Claude Code's
``~/.claude/projects/<project-key>/<session-uuid>.jsonl`` layout:

    <session.cwd>/.localcode/sessions/<session-uuid>/
        meta.json          ← Session metadata. Atomic-rewritten via .tmp+rename.
        messages.jsonl     ← Append-only event log, one JSON object per line.

A user-global index lets us list/get sessions without walking the
filesystem looking for ``.localcode/sessions/`` directories:

    ~/.localcode/sessions-index.json   {session_id: {cwd, created_at}}

Sessions without a cwd live under ``~/.localcode/sessions/_global/`` so
they always have a stable home.

# Design notes

  * **Mid-turn checkpoints append, never rewrite.** Every checkpoint adds
    a new line to ``messages.jsonl`` with the same message id and an
    incrementing ``checkpoint`` counter. ``list_messages`` dedups by id
    and returns the latest line per id. This keeps writes atomic — a
    crash mid-write loses at most the trailing line, never the whole
    file. Compaction at cleanup time collapses each session's log to one
    final entry per id.

  * **POSIX append is atomic for small writes.** Up to ``PIPE_BUF`` bytes
    (4096 on macOS, 8192 on Linux) a single ``write()`` to an
    ``O_APPEND`` fd is atomic against concurrent appends from other fds.
    Our typical message line stays well under that. For lines that
    exceed the limit (e.g. a full markdown plan) the per-session asyncio
    Lock in ``routes/sessions.py`` already serialises turns, so concurrent
    writes can't interleave anyway.

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
from collections.abc import Iterator
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

# Index file mapping ``session_id → {cwd, created_at}``. Lets us list sessions
# across project cwds without filesystem scans. Atomic-rewritten on every
# create / delete via .tmp+rename.
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
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
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


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically — write to ``.tmp``, fsync,
    rename. A crash mid-write leaves either the old file or the new file,
    never a torn write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
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


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(_to_jsonable(payload), ensure_ascii=False))


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
# self-consistent snapshot).
_index_lock = asyncio.Lock()


def _load_index() -> dict[str, dict[str, Any]]:
    """Return ``{session_id: {cwd, created_at}}``. Always returns a fresh
    dict — callers can mutate without affecting cached state."""
    raw = _read_json(INDEX_PATH, default={}) or {}
    if not isinstance(raw, dict):
        logger.warning("sessions-index.json is corrupt; starting fresh")
        return {}
    return raw


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    USER_GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(INDEX_PATH, index)


async def _index_upsert(session_id: str, entry: dict[str, Any]) -> None:
    async with _index_lock:
        idx = _load_index()
        idx[session_id] = entry
        _save_index(idx)


async def _index_remove(session_id: str) -> None:
    async with _index_lock:
        idx = _load_index()
        if session_id in idx:
            del idx[session_id]
            _save_index(idx)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


class SessionStore:
    """Filesystem-backed CRUD for sessions + their message logs."""

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
        sdir = _session_dir(sid, cwd)
        sdir.mkdir(parents=True, exist_ok=True)
        _write_json(sdir / "meta.json", meta)
        # Touch messages.jsonl so the dir is fully provisioned even if no
        # message arrives yet (empty session looks the same as a populated
        # one to readers).
        (sdir / "messages.jsonl").touch(exist_ok=True)
        await _index_upsert(sid, {"cwd": cwd, "created_at": now})
        return meta

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cwd = _resolve_cwd(session_id)
        if cwd is None and not (GLOBAL_SESSIONS_DIR / session_id).exists():
            # Index miss AND no fallback → unknown session.
            return None
        sdir = _session_dir(session_id, cwd)
        meta = _read_json(sdir / "meta.json")
        if not isinstance(meta, dict):
            return None
        return meta

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return all known sessions sorted by ``updated_at`` descending —
        matches the legacy ``ORDER BY updated_at DESC``."""
        idx = _load_index()
        out: list[dict[str, Any]] = []
        stale: list[str] = []
        for sid, entry in idx.items():
            sdir = _session_dir(sid, entry.get("cwd"))
            meta = _read_json(sdir / "meta.json")
            if not isinstance(meta, dict):
                # Index entry points to a session that no longer exists on
                # disk — clean it out so future lists are accurate.
                stale.append(sid)
                continue
            out.append(meta)
        if stale:
            async with _index_lock:
                live = _load_index()
                for sid in stale:
                    live.pop(sid, None)
                _save_index(live)
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    async def update_session(
        self, session_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        """Atomic-rewrite meta.json with the given fields applied. Bumps
        ``updated_at`` automatically."""
        meta = await self.get_session(session_id)
        if meta is None:
            return None
        meta.update(fields)
        meta["updated_at"] = _now_iso()
        sdir = _session_dir(session_id, meta.get("cwd"))
        _write_json(sdir / "meta.json", meta)
        # Mirror updated_at into the index too so list_sessions can sort
        # without rereading every meta.json.
        await _index_upsert(
            session_id,
            {"cwd": meta.get("cwd"), "created_at": meta.get("created_at")},
        )
        return meta

    async def delete_session(self, session_id: str) -> bool:
        cwd = _resolve_cwd(session_id)
        sdir = _session_dir(session_id, cwd)
        existed = sdir.exists()
        if existed:
            shutil.rmtree(sdir, ignore_errors=True)
        await _index_remove(session_id)
        return existed

    async def delete_all_sessions(self) -> int:
        """Wipe every known session. Returns the count for the response."""
        idx = _load_index()
        count = 0
        for sid, entry in list(idx.items()):
            sdir = _session_dir(sid, entry.get("cwd"))
            if sdir.exists():
                shutil.rmtree(sdir, ignore_errors=True)
                count += 1
        async with _index_lock:
            _save_index({})
        return count

    # ───── messages ─────────────────────────────────────────────────────────

    async def append_message(
        self,
        session_id: str,
        message: dict[str, Any],
        *,
        bump_updated_at: bool = True,
        fsync: bool = True,
    ) -> dict[str, Any]:
        """Append a message line to the session's messages.jsonl.

        Mid-turn checkpoints call this with the same ``id`` repeatedly —
        ``list_messages`` dedups by id keeping the latest entry. We don't
        rewrite the file on each checkpoint because append is atomic and
        cheap; a crash loses at most the trailing checkpoint, never the
        whole turn.

        ``bump_updated_at`` controls whether we also rewrite ``meta.json``
        and the user-global index to update the session's ``updated_at``.
        For mid-turn checkpoints set this to ``False`` — they fire on every
        tool boundary and the meta+index rewrite (3 extra file ops per
        checkpoint) dominates the hot path. The turn's ``finally`` clause
        does a single ``update_session`` at end-of-turn to keep the sidebar
        sorted accurately.

        ``fsync`` forces the appended line to disk before returning. Default
        True so a backend crash can't silently lose the last few
        checkpoints. The cost is a single ``fdatasync`` per call.
        """
        cwd = _resolve_cwd(session_id)
        sdir = _session_dir(session_id, cwd)
        if not sdir.exists():
            raise FileNotFoundError(f"unknown session {session_id!r}")
        msg = dict(message)
        msg.setdefault("id", _new_id())
        msg.setdefault("created_at", _now_iso())
        line = json.dumps(_to_jsonable(msg), ensure_ascii=False)
        path = sdir / "messages.jsonl"
        # O_APPEND on POSIX is atomic for writes up to PIPE_BUF; concurrent
        # writers within the process serialise via the per-session lock that
        # SessionRunner holds around turn execution.
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if bump_updated_at:
            await self.update_session(session_id)
        return msg

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
          1. Read messages.jsonl line-by-line, parse each JSON object.
          2. Dedup by ``id``, keeping the LATEST entry — collapses
             mid-turn checkpoints to their final state.
          3. Sort oldest → newest.
          4. Apply ``before`` filter (return msgs whose created_at < before).
          5. Take the trailing ``limit`` (most recent) for pagination.
          6. Reverse to oldest → newest within the page (frontend
             convention).
        """
        cwd = _resolve_cwd(session_id)
        sdir = _session_dir(session_id, cwd)
        path = sdir / "messages.jsonl"
        if not path.exists():
            return [], None, False

        # Dedup keeps insertion order via dict, then we sort by created_at.
        # Stream line-by-line rather than read_text().splitlines() so big
        # logs (long-lived sessions, many mid-turn checkpoints) don't
        # double-allocate the file in memory just to split it.
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

        msgs = list(latest.values())
        msgs.sort(key=lambda m: m.get("created_at", ""))

        if before is not None:
            cutoff = before.isoformat()
            msgs = [m for m in msgs if m.get("created_at", "") < cutoff]

        page_size = limit or len(msgs)
        # Take the trailing window: the N most recent BEFORE the cutoff.
        if len(msgs) > page_size:
            page = msgs[-page_size - 1 :]  # one extra → has_more determination
            has_more = True
            page = page[1:]  # drop the extra; that's the cursor for next call
            next_before_str = page[0].get("created_at") if page else None
        else:
            page = msgs
            has_more = False
            next_before_str = None

        next_before: datetime | None = None
        if next_before_str:
            try:
                next_before = datetime.fromisoformat(next_before_str)
            except ValueError:
                next_before = None

        return page, next_before, has_more

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
        for sid, entry in list(idx.items()):
            cwd = entry.get("cwd")
            sdir = _session_dir(sid, cwd)
            meta = _read_json(sdir / "meta.json")
            if not isinstance(meta, dict):
                # Orphan index entry. Drop it.
                await _index_remove(sid)
                continue
            updated_str = meta.get("updated_at") or meta.get("created_at") or ""
            try:
                updated_ts = datetime.fromisoformat(updated_str).timestamp()
            except ValueError:
                updated_ts = 0
            if updated_ts < cutoff:
                shutil.rmtree(sdir, ignore_errors=True)
                await _index_remove(sid)
                deleted += 1
            else:
                if _compact_messages(sdir / "messages.jsonl"):
                    compacted += 1
                kept += 1

        # Touch the sentinel so the 24h cooldown takes effect.
        CLEANUP_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        CLEANUP_SENTINEL.touch()

        if deleted or compacted:
            logger.info(
                "session cleanup: deleted=%d compacted=%d kept=%d (retention=%dd)",
                deleted, compacted, kept, retention_days,
            )
        return {"deleted": deleted, "compacted": compacted, "kept": kept}


# Module-level singleton so the import-time circular check stays tidy and
# routes get a stable reference.
store = SessionStore()


# ─────────────────────────────────────────────────────────────────────────────
# Internals
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
    if entry is None:
        return None
    return entry.get("cwd")


def _compact_messages(path: Path) -> bool:
    """Rewrite messages.jsonl with one entry per id (latest wins), in
    chronological order. Returns True if the file shrunk.

    Run during cleanup so long-lived sessions don't accumulate stale
    mid-turn checkpoints forever. No-op for sessions whose log already
    has unique ids (the common case for newer turns).
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
