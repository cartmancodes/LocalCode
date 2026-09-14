"""The extension API — pi's ``core/extensions`` in Python.

An extension is a Python module exposing ``def setup(api)`` (sync or async).
It registers handlers with ``api.on(event, handler)``, tools with
``api.register_tool(...)`` and slash commands with ``api.register_command(...)``.
"""

from __future__ import annotations

from .api import ExtensionAPI
from .loader import LoadExtensionsResult, discover_extension_paths, load_extension, load_extensions
from .runner import ExtensionRunner, LoadedExtension, SimpleExtensionContext
from .types import (
    SUPPORTED_EVENTS,
    UNSUPPORTED_EVENTS,
    ExtensionError,
    ExtensionUIContext,
    NoUIBridge,
    RegisteredCommand,
    ToolDefinition,
    UIBridge,
)

__all__ = [
    "SUPPORTED_EVENTS",
    "UNSUPPORTED_EVENTS",
    "ExtensionAPI",
    "ExtensionError",
    "ExtensionRunner",
    "ExtensionUIContext",
    "LoadExtensionsResult",
    "LoadedExtension",
    "NoUIBridge",
    "RegisteredCommand",
    "SimpleExtensionContext",
    "ToolDefinition",
    "UIBridge",
    "discover_extension_paths",
    "load_extension",
    "load_extensions",
]
