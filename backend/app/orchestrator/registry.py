from __future__ import annotations

import asyncio
from typing import Literal

from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .fleet import FleetProvider

_singletons: dict[str, Provider] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the lock so it binds to the running event loop. (A module-
    level Lock created at import time can latch to the wrong loop in tests.)"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


ProviderName = Literal["claude", "codex", "fleet"]

# Every provider the registry can build. Named once so `_build_provider`,
# `get_provider` and `warm_up` cannot drift: a provider registered in the
# builder but missing from warm_up pays its construction cost mid-turn
# instead of at boot, and one missing from the builder is a 500 on the first
# request that names it.
PROVIDER_NAMES: tuple[str, ...] = ("claude", "codex", "fleet")


def _build_provider(name: ProviderName) -> Provider:
    if name == "claude":
        return ClaudeProvider()
    if name == "codex":
        return CodexProvider()
    if name == "fleet":
        return FleetProvider()
    raise ValueError(f"Unknown provider: {name}")  # pragma: no cover


async def get_provider(name: ProviderName) -> Provider:
    """Return the singleton provider, building it on first call.

    Async + lock-guarded to prevent two concurrent first-callers from each
    constructing a provider (and leaking the loser's resources — e.g.
    a provider's HTTP client). Cheap fast-path: most calls just hit
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
    for name in PROVIDER_NAMES:
        await get_provider(name)  # type: ignore[arg-type]


async def shutdown_all() -> None:
    for p in list(_singletons.values()):
        try:
            await p.aclose()
        except Exception:
            pass
    _singletons.clear()
