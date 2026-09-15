"""Cost regressions for the filesystem session store.

These are the net under the audit's persistence defects. Each test measures
real bytes moved or real files opened, never call counts, because the defects
are about volume:

  * D13.2 — a mid-turn checkpoint used to append a *full snapshot* of the
    growing assistant message to ``messages.jsonl``. 200 checkpoints on a
    turn ending at 2 MB wrote ~200 MB.
  * D13.3 — nothing shrank that log while the session stayed in use.
  * D13.4 — one page of messages read (and dict-deduped) the entire file.
  * D13.5 — ``GET /api/sessions`` opened and parsed every session's
    ``meta.json``.

The paging-contract tests exist for a different reason: the frontend pages on
``messages`` / ``next_before`` / ``has_more``, so they pin the *current*
behaviour byte-for-byte and must pass before and after the refactor.
"""
from __future__ import annotations

import json
import shutil
import tracemalloc
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.app.schemas import SessionOut
from backend.app.session_runner.accumulator import TurnAccumulator
from backend.app.storage import sessions as sessions_mod
from backend.app.storage.sessions import store

# Fixed clock for the paging tests so cursor assertions are exact rather than
# "whatever now() happened to be".
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Measurement: count bytes and opens per file, through pathlib.Path.open
# ─────────────────────────────────────────────────────────────────────────────


class FileIOCounter:
    """Per-filename byte and open counts.

    Keyed by basename with a trailing ``.tmp`` stripped, so the atomic
    ``write .tmp + rename`` dance is charged to the file it targets.
    """

    def __init__(self) -> None:
        self.bytes_written: dict[str, int] = defaultdict(int)
        self.bytes_read: dict[str, int] = defaultdict(int)
        self.write_opens: dict[str, int] = defaultdict(int)
        self.read_opens: dict[str, int] = defaultdict(int)

    def message_bytes_written(self) -> int:
        return self.bytes_written["messages.jsonl"] + self.bytes_written["current.json"]

    def message_writes(self) -> int:
        return self.write_opens["messages.jsonl"] + self.write_opens["current.json"]


class _CountingHandle:
    """File-object proxy that tallies every byte that crosses it."""

    def __init__(self, inner: Any, counter: FileIOCounter, key: str) -> None:
        self._inner = inner
        self._counter = counter
        self._key = key

    def __enter__(self) -> _CountingHandle:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)

    def write(self, data: Any) -> Any:
        self._counter.bytes_written[self._key] += _bytelen(data)
        return self._inner.write(data)

    def read(self, *args: Any) -> Any:
        data = self._inner.read(*args)
        self._counter.bytes_read[self._key] += _bytelen(data)
        return data

    def readline(self, *args: Any) -> Any:
        data = self._inner.readline(*args)
        self._counter.bytes_read[self._key] += _bytelen(data)
        return data

    def __iter__(self) -> Iterator[Any]:
        for line in self._inner:
            self._counter.bytes_read[self._key] += _bytelen(line)
            yield line

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _bytelen(data: Any) -> int:
    if isinstance(data, str):
        return len(data.encode("utf-8"))
    try:
        return len(data)
    except TypeError:  # pragma: no cover — non-sized payloads aren't written
        return 0


def _normalize(name: str) -> str:
    return name[:-4] if name.endswith(".tmp") else name


@contextmanager
def counting_file_io(monkeypatch: pytest.MonkeyPatch) -> Iterator[FileIOCounter]:
    """Count bytes/opens for every ``Path.open`` inside the block."""
    counter = FileIOCounter()
    real_open = Path.open

    def patched(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, mode, *args, **kwargs)
        key = _normalize(self.name)
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            counter.write_opens[key] += 1
        else:
            counter.read_opens[key] += 1
        return _CountingHandle(handle, counter, key)

    monkeypatch.setattr(Path, "open", patched)
    try:
        yield counter
    finally:
        monkeypatch.setattr(Path, "open", real_open)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _new_session(home: Path, **fields: Any) -> dict[str, Any]:
    return await store.create_session(
        provider="claude", model="claude-sonnet-4-6", cwd=str(home / "proj"), **fields
    )


def _session_dir(meta: dict[str, Any]) -> Path:
    return Path(meta["cwd"]) / ".localcode" / "sessions" / meta["id"]


async def _seed(sid: str, count: int, *, filler: int = 0) -> None:
    """Append ``count`` messages one second apart on the fixed clock."""
    for i in range(count):
        text = f"m{i}" + ("x" * filler)
        await store.append_message(
            sid,
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": [{"type": "text", "text": text}],
                "created_at": (BASE_TS + timedelta(seconds=i)).isoformat(),
            },
            bump_updated_at=False,
            fsync=False,
        )


def _texts(msgs: list[dict[str, Any]]) -> list[str]:
    return [m["content"][0]["text"] for m in msgs]


# A log in the pre-``current.json`` format: one snapshot line per mid-turn
# checkpoint, ten per assistant message, each line ~100 KB — which is what a
# tool-heavy turn actually left behind. 200 lines ≈ 20 MB.
_LEGACY_IDS = 20
_LEGACY_LINES_PER_ID = 10
_LEGACY_LINE_FILLER = 100_000


def _write_legacy_log(path: Path) -> int:
    """Write a pre-change log directly (no store API can produce one now).
    Returns its size."""
    with path.open("w", encoding="utf-8") as f:
        for i in range(_LEGACY_IDS):
            for n in range(_LEGACY_LINES_PER_ID):
                f.write(
                    json.dumps(
                        {
                            "id": f"legacy{i:03d}",
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": f"c{n}" + "x" * _LEGACY_LINE_FILLER}
                            ],
                            "created_at": (
                                BASE_TS + timedelta(seconds=i * 100 + n)
                            ).isoformat(),
                        }
                    )
                    + "\n"
                )
    return path.stat().st_size


# ─────────────────────────────────────────────────────────────────────────────
# D13.2 — checkpoint write volume
# ─────────────────────────────────────────────────────────────────────────────


async def test_two_hundred_checkpoints_write_under_three_times_the_message(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 tool boundaries on a turn ending at ~2 MB must not write ~200 MB.

    This is D13.2 stated as a budget: the total bytes that reach
    ``messages.jsonl`` + ``current.json`` stay under 3x the final message,
    which is only possible if a checkpoint overwrites one file instead of
    appending a fresh copy of everything accumulated so far.
    """
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    acc = TurnAccumulator()

    with counting_file_io(monkeypatch) as counter:
        for i in range(200):
            acc.add_tool_use({"id": f"t{i}", "name": "dispatch", "input": {"d": "a" * 5_000}})
            acc.add_tool_result({"tool_use_id": f"t{i}", "content": "b" * 5_000})
            await acc.checkpoint(sid)
        await acc.checkpoint(sid, final=True)

    log = _session_dir(meta) / "messages.jsonl"
    # The final message, not the file: on the pre-fix code the file IS the
    # 200 accumulated snapshots, which would make a file-relative budget
    # trivially satisfiable.
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    final_size = len(lines[-1].encode("utf-8"))
    written = counter.message_bytes_written()
    print(
        f"\n[cost] final message {final_size} bytes, "
        f"total bytes written {written} ({written / final_size:.1f}x), "
        f"log file {log.stat().st_size} bytes, "
        f"message-file writes {counter.message_writes()}"
    )
    assert final_size > 1_500_000, "the simulated turn should end around 2 MB"
    assert written < 3 * final_size, (
        f"wrote {written} bytes for a {final_size}-byte message "
        f"({written / final_size:.1f}x the budget of 3x)"
    )
    assert log.stat().st_size < 3 * final_size, "the log must not hold every checkpoint"


async def test_finalized_turns_leave_one_line_each_and_no_current_file(
    isolated_store: Path,
) -> None:
    """D13.3: the log grows by exactly one line per message, and the
    in-progress file is gone once the turn finalizes."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    for turn in range(20):
        await store.append_message(
            sid,
            {"role": "user", "content": [{"type": "text", "text": f"prompt {turn}"}]},
            bump_updated_at=False,
            fsync=False,
        )
        acc = TurnAccumulator()
        acc.add_text("thinking")
        acc.add_tool_use({"id": f"t{turn}", "name": "dispatch", "input": {}})
        await acc.checkpoint(sid)
        acc.add_tool_result({"tool_use_id": f"t{turn}", "content": "done"})
        await acc.checkpoint(sid)
        await acc.checkpoint(sid, final=True)

    sdir = _session_dir(meta)
    lines = [ln for ln in (sdir / "messages.jsonl").read_text().splitlines() if ln.strip()]
    assert len(lines) == 40, "expected 20 user + 20 assistant lines, one per message"
    ids = [json.loads(ln)["id"] for ln in lines]
    assert len(set(ids)) == 40, "a finalized message must appear in the log exactly once"
    assert not (sdir / "current.json").exists()

    msgs, _, _ = await store.list_messages(sid)
    assert len(msgs) == 40


# ─────────────────────────────────────────────────────────────────────────────
# Crash recovery — an orphaned current.json
# ─────────────────────────────────────────────────────────────────────────────


async def test_orphaned_current_file_reads_back_as_the_newest_message(
    isolated_store: Path,
) -> None:
    """A backend killed mid-turn leaves ``current.json`` behind. It is a
    coherent message and must still be visible to the chat."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    await _seed(sid, 2)

    orphan = {
        "id": "orphan01",
        "role": "assistant",
        "content": [{"type": "text", "text": "half a turn"}],
        "cost_usd": None,
        "duration_ms": None,
        "created_at": (BASE_TS + timedelta(seconds=99)).isoformat(),
    }
    (_session_dir(meta) / "current.json").write_text(json.dumps(orphan))

    msgs, _, _ = await store.list_messages(sid)
    assert [m["id"] for m in msgs][-1] == "orphan01"
    assert len(msgs) == 3


async def test_orphaned_current_file_is_promoted_to_the_log_exactly_once(
    isolated_store: Path,
) -> None:
    """The next append promotes the orphan into the log and clears the file,
    so it is never counted twice."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    sdir = _session_dir(meta)
    orphan = {
        "id": "orphan01",
        "role": "assistant",
        "content": [{"type": "text", "text": "half a turn"}],
        "created_at": (BASE_TS + timedelta(seconds=1)).isoformat(),
    }
    (sdir / "current.json").write_text(json.dumps(orphan))

    await store.append_message(
        sid,
        {
            "role": "user",
            "content": [{"type": "text", "text": "next prompt"}],
            "created_at": (BASE_TS + timedelta(seconds=2)).isoformat(),
        },
        bump_updated_at=False,
        fsync=False,
    )
    assert not (sdir / "current.json").exists()

    # A second append must not re-promote it.
    await store.append_message(
        sid,
        {
            "role": "user",
            "content": [{"type": "text", "text": "third prompt"}],
            "created_at": (BASE_TS + timedelta(seconds=3)).isoformat(),
        },
        bump_updated_at=False,
        fsync=False,
    )
    lines = [ln for ln in (sdir / "messages.jsonl").read_text().splitlines() if ln.strip()]
    ids = [json.loads(ln)["id"] for ln in lines]
    assert ids.count("orphan01") == 1
    msgs, _, _ = await store.list_messages(sid)
    assert [m["content"][0]["text"] for m in msgs] == [
        "half a turn",
        "next prompt",
        "third prompt",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint throttle
# ─────────────────────────────────────────────────────────────────────────────


async def test_rapid_checkpoints_inside_the_interval_write_once(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 tool boundaries in quick succession, none of them adding much, are
    one checkpoint's worth of durability — not 50 writes."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    acc = TurnAccumulator()

    with counting_file_io(monkeypatch) as counter:
        for i in range(50):
            acc.add_tool_use({"id": f"t{i}", "name": "dispatch", "input": {"d": "tiny"}})
            await acc.checkpoint(sid)
        assert counter.message_writes() == 1, (
            "expected a single write inside the throttle window, got "
            f"{counter.message_writes()}"
        )
        await acc.checkpoint(sid, final=True)
        assert counter.message_writes() == 2, "final=True must always write"

    msgs, _, _ = await store.list_messages(sid)
    assert len(msgs) == 1
    assert len(msgs[0]["content"]) == 50, "the throttle must not drop accumulated blocks"


async def test_a_throttled_checkpoint_never_builds_a_snapshot(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``checkpoint()`` used to call ``_snapshot()`` (a full copy of
    ``self.blocks``) before checking whether the throttle would skip the
    write, paying that copy on every tool boundary even when nothing was
    going to be written. The throttle decision (``_should_write``, and the
    final-emptiness check) must be made from cheap running state so a
    skipped write is genuinely skipped, not just its file write."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    acc = TurnAccumulator()

    calls = 0
    real_snapshot = TurnAccumulator._snapshot

    def counting_snapshot(self: TurnAccumulator) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return real_snapshot(self)

    monkeypatch.setattr(TurnAccumulator, "_snapshot", counting_snapshot)

    acc.add_tool_use({"id": "t0", "name": "dispatch", "input": {"d": "tiny"}})
    await acc.checkpoint(sid)  # first write is never throttled
    assert calls == 1

    for i in range(1, 10):
        acc.add_tool_use({"id": f"t{i}", "name": "dispatch", "input": {"d": "tiny"}})
        await acc.checkpoint(sid)  # inside both the growth and time throttle
    assert calls == 1, f"expected the throttle to skip every one of these 9 checkpoints without snapshotting, got {calls} snapshot(s)"

    await acc.checkpoint(sid, final=True)  # final=True always writes
    assert calls == 2


async def test_growth_past_the_threshold_forces_a_checkpoint(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time throttle alone would let a fast, huge turn go unprotected, so
    growth triggers a write even inside the interval."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    acc = TurnAccumulator()

    with counting_file_io(monkeypatch) as counter:
        acc.add_text("x" * 200)
        await acc.checkpoint(sid)
        first = counter.message_writes()
        acc.add_text("y" * 400_000)
        await acc.checkpoint(sid)
        assert counter.message_writes() == first + 1


# ─────────────────────────────────────────────────────────────────────────────
# D13.4 — the /messages paging contract, and the cost of serving one page
# ─────────────────────────────────────────────────────────────────────────────


async def test_page_is_trailing_window_oldest_first_with_cursor(
    isolated_store: Path,
) -> None:
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    await _seed(sid, 10)

    page, next_before, has_more = await store.list_messages(sid, limit=4)
    assert _texts(page) == ["m6", "m7", "m8", "m9"]
    assert has_more is True
    assert next_before == BASE_TS + timedelta(seconds=6)


async def test_before_cursor_walks_back_page_by_page(isolated_store: Path) -> None:
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    await _seed(sid, 10)

    _, cursor, _ = await store.list_messages(sid, limit=4)
    page, cursor2, has_more = await store.list_messages(sid, before=cursor, limit=4)
    assert _texts(page) == ["m2", "m3", "m4", "m5"]
    assert has_more is True
    assert cursor2 == BASE_TS + timedelta(seconds=2)

    page, cursor3, has_more = await store.list_messages(sid, before=cursor2, limit=4)
    assert _texts(page) == ["m0", "m1"]
    assert has_more is False
    assert cursor3 is None


async def test_limit_above_total_returns_everything_without_a_cursor(
    isolated_store: Path,
) -> None:
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    await _seed(sid, 3)

    page, next_before, has_more = await store.list_messages(sid, limit=50)
    assert _texts(page) == ["m0", "m1", "m2"]
    assert has_more is False
    assert next_before is None

    page, next_before, has_more = await store.list_messages(sid)
    assert _texts(page) == ["m0", "m1", "m2"]
    assert (next_before, has_more) == (None, False)


async def test_empty_session_pages_as_empty(isolated_store: Path) -> None:
    meta = await _new_session(isolated_store)
    assert await store.list_messages(meta["id"], limit=10) == ([], None, False)


async def test_unknown_session_pages_as_empty(isolated_store: Path) -> None:
    assert await store.list_messages("does-not-exist", limit=10) == ([], None, False)


async def test_paging_walks_the_whole_history_without_gaps_or_repeats(
    isolated_store: Path,
) -> None:
    """The cursor walk is the contract the frontend actually exercises, and a
    tail-window read is where it would break: 500 messages at ~2 KB each is
    wider than one window, so every page but the first needs a widening."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    await _seed(sid, 500, filler=2_000)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page, cursor, has_more = await store.list_messages(sid, before=cursor, limit=50)
        assert len(page) == 50, f"page {pages} returned {len(page)} messages"
        seen = _texts(page) + seen
        pages += 1
        assert pages <= 10, "the walk should terminate after exactly 10 pages"
        if not has_more:
            break
    assert cursor is None
    assert pages == 10
    assert [t.rstrip("x") for t in seen] == [f"m{i}" for i in range(500)]


async def test_legacy_checkpoint_duplicates_collapse_to_one_message(
    isolated_store: Path,
) -> None:
    """A log written before the in-progress message moved out holds one line
    per checkpoint, all sharing an id. Readers still collapse those."""
    meta = await _new_session(isolated_store)
    lines = [
        json.dumps(
            {
                "id": "legacy01",
                "role": "assistant",
                "content": [{"type": "text", "text": f"checkpoint {n}"}],
                "created_at": (BASE_TS + timedelta(seconds=n)).isoformat(),
            }
        )
        for n in (1, 2, 3)
    ]
    (_session_dir(meta) / "messages.jsonl").write_text("\n".join(lines) + "\n")

    msgs, next_before, has_more = await store.list_messages(meta["id"], limit=10)
    assert len(msgs) == 1
    assert msgs[0]["content"][0]["text"] == "checkpoint 3"
    assert (next_before, has_more) == (None, False)


async def test_reading_a_legacy_log_does_not_scale_memory_with_the_file(
    isolated_store: Path,
) -> None:
    """A tail window can't serve a log whose lines are ~100 KB each: no window
    yields a page, so a reader that keeps widening and re-materializing the
    accumulated blob ends up holding the whole file several times over. The
    first read after upgrading is exactly that case, and compaction is not
    guaranteed to have run first (the cleanup sentinel may be fresh).

    Peak allocation must stay bounded by the window cap plus the messages
    themselves, not by the size of the file.
    """
    meta = await _new_session(isolated_store)
    log_size = _write_legacy_log(_session_dir(meta) / "messages.jsonl")
    assert log_size > 18_000_000, "the synthetic legacy log should be around 20 MB"

    tracemalloc.start()
    try:
        msgs, _, has_more = await store.list_messages(meta["id"], limit=50)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    print(
        f"\n[cost] legacy log {log_size} bytes → peak {peak} bytes "
        f"({peak / log_size:.2f}x the file) reading one page"
    )
    # Correctness first: ten checkpoint lines per id collapse to one message.
    assert len(msgs) == _LEGACY_IDS
    assert has_more is False
    assert msgs[0]["content"][0]["text"].startswith("c9")
    assert peak < 8 * 1024 * 1024, (
        f"peak {peak} bytes reading a {log_size}-byte log — memory is scaling "
        "with the file, not with the page"
    )


async def test_one_page_does_not_read_the_whole_log(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13.4: serving 50 messages out of 500 must read the tail, not the
    ~1 MB file. Measured in bytes, not seconds."""
    meta = await _new_session(isolated_store)
    sid = meta["id"]
    await _seed(sid, 500, filler=2_000)
    log = _session_dir(meta) / "messages.jsonl"
    log_size = log.stat().st_size
    assert log_size > 900_000, "the seeded log should be around 1 MB"

    with counting_file_io(monkeypatch) as counter:
        page, _, has_more = await store.list_messages(sid, limit=50)
    read = counter.bytes_read["messages.jsonl"]
    print(f"\n[cost] one 50-message page read {read} of {log_size} log bytes")
    assert len(page) == 50
    assert has_more is True
    assert read < 256 * 1024, f"read {read} bytes of a {log_size}-byte log for one page"


# ─────────────────────────────────────────────────────────────────────────────
# D13.5 — GET /api/sessions
# ─────────────────────────────────────────────────────────────────────────────


def test_index_mirrors_every_field_session_out_exposes() -> None:
    """The index is a *projection* of meta.json now, not a pass-through. A
    field added to SessionOut (and written by create_session) but forgotten
    here would silently serve None to the sidebar for every session — the
    exact breakage the widened index was supposed to avoid."""
    assert set(sessions_mod._INDEX_MIRRORED_FIELDS) == set(SessionOut.model_fields)


async def test_list_sessions_reads_no_meta_files_when_the_index_is_current(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = [
        await _new_session(isolated_store, title=f"s{i}", fleet_config_override={"max_steps": i})
        for i in range(50)
    ]
    with counting_file_io(monkeypatch) as counter:
        rows = await store.list_sessions()
    assert counter.read_opens["meta.json"] == 0, "the index already mirrors what the sidebar needs"
    assert len(rows) == 50
    by_id = {r["id"]: r for r in rows}
    for meta in created:
        row = by_id[meta["id"]]
        for field in ("title", "provider", "model", "cwd", "created_at", "updated_at"):
            assert row[field] == meta[field], field
        # The sidebar renders the crew bar from this field — it has to survive
        # the trip through the index, not just the six obvious ones.
        assert row["fleet_config_override"] == meta["fleet_config_override"]


async def test_list_sessions_falls_back_to_meta_for_a_pre_widening_index_entry(
    isolated_store: Path,
) -> None:
    """Existing installs have narrow index entries written before this change.
    They must still list completely."""
    meta = await _new_session(isolated_store, title="legacy")
    index_path = sessions_mod.INDEX_PATH
    index = json.loads(index_path.read_text())
    index[meta["id"]] = {"cwd": meta["cwd"], "created_at": meta["created_at"]}
    index_path.write_text(json.dumps(index))

    rows = await store.list_sessions()
    assert [r["title"] for r in rows] == ["legacy"]
    assert rows[0]["updated_at"] == meta["updated_at"]
    assert rows[0]["provider"] == "claude"


async def test_list_sessions_prunes_an_index_entry_whose_session_is_gone(
    isolated_store: Path,
) -> None:
    """Self-healing survives the switch away from reading every meta.json."""
    meta = await _new_session(isolated_store)
    shutil.rmtree(_session_dir(meta))
    assert await store.list_sessions() == []
    assert json.loads(sessions_mod.INDEX_PATH.read_text()) == {}
