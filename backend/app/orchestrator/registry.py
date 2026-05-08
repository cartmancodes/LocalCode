from __future__ import annotations

from typing import Literal

from .base import Provider
from .claude import ClaudeProvider
from .fleet import FleetProvider
from .opencode import OpenCodeProvider


_singletons: dict[str, Provider] = {}


def get_provider(name: Literal["claude", "opencode", "fleet"]) -> Provider:
    if name not in _singletons:
        if name == "claude":
            _singletons[name] = ClaudeProvider()
        elif name == "opencode":
            _singletons[name] = OpenCodeProvider()
        elif name == "fleet":
            _singletons[name] = FleetProvider()
        else:  # pragma: no cover — Pydantic constrains this upstream
            raise ValueError(f"Unknown provider: {name}")
    return _singletons[name]


async def shutdown_all() -> None:
    for p in list(_singletons.values()):
        try:
            await p.aclose()
        except Exception:
            pass
    _singletons.clear()
