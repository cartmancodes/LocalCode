from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session, session_scope
from ..models import Message, Session
from ..orchestrator import get_provider
from ..orchestrator.base import RunContext
from ..schemas import CreateSessionRequest, MessageOut, SessionOut


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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
        cwd=body.cwd,
        title=body.title or "New chat",
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return SessionOut.model_validate(s, from_attributes=True)


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: str, db: AsyncSession = Depends(get_session)
) -> list[MessageOut]:
    rows = (
        await db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
        )
    ).scalars().all()
    return [MessageOut.model_validate(r, from_attributes=True) for r in rows]


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, db: AsyncSession = Depends(get_session)) -> None:
    s = await db.get(Session, session_id)
    if not s:
        raise HTTPException(404)
    await db.delete(s)
    await db.commit()


@router.websocket("/{session_id}/ws")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    """Bidirectional chat. Client sends `{prompt: str}`; we stream events back.

    Each turn: persist the user message, run the provider, persist the assistant
    message (concatenated text + tool blocks), and emit unified events to the
    client as JSON frames.
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
        upstream_id = sess.upstream_id

    provider = get_provider(provider_name)  # type: ignore[arg-type]

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
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

            # Persist user turn.
            async with session_scope() as db:
                db.add(
                    Message(
                        session_id=session_id,
                        role="user",
                        content=[{"type": "text", "text": prompt}],
                    )
                )

            await websocket.send_json(
                {
                    "type": "session.started",
                    "data": {"provider": provider_name, "model": model},
                }
            )

            ctx = RunContext(
                model=model,
                prompt=prompt,
                cwd=cwd,
                upstream_session_id=upstream_id,
            )

            assistant_blocks: list[dict[str, Any]] = []
            cost_usd: float | None = None
            duration_ms: int | None = None
            text_buf: list[str] = []

            async for ev in provider.run(ctx):
                # Update local accumulators for persistence.
                if ev.type == "assistant.text":
                    text_buf.append(ev.data.get("text", ""))
                elif ev.type == "assistant.tool_use":
                    if text_buf:
                        assistant_blocks.append(
                            {"type": "text", "text": "".join(text_buf)}
                        )
                        text_buf = []
                    assistant_blocks.append({"type": "tool_use", **ev.data})
                elif ev.type == "tool.result":
                    assistant_blocks.append({"type": "tool_result", **ev.data})
                elif ev.type == "assistant.done":
                    cost_usd = ev.data.get("cost_usd")
                    duration_ms = ev.data.get("duration_ms")

                await websocket.send_json(ev.to_json())

            if text_buf:
                assistant_blocks.append({"type": "text", "text": "".join(text_buf)})

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
    finally:
        # Don't close the provider — it's a singleton shared across sessions.
        try:
            await websocket.close()
        except Exception:
            pass
