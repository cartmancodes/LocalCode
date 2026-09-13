from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import quota
from ..config import get_settings
from ..usage import TurnUsage, cache_hit_rate, uncached_share, usage_log_from_settings

router = APIRouter(prefix="/api/system", tags=["system"])

# Fixed window for the headline numbers. /quota below is the governor's view;
# this endpoint stays a read-only snapshot of the raw turn log.
_USAGE_WINDOW_S = 3600


@router.get("/cwd")
async def get_system_cwd() -> dict[str, Any]:
    """Return the orchestrator process's current working directory plus the
    configured allowlist of valid roots. The UI uses this as the sensible
    default when the user hasn't explicitly chosen a project root."""
    s = get_settings()
    return {
        "cwd": str(Path.cwd().resolve()),
        "home": str(Path.home()),
        "allowed_roots": [str(r) for r in s.cwd_allowlist()],
        # When `allowed_roots` is empty the backend is permissive — any cwd
        # the user passes is accepted (single-user dev mode).
        "permissive": len(s.cwd_allowlist()) == 0,
    }


def _stats(entries: list[TurnUsage]) -> dict[str, Any]:
    return {
        "turns": len(entries),
        "cache_hit_rate": cache_hit_rate(entries),
        "uncached_share": uncached_share(entries),
        "input_tokens": sum(e.input_tokens for e in entries),
        "output_tokens": sum(e.output_tokens for e in entries),
        "cache_read_tokens": sum(e.cache_read_tokens for e in entries),
    }


@router.get("/usage")
async def get_system_usage() -> dict[str, Any]:
    """Cache hit rate and token counts over the last hour — the number that
    proves (or disproves) Task 4's persistent-client cache win is real.

    Built fresh per request via ``usage_log_from_settings`` rather than
    cached: the log is small to read, a cached ``UsageLog`` instance would
    resolve ``Path.home()`` once at whatever moment it was first built
    (which for a test can be before HOME is redirected), and resolving
    through the *same* function ``ClaudeProvider`` uses to build its
    writer-side log is what keeps this endpoint reading the file the
    provider actually wrote — see ``usage.usage_log_from_settings``.
    """
    # Reading the log is file I/O; keep it off the event loop like the rest
    # of the app's persistence (storage/sessions.py's asyncio.to_thread use).
    entries = await asyncio.to_thread(usage_log_from_settings().recent, _USAGE_WINDOW_S)
    by_provider: dict[str, list[TurnUsage]] = {}
    for entry in entries:
        by_provider.setdefault(entry.provider, []).append(entry)
    return {
        "window_s": _USAGE_WINDOW_S,
        **_stats(entries),
        "by_provider": {provider: _stats(es) for provider, es in by_provider.items()},
    }


@router.get("/quota")
async def get_system_quota() -> dict[str, Any]:
    """Remaining headroom per subscription — the number the top bar shows and
    the number ``provider: "auto"`` routes on.

    Read from the SAME ``get_governor()`` the recording sites write through, so
    the meter cannot drift onto a different ledger than the turns (the split
    ``usage_log_from_settings`` exists to prevent, one file over).

    ``queue_suggested`` is advice, not a queue: "every governed subscription is
    at or below the threshold, so hold this work rather than starting it".
    """
    governor = quota.get_governor()
    body = governor.to_dict()
    body["queue_suggested"] = governor.should_queue(list(quota.governed_providers()))
    return body
