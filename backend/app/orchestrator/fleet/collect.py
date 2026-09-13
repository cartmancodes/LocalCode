"""Run a sub-provider and reduce its event stream to a bounded step envelope.

``collect_step`` is the real function: it drives one sub-provider to
completion and returns a :class:`StepResult` — a capped summary, a capped tool
digest, the parsed verdict for a gate role, the reported token usage, and a
pointer to the artifact holding the full output when the output was too large
to inline. ``collect_text`` remains as a thin wrapper over
``collect_step(...).context_text()`` for callers that only want text.

Why an envelope rather than the transcript it used to return: the old version
handed the orchestrator everything the sub-provider said, so a coder that
``cat``ed a 2 MB test log spent the rest of the turn's context on it and, worse,
changed the prompt prefix for every later turn (see ``artifacts.py`` on why
that is a cache problem as much as a window problem). Eviction happens here,
once, on the way out of the step — not at each consumer, where one forgetful
caller is all it takes to put the megabytes back.

If the sub-provider produced only tool calls (no narrative text), the digest of
those calls is still reported so downstream gates can verify what the worker
actually did rather than trusting a narrative summary.

**Loop isolation.** This is invoked from inside the orchestrator's MCP tool
callback, which itself runs inside the orchestrator's own
``claude_agent_sdk.query()``. Driving a *second* ``query()`` on the same event
loop deadlocks the SDK transport (the inner session never receives events →
600 s timeout — the original "planner planned but coder never started" bug).
So this module is written to be **event-loop-agnostic**: it builds its own
fresh provider instance (never the shared, loop-bound registry singleton) and
closes it. ``provider._run_step_with_role`` runs this whole coroutine in a
*separate OS process*, so the sub-provider's SDK session is fully isolated
from the orchestrator's.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from ...artifacts import ArtifactStore
from ...config import get_settings
from ..base import RunContext
from .envelope import MAX_TOOL_DIGEST_CHARS, StepResult, coerce_usage
from .gate import GATE_ROLES, parse_verdict
from .models import RoleConfig

# The token counts Task 7's per-turn budget and Task 11's quota governor read.
# Pulled out of the sub-provider's ``assistant.done`` usage dict by name so the
# envelope's ``usage`` stays ``dict[str, int]`` — the same dict also carries
# ``provider``/``model``/``cost_usd``, which are not token counts and would
# break that contract for everything downstream.
_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


async def collect_step(
    role: RoleConfig,
    prompt: str,
    cwd: str | None,
    additional_dirs: list[str] | None = None,
    *,
    permission_mode: str | None = None,
    role_name: str | None = None,
    progress: threading.Event | None = None,
    session_id: str | None = None,
) -> StepResult:
    """Invoke a sub-provider and return its bounded :class:`StepResult`.

    ``progress`` (a thread-safe ``threading.Event``, or anything with the same
    ``is_set``/``set`` shape — see ``subproc._StdoutFirstSignal``) is set the
    moment the sub-provider yields its FIRST event. The caller uses this to
    tell "healthy but slow" apart from "wedged backend, zero output" and fail
    fast on the latter instead of waiting the full step timeout.

    An ``error`` event raises ``RuntimeError``: a step that failed must not be
    reported as a step that produced nothing, because "no output" is a state
    the pipeline tries to recover from by re-prompting.
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
        # Threaded through so a sub-provider that keys per-session state (Task
        # 4's SDK client reuse, a quota window) on the session can find it
        # instead of treating every step as a brand-new session.
        session_id=session_id,
        additional_dirs=list(additional_dirs or []),
        system_prompt=role.system_prompt,
        permission_mode=permission_mode,
        extras=_role_extras(role_name),
    )
    chunks: list[str] = []
    tool_calls: list[tuple[str, str, Any]] = []  # (id, name, input)
    tool_results: dict[str, tuple[str, bool]] = {}  # id -> (content, is_error)
    usage: dict[str, int] | None = None
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
            elif ev.type == "assistant.done":
                usage = _token_usage(ev.data.get("usage"))
            elif ev.type == "error":
                raise RuntimeError(ev.data.get("message") or "sub-provider error")
    finally:
        try:
            await sub.aclose()
        except Exception:
            pass

    text = "".join(chunks).strip()
    digest = _tool_digest(role, tool_calls, tool_results)

    # Parse the verdict from the FULL text, before summarizing. A long review
    # whose verdict sat past the head/tail trim would otherwise fail safe for
    # no reason — the gate did decide, we just threw the decision away.
    structured: dict[str, Any] | None = None
    if role_name in GATE_ROLES:
        structured = parse_verdict(text, role_name).to_dict()

    # Construct the store here, per call: its default root resolves
    # ``Path.home()`` when it is built, so a module-level instance would freeze
    # the developer's real home into every test run (see ``artifacts.py``).
    max_bytes = get_settings().artifact_inline_max_bytes
    summary, ref = ArtifactStore().store_if_large(
        text, kind="step-output", max_bytes=max_bytes
    )
    return StepResult(
        summary=summary,
        structured=structured,
        artifact_id=ref.id if ref else None,
        artifact_path=str(ref.path) if ref else None,
        tool_digest=digest,
        full_bytes=len(text.encode("utf-8")),
        usage=usage,
    )


async def collect_text(
    role: RoleConfig,
    prompt: str,
    cwd: str | None,
    additional_dirs: list[str] | None = None,
    *,
    permission_mode: str | None = None,
    role_name: str | None = None,
    progress: threading.Event | None = None,
    session_id: str | None = None,
) -> str:
    """``collect_step`` for callers that only want text.

    Kept because this name has been the module's public surface since the
    fleet existed. It is no longer the place where the eviction rule lives —
    it just asks the envelope what an orchestrator may see, so a caller that
    never migrates to ``collect_step`` still gets a bounded string.
    """
    result = await collect_step(
        role,
        prompt,
        cwd,
        additional_dirs,
        permission_mode=permission_mode,
        role_name=role_name,
        progress=progress,
        session_id=session_id,
    )
    return result.context_text()


# Back-compat alias — the original module exposed this underscore name.
_collect_text = collect_text


def _token_usage(raw: Any) -> dict[str, int] | None:
    """The token counts out of a sub-provider's ``assistant.done`` usage dict,
    or ``None`` when it reported none.

    Selects the keys (the event's dict also carries ``provider``, ``model``,
    ``cost_usd``, which are not token counts) and hands them to
    ``coerce_usage`` for the ``int`` contract, so this path and ``from_wire``
    cannot drift apart about what a count is.

    Defensive at every step, deliberately: usage is telemetry, and Task 5's
    rule is that telemetry must never be able to fail the step it describes. A
    provider that reports a string, a list, or a token count of ``"lots"``
    loses its usage, not its result.
    """
    if not isinstance(raw, dict):
        return None
    return coerce_usage({key: raw.get(key) for key in _USAGE_TOKEN_KEYS})


def _tool_digest(
    role: RoleConfig,
    tool_calls: list[tuple[str, str, Any]],
    tool_results: dict[str, tuple[str, bool]],
) -> str:
    """A capped, human-readable log of what the step's tools actually did.

    Always included when tools fired, so a gate can check the worker's claims
    against its actions rather than trusting a narrative summary — but capped
    at ``MAX_TOOL_DIGEST_CHARS``, because a step with 400 tool calls used to
    produce a digest far larger than the narrative it annotates and that
    digest went into context verbatim. Entries are dropped whole (never cut
    mid-entry) and the tail line says how many calls are missing, so a reader
    can tell "that's all of it" from "there was more".
    """
    if not tool_calls:
        return ""
    header = f"(tool activity from {role.provider}:{role.model})"
    lines: list[str] = [header]
    used = len(header)
    for index, (tid, name, tinput) in enumerate(tool_calls):
        inp_str = json.dumps(tinput, default=str) if tinput is not None else "{}"
        if len(inp_str) > 400:
            inp_str = inp_str[:400] + "…"
        entry = [f"- {name} input={inp_str}"]
        if tid in tool_results:
            content, is_error = tool_results[tid]
            tag = "ERR" if is_error else "OK"
            snippet = content.replace("\n", " ")[:300]
            entry.append(f"    [{tag}] {snippet}")
        cost = sum(len(line) + 1 for line in entry)
        if used + cost > MAX_TOOL_DIGEST_CHARS:
            lines.append(f"… ({len(tool_calls) - index} more tool calls)")
            break
        lines.extend(entry)
        used += cost
    return "\n".join(lines)


def _role_extras(role_name: str | None) -> dict[str, Any]:
    if role_name != "planner":
        return {}
    # The planner must produce a plan artifact only; implementation belongs to
    # the coder and review belongs to the reviewer. Superpowers-style planning
    # still needs read/search access to inspect the repo before writing a plan.
    return {
        "claude_allowed_tools": ["Read", "Glob", "Grep", "LS"],
        "claude_disable_settings": True,
        "claude_disable_skills": True,
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
