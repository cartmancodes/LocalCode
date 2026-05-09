from __future__ import annotations

import asyncio
from typing import Literal

from .base import Provider
from .claude import ClaudeProvider
from .fleet import FleetProvider
from .opencode import OpenCodeProvider


_singletons: dict[str, Provider] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the lock so it binds to the running event loop. (A module-
    level Lock created at import time can latch to the wrong loop in tests.)"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _build_provider(name: Literal["claude", "opencode", "fleet"]) -> Provider:
    if name == "claude":
        return ClaudeProvider()
    if name == "opencode":
        return OpenCodeProvider()
    if name == "fleet":
        return FleetProvider()
    raise ValueError(f"Unknown provider: {name}")  # pragma: no cover


async def get_provider(name: Literal["claude", "opencode", "fleet"]) -> Provider:
    """Return the singleton provider, building it on first call.

    Async + lock-guarded to prevent two concurrent first-callers from each
    constructing a provider (and leaking the loser's resources — e.g.
    OpenCodeProvider's httpx client). Cheap fast-path: most calls just hit
    the dict.
    """
    inst = _singletons.get(name)
    if inst is not None:
        return inst
    async with _get_lock():
        inst = _singletons.get(name)
        if inst is None:
            inst = _build_provider(name)
            _singletons[name] = inst
        return inst


async def warm_up() -> None:
    """Eagerly construct every provider at app startup. Avoids first-call
    latency and surfaces config errors during boot rather than mid-WS-turn.
    """
    for name in ("claude", "opencode", "fleet"):
        await get_provider(name)  # type: ignore[arg-type]


async def shutdown_all() -> None:
    for p in list(_singletons.values()):
        try:
            await p.aclose()
        except Exception:
            pass
    _singletons.clear()
