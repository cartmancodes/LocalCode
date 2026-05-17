from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)

from ..config import get_settings
from ..orchestrator import get_provider
from ..schemas import CreateSessionRequest, MessagesPage, MessageOut, SessionOut
from ..session_runner import drop_all_runners, drop_runner, get_runner
from ..storage.sessions import store as session_store


logger = logging.getLogger(__name__)


# Concurrency note: the per-session asyncio.Lock that used to live here moved
# into `SessionRunner` so it covers turn execution rather than just the WS
# handler. WS connections are viewers — they subscribe/unsubscribe without
# holding any session-wide lock.


# ─────────────────────────────────────────────────────────────────────────────
# Path validation — `cwd` is user-supplied and ends up as the working dir of
# the spawned `claude` CLI. An allowlist (configurable via Settings) prevents
# trivial path traversal. Empty allowlist = permissive (single-user dev mode).
# ─────────────────────────────────────────────────────────────────────────────

def _validate_cwd(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    s = get_settings()
    roots = s.cwd_allowlist()
    p = Path(cwd).expanduser().resolve()
    if not roots:
        return str(p)
    for r in roots:
        if p == r or r in p.parents:
            return str(p)
    raise HTTPException(
        status_code=400,
        detail=f"cwd {p!s} is not under any allowed root: {[str(r) for r in roots]}",
    )


def _validate_additional_dirs(dirs: list[str] | None) -> list[str] | None:
    """Each additional dir is validated like `cwd`. Empties are dropped;
    duplicates are deduped."""
    if not dirs:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for raw in dirs:
        v = (raw or "").strip()
        if not v:
            continue
        validated = _validate_cwd(v)  # raises 400 if outside the allowlist
        if validated and validated not in seen:
            seen.add(validated)
            out.append(validated)
    return out or None


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ─────────────────────────────────────────────────────────────────────────────
# REST
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SessionOut])
async def list_sessions() -> list[SessionOut]:
    rows = await session_store.list_sessions()
    return [SessionOut.model_validate(r) for r in rows]


@router.post("", response_model=SessionOut)
async def create_session(body: CreateSessionRequest) -> SessionOut:
    meta = await session_store.create_session(
        provider=body.provider,
        model=body.model,
        cwd=_validate_cwd(body.cwd),
        additional_dirs=_validate_additional_dirs(body.additional_dirs),
        title=body.title or "New chat",
        permission_mode=body.permission_mode,
        fleet_config_override=body.fleet_config_override,
    )
    return SessionOut.model_validate(meta)


@router.delete("", status_code=204, response_model=None)
async def delete_all_sessions() -> None:
    """Wipe every session — removes the on-disk session dirs and the
    user-global index. In-memory runners are torn down too (any in-flight
    turn is cancelled) so a reused id doesn't inherit stale state."""
    await session_store.delete_all_sessions()
    await drop_all_runners()


@router.get("/{session_id}/messages", response_model=MessagesPage)
async def get_messages(
    session_id: str,
    before: datetime | None = Query(default=None, description="ISO datetime — return messages older than this"),
    limit: int | None = Query(default=None, ge=1, description="Page size; defaults from settings"),
) -> MessagesPage:
    s = get_settings()
    page_size = min(limit or s.messages_page_default, s.messages_page_max)
    msgs, next_before, has_more = await session_store.list_messages(
        session_id, before=before, limit=page_size
    )
    return MessagesPage(
        messages=[MessageOut.model_validate(m) for m in msgs],
        next_before=next_before,
        has_more=has_more,
    )


@router.delete("/{session_id}", status_code=204, response_model=None)
async def delete_session(session_id: str) -> None:
    existed = await session_store.delete_session(session_id)
    if not existed:
        raise HTTPException(404)
    await drop_runner(session_id)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────────────────────

WS_IDLE_TIMEOUT_S = 30 * 60       # 30 minutes — closes idle sockets
WS_HEARTBEAT_INTERVAL_S = 30      # ping cadence (frontend ignores `type:"ping"`)


async def _ws_heartbeat(ws: WebSocket) -> None:
    """Periodic ping so dead TCP connections are detected and the WS stack
    keeps idle sockets warm against intermediaries that close on inactivity.
    Cancelled by the parent task; exceptions are swallowed because failure
    here just means the next real send will fail and we'll clean up there."""
    try:
        while True:
            await asyncio.sleep(WS_HEARTBEAT_INTERVAL_S)
            try:
                await ws.send_json({"type": "ping", "data": {}})
            except Exception:
                return
    except asyncio.CancelledError:
        pass


@router.websocket("/{session_id}/ws")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    """Viewer + control channel for a session.

    The actual turn execution lives in `SessionRunner` and runs as an
    independent task. This handler does three things:

      1. Subscribes to the runner's broadcast on connect (with optional
         `?since=<id>` replay so a reconnect picks up missed events
         without refetching `/messages`).
      2. Forwards inbound frames: `{prompt}` starts a new turn (rejected
         if one is already running); `{type:"approval"}` is routed to the
         runner's approval queue; `{type:"ping"|"pong"}` is keepalive.
      3. Pumps live events from its subscriber queue out to the socket.

    A WS disconnect just unsubscribes — the running turn keeps going and
    a reconnect resumes streaming.
    """
    await websocket.accept()

    sess = await session_store.get_session(session_id)
    if not sess:
        await websocket.send_json(
            {"type": "error", "data": {"message": "session not found"}}
        )
        await websocket.close()
        return
    provider_name = sess["provider"]
    model = sess["model"]
    cwd = sess.get("cwd")
    additional_dirs = list(sess.get("additional_dirs") or [])
    upstream_id = sess.get("upstream_id")
    permission_mode = sess.get("permission_mode")
    fleet_override = sess.get("fleet_config_override")

    runner = await get_runner(session_id)

    # Optional `?since=<id>` query for replay. The frontend tracks the
    # highest `_id` it received and passes it on reconnect; we replay any
    # buffered events newer than that.
    since_raw = websocket.query_params.get("since")
    since_id: int | None = None
    if since_raw and since_raw.isdigit():
        since_id = int(since_raw)

    queue, replay = await runner.subscribe(since_id=since_id)

    # Replay buffered events first so the client catches up before live
    # events arrive. If the client died between subscribe and replay, the
    # forwarder below will detect it on the next send and bail.
    for ev in replay:
        try:
            await websocket.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            await runner.unsubscribe(queue)
            return

    heartbeat = asyncio.create_task(_ws_heartbeat(websocket))

    async def _forward_events() -> None:
        """Drain the subscriber queue → WS until the WS dies."""
        while True:
            ev = await queue.get()
            try:
                await websocket.send_json(ev)
            except (WebSocketDisconnect, RuntimeError):
                return

    forwarder = asyncio.create_task(_forward_events())

    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=WS_IDLE_TIMEOUT_S
                )
            except WebSocketDisconnect:
                return
            except asyncio.TimeoutError:
                logger.info("ws %s closed: idle timeout", session_id)
                try:
                    await websocket.close(code=1001, reason="idle timeout")
                except Exception:
                    pass
                return

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "data": {"message": "invalid JSON frame"}}
                )
                continue

            # Keepalive: server pings every WS_HEARTBEAT_INTERVAL_S; the
            # client echoes back so the server's `receive_text()` resets
            # its idle timer. Either direction's frame counts.
            if msg.get("type") in ("ping", "pong"):
                continue

            if msg.get("type") == "approval":
                await runner.submit_approval(msg)
                continue

            prompt = msg.get("prompt") or ""
            if not prompt.strip():
                await websocket.send_json(
                    {"type": "error", "data": {"message": "empty prompt"}}
                )
                continue

            provider = await get_provider(provider_name)  # type: ignore[arg-type]
            started = runner.start_turn(
                provider=provider,
                provider_name=provider_name,
                model=model,
                cwd=cwd,
                additional_dirs=additional_dirs,
                upstream_id=upstream_id,
                fleet_override=fleet_override,
                permission_mode=permission_mode,
                prompt=prompt,
            )
            if not started:
                # Reject silently-queueing a prompt — the user expects their
                # message to either start running now or get a clear error.
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {
                            "message": (
                                "another turn is already running on this "
                                "session — wait for it to finish, or open a "
                                "new chat."
                            )
                        },
                    }
                )
    finally:
        heartbeat.cancel()
        forwarder.cancel()
        try:
            await forwarder
        except (asyncio.CancelledError, Exception):
            pass
        await runner.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:
            pass
