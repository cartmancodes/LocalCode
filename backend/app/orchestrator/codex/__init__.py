"""The Codex provider: `codex app-server` JSON-RPC over stdio.

Split four ways so a schema bump touches one file. ``protocol.py`` holds every
wire name, ``jsonrpc.py`` the transport (including the bidirectional request
handling the approval callbacks need), ``client.py`` the process and the turn
stream, and ``provider.py`` the translation into LocalCode Events plus the
bridge into the shared approval gate.
"""
from __future__ import annotations

from .client import CodexAppServer, CodexBroker, CodexUnavailable
from .jsonrpc import JsonRpcError, StdioJsonRpc
from .provider import CodexProvider

__all__ = [
    "CodexAppServer",
    "CodexBroker",
    "CodexProvider",
    "CodexUnavailable",
    "JsonRpcError",
    "StdioJsonRpc",
]
