"""AssistantMessageBuilder — one place that turns engine deltas into pi's
``AssistantMessageEvent`` stream and the final ``AssistantMessage``.

Both engines feed this so their ``message_update`` events are shaped
identically (``text_start`` → ``text_delta``… → ``text_end``,
``toolcall_start`` → ``toolcall_end``), which is what the RPC layer and the
React UI render.
"""

from __future__ import annotations

from typing import Any

from ..messages import AssistantMessage, StopReason, Usage, empty_usage, now_ms


class AssistantMessageBuilder:
    def __init__(self, *, api: str, provider: str, model: str) -> None:
        self.message: AssistantMessage = {
            "role": "assistant",
            "content": [],
            "api": api,
            "provider": provider,
            "model": model,
            "usage": empty_usage(),
            "stopReason": "pending",
            "timestamp": now_ms(),
        }
        self._open: tuple[str, int] | None = None  # (kind, contentIndex)

    # ── events ─────────────────────────────────────────────────────────

    def start_event(self) -> dict[str, Any]:
        return {"type": "start", "partial": self.message}

    def _open_block(self, kind: str) -> list[dict[str, Any]]:
        if self._open and self._open[0] == kind:
            return []
        events = self.close_block()
        block: dict[str, Any] = (
            {"type": "text", "text": ""}
            if kind == "text"
            else {
                "type": "thinking",
                "thinking": "",
            }
        )
        self.message["content"].append(block)  # type: ignore[arg-type]
        index = len(self.message["content"]) - 1
        self._open = (kind, index)
        events.append({"type": f"{kind}_start", "contentIndex": index, "partial": self.message})
        return events

    def close_block(self) -> list[dict[str, Any]]:
        if not self._open:
            return []
        kind, index = self._open
        self._open = None
        block = self.message["content"][index]
        content = block["text"] if kind == "text" else block["thinking"]  # type: ignore[typeddict-item]
        return [
            {
                "type": f"{kind}_end",
                "contentIndex": index,
                "content": content,
                "partial": self.message,
            }
        ]

    def text_delta(self, delta: str) -> list[dict[str, Any]]:
        if not delta:
            return []
        events = self._open_block("text")
        _kind, index = self._open  # type: ignore[misc]
        self.message["content"][index]["text"] += delta  # type: ignore[typeddict-item]
        events.append(
            {"type": "text_delta", "contentIndex": index, "delta": delta, "partial": self.message}
        )
        return events

    def thinking_delta(self, delta: str) -> list[dict[str, Any]]:
        if not delta:
            return []
        events = self._open_block("thinking")
        _kind, index = self._open  # type: ignore[misc]
        self.message["content"][index]["thinking"] += delta  # type: ignore[typeddict-item]
        events.append(
            {
                "type": "thinking_delta",
                "contentIndex": index,
                "delta": delta,
                "partial": self.message,
            }
        )
        return events

    def tool_call(self, call_id: str, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        events = self.close_block()
        call = {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}
        self.message["content"].append(call)  # type: ignore[arg-type]
        index = len(self.message["content"]) - 1
        events.append({"type": "toolcall_start", "contentIndex": index, "partial": self.message})
        events.append(
            {
                "type": "toolcall_end",
                "contentIndex": index,
                "toolCall": call,
                "partial": self.message,
            }
        )
        return events

    def finish(
        self,
        stop_reason: StopReason,
        usage: Usage | None = None,
        error_message: str | None = None,
    ) -> list[dict[str, Any]]:
        events = self.close_block()
        self.message["stopReason"] = stop_reason
        if usage is not None:
            self.message["usage"] = usage
        if error_message:
            self.message["errorMessage"] = error_message
        if stop_reason in ("aborted", "error"):
            events.append({"type": "error", "reason": stop_reason, "error": self.message})
        else:
            events.append({"type": "done", "reason": stop_reason, "message": self.message})
        return events

    # ── convenience ────────────────────────────────────────────────────

    @property
    def has_tool_calls(self) -> bool:
        return any(c.get("type") == "toolCall" for c in self.message["content"])

    def tool_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.message["content"] if c.get("type") == "toolCall"]


def usage_from_counts(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cost_total: float = 0.0,
) -> Usage:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
        "cost": {
            "input": 0.0,
            "output": 0.0,
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "total": cost_total,
        },
    }
