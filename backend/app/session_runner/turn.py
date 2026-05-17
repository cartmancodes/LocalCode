"""The turn execution pipeline: persist the prompt, drain the provider,
checkpoint as it goes, and always finalize cleanly.

Pulled out of ``SessionRunner`` so the runner is left owning only
concurrency (the per-session lock, the approval channel, the task handle).
Everything provider-facing lives here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..orchestrator.base import Provider, RunContext
from ..storage.sessions import store as session_store
from .accumulator import TurnAccumulator
from .bus import EventBus

logger = logging.getLogger(__name__)


async def execute_turn(
    *,
    session_id: str,
    bus: EventBus,
    approval_q: asyncio.Queue[dict[str, Any]],
    provider: Provider,
    provider_name: str,
    model: str,
    cwd: str | None,
    additional_dirs: list[str],
    upstream_id: str | None,
    fleet_override: dict[str, Any] | None,
    permission_mode: str | None,
    prompt: str,
) -> None:
    """Run one turn end-to-end.

    The caller (``SessionRunner``) holds the per-session lock around this and
    owns ``approval_q``'s lifecycle. We persist the user message first so a
    later failure still leaves a coherent prompt in history, then drain the
    provider, checkpointing on every tool boundary. The ``finally`` block is
    load-bearing: it flushes trailing text, repairs dangling tool_use blocks,
    writes a final checkpoint, and guarantees an ``assistant.done`` so every
    viewer's UI clears its working indicator — even on error/cancellation.
    """
    await session_store.append_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": prompt}]},
    )
    await bus.broadcast(
        {"type": "session.started", "data": {"provider": provider_name, "model": model}}
    )

    ctx = RunContext(
        model=model,
        prompt=prompt,
        cwd=cwd,
        additional_dirs=additional_dirs,
        upstream_session_id=upstream_id,
        extras={"fleet_config_override": fleet_override} if fleet_override else {},
        approval_channel=approval_q,
        permission_mode=permission_mode,
    )

    acc = TurnAccumulator()
    saw_done = False
    try:
        async for ev in provider.run(ctx):
            if ev.type == "assistant.text":
                # Heartbeats are live-UI chrome only; skip persistence.
                if not ev.data.get("heartbeat"):
                    acc.add_text(ev.data.get("text", ""))
            elif ev.type == "assistant.tool_use":
                acc.flush_text()
                acc.add_tool_use(ev.data)
                await acc.checkpoint(session_id)
            elif ev.type == "tool.result":
                acc.add_tool_result(ev.data)
                await acc.checkpoint(session_id)
            elif ev.type == "assistant.done":
                saw_done = True
                acc.set_done(
                    cost_usd=ev.data.get("cost_usd"),
                    duration_ms=ev.data.get("duration_ms"),
                )
            await bus.broadcast(ev.to_json())
    except asyncio.CancelledError:
        # Backend shutdown / session delete. Finally still runs and writes
        # the synthetic tool_result so persisted state stays consistent.
        await bus.broadcast(
            {
                "type": "error",
                "data": {"message": "turn cancelled (backend shutdown)"},
            }
        )
        raise
    except Exception as exc:
        logger.exception("turn raised for %s", session_id)
        await bus.broadcast(
            {"type": "error", "data": {"message": str(exc) or repr(exc)}}
        )
    finally:
        acc.flush_text()
        acc.synthesize_missing_results()
        if acc.blocks:
            await acc.checkpoint(session_id)
        if not saw_done:
            # Always emit assistant.done so subscribers' UIs clear their
            # working indicator, even on error / cancellation.
            await bus.broadcast({"type": "assistant.done", "data": {}})
