from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.core.messages import empty_usage, user_message
from backend.app.core.session_manager import (
    CURRENT_SESSION_VERSION,
    SessionManager,
    build_context_entries,
    get_default_session_dir_path,
)


def assistant(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "fake",
        "provider": "fake",
        "model": "fake-1",
        "usage": empty_usage(),
        "stopReason": "stop",
        "timestamp": 1,
    }


def read_lines(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def test_session_dir_encoding(agent_dir: Path) -> None:
    d = get_default_session_dir_path("/home/me/proj:x")
    assert d.parent == agent_dir / "sessions"
    assert d.name == "--home-me-proj-x--"


def test_file_deferred_until_assistant_message(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    path = mgr.get_session_file()
    assert path and path.endswith(f"_{mgr.get_session_id()}.jsonl")
    mgr.append_message(user_message("hi"))
    assert not Path(path).exists(), "no assistant message yet → no file"
    mgr.append_message(assistant("hello"))
    lines = read_lines(path)
    assert lines[0]["type"] == "session"
    assert lines[0]["version"] == CURRENT_SESSION_VERSION
    assert lines[0]["cwd"] == str(project.resolve())
    assert [line["type"] for line in lines[1:]] == ["message", "message"]
    # subsequent appends go straight to disk
    mgr.append_message(user_message("again"))
    assert len(read_lines(path)) == 4


def test_entries_chain_on_leaf_and_ids_are_short(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    a = mgr.append_message(user_message("a"))
    b = mgr.append_message(assistant("b"))
    c = mgr.append_message(user_message("c"))
    assert len(a) == 8
    entries = mgr.get_entries()
    assert [e["parentId"] for e in entries] == [None, a, b]
    assert mgr.get_leaf_id() == c
    assert [e["id"] for e in mgr.get_branch()] == [a, b, c]


def test_branch_in_place_builds_a_tree(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    a = mgr.append_message(user_message("a"))
    b = mgr.append_message(assistant("b"))
    mgr.append_message(user_message("c"))
    mgr.branch(a)
    d = mgr.append_message(user_message("d"))
    assert mgr.get_leaf_id() == d
    assert [e["id"] for e in mgr.get_branch()] == [a, d]
    tree = mgr.get_tree()
    assert len(tree) == 1 and tree[0]["entry"]["id"] == a
    children = [n["entry"]["id"] for n in tree[0]["children"]]
    assert children == [b, d]
    # the abandoned branch is still on disk
    assert len(mgr.get_entries()) == 4


def test_labels_and_session_info(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    a = mgr.append_message(user_message("a"))
    mgr.append_message(assistant("b"))
    mgr.append_label_change(a, "checkpoint")
    mgr.append_session_info("my  work\nhere")
    assert mgr.get_label(a) == "checkpoint"
    assert mgr.get_session_name() == "my  work here"
    assert mgr.get_tree()[0]["label"] == "checkpoint"
    mgr.append_label_change(a, None)
    assert mgr.get_label(a) is None
    with pytest.raises(KeyError):
        mgr.append_label_change("nope", "x")


def test_compaction_shapes_the_context(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.in_memory(project)
    ids = []
    for i in range(6):
        msg = user_message(f"m{i}") if i % 2 == 0 else assistant(f"m{i}")
        ids.append(mgr.append_message(msg))
    comp = mgr.append_compaction(
        "summary of m0..m3", first_kept_entry_id=ids[4], tokens_before=1234
    )
    after = mgr.append_message(user_message("after"))
    ctx = mgr.build_context_entries()
    assert [e["id"] for e in ctx] == [comp, ids[4], ids[5], after]
    messages = mgr.build_session_context()["messages"]
    assert messages[0]["role"] == "compactionSummary"
    assert messages[0]["summary"] == "summary of m0..m3"
    assert messages[0]["tokensBefore"] == 1234
    # library function agrees with the method
    assert build_context_entries(mgr.get_entries(), mgr.get_leaf_id()) == ctx


def test_model_and_thinking_changes_flow_into_context(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.in_memory(project)
    mgr.append_model_change("claude", "claude-sonnet-4-6")
    mgr.append_thinking_level_change("high")
    mgr.append_message(user_message("x"))
    ctx = mgr.build_session_context()
    assert ctx["model"] == {"provider": "claude", "modelId": "claude-sonnet-4-6"}
    assert ctx["thinkingLevel"] == "high"
    assert len(ctx["messages"]) == 1


def test_open_restores_leaf_labels_and_name(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    a = mgr.append_message(user_message("a"))
    mgr.append_message(assistant("b"))
    mgr.append_label_change(a, "L")
    mgr.append_session_info("named")
    path = mgr.get_session_file()
    assert path
    reopened = SessionManager.open(path)
    assert reopened.get_session_id() == mgr.get_session_id()
    assert reopened.get_leaf_id() == mgr.get_leaf_id()
    assert reopened.get_label(a) == "L"
    assert reopened.get_session_name() == "named"
    assert reopened.get_cwd() == str(project.resolve())
    reopened.append_message(user_message("c"))
    assert len(read_lines(path)) == 6


def test_continue_recent_and_list(agent_dir: Path, project: Path) -> None:
    first = SessionManager.create(project)
    first.append_message(user_message("first question"))
    first.append_message(assistant("answer"))
    second = SessionManager.create(project)
    second.append_message(user_message("second question"))
    second.append_message(assistant("answer"))
    second.append_session_info("second")
    resumed = SessionManager.continue_recent(project)
    assert resumed.get_session_id() == second.get_session_id()
    infos = SessionManager.list(project)
    assert [i["id"] for i in infos] == [second.get_session_id(), first.get_session_id()]
    assert infos[0]["name"] == "second"
    assert infos[0]["messageCount"] == 2
    assert infos[1]["firstMessage"] == "first question"
    assert SessionManager.list_all()[0]["id"] == second.get_session_id()


def test_fork_from_copies_history_and_points_at_parent(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    a = mgr.append_message(user_message("a"))
    b = mgr.append_message(assistant("b"))
    c = mgr.append_message(user_message("c"))
    source = mgr.get_session_file()
    assert source
    fork = SessionManager.fork_from(source, project)
    assert fork.get_session_id() != mgr.get_session_id()
    assert fork.get_header()["parentSession"] == str(Path(source).resolve())
    assert [e["id"] for e in fork.get_entries()] == [a, b, c]
    assert fork.get_leaf_id() == c
    fork.branch(a)
    d = fork.append_message(user_message("d"))
    assert [e["id"] for e in fork.get_branch()] == [a, d]
    # eagerly written, and the source is untouched
    assert len(read_lines(fork.get_session_file())) == 5
    assert len(read_lines(source)) == 4
    listed = {i["id"]: i for i in SessionManager.list(project)}
    assert listed[fork.get_session_id()]["parentSessionPath"] == str(Path(source).resolve())
    assert "parentSessionPath" not in listed[mgr.get_session_id()]


def test_branch_with_summary_records_where_we_came_from(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.in_memory(project)
    a = mgr.append_message(user_message("a"))
    b = mgr.append_message(assistant("b"))
    sid = mgr.branch_with_summary(a, "we tried b")
    entry = mgr.get_entry(sid)
    assert entry["type"] == "branch_summary"
    assert entry["fromId"] == b
    assert entry["parentId"] == a
    messages = mgr.build_session_context()["messages"]
    assert messages[-1]["role"] == "branchSummary"
    assert messages[-1]["fromId"] == b


def test_create_branched_session_clones_the_active_path(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    a = mgr.append_message(user_message("a"))
    b = mgr.append_message(assistant("b"))
    mgr.append_label_change(a, "L")
    mgr.append_message(user_message("side"))
    mgr.branch(b)
    old_file = mgr.get_session_file()
    old_id = mgr.get_session_id()
    new_file = mgr.create_branched_session(b)
    assert new_file and new_file != old_file
    assert mgr.get_session_id() != old_id
    assert [e["id"] for e in mgr.get_entries()] == [a, b]
    assert mgr.get_header()["parentSession"] == old_file
    lines = read_lines(new_file)
    assert len(lines) == 3 and lines[1]["parentId"] is None and lines[2]["parentId"] == a


def test_in_memory_never_writes(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.in_memory(project)
    mgr.append_message(user_message("a"))
    mgr.append_message(assistant("b"))
    assert mgr.get_session_file() is None
    assert not (agent_dir / "sessions").exists()


def test_torn_last_line_is_ignored(agent_dir: Path, project: Path) -> None:
    mgr = SessionManager.create(project)
    mgr.append_message(user_message("a"))
    mgr.append_message(assistant("b"))
    path = Path(mgr.get_session_file())
    path.write_text(path.read_text() + '{"type":"message","id":"zzz"')
    reopened = SessionManager.open(path)
    assert len(reopened.get_entries()) == 2
