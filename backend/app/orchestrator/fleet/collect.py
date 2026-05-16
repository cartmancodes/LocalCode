"""Run a sub-provider and reduce its event stream to reviewable text.

If the sub-provider produced only tool calls (no narrative text), a structured
digest of those calls is returned/appended so downstream gates can verify what
the worker actually did rather than trusting a narrative summary.
"""
from __future__ import annotations

import json
from typing import Any

from ..base import RunContext
from .models import RoleConfig


async def collect_text(
    role: RoleConfig,
    prompt: str,
    cwd: str | None,
    additional_dirs: list[str] | None = None,
) -> str:
    """Invoke a sub-provider and return its concatenated assistant text.

    If the sub-provider produced only tool calls (no narrative text), fall back
    to a structured digest of those tool calls so downstream steps — and
    especially the reviewer — have something to act on. Without this, a coder
    that edits files but doesn't summarise produces "" and the reviewer
    correctly NACKs with "no output to review".
    """
    # Lazy import — the registry imports the fleet provider.
    from ..registry import get_provider

    sub = await get_provider(role.provider)  # type: ignore[arg-type]
    sub_ctx = RunContext(
        model=role.model,
        prompt=prompt,
        cwd=cwd,
        additional_dirs=list(additional_dirs or []),
        system_prompt=role.system_prompt,
    )
    chunks: list[str] = []
    tool_calls: list[tuple[str, str, Any]] = []  # (id, name, input)
    tool_results: dict[str, tuple[str, bool]] = {}  # id -> (content, is_error)
    async for ev in sub.run(sub_ctx):
        if ev.type == "assistant.text":
            chunks.append(ev.data.get("text", ""))
        elif ev.type == "assistant.tool_use":
            tool_calls.append(
                (ev.data.get("id", ""), ev.data.get("name", ""), ev.data.get("input"))
            )
        elif ev.type == "tool.result":
            content = ev.data.get("content")
            if isinstance(content, list):
                # Anthropic tool_result blocks come as a list of {type:text, text:...}
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

    text = "".join(chunks).strip()

    # Build a tool-activity digest. We always include it (when tools fired)
    # so downstream gates can verify what the worker actually did rather
    # than trusting its narrative summary. Previously, ANY non-empty text
    # caused tool activity to be dropped — which let the coder say "Starting
    # with the scaffold…" while having made zero edits, and the Reviewer had
    # no way to detect the gap from the prompt alone.
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
