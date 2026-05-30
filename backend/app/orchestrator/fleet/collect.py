"""Run a sub-provider and reduce its event stream to reviewable text.

If the sub-provider produced only tool calls (no narrative text), a structured
digest of those calls is returned/appended so downstream gates can verify what
the worker actually did rather than trusting a narrative summary.

**Loop isolation.** This is invoked from inside the orchestrator's MCP tool
callback, which itself runs inside the orchestrator's own
``claude_agent_sdk.query()``. Driving a *second* ``query()`` on the same event
loop deadlocks the SDK transport (the inner session never receives events →
600 s timeout — the original "planner planned but coder never started" bug).
So ``collect_text`` is written to be **event-loop-agnostic**: it builds its
own fresh provider instance (never the shared, loop-bound registry singleton)
and closes it. ``provider._run_step_with_role`` runs this whole coroutine on a
*separate thread with its own event loop*, so the sub-provider's SDK session
is fully isolated from the orchestrator's.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from ..base import RunContext
from .models import RoleConfig


async def collect_text(
    role: RoleConfig,
    prompt: str,
    cwd: str | None,
    additional_dirs: list[str] | None = None,
    *,
    permission_mode: str | None = None,
    role_name: str | None = None,
    progress: threading.Event | None = None,
) -> str:
    """Invoke a sub-provider and return its concatenated assistant text.

    ``progress`` (a thread-safe ``threading.Event``) is set the moment the
    sub-provider yields its FIRST event — the caller uses this to tell
    "healthy but slow" apart from "wedged backend, zero output" and fail fast
    on the latter instead of waiting the full step timeout.

    If the sub-provider produced only tool calls (no narrative text), fall
    back to a structured digest of those tool calls so downstream steps — and
    especially the reviewer — have something to act on.
    """
    # Build a FRESH provider rather than the shared registry singleton: the
    # singleton's asyncio.Lock is bound to the main loop and would explode
    # when touched from this isolated thread's loop. A per-step provider is
    # loop-local and correct; we close it below so opencode's httpx client
    # (bound to this loop) doesn't leak.
    from ..registry import _build_provider

    sub = _build_provider(role.provider)  # type: ignore[arg-type]
    sub_ctx = RunContext(
        model=role.model,
        prompt=prompt,
        cwd=cwd,
        additional_dirs=list(additional_dirs or []),
        system_prompt=role.system_prompt,
        permission_mode=permission_mode,
        extras=_role_extras(role_name),
    )
    chunks: list[str] = []
    tool_calls: list[tuple[str, str, Any]] = []  # (id, name, input)
    tool_results: dict[str, tuple[str, bool]] = {}  # id -> (content, is_error)
    try:
        async for ev in sub.run(sub_ctx):
            # First sign of life from the backend — lets the caller stop
            # waiting on a wedged provider quickly.
            if progress is not None and not progress.is_set():
                progress.set()
            if ev.type == "assistant.text":
                chunks.append(ev.data.get("text", ""))
            elif ev.type == "assistant.tool_use":
                tool_calls.append(
                    (ev.data.get("id", ""), ev.data.get("name", ""), ev.data.get("input"))
                )
            elif ev.type == "tool.result":
                content = ev.data.get("content")
                if isinstance(content, list):
                    # Anthropic tool_result blocks come as a list of
                    # {type:text, text:...}
                    content = "\n".join(
                        str(b.get("text", b)) if isinstance(b, dict) else str(b)
                        for b in content
                    )
                tool_results[ev.data.get("tool_use_id", "")] = (
                    str(content or ""),
                    bool(ev.data.get("is_error")),
                )
            elif ev.type == "error":
                raise RuntimeError(ev.data.get("message") or "sub-provider error")
    finally:
        try:
            await sub.aclose()
        except Exception:
            pass

    text = "".join(chunks).strip()

    # Build a tool-activity digest. We always include it (when tools fired)
    # so downstream gates can verify what the worker actually did rather
    # than trusting its narrative summary.
    digest_lines: list[str] = []
    if tool_calls:
        digest_lines.append(f"(tool activity from {role.provider}:{role.model})")
        for tid, name, tinput in tool_calls:
            inp_str = json.dumps(tinput, default=str) if tinput is not None else "{}"
            if len(inp_str) > 400:
                inp_str = inp_str[:400] + "…"
            digest_lines.append(f"- {name} input={inp_str}")
            if tid in tool_results:
                content, is_error = tool_results[tid]
                tag = "ERR" if is_error else "OK"
                snippet = content.replace("\n", " ")[:300]
                digest_lines.append(f"    [{tag}] {snippet}")

    if text and digest_lines:
        return text + "\n\n---\n" + "\n".join(digest_lines)
    if text:
        return text
    if digest_lines:
        return "\n".join(digest_lines)
    return ""


# Back-compat alias — the original module exposed this underscore name.
_collect_text = collect_text


def _role_extras(role_name: str | None) -> dict[str, Any]:
    if role_name != "planner":
        return {}
    # The planner must produce a plan artifact only; implementation belongs to
    # the coder and review belongs to the reviewer.
    return {
        "claude_no_tools": True,
        "claude_disallowed_tools": [
            "Edit",
            "Write",
            "MultiEdit",
            "NotebookEdit",
            "Bash",
            "BashOutput",
            "KillBash",
            "Agent",
            "Task",
            "Skill",
            "ToolSearch",
            "Monitor",
            "RemoteTrigger",
            "TaskStop",
        ]
    }
