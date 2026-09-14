"""Non-interactive modes (pi: ``-p`` print and ``--mode json``)."""

from __future__ import annotations

import sys
from typing import Any

from .agent_session import AgentSession
from .rpc.events import to_json_event
from .rpc.jsonl import serialize_json_line


async def run_json_mode(session: AgentSession, prompt: str, *, stdout: Any = None) -> int:
    """Run one prompt; stream every session event as a JSON line; exit."""
    out = stdout or sys.stdout
    session.subscribe(lambda ev: (out.write(serialize_json_line(to_json_event(ev))), out.flush()))
    await session.start()
    try:
        await session.prompt(prompt, source="rpc")
        await session.wait_for_idle()
    finally:
        await session.shutdown("quit")
    return 0


async def run_print_mode(session: AgentSession, prompt: str, *, stdout: Any = None) -> int:
    """Run one prompt; print the final assistant text; exit non-zero on error."""
    out = stdout or sys.stdout
    errors: list[str] = []
    session.subscribe(
        lambda ev: errors.append(str(ev.get("message"))) if ev.get("type") == "error" else None
    )
    await session.start()
    try:
        await session.prompt(prompt, source="rpc")
        await session.wait_for_idle()
    finally:
        await session.shutdown("quit")
    text = session.get_last_assistant_text()
    if text:
        out.write(text.rstrip("\n") + "\n")
        out.flush()
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 1
    return 0
