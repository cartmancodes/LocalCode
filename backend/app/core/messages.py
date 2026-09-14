"""Message shapes stored inside session entries.

These are pi-ai's message types (``packages/ai/src/types.ts``) plus the
custom roles pi's coding agent adds (``core/messages.ts``). Keeping the
stored shape identical to pi's means the session files, the RPC ``get_messages``
payload and any future pi-compatible tooling all agree on what a message is.

Engines translate their native streams into these shapes; nothing in the
shell ever sees a vendor-specific message.
"""

from __future__ import annotations

import time
from typing import Any, Literal, NotRequired, TypedDict


class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class ThinkingContent(TypedDict):
    type: Literal["thinking"]
    thinking: str
    thinkingSignature: NotRequired[str]
    redacted: NotRequired[bool]


class ImageContent(TypedDict):
    type: Literal["image"]
    data: str  # base64
    mimeType: str


class ToolCall(TypedDict):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: dict[str, Any]


class UsageCost(TypedDict):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    total: float


class Usage(TypedDict):
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    totalTokens: int
    cost: UsageCost
    reasoning: NotRequired[int]


StopReason = Literal["pending", "stop", "length", "toolUse", "error", "aborted", "deferred"]


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str | list[TextContent | ImageContent]
    timestamp: int  # unix ms


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: list[TextContent | ThinkingContent | ToolCall]
    api: str
    provider: str
    model: str
    usage: Usage
    stopReason: StopReason
    timestamp: int
    errorMessage: NotRequired[str]
    responseId: NotRequired[str]


class ToolResultMessage(TypedDict):
    role: Literal["toolResult"]
    toolCallId: str
    toolName: str
    content: list[TextContent | ImageContent]
    isError: bool
    timestamp: int
    details: NotRequired[Any]
    usage: NotRequired[Usage]


class CustomMessage(TypedDict):
    role: Literal["custom"]
    customType: str
    content: str | list[TextContent | ImageContent]
    display: bool
    timestamp: int
    details: NotRequired[Any]


class CompactionSummaryMessage(TypedDict):
    role: Literal["compactionSummary"]
    summary: str
    tokensBefore: int
    timestamp: int


class BranchSummaryMessage(TypedDict):
    role: Literal["branchSummary"]
    summary: str
    fromId: str | None
    timestamp: int


class BashExecutionMessage(TypedDict):
    role: Literal["bashExecution"]
    command: str
    output: str
    exitCode: int | None
    cancelled: bool
    truncated: bool
    timestamp: int
    fullOutputPath: NotRequired[str]
    excludeFromContext: NotRequired[bool]


Message = UserMessage | AssistantMessage | ToolResultMessage
AgentMessage = (
    Message | CustomMessage | CompactionSummaryMessage | BranchSummaryMessage | BashExecutionMessage
)


def now_ms() -> int:
    return int(time.time() * 1000)


def empty_usage() -> Usage:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0},
    }


def user_message(text: str, images: list[ImageContent] | None = None) -> UserMessage:
    content: list[TextContent | ImageContent] = [{"type": "text", "text": text}]
    if images:
        content.extend(images)
    return {"role": "user", "content": content, "timestamp": now_ms()}


def custom_message(
    custom_type: str,
    content: str | list[TextContent | ImageContent],
    display: bool,
    details: Any = None,
    timestamp: int | None = None,
) -> CustomMessage:
    msg: CustomMessage = {
        "role": "custom",
        "customType": custom_type,
        "content": content,
        "display": display,
        "timestamp": timestamp if timestamp is not None else now_ms(),
    }
    if details is not None:
        msg["details"] = details
    return msg


def compaction_summary_message(
    summary: str, tokens_before: int, timestamp: int | None = None
) -> CompactionSummaryMessage:
    return {
        "role": "compactionSummary",
        "summary": summary,
        "tokensBefore": tokens_before,
        "timestamp": timestamp if timestamp is not None else now_ms(),
    }


def branch_summary_message(
    summary: str, from_id: str | None, timestamp: int | None = None
) -> BranchSummaryMessage:
    return {
        "role": "branchSummary",
        "summary": summary,
        "fromId": from_id,
        "timestamp": timestamp if timestamp is not None else now_ms(),
    }


def message_text(message: dict[str, Any]) -> str:
    """Concatenated text of a message's content, for search/preview."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if part.get("type") == "text")
    if message.get("role") in ("compactionSummary", "branchSummary"):
        return str(message.get("summary", ""))
    return ""
