"""Engines — one per official agent binary, plus a scripted fake.

``create_engine(name, **options)`` consults extension-registered factories
first (pi: ``api.register_provider``), then the built-ins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import (
    Engine,
    EngineCapabilities,
    EngineConfig,
    EngineError,
    EngineEvent,
    EngineHooks,
    NotSupportedError,
    ToolExecutor,
)
from .fake import FakeEngine
from .stream import AssistantMessageBuilder, usage_from_counts

BUILTIN_ENGINES: dict[str, Callable[..., Engine]] = {"fake": FakeEngine}


def _lazy_builtins() -> dict[str, Callable[..., Engine]]:
    # Vendor SDK imports are deferred so a missing `claude-agent-sdk` never
    # breaks the fake/test path.
    engines: dict[str, Callable[..., Engine]] = dict(BUILTIN_ENGINES)
    try:
        from .claude import ClaudeEngine

        engines["claude"] = ClaudeEngine
    except ImportError:
        pass
    try:
        from .codex import CodexEngine

        engines["codex"] = CodexEngine
    except ImportError:
        pass
    return engines


def create_engine(
    name: str, *, factories: dict[str, Callable[..., Engine]] | None = None, **options: Any
) -> Engine:
    registry = {**_lazy_builtins(), **(factories or {})}
    try:
        factory = registry[name]
    except KeyError as exc:
        raise EngineError(f"unknown engine {name!r}; known: {sorted(registry)}") from exc
    return factory(**options)


__all__ = [
    "AssistantMessageBuilder",
    "BUILTIN_ENGINES",
    "Engine",
    "EngineCapabilities",
    "EngineConfig",
    "EngineError",
    "EngineEvent",
    "EngineHooks",
    "FakeEngine",
    "NotSupportedError",
    "ToolExecutor",
    "create_engine",
    "usage_from_counts",
]
