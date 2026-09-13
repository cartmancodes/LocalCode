"""Turn-level token usage: the thing that makes the prompt-cache win visible.

Task 4 made the Claude provider hold one live client per session so the
server-side prompt cache stays warm across turns instead of going cold on
every message. Nothing about that is observable without this module —
``cache_hit_rate`` is the number that proves (or disproves) the cache is
doing anything, and Task 11's quota governor needs the raw token counts to
decide when a session is burning budget too fast.

``parse_claude_usage`` reads ``ResultMessage.usage`` defensively: the SDK's
usage dict shape has drifted between snake_case (the Python SDK's own
convention) and the Anthropic API's camelCase before, and a turn simply
having no usage at all (``usage=None``) must not raise — a turn's tokens
missing from the log is a lesser failure than a turn's ``assistant.done``
event never reaching the UI because usage-parsing blew up.

``UsageLog`` appends one JSON line per turn to ``~/.localcode/usage.jsonl``
(default path resolved lazily — see ``default_usage_log_path`` — for the same
"not at import time" reason as ``artifacts.default_artifact_root``). It
rotates at 8 MB rather than growing forever, and a truncated last line (the
process was killed mid-``write``) is skipped on read rather than raising:
losing one in-flight entry is fine, losing the ability to read the rest of
the log is not.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import get_settings

# Rotate before the log becomes an unpleasant thing to read or ship.
_MAX_LOG_BYTES = 8 * 1024 * 1024


def default_usage_log_path() -> Path:
    """``~/.localcode/usage.jsonl``, resolved when a ``UsageLog`` is actually
    constructed — never at import time. See the module docstring."""
    return Path.home() / ".localcode" / "usage.jsonl"


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token accounting, as logged and as reported over the API."""

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None
    session_id: str | None
    ts: float


def _pick_int(usage: Any, *keys: str) -> int:
    """First present key among ``keys``, coerced to ``int``; 0 if none present
    or the value can't be coerced. Covers both the SDK's snake_case and the
    Anthropic API's camelCase spellings without caring which one showed up.

    ``usage`` is typed ``Any``, not ``dict``, on purpose: a malformed SDK
    response can hand back a list, a string, or any other truthy value in
    ``ResultMessage.usage``, and even something that looks dict-shaped can
    have a ``.get`` that itself raises. Every path here is guarded so a
    usage-parsing exception can never take down the ``assistant.done``
    event it is attached to (see ``parse_claude_usage``'s docstring).
    """
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        try:
            value = usage.get(key)
        except Exception:
            return 0
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def parse_claude_usage(
    result_message: Any,
    *,
    provider: str,
    model: str,
    session_id: str | None,
) -> TurnUsage:
    """Build a :class:`TurnUsage` from a claude-agent-sdk ``ResultMessage``.

    Entirely defensive: ``result_message.usage`` may be ``None`` (an errored
    or very short turn), a snake_case dict, a camelCase one, or — a malformed
    SDK response — a list, a string, a number, or a dict-like object whose
    own ``.get`` raises. None of those may raise here: this sits inside the
    per-turn loop in ``ClaudeProvider.run`` with no local guard, so an
    exception escaping this function is caught by that loop's outer
    ``except Exception`` and turns a *successful* turn into an ``error``
    event, discarding the persistent client and dropping that turn's
    ``assistant.done`` entirely. Missing or malformed keys become 0 instead.
    """
    try:
        raw_usage = getattr(result_message, "usage", None)
    except Exception:
        # A property that RAISES is not a missing attribute, and ``getattr``'s
        # default only covers the second: it swallows ``AttributeError`` and
        # lets everything else through. A ``ResultMessage`` whose ``usage`` is
        # computed lazily (or proxied over a transport that has since closed)
        # therefore took the whole turn down from here — the successful turn
        # became an ``error`` event, its ``assistant.done`` was never emitted
        # and the persistent client was discarded. Usage is telemetry; a turn
        # is the product. Degrade to zeros, exactly as every other malformed
        # shape below does.
        raw_usage = None
    # ``_pick_int`` already treats a non-dict (or a dict whose ``.get``
    # raises) as "no keys present", but the shape check is repeated here so
    # this function's own contract — "never raises, whatever ``usage`` is"
    # — does not silently depend on every future caller of ``_pick_int``
    # keeping that behavior.
    usage: Any = raw_usage if isinstance(raw_usage, dict) else {}
    try:
        input_tokens = _pick_int(usage, "input_tokens", "inputTokens")
        output_tokens = _pick_int(usage, "output_tokens", "outputTokens")
        cache_read_tokens = _pick_int(
            usage,
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cache_read_tokens",
            "cacheReadTokens",
        )
        cache_creation_tokens = _pick_int(
            usage,
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
            "cache_creation_tokens",
            "cacheCreationTokens",
        )
    except Exception:
        # Belt-and-suspenders: even if a future edit to _pick_int reopens a
        # crash path, an unexpected usage shape degrades to zeros here
        # rather than taking the turn's assistant.done event down with it.
        input_tokens = output_tokens = cache_read_tokens = cache_creation_tokens = 0
    return TurnUsage(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_usd=getattr(result_message, "total_cost_usd", None),
        session_id=session_id,
        ts=time.time(),
    )


class UsageLog:
    """Append-only JSONL of :class:`TurnUsage`, one line per turn."""

    def __init__(self, path: Path | None = None) -> None:
        # Same "None is the only safe default-time value" reasoning as
        # ArtifactStore: the real Path.home() lookup happens here, at
        # construction, not when this module is imported.
        self.path = path if path is not None else default_usage_log_path()

    def append(self, usage: TurnUsage) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        line = json.dumps(asdict(usage), sort_keys=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size > _MAX_LOG_BYTES:
            rotated = self.path.with_name(self.path.name + ".1")
            os.replace(self.path, rotated)  # atomic on POSIX; drops any old .1

    def recent(self, window_s: float) -> list[TurnUsage]:
        """Entries with ``ts`` within ``window_s`` seconds of now.

        A truncated final line — the process died mid-``write`` — is skipped
        rather than raising: the rest of the log is still good.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        cutoff = time.time() - window_s
        out: list[TurnUsage] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("ts", 0.0) >= cutoff:
                out.append(TurnUsage(**data))
        return out


def usage_log_from_settings() -> UsageLog:
    """The one ``UsageLog`` every writer and reader in this process should
    build, resolved from ``Settings.usage_log_path`` rather than each call
    site reaching for ``Path.home()`` (or ``UsageLog``'s bare default)
    independently.

    Both ``ClaudeProvider`` (the writer) and ``GET /api/system/usage`` (the
    reader) call this. If they resolved the path separately — the provider
    honouring ``usage_log_path`` and the endpoint not, say — setting
    ``USAGE_LOG_PATH`` would silently split them onto two different files:
    the provider keeps logging turns nobody ever reads back, and the
    endpoint this module exists to support reports zero usage forever.
    Routing both through one function makes that divergence structurally
    impossible rather than a matter of remembering to keep two call sites
    in sync.
    """
    override = get_settings().usage_log_path
    path = Path(override).expanduser().resolve() if override else None
    return UsageLog(path)


def cache_hit_rate(entries: list[TurnUsage]) -> float:
    """Fraction of (cache_read + input) tokens that were served from cache.

    ``0.0`` when the denominator is 0 rather than raising — an empty or
    all-zero window is not an error, it is "nothing to report yet".
    """
    cache_read = sum(e.cache_read_tokens for e in entries)
    input_tokens = sum(e.input_tokens for e in entries)
    denom = cache_read + input_tokens
    return cache_read / denom if denom else 0.0


def uncached_share(entries: list[TurnUsage]) -> float:
    """The complement of :func:`cache_hit_rate`: fraction paid at full price."""
    cache_read = sum(e.cache_read_tokens for e in entries)
    input_tokens = sum(e.input_tokens for e in entries)
    denom = cache_read + input_tokens
    return input_tokens / denom if denom else 0.0
