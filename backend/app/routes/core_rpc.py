"""WebSocket transport for the pi-shaped core's RPC protocol.

Same frames as ``localcode --mode rpc`` on stdio — commands in, responses
and events out, ``extension_ui_request`` / ``extension_ui_response`` for
dialogs — so the React UI and any editor client speak one protocol.

    ws://host/api/core/rpc?engine=claude&model=claude-sonnet-4-6&cwd=/repo
                          &session=new|continue|<path>&trust=1&permission=ask
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.agent_session import create_agent_session
from ..core.engines import create_engine
from ..core.rpc.jsonl import serialize_json_line
from ..core.rpc.server import RpcServer, RpcUIBridge

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/core", tags=["core"])


def _cwd_from(raw: str | None) -> str:
    return os.path.abspath(raw or os.getcwd())


@router.websocket("/rpc")
async def core_rpc(websocket: WebSocket) -> None:
    q = websocket.query_params
    await websocket.accept()
    outbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def send(frame: dict[str, Any]) -> None:
        outbox.put_nowait(frame)

    async def sender() -> None:
        while True:
            frame = await outbox.get()
            if frame is None:
                return
            await websocket.send_text(serialize_json_line(frame).rstrip("\n"))

    sender_task = asyncio.create_task(sender())
    session = None
    server = None
    try:
        engine = create_engine(q.get("engine", "claude"))
        if q.get("rate_limit") and hasattr(engine, "rate_limit"):
            engine.rate_limit = json.loads(q["rate_limit"])
        ui = RpcUIBridge(send)
        session = await create_agent_session(
            engine=engine,
            cwd=_cwd_from(q.get("cwd")),
            session=q.get("session", "new"),
            in_memory=q.get("memory") == "1",
            project_trusted=q.get("trust") == "1",
            ui_bridge=ui,
            mode="rpc",
            model=q.get("model") or None,
            thinking_level=q.get("thinking", "off"),
            permission_mode=q.get("permission_mode") or None,
            default_permission=q.get("permission", "ask"),  # type: ignore[arg-type]
        )
        models = [
            {"provider": p, "modelId": m}
            for p, _, m in (item.partition("/") for item in (q.get("models") or "").split(","))
            if m
        ]
        server = RpcServer(session, send, models=models, ui_bridge=ui)
        await session.start()
        send({"type": "ready", "state": session.get_state()})
        while True:
            line = await websocket.receive_text()
            await server.handle_line(line)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — report, then close
        logger.exception("core rpc websocket failed")
        with contextlib.suppress(Exception):
            send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        if server is not None:
            await server.close()
        if session is not None:
            with contextlib.suppress(Exception):
                await session.shutdown("quit")
        outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(sender_task, timeout=2)
        with contextlib.suppress(Exception):
            await websocket.close()
