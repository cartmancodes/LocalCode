"""Sessions as a JSONL tree — pi's session format, version 3.

File layout (pi: ``core/session-manager.ts``):

    {"type":"session","version":3,"id":"…","timestamp":"…","cwd":"…","parentSession":"…"}
    {"type":"message","id":"a1b2c3d4","parentId":null,"timestamp":"…","message":{…}}
    {"type":"model_change","id":"…","parentId":"a1b2c3d4","timestamp":"…","provider":"…","modelId":"…"}
    {"type":"compaction","id":"…","parentId":"…","timestamp":"…","summary":"…","firstKeptEntryId":"…","tokensBefore":…}
    …

Every entry has ``id`` and ``parentId``. Appending parents the new entry on
the current *leaf* and advances the leaf; ``branch(id)`` moves the leaf back
to an earlier entry without touching the file, so the next append starts a
new branch in place. Compaction is an entry like any other — the full history
stays on disk and ``build_context_entries`` decides what the model sees.

Two pi behaviours worth knowing because they are surprising:

- The file is not created until the session holds an assistant message.
  An abandoned empty session leaves nothing behind. Forks are the exception:
  they copy history, so they are written eagerly.
- Label and session-info entries advance the leaf like everything else.
  Forking strips label entries out of the copied path.

Entries are plain dicts (the JSON shapes) rather than dataclasses so the
file, the RPC payloads and the in-memory state are the same objects.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from .config import get_default_agent_dir
from .ids import assert_valid_session_id, create_session_id, generate_id
from .messages import (
    AgentMessage,
    branch_summary_message,
    compaction_summary_message,
    custom_message,
    message_text,
)

CURRENT_SESSION_VERSION = 3

Entry = dict[str, Any]
FileEntry = dict[str, Any]


class SessionTreeNode(TypedDict, total=False):
    entry: Entry
    children: list[SessionTreeNode]
    label: str
    labelTimestamp: str


class SessionContext(TypedDict):
    messages: list[AgentMessage]
    thinkingLevel: str
    model: dict[str, str] | None


class SessionInfo(TypedDict, total=False):
    path: str
    id: str
    cwd: str
    name: str
    parentSessionPath: str
    created: str
    modified: str
    messageCount: int
    firstMessage: str
    allMessagesText: str


class NewSessionOptions(TypedDict, total=False):
    id: str
    parentSession: str


# ── helpers ────────────────────────────────────────────────────────────────


def _abs(path: str | Path) -> str:
    """Absolute, normalised, symlinks *not* followed (Node ``path.resolve``)."""
    return os.path.abspath(os.path.expanduser(str(path)))


def _iso_now() -> str:
    # pi: new Date().toISOString() → "2026-09-13T06:16:00.123Z"
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_from_mtime(mtime: float) -> str:
    return (
        datetime.fromtimestamp(mtime, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _file_timestamp(iso: str) -> str:
    return iso.replace(":", "-").replace(".", "-")


def get_default_session_dir_path(cwd: str | Path, agent_dir: str | Path | None = None) -> Path:
    """``<agent_dir>/sessions/--<cwd with / \\ : → ->--`` (pi's encoding)."""
    base = Path(agent_dir) if agent_dir else get_default_agent_dir()
    resolved = str(Path(_abs(cwd)))
    stripped = resolved[1:] if resolved[:1] in ("/", "\\") else resolved
    safe = "--" + "".join("-" if ch in "/\\:" else ch for ch in stripped) + "--"
    return base / "sessions" / safe


def get_default_session_dir(cwd: str | Path, agent_dir: str | Path | None = None) -> Path:
    path = get_default_session_dir_path(cwd, agent_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_session_entries(content: str) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # A torn last line (crash mid-write) must not poison the session.
            continue
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            entries.append(obj)
    return entries


def load_entries_from_file(path: str | Path) -> list[FileEntry]:
    p = Path(path)
    if not p.exists():
        return []
    return parse_session_entries(p.read_text(encoding="utf-8"))


def find_most_recent_session(session_dir: str | Path, cwd: str | None = None) -> str | None:
    d = Path(session_dir)
    if not d.is_dir():
        return None
    files = sorted(
        (f for f in d.iterdir() if f.suffix == ".jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for f in files:
        if cwd is None:
            return str(f)
        entries = load_entries_from_file(f)
        header = entries[0] if entries and entries[0].get("type") == "session" else None
        if header and header.get("cwd") == cwd:
            return str(f)
    return None


def build_session_path(
    entries: list[Entry], leaf_id: str | None, by_id: dict[str, Entry] | None = None
) -> list[Entry]:
    """Root → leaf path by walking ``parentId`` links."""
    if not leaf_id:
        return []
    index = by_id if by_id is not None else {e["id"]: e for e in entries}
    path: list[Entry] = []
    current = index.get(leaf_id)
    seen: set[str] = set()
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        path.append(current)
        parent = current.get("parentId")
        current = index.get(parent) if parent else None
    path.reverse()
    return path


def get_latest_compaction_entry(entries: list[Entry]) -> Entry | None:
    for entry in reversed(entries):
        if entry.get("type") == "compaction":
            return entry
    return None


def build_context_entries(
    entries: list[Entry], leaf_id: str | None, by_id: dict[str, Entry] | None = None
) -> list[Entry]:
    """The entries the model should see on the current branch.

    With a compaction on the path: the compaction entry itself (rendered as a
    summary), then the entries from ``firstKeptEntryId`` up to the compaction,
    then everything after it. Without one: the whole path.
    """
    path = build_session_path(entries, leaf_id, by_id)
    compaction = get_latest_compaction_entry(path)
    if compaction is None:
        return path
    idx = next((i for i, e in enumerate(path) if e["id"] == compaction["id"]), -1)
    if idx < 0:
        return path
    context: list[Entry] = [compaction]
    found_first_kept = False
    for entry in path[:idx]:
        if entry["id"] == compaction.get("firstKeptEntryId"):
            found_first_kept = True
        if found_first_kept:
            context.append(entry)
    context.extend(path[idx + 1 :])
    return context


def _ts_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def session_entry_to_context_messages(entry: Entry) -> list[AgentMessage]:
    kind = entry.get("type")
    if kind == "message":
        message = dict(entry["message"])
        roles = ("user", "assistant", "toolResult")
        if message.get("role") in roles and message.get("content") is None:
            message["content"] = []
        return [message]  # type: ignore[list-item]
    if kind == "custom_message":
        return [
            custom_message(
                entry["customType"],
                entry.get("content") or [],
                bool(entry.get("display")),
                entry.get("details"),
                _ts_ms(entry.get("timestamp")),
            )
        ]
    if kind == "branch_summary" and entry.get("summary"):
        return [
            branch_summary_message(
                entry["summary"], entry.get("fromId"), _ts_ms(entry.get("timestamp"))
            )
        ]
    if kind == "compaction":
        return [
            compaction_summary_message(
                entry.get("summary", ""),
                int(entry.get("tokensBefore") or 0),
                _ts_ms(entry.get("timestamp")),
            )
        ]
    return []


def build_session_context(
    entries: list[Entry], leaf_id: str | None, by_id: dict[str, Entry] | None = None
) -> SessionContext:
    messages: list[AgentMessage] = []
    thinking_level = "off"
    model: dict[str, str] | None = None
    for entry in build_context_entries(entries, leaf_id, by_id):
        kind = entry.get("type")
        if kind == "thinking_level_change":
            thinking_level = entry.get("thinkingLevel", thinking_level)
        elif kind == "model_change":
            model = {"provider": entry.get("provider", ""), "modelId": entry.get("modelId", "")}
        else:
            messages.extend(session_entry_to_context_messages(entry))
    return {"messages": messages, "thinkingLevel": thinking_level, "model": model}


# ── the manager ────────────────────────────────────────────────────────────


class SessionManager:
    def __init__(
        self,
        cwd: str | Path,
        session_dir: str | Path,
        session_file: str | Path | None = None,
        persist: bool = True,
        options: NewSessionOptions | None = None,
        preloaded_entries: list[FileEntry] | None = None,
    ) -> None:
        self._cwd = str(Path(_abs(cwd)))
        self._session_dir = str(Path(_abs(session_dir))) if session_dir else ""
        self._persist = persist
        self._session_file: str | None = None
        self._session_id = ""
        self._file_entries: list[FileEntry] = []
        self._by_id: dict[str, Entry] = {}
        self._labels_by_id: dict[str, str] = {}
        self._label_ts_by_id: dict[str, str] = {}
        self._leaf_id: str | None = None
        self._flushed = False

        if persist and self._session_dir:
            Path(self._session_dir).mkdir(parents=True, exist_ok=True)

        if session_file is not None:
            self._session_file = str(Path(_abs(session_file)))
            entries = (
                preloaded_entries
                if preloaded_entries is not None
                else load_entries_from_file(self._session_file)
            )
            if entries:
                self._load(entries)
                # An existing file was fully read; further appends go straight to disk.
                self._flushed = Path(self._session_file).exists()
            else:
                self.new_session(options)
        else:
            self.new_session(options)

    # ── construction ───────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        cwd: str | Path,
        session_dir: str | Path | None = None,
        options: NewSessionOptions | None = None,
    ) -> SessionManager:
        d = Path(session_dir) if session_dir else get_default_session_dir(cwd)
        return cls(cwd, d, None, True, options)

    @classmethod
    def open(
        cls,
        path: str | Path,
        session_dir: str | Path | None = None,
        cwd_override: str | None = None,
    ) -> SessionManager:
        resolved = Path(_abs(path))
        entries = load_entries_from_file(resolved)
        header = entries[0] if entries and entries[0].get("type") == "session" else None
        cwd = cwd_override or (header.get("cwd") if header else None) or os.getcwd()
        d = Path(session_dir) if session_dir else resolved.parent
        return cls(cwd, d, resolved, True, None, entries)

    @classmethod
    def continue_recent(
        cls, cwd: str | Path, session_dir: str | Path | None = None
    ) -> SessionManager:
        d = Path(session_dir) if session_dir else get_default_session_dir(cwd)
        recent = find_most_recent_session(d)
        if recent:
            return cls.open(recent, d)
        return cls.create(cwd, d)

    @classmethod
    def in_memory(
        cls,
        cwd: str | Path | None = None,
        options: NewSessionOptions | None = None,
        entries: list[FileEntry] | None = None,
    ) -> SessionManager:
        mgr = cls(cwd or os.getcwd(), "", None, False, options)
        if entries:
            mgr._load(entries)
        return mgr

    @classmethod
    def fork_from(
        cls,
        source_path: str | Path,
        target_cwd: str | Path,
        session_dir: str | Path | None = None,
        options: NewSessionOptions | None = None,
    ) -> SessionManager:
        """Copy a session's history into a new file with a ``parentSession``
        pointer. The new session's leaf is the copied file's last entry; call
        ``branch()`` to move it to the fork point."""
        source = Path(_abs(source_path))
        source_entries = load_entries_from_file(source)
        if not source_entries:
            raise ValueError(f"cannot fork: source session file is empty or invalid: {source}")
        if source_entries[0].get("type") != "session":
            raise ValueError(f"cannot fork: source session has no header: {source}")
        target = Path(_abs(target_cwd))
        d = Path(session_dir) if session_dir else get_default_session_dir(target)
        d.mkdir(parents=True, exist_ok=True)
        if options and "id" in options:
            assert_valid_session_id(options["id"])
        new_id = (options or {}).get("id") or create_session_id()
        timestamp = _iso_now()
        new_file = d / f"{_file_timestamp(timestamp)}_{new_id}.jsonl"
        header: FileEntry = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": new_id,
            "timestamp": timestamp,
            "cwd": str(target),
            "parentSession": str(source),
        }
        copied = [header, *[e for e in source_entries if e.get("type") != "session"]]
        with new_file.open("x", encoding="utf-8") as fh:
            for e in copied:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        mgr = cls(target, d, new_file, True, None, copied)
        mgr._flushed = True
        return mgr

    @classmethod
    def list(cls, cwd: str | Path, session_dir: str | Path | None = None) -> list[SessionInfo]:
        d = Path(session_dir) if session_dir else get_default_session_dir_path(cwd)
        return cls._list_dir(d, cwd_filter=str(Path(_abs(cwd))))

    @classmethod
    def list_all(cls, session_dir: str | Path | None = None) -> list[SessionInfo]:
        root = Path(session_dir) if session_dir else get_default_agent_dir() / "sessions"
        if not root.is_dir():
            return []
        infos: list[SessionInfo] = []
        for sub in root.iterdir():
            if sub.is_dir():
                infos.extend(cls._list_dir(sub))
        infos.sort(key=lambda i: Path(i["path"]).stat().st_mtime, reverse=True)
        return infos

    @classmethod
    def _list_dir(cls, d: Path, cwd_filter: str | None = None) -> list[SessionInfo]:
        if not d.is_dir():
            return []
        infos: list[SessionInfo] = []
        mtimes: dict[str, float] = {}
        for f in d.iterdir():
            if f.suffix != ".jsonl":
                continue
            entries = load_entries_from_file(f)
            if not entries or entries[0].get("type") != "session":
                continue
            header = entries[0]
            if cwd_filter and header.get("cwd") not in (None, "", cwd_filter):
                continue
            messages = [e["message"] for e in entries[1:] if e.get("type") == "message"]
            first = next((message_text(m) for m in messages if m.get("role") == "user"), "")
            name = next(
                (
                    e.get("name")
                    for e in reversed(entries)
                    if e.get("type") == "session_info" and e.get("name")
                ),
                None,
            )
            mtime = f.stat().st_mtime
            info: SessionInfo = {
                "path": str(f),
                "id": header.get("id", ""),
                "cwd": header.get("cwd", ""),
                "created": header.get("timestamp", ""),
                "modified": _iso_from_mtime(mtime),
                "messageCount": len(messages),
                "firstMessage": first,
                "allMessagesText": "\n".join(message_text(m) for m in messages),
            }
            if name:
                info["name"] = name
            if header.get("parentSession"):
                info["parentSessionPath"] = header["parentSession"]
            infos.append(info)
            mtimes[str(f)] = mtime
        # Sort on the raw mtime: the ISO string is millisecond-truncated and
        # sessions written in quick succession would tie.
        infos.sort(key=lambda i: mtimes.get(i["path"], 0.0), reverse=True)
        return infos

    # ── session lifecycle ──────────────────────────────────────────────

    def new_session(self, options: NewSessionOptions | None = None) -> str | None:
        if options and "id" in options:
            assert_valid_session_id(options["id"])
        self._session_id = (options or {}).get("id") or create_session_id()
        timestamp = _iso_now()
        header: FileEntry = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": self._session_id,
            "timestamp": timestamp,
            "cwd": self._cwd,
        }
        parent = (options or {}).get("parentSession")
        if parent:
            header["parentSession"] = parent
        self._file_entries = [header]
        self._by_id.clear()
        self._labels_by_id.clear()
        self._label_ts_by_id.clear()
        self._leaf_id = None
        self._flushed = False
        if self._persist and self._session_dir:
            name = f"{_file_timestamp(timestamp)}_{self._session_id}.jsonl"
            self._session_file = str(Path(self._session_dir) / name)
        return self._session_file

    def set_session_file(self, session_file: str | Path) -> None:
        self._session_file = str(Path(_abs(session_file)))
        self._flushed = False

    def _load(self, entries: list[FileEntry]) -> None:
        self._file_entries = list(entries)
        self._by_id.clear()
        self._labels_by_id.clear()
        self._label_ts_by_id.clear()
        self._leaf_id = None
        header = next((e for e in entries if e.get("type") == "session"), None)
        if header:
            self._session_id = header.get("id") or create_session_id()
            if header.get("cwd"):
                self._cwd = header["cwd"]
        for entry in entries:
            if entry.get("type") == "session":
                continue
            self._by_id[entry["id"]] = entry
            if entry.get("type") == "label":
                if entry.get("label"):
                    self._labels_by_id[entry["targetId"]] = entry["label"]
                    self._label_ts_by_id[entry["targetId"]] = entry.get("timestamp", "")
                else:
                    self._labels_by_id.pop(entry["targetId"], None)
                    self._label_ts_by_id.pop(entry["targetId"], None)
            self._leaf_id = entry["id"]

    # ── accessors ──────────────────────────────────────────────────────

    def is_persisted(self) -> bool:
        return self._persist

    def get_cwd(self) -> str:
        return self._cwd

    def get_session_dir(self) -> str:
        return self._session_dir

    def uses_default_session_dir(self) -> bool:
        return self._session_dir == str(get_default_session_dir_path(self._cwd))

    def get_session_id(self) -> str:
        return self._session_id

    def get_session_file(self) -> str | None:
        return self._session_file

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def get_leaf_entry(self) -> Entry | None:
        return self._by_id.get(self._leaf_id) if self._leaf_id else None

    def get_entry(self, entry_id: str) -> Entry | None:
        return self._by_id.get(entry_id)

    def get_children(self, parent_id: str) -> list[Entry]:
        return [e for e in self.get_entries() if e.get("parentId") == parent_id]

    def get_label(self, entry_id: str) -> str | None:
        return self._labels_by_id.get(entry_id)

    def get_header(self) -> FileEntry | None:
        return next((e for e in self._file_entries if e.get("type") == "session"), None)

    def get_entries(self) -> list[Entry]:
        return [e for e in self._file_entries if e.get("type") != "session"]

    def get_session_name(self) -> str | None:
        for entry in reversed(self.get_entries()):
            if entry.get("type") == "session_info":
                name = (entry.get("name") or "").strip()
                return name or None
        return None

    def get_branch(self, from_id: str | None = None) -> list[Entry]:
        return build_session_path(self.get_entries(), from_id or self._leaf_id, self._by_id)

    def build_context_entries(self) -> list[Entry]:
        return build_context_entries(self.get_entries(), self._leaf_id, self._by_id)

    def build_session_context(self) -> SessionContext:
        return build_session_context(self.get_entries(), self._leaf_id, self._by_id)

    def get_tree(self) -> list[SessionTreeNode]:
        entries = self.get_entries()
        nodes: dict[str, SessionTreeNode] = {}
        roots: list[SessionTreeNode] = []
        for entry in entries:
            node: SessionTreeNode = {"entry": entry, "children": []}
            label = self._labels_by_id.get(entry["id"])
            if label is not None:
                node["label"] = label
                node["labelTimestamp"] = self._label_ts_by_id.get(entry["id"], "")
            nodes[entry["id"]] = node
        for entry in entries:
            node = nodes[entry["id"]]
            parent_id = entry.get("parentId")
            if parent_id is None or parent_id == entry["id"]:
                roots.append(node)
            else:
                parent = nodes.get(parent_id)
                if parent is not None:
                    parent["children"].append(node)
                else:
                    roots.append(node)
        return roots

    # ── appends ────────────────────────────────────────────────────────

    def _new_entry(self, entry_type: str, **fields: Any) -> Entry:
        entry: Entry = {
            "type": entry_type,
            "id": generate_id(self._by_id),
            "parentId": self._leaf_id,
            "timestamp": _iso_now(),
        }
        entry.update(fields)
        return entry

    def _append_entry(self, entry: Entry) -> None:
        self._file_entries.append(entry)
        self._by_id[entry["id"]] = entry
        self._leaf_id = entry["id"]
        self._persist_entry(entry)

    def _has_assistant_message(self) -> bool:
        for e in self._file_entries:
            if e.get("type") == "message" and e["message"].get("role") == "assistant":
                return True
        return False

    def _persist_entry(self, entry: Entry) -> None:
        if not self._persist or not self._session_file:
            return
        if not self._has_assistant_message():
            if self._flushed:
                self._append_line(entry)
            return
        if not self._flushed:
            path = Path(self._session_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                for e in self._file_entries:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            self._flushed = True
        else:
            self._append_line(entry)

    def _append_line(self, entry: Entry) -> None:
        assert self._session_file is not None
        with Path(self._session_file).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def append_message(self, message: AgentMessage | dict[str, Any]) -> str:
        entry = self._new_entry("message", message=message)
        self._append_entry(entry)
        return entry["id"]

    def append_thinking_level_change(self, thinking_level: str) -> str:
        entry = self._new_entry("thinking_level_change", thinkingLevel=thinking_level)
        self._append_entry(entry)
        return entry["id"]

    def append_model_change(self, provider: str, model_id: str) -> str:
        entry = self._new_entry("model_change", provider=provider, modelId=model_id)
        self._append_entry(entry)
        return entry["id"]

    def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
        details: Any = None,
        from_hook: bool | None = None,
        usage: dict[str, Any] | None = None,
    ) -> str:
        fields: dict[str, Any] = {
            "summary": summary,
            "firstKeptEntryId": first_kept_entry_id,
            "tokensBefore": tokens_before,
        }
        if details is not None:
            fields["details"] = details
        if usage is not None:
            fields["usage"] = usage
        if from_hook is not None:
            fields["fromHook"] = from_hook
        entry = self._new_entry("compaction", **fields)
        self._append_entry(entry)
        return entry["id"]

    def append_custom_entry(self, custom_type: str, data: Any = None) -> str:
        fields: dict[str, Any] = {"customType": custom_type}
        if data is not None:
            fields["data"] = data
        entry = self._new_entry("custom", **fields)
        self._append_entry(entry)
        return entry["id"]

    def append_custom_message_entry(
        self, custom_type: str, content: Any, display: bool, details: Any = None
    ) -> str:
        fields: dict[str, Any] = {"customType": custom_type, "content": content, "display": display}
        if details is not None:
            fields["details"] = details
        entry = self._new_entry("custom_message", **fields)
        self._append_entry(entry)
        return entry["id"]

    def append_session_info(self, name: str) -> str:
        sanitized = " ".join(name.replace("\r", "\n").split("\n")).strip()
        entry = self._new_entry("session_info", name=sanitized)
        self._append_entry(entry)
        return entry["id"]

    def append_label_change(self, target_id: str, label: str | None) -> str:
        if target_id not in self._by_id:
            raise KeyError(f"entry {target_id} not found")
        entry = self._new_entry("label", targetId=target_id, label=label)
        self._append_entry(entry)
        if label:
            self._labels_by_id[target_id] = label
            self._label_ts_by_id[target_id] = entry["timestamp"]
        else:
            self._labels_by_id.pop(target_id, None)
            self._label_ts_by_id.pop(target_id, None)
        return entry["id"]

    # ── branching ──────────────────────────────────────────────────────

    def branch(self, branch_from_id: str) -> None:
        if branch_from_id not in self._by_id:
            raise KeyError(f"entry {branch_from_id} not found")
        self._leaf_id = branch_from_id

    def reset_leaf(self) -> None:
        self._leaf_id = None

    def branch_with_summary(
        self,
        branch_from_id: str | None,
        summary: str,
        details: Any = None,
        from_hook: bool | None = None,
        usage: dict[str, Any] | None = None,
    ) -> str:
        """Move the leaf to ``branch_from_id`` and record a summary of the
        branch being left, so the model keeps what it learned there."""
        if branch_from_id is not None and branch_from_id not in self._by_id:
            raise KeyError(f"entry {branch_from_id} not found")
        from_id = self._leaf_id
        self._leaf_id = branch_from_id
        fields: dict[str, Any] = {"fromId": from_id, "summary": summary}
        if details is not None:
            fields["details"] = details
        if usage is not None:
            fields["usage"] = usage
        if from_hook is not None:
            fields["fromHook"] = from_hook
        entry = self._new_entry("branch_summary", **fields)
        self._append_entry(entry)
        return entry["id"]

    def create_branched_session(self, leaf_id: str) -> str | None:
        """Copy the path root→``leaf_id`` (labels stripped) into a fresh
        session file and switch this manager to it. pi's ``/clone``."""
        path = self.get_branch(leaf_id)
        if not path:
            raise KeyError(f"entry {leaf_id} not found")
        previous_file = self._session_file
        copied: list[Entry] = []
        parent_id: str | None = None
        for entry in path:
            if entry.get("type") == "label":
                continue
            clone = dict(entry)
            clone["parentId"] = parent_id
            copied.append(clone)
            parent_id = clone["id"]
        self.new_session({"parentSession": previous_file} if previous_file else None)
        header = self._file_entries[0]
        self._file_entries = [header, *copied]
        self._by_id = {e["id"]: e for e in copied}
        self._leaf_id = copied[-1]["id"] if copied else None
        if self._persist and self._session_file:
            p = Path(self._session_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("x", encoding="utf-8") as fh:
                for e in self._file_entries:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            self._flushed = True
        return self._session_file
