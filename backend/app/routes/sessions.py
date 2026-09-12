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
from ..orchestrator.permissions import is_within
from ..schemas import CreateSessionRequest, MessageOut, MessagesPage, SessionOut
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
    # Denied roots are checked FIRST, and before the "empty allowlist is
    # permissive" shortcut below: `~` is an allowed root, so without this a
    # session could be rooted at `~/.ssh` or `~/.claude` and every tool the
    # spawned CLI runs would be inside a credential store. `is_within`
    # re-resolves both sides, so a symlink pointing into one is caught too.
    for denied_root in s.denied_path_list():
        if is_within(p, (denied_root,)):
            raise HTTPException(
                status_code=400,
                detail=f"cwd {p!s} is under a denied directory: {denied_root!s}",
            )
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
    user-global index. In-memory runners are torn down first so any
    in-flight checkpoint doesn't race the rmtree and raise
    FileNotFoundError mid-write."""
    await drop_all_runners()
    await session_store.delete_all_sessions()


@router.get("/{session_id}/messages", response_model=MessagesPage)
async def get_messages(
    session_id: str,
    before: datetime | None = Query(
        default=None, description="ISO datetime — return messages older than this"
    ),
    limit: int | None = Query(
        default=None, ge=1, description="Page size; defaults from settings"
    ),
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
    # Cancel the runner BEFORE removing the dir so any in-flight checkpoint
    # finishes against a valid path. Otherwise the rmtree races the
    # accumulator and the trailing checkpoint disappears with a swallowed
    # FileNotFoundError.
    await drop_runner(session_id)
    existed = await session_store.delete_session(session_id)
    if not existed:
        raise HTTPException(404)


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
    if runner is None:
        # The session was deleted between the lookup above and now. The
        # registry refuses to resurrect it rather than hand this viewer a live
        # bus for a session that can never persist anything again.
        await websocket.send_json(
            {"type": "error", "data": {"message": "session not found"}}
        )
        await websocket.close()
        return

    # Optional `?since=<id>` query for replay. The frontend tracks the
    # highest `_id` it received and passes it on reconnect; we replay any
    # buffered events newer than that.
    since_raw = websocket.query_params.get("since")
    since_id: int | None = None
    if since_raw and since_raw.isdigit():
        since_id = int(since_raw)

    subscription = await runner.subscribe(since_id=since_id)
    queue, replay = subscription.queue, subscription.replay

    # Replay buffered events first so the client catches up before live
    # events arrive. If the client died between subscribe and replay, the
    # forwarder below will detect it on the next send and bail.
    for ev in replay:
        try:
            await websocket.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            await runner.unsubscribe(queue)
            return

    # A turn blocked on an approval gate has nobody to answer it if the tab that
    # asked is gone, and the card lives only in the replay ring — which a fresh
    # connection (no `?since=`) never reads. Re-emit it here, after the replay so
    # the card lands in chat order and before live events so a decision already
    # in flight still wins. A turn can have several gates open at once
    # (parallel tool calls each raise their own — see
    # `approvals._ApprovalRouter`), so every open card gets this treatment, not
    # just the newest: a single-card check here would silently drop whichever
    # gate isn't picked, and the caller would have no way to answer it.
    #
    # Read *after* subscribing, and decided per-card by event id rather than
    # approval id: approval ids are unique per gate now (`next_approval_id`),
    # but an id says nothing about whether *this* viewer has already been
    # handed the card, which is the only question here. The three ways it can
    # already have it — and each is a duplicate if we send it too:
    for pending in runner.pending_approvals:
        card_id = int(pending.event.get("_id") or 0)
        already_seen = card_id <= (since_id or 0)  # it had the card before reconnecting
        incoming = card_id > subscription.watermark  # the live queue carries it
        in_replay = any(ev.get("_id") == card_id for ev in replay)
        if not (already_seen or incoming or in_replay):
            logger.info(
                "ws %s: re-offering outstanding approval %s to a new viewer",
                session_id,
                pending.approval_id,
            )
            try:
                await websocket.send_json(pending.event)
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
            except TimeoutError:
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

            # Provider session ids are learned after the first turn. Refresh
            # metadata so follow-up prompts sent over this same WebSocket
            # resume the provider-native conversation instead of starting over.
            refreshed = await session_store.get_session(session_id)
            if refreshed:
                upstream_id = refreshed.get("upstream_id")

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
                # A retired runner means this session was deleted under the
                # socket, which is a different story from a busy session.
                if runner.is_retired:
                    reason = (
                        "this session was deleted — open a new chat to "
                        "continue."
                    )
                else:
                    reason = (
                        "another turn is already running on this session — "
                        "wait for it to finish, or open a new chat."
                    )
                await websocket.send_json(
                    {"type": "error", "data": {"message": reason}}
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
