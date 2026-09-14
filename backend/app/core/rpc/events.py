"""``to_json_event`` — pi's ``modes/json-event.ts``.

``message_update`` events carry the full partial message in memory; on the
wire they are thinned to ``{type, usage, assistantMessageEvent}`` with the
``partial`` snapshot removed, and ``toolcall_start`` gains ``id`` and
``toolName`` so a client can open a tool card before the arguments finish
streaming.
"""

from __future__ import annotations

from typing import Any


def to_json_event(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") != "message_update":
        return event
    message = event.get("message") or {}
    ame = dict(event.get("assistantMessageEvent") or {})
    partial = ame.pop("partial", None)
    if ame.get("type") == "toolcall_start":
        source = partial or message
        content = source.get("content") or []
        idx = ame.get("contentIndex", -1)
        if 0 <= idx < len(content) and content[idx].get("type") == "toolCall":
            ame["id"] = content[idx]["id"]
            ame["toolName"] = content[idx]["name"]
    if ame.get("type") in ("done", "error"):
        for key in ("message", "error"):
            if isinstance(ame.get(key), dict):
                ame[key] = {k: v for k, v in ame[key].items() if k != "content"} | {
                    "content": ame[key].get("content", [])
                }
    return {"type": "message_update", "usage": message.get("usage"), "assistantMessageEvent": ame}
