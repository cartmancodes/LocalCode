from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session, session_scope
from ..models import Message, Session
from ..orchestrator import get_provider
from ..orchestrator.base import Event, Provider, RunContext
from ..schemas import CreateSessionRequest, MessagesPage, MessageOut, SessionOut


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Per-session locking — prevents two browser tabs (or two queued prompts on
# one tab) from running concurrent turns on the same session, which would
# scramble the OpenCode SSE stream and the message ordering in the DB.
# ─────────────────────────────────────────────────────────────────────────────

_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        return _session_locks.setdefault(session_id, asyncio.Lock())


async def _drop_session_lock(session_id: str) -> None:
    async with _session_locks_guard:
        _session_locks.pop(session_id, None)


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


# ─────────────────────────────────────────────────────────────────────────────
# Safe wrapper around a provider's `run`: turns exceptions into events and
# guarantees an `assistant.done` so the UI clears its working indicator. Note
# that the *consumer* is responsible for `aclose()`-ing this generator on
# early exit (e.g. WS disconnect) so the inner provider's resources are freed.
# ─────────────────────────────────────────────────────────────────────────────

async def _safe_run(provider: Provider, ctx: RunContext) -> AsyncIterator[Event]:
    saw_done = False
    try:
        async for ev in provider.run(ctx):
            if ev.type == "assistant.done":
                saw_done = True
            yield ev
    except Exception as exc:  # noqa: BLE001
        logger.exception("provider.run raised")
        yield Event(type="error", data={"message": str(exc) or repr(exc)})
    if not saw_done:
        yield Event(type="assistant.done", data={})


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ─────────────────────────────────────────────────────────────────────────────
# REST
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SessionOut])
async def list_sessions(db: AsyncSession = Depends(get_session)) -> list[SessionOut]:
    rows = (await db.execute(select(Session).order_by(Session.updated_at.desc()))).scalars().all()
    return [SessionOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("", response_model=SessionOut)
async def create_session(
    body: CreateSessionRequest, db: AsyncSession = Depends(get_session)
) -> SessionOut:
    s = Session(
        provider=body.provider,
        model=body.model,
        cwd=_validate_cwd(body.cwd),
        additional_dirs=_validate_additional_dirs(body.additional_dirs),
        title=body.title or "New chat",
        fleet_config_override=body.fleet_config_override,
    )
    db.add(s)
    # `get_session` commits on clean exit — but we need `s.id` populated NOW
    # (so the response can include it), so flush + refresh explicitly.
    await db.flush()
    await db.refresh(s)
    return SessionOut.model_validate(s, from_attributes=True)


@router.delete("", status_code=204)
async def delete_all_sessions(db: AsyncSession = Depends(get_session)) -> None:
    """Wipe every session.

    Bulk-DELETE rather than per-row ORM delete: messages are pruned via the
    `ON DELETE CASCADE` on `messages.session_id`, so one round-trip suffices.
    """
    await db.execute(delete(Session))
    # Drop any in-memory per-session locks too, so they don't leak when the
    # same id is later reused by a new session.
    async with _session_locks_guard:
        _session_locks.clear()


@router.get("/{session_id}/messages", response_model=MessagesPage)
async def get_messages(
    session_id: str,
    before: datetime | None = Query(default=None, description="ISO datetime — return messages older than this"),
    limit: int | None = Query(default=None, ge=1, description="Page size; defaults from settings"),
    db: AsyncSession = Depends(get_session),
) -> MessagesPage:
    s = get_settings()
    page_size = min(limit or s.messages_page_default, s.messages_page_max)

    stmt = select(Message).where(Message.session_id == session_id)
    if before is not None:
        stmt = stmt.where(Message.created_at < before)
    # Fetch one extra row to determine `has_more` cheaply.
    stmt = stmt.order_by(Message.created_at.desc()).limit(page_size + 1)
    rows = (await db.execute(stmt)).scalars().all()

    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_before = rows[-1].created_at if (has_more and rows) else None

    # Frontend expects oldest → newest within the returned page.
    rows.reverse()
    return MessagesPage(
        messages=[MessageOut.model_validate(r, from_attributes=True) for r in rows],
        next_before=next_before,
        has_more=has_more,
    )


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, db: AsyncSession = Depends(get_session)) -> None:
    s = await db.get(Session, session_id)
    if not s:
        raise HTTPException(404)
    await db.delete(s)
    await _drop_session_lock(session_id)


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
    """Bidirectional chat. Client sends `{prompt: str}`; we stream events back.

    Concurrency: a per-session asyncio.Lock serializes turns so two tabs (or
    queued prompts on one tab) don't scramble the OpenCode event stream or
    interleave DB writes.

    Resource hygiene: the inner provider generator is `aclose()`-d on every
    exit path so a WS disconnect mid-stream doesn't leak a Claude subprocess
    or an open SSE connection. Whatever output we accumulated up to the
    disconnect is still persisted as the assistant message.

    Liveness: an idle WS is closed after `WS_IDLE_TIMEOUT_S` and pinged every
    `WS_HEARTBEAT_INTERVAL_S` to detect half-open TCP sessions.
    """
    await websocket.accept()

    # Load the session and pin provider/model from when it was created.
    async with session_scope() as db:
        sess = await db.get(Session, session_id)
        if not sess:
            await websocket.send_json({"type": "error", "data": {"message": "session not found"}})
            await websocket.close()
            return
        provider_name = sess.provider
        model = sess.model
        cwd = sess.cwd
        additional_dirs = list(sess.additional_dirs or [])
        upstream_id = sess.upstream_id
        fleet_override = sess.fleet_config_override

    provider = await get_provider(provider_name)  # type: ignore[arg-type]
    lock = await _get_session_lock(session_id)
    heartbeat = asyncio.create_task(_ws_heartbeat(websocket))

    try:
        while True:
            # Idle-timeout the receive so silent clients don't pin resources.
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

            prompt = msg.get("prompt") or ""
            if not prompt.strip():
                await websocket.send_json(
                    {"type": "error", "data": {"message": "empty prompt"}}
                )
                continue

            async with lock:
                # Per-turn back-channel for HITL approvals. The reader task
                # below drains inbound WS frames during the turn and routes
                # approval messages into this queue; everything else is
                # silently dropped (consistent with prior behaviour, which
                # also ignored frames mid-turn).
                approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

                async def _approval_reader() -> None:
                    while True:
                        try:
                            raw = await websocket.receive_text()
                        except WebSocketDisconnect:
                            return
                        except RuntimeError:
                            # Socket closed underneath us.
                            return
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict) and msg.get("type") == "approval":
                            await approval_q.put(msg)

                reader = asyncio.create_task(_approval_reader())
                try:
                    if not await _run_one_turn(
                        websocket=websocket,
                        session_id=session_id,
                        provider=provider,
                        provider_name=provider_name,
                        model=model,
                        cwd=cwd,
                        additional_dirs=additional_dirs,
                        upstream_id=upstream_id,
                        fleet_override=fleet_override,
                        prompt=prompt,
                        approval_channel=approval_q,
                    ):
                        # Client disconnected during the turn. Stop receiving.
                        return
                finally:
                    reader.cancel()
                    try:
                        await reader
                    except (asyncio.CancelledError, Exception):
                        pass
    finally:
        heartbeat.cancel()
        # Don't close the provider — it's a singleton shared across sessions.
        try:
            await websocket.close()
        except Exception:
            pass


async def _run_one_turn(
    *,
    websocket: WebSocket,
    session_id: str,
    provider: Provider,
    provider_name: str,
    model: str,
    cwd: str | None,
    additional_dirs: list[str],
    upstream_id: str | None,
    fleet_override: dict[str, Any] | None,
    prompt: str,
    approval_channel: asyncio.Queue[dict[str, Any]] | None = None,
) -> bool:
    """Drive one user → assistant turn.

    Returns True if the WS should keep accepting prompts, False if the client
    disconnected mid-stream (caller should stop the receive loop).
    """
    # Persist the user turn.
    async with session_scope() as db:
        db.add(
            Message(
                session_id=session_id,
                role="user",
                content=[{"type": "text", "text": prompt}],
            )
        )

    # Send the kickoff frame. If even THIS fails the client is already gone —
    # but we already wrote the user message, which is fine.
    try:
        await websocket.send_json(
            {
                "type": "session.started",
                "data": {"provider": provider_name, "model": model},
            }
        )
    except (WebSocketDisconnect, RuntimeError):
        return False

    ctx = RunContext(
        model=model,
        prompt=prompt,
        cwd=cwd,
        additional_dirs=additional_dirs,
        upstream_session_id=upstream_id,
        extras={"fleet_config_override": fleet_override} if fleet_override else {},
        approval_channel=approval_channel,
    )

    assistant_blocks: list[dict[str, Any]] = []
    cost_usd: float | None = None
    duration_ms: int | None = None
    text_buf: list[str] = []
    client_alive = True

    events = _safe_run(provider, ctx)
    try:
        async for ev in events:
            # Always update local accumulators — even if the client is gone we
            # want to persist what the provider produced so the chat history
            # is consistent.
            if ev.type == "assistant.text":
                text_buf.append(ev.data.get("text", ""))
            elif ev.type == "assistant.tool_use":
                if text_buf:
                    assistant_blocks.append({"type": "text", "text": "".join(text_buf)})
                    text_buf = []
                assistant_blocks.append({"type": "tool_use", **ev.data})
            elif ev.type == "tool.result":
                assistant_blocks.append({"type": "tool_result", **ev.data})
            elif ev.type == "assistant.done":
                cost_usd = ev.data.get("cost_usd")
                duration_ms = ev.data.get("duration_ms")

            if not client_alive:
                # Client gone — stop streaming. Break (rather than continue)
                # so the provider doesn't keep producing tokens we'll throw
                # away. The `finally` below closes the generator cleanly.
                break

            try:
                await websocket.send_json(ev.to_json())
            except (WebSocketDisconnect, RuntimeError):
                client_alive = False
                break
    finally:
        # Critical: close the wrapper generator so the underlying
        # provider.run's `finally` blocks (httpx stream close, subprocess
        # cleanup) actually execute.
        await events.aclose()

        # Flush any trailing text.
        if text_buf:
            assistant_blocks.append({"type": "text", "text": "".join(text_buf)})

        # Persist whatever we got — even on disconnect.
        if assistant_blocks:
            try:
                async with session_scope() as db:
                    db.add(
                        Message(
                            session_id=session_id,
                            role="assistant",
                            content=assistant_blocks,
                            cost_usd=cost_usd,
                            duration_ms=duration_ms,
                        )
                    )
            except Exception:
                logger.exception("failed to persist assistant turn for %s", session_id)

    return client_alive
