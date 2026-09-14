"""End-to-end over the real ASGI app: the WebSocket transport, a full turn,
approvals, quota, and packages — the surface the React client talks to."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from backend.app.core.packages import install_package
from backend.app.main import create_app

FLEET_DIR = Path(__file__).resolve().parents[2] / "packages" / "fleet"


def drain_until(ws: Any, predicate: Any, limit: int = 400) -> list[dict]:
    """Collect frames until `predicate` matches one (inclusive)."""
    frames: list[dict] = []
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if predicate(frame):
            return frames
    raise AssertionError(f"predicate never matched; saw {[f['type'] for f in frames]}")


def response_for(frames: list[dict], command_id: str) -> dict:
    return next(f for f in frames if f.get("type") == "response" and f.get("id") == command_id)


def core_url(project: Path, **extra: str) -> str:
    query = {"engine": "fake", "memory": "1", "cwd": str(project), "permission": "allow", **extra}
    return "/api/core/rpc?" + "&".join(f"{k}={v}" for k, v in query.items())


def test_full_turn_over_the_app(agent_dir: Path, project: Path) -> None:
    client = TestClient(create_app())
    with client.websocket_connect(core_url(project)) as ws:
        ready = json.loads(ws.receive_text())
        assert ready["type"] == "ready"
        assert ready["state"]["engine"] == "fake"
        assert ready["state"]["quotaStatus"] == "unknown"  # nothing reported yet

        ws.send_text(json.dumps({"id": "p1", "type": "prompt", "message": "hello over the app"}))
        frames = drain_until(ws, lambda f: f["type"] == "agent_settled")
        kinds = [f["type"] for f in frames]
        assert response_for(frames, "p1")["success"] is True
        assert kinds.count("turn_start") == 1
        assert "message_start" in kinds and "message_end" in kinds and "agent_end" in kinds

        deltas = [
            f["assistantMessageEvent"]["delta"]
            for f in frames
            if f["type"] == "message_update" and f["assistantMessageEvent"]["type"] == "text_delta"
        ]
        assert "".join(deltas) == "echo: hello over the app"
        # the wire form is thinned: no partial message snapshots
        assert all(
            "partial" not in f["assistantMessageEvent"]
            for f in frames
            if f["type"] == "message_update"
        )

        ws.send_text(json.dumps({"id": "s1", "type": "get_session_stats"}))
        stats = response_for(drain_until(ws, lambda f: f.get("id") == "s1"), "s1")["data"]
        assert stats["assistantMessages"] == 1 and stats["userMessages"] == 1
        assert stats["quota"]["engine"] == "fake"

        ws.send_text(json.dumps({"id": "q1", "type": "get_quota"}))
        quota = response_for(drain_until(ws, lambda f: f.get("id") == "q1"), "q1")["data"]
        assert quota["status"] == "unknown" and quota["headroom"] == 1.0
        assert quota["summary"] == "no quota reported yet"


def test_quota_reaches_the_client(agent_dir: Path, project: Path) -> None:
    """A vendor rate-limit payload becomes headroom the UI can render."""
    import urllib.parse

    payload = urllib.parse.quote(
        json.dumps({"plan": "plus", "primary": {"usedPercent": 85, "windowMinutes": 300}})
    )
    client = TestClient(create_app())
    with client.websocket_connect(core_url(project, rate_limit=payload)) as ws:
        assert json.loads(ws.receive_text())["state"]["quotaStatus"] == "unknown"
        ws.send_text(json.dumps({"id": "p", "type": "prompt", "message": "hi"}))
        frames = drain_until(ws, lambda f: f["type"] == "agent_settled")
        assert any(f["type"] == "rate_limit" for f in frames)

        ws.send_text(json.dumps({"id": "q", "type": "get_quota"}))
        report = response_for(drain_until(ws, lambda f: f.get("id") == "q"), "q")["data"]
        assert report["status"] == "warning" and report["headroom"] == 0.15
        assert report["current"]["plan"] == "plus"
        assert report["current"]["windows"][0]["durationMinutes"] == 300
        assert "fake: 15% left (warning" in report["summary"]

        ws.send_text(json.dumps({"id": "s", "type": "get_state"}))
        state = response_for(drain_until(ws, lambda f: f.get("id") == "s"), "s")["data"]
        assert state["quotaStatus"] == "warning" and state["quotaHeadroom"] == 0.15


def test_packages_are_visible_and_their_tools_load(agent_dir: Path, project: Path) -> None:
    install_package(str(FLEET_DIR), cwd=project, agent_dir=agent_dir, scope="project")
    client = TestClient(create_app())
    with client.websocket_connect(core_url(project, trust="1")) as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"id": "pk", "type": "get_packages"}))
        data = response_for(drain_until(ws, lambda f: f.get("id") == "pk"), "pk")["data"]
        assert [p["name"] for p in data["packages"]] == ["fleet"]
        assert data["packages"][0]["scope"] == "project"

        ws.send_text(json.dumps({"id": "cm", "type": "get_commands"}))
        commands = response_for(drain_until(ws, lambda f: f.get("id") == "cm"), "cm")["data"]
        assert any(c["name"] == "fleet" for c in commands["commands"])


def test_extension_dialog_round_trips_over_the_socket(agent_dir: Path, project: Path) -> None:
    """An approval prompt reaches the client and its answer reaches the tool."""
    extensions = project / ".localcode" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "ask.py").write_text(
        "def setup(api):\n"
        "    async def ask(event, ctx):\n"
        "        ok = await ctx.ui.confirm('Run it?', event['text'])\n"
        "        return {'action': 'transform', 'text': ('yes: ' if ok else 'no: ') + event['text']}\n"
        "    api.on('input', ask)\n"
    )
    client = TestClient(create_app())
    with client.websocket_connect(core_url(project, trust="1")) as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"id": "p", "type": "prompt", "message": "do the thing"}))
        frames = drain_until(ws, lambda f: f["type"] == "extension_ui_request")
        request = frames[-1]
        assert request["method"] == "confirm" and request["title"] == "Run it?"
        assert request["message"] == "do the thing"

        ws.send_text(
            json.dumps({"type": "extension_ui_response", "id": request["id"], "confirmed": True})
        )
        settled = drain_until(ws, lambda f: f["type"] == "agent_settled")
        deltas = "".join(
            f["assistantMessageEvent"]["delta"]
            for f in settled
            if f["type"] == "message_update" and f["assistantMessageEvent"]["type"] == "text_delta"
        )
        assert deltas == "echo: yes: do the thing"


def test_unknown_command_is_answered_not_dropped(agent_dir: Path, project: Path) -> None:
    client = TestClient(create_app())
    with client.websocket_connect(core_url(project)) as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"id": "x", "type": "not_a_command"}))
        frame = json.loads(ws.receive_text())
        assert frame["success"] is False and "unknown command" in frame["error"]
        # a malformed line gets an answer too, rather than silence
        ws.send_text("{not json")
        frame = json.loads(ws.receive_text())
        assert frame["success"] is False and "invalid JSON" in frame["error"]
