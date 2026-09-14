"""The WebSocket transport speaks the same RPC frames as stdio."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routes import core_rpc


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(core_rpc.router)
    return app


def test_ws_rpc_roundtrip(agent_dir: Path, project: Path) -> None:
    client = TestClient(make_app())
    url = f"/api/core/rpc?engine=fake&memory=1&cwd={project}&permission=allow&models=fake/fake-1,fake/fake-2"
    with client.websocket_connect(url) as ws:
        ready = json.loads(ws.receive_text())
        assert ready["type"] == "ready" and ready["state"]["engine"] == "fake"
        ws.send_text(json.dumps({"id": "1", "type": "get_available_models"}))
        resp = json.loads(ws.receive_text())
        assert resp["command"] == "get_available_models" and [
            m["modelId"] for m in resp["data"]["models"]
        ] == ["fake-1", "fake-2"]
        ws.send_text(json.dumps({"id": "2", "type": "prompt", "message": "hello over ws"}))
        frames = []
        while True:
            frame = json.loads(ws.receive_text())
            frames.append(frame)
            if frame["type"] == "agent_settled":
                break
        assert frames[0] == {"type": "response", "command": "prompt", "id": "2", "success": True}
        deltas = [
            f["assistantMessageEvent"]["delta"]
            for f in frames
            if f["type"] == "message_update" and f["assistantMessageEvent"]["type"] == "text_delta"
        ]
        assert "".join(deltas) == "echo: hello over ws"
        ws.send_text(json.dumps({"id": "3", "type": "get_last_assistant_text"}))
        assert json.loads(ws.receive_text())["data"] == {"text": "echo: hello over ws"}
