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
    owns ``approval_q``'s lifecycle. We persist the user message first — so a
    later failure still leaves a coherent prompt in history — then drain the
    provider, checkpointing on every tool boundary. Persisting the prompt is
    *inside* the try: it is the one step that fails when the session directory
    was deleted under us (a delete while a tab is open), and when it raised
    outside the try the turn died with no ``error`` and no ``assistant.done``,
    leaving every viewer's working indicator spinning until reload.

    The ``finally`` block is load-bearing: it flushes trailing text, repairs
    dangling tool_use blocks, writes a final checkpoint, and emits
    ``assistant.done`` unless the provider's own one already *reached the bus*.
    That is the terminal-event contract — exactly one per turn, however the
    turn ends — and it matters in both directions, because every viewer clears
    its working indicator on that event: zero leaves it spinning until reload,
    two clears it for a turn that is still running.
    """
    ctx = RunContext(
        model=model,
        prompt=prompt,
        # Providers key per-session state on this (Task 4's persistent SDK
        # client); without it every turn looks like a new session to them.
        session_id=session_id,
        cwd=cwd,
        additional_dirs=additional_dirs,
        upstream_session_id=upstream_id,
        extras={"fleet_config_override": fleet_override} if fleet_override else {},
        approval_channel=approval_q,
        permission_mode=permission_mode,
    )

    acc = TurnAccumulator()
    # "A terminal event has reached the bus", not "a done event arrived" — see
    # where it is set in the drain loop.
    done_broadcast = False
    try:
        try:
            await session_store.append_message(
                session_id,
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            )
        except FileNotFoundError:
            # The session directory is gone — deleted (or wiped) while this
            # prompt was in flight. Nothing is left to persist into, so report
            # it and return; `finally` still emits the single terminal event.
            # Raising instead (what this did when the append sat outside the
            # try) killed a detached task silently: no `error`, no
            # `assistant.done`, a working indicator spinning until reload, and
            # the only trace "Task exception was never retrieved" at GC.
            logger.warning("prompt for deleted session %s discarded", session_id)
            await bus.broadcast(
                {
                    "type": "error",
                    "data": {
                        "message": (
                            "this session was deleted — the prompt was not "
                            "run. Open a new chat to continue."
                        )
                    },
                }
            )
            return
        await bus.broadcast(
            {
                "type": "session.started",
                "data": {"provider": provider_name, "model": model},
            }
        )

        try:
            opened_upstream_id = await provider.open_session(ctx)
        except Exception:
            opened_upstream_id = None
        if opened_upstream_id and opened_upstream_id != upstream_id:
            upstream_id = opened_upstream_id
            ctx.upstream_session_id = opened_upstream_id
            await session_store.update_session(
                session_id, upstream_id=opened_upstream_id
            )

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
                new_upstream_id = ev.data.get("upstream_session_id")
                if new_upstream_id and new_upstream_id != upstream_id:
                    upstream_id = new_upstream_id
                    await session_store.update_session(
                        session_id, upstream_id=new_upstream_id
                    )
                acc.set_done(
                    cost_usd=ev.data.get("cost_usd"),
                    duration_ms=ev.data.get("duration_ms"),
                )
            await bus.broadcast(ev.to_json())
            if ev.type == "assistant.done":
                # Set only once the done has actually reached the bus. The
                # `finally` reads this to decide whether the turn still owes
                # subscribers a terminal event, and the work above can raise —
                # persisting a changed upstream_session_id does I/O. Setting it
                # on arrival instead meant such a failure broadcast an `error`
                # and then skipped the terminal event: zero `assistant.done`,
                # and a working indicator spinning until reload.
                done_broadcast = True
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
            # final=True bumps updated_at once; mid-turn checkpoints skip
            # the bump to keep the per-tool I/O cost down.
            await acc.checkpoint(session_id, final=True)
        else:
            # No blocks were ever emitted (provider errored before any
            # output). Still bump updated_at so the session moves to the
            # top of the sidebar — the user just sent a prompt.
            try:
                await session_store.update_session(session_id)
            except Exception:
                logger.debug("end-of-turn touch failed for %s", session_id)
        if not done_broadcast:
            # Always emit assistant.done so subscribers' UIs clear their
            # working indicator, even on error / cancellation.
            await bus.broadcast({"type": "assistant.done", "data": {}})
