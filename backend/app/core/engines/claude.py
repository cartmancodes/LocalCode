"""ClaudeEngine — the official ``claude`` CLI through ``claude-agent-sdk``.

One persistent :class:`ClaudeSDKClient` per engine (streaming-input mode),
so a session keeps its prompt cache, can be interrupted, and can change
model mid-way. The CLI reads its own OAuth token from ``~/.claude``; this
module never sees it.

Compile-down of extension hooks (see ``base.py``):

    tool_call        → PreToolUse hook (deny / updatedInput) — always fires
    permission ask   → can_use_tool (only when the CLI would prompt)
    tool_result      → PostToolUse hook (additionalContext only; the SDK
                       cannot rewrite built-in tool output freely)
    before_agent_start → UserPromptSubmit hook (additionalContext)
    agent_end        → Stop hook
    compaction       → PreCompact hook → ``compaction`` engine event

Not available through the Python SDK as callbacks: ``SessionStart`` /
``SessionEnd`` (TypeScript only); the session emits its own.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk import (
    tool as sdk_tool,
)

from ..extensions.types import ToolDefinition
from ..messages import now_ms
from .base import (
    EngineCapabilities,
    EngineConfig,
    EngineEvent,
    EngineHooks,
    NotSupportedError,
    ToolExecutor,
)
from .stream import AssistantMessageBuilder, usage_from_counts

try:  # newer SDKs
    from claude_agent_sdk import RateLimitEvent
except ImportError:  # pragma: no cover
    RateLimitEvent = None  # type: ignore[assignment,misc]

ClientFactory = Callable[[ClaudeAgentOptions], Any]

# pi thinking level → SDK effort / thinking config
_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
_NO_APPROVAL_MSG = (
    "no approval handler: install an approval extension (ctx.ui.confirm on tool_call) "
    "or start the engine with permission_mode='acceptEdits'"
)


class ClaudeEngine:
    name = "claude"

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        api_label: str = "anthropic-messages",
    ) -> None:
        self._client_factory = client_factory or ClaudeSDKClient
        self._api_label = api_label
        self._config: EngineConfig | None = None
        self._client: Any = None
        self._session_id: str | None = None
        self._model: str | None = None
        self._thinking_level = "off"
        self._hooks = EngineHooks()
        self._side: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self._tools: list[ToolDefinition] = []
        self._executor: ToolExecutor | None = None
        self._needs_reconnect = False
        self._fork_next = False
        self._running = False

    # ── protocol properties ────────────────────────────────────────────

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            steer=False,
            follow_up=True,
            fork=True,
            compact=True,
            tool_input_rewrite=True,
            custom_tools=True,
            thinking_levels=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._model

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self, config: EngineConfig) -> None:
        self._config = config
        self._model = config.model
        self._thinking_level = config.thinking_level or "off"
        self._session_id = config.session_id
        self._needs_reconnect = True

    def _options(self) -> ClaudeAgentOptions:
        assert self._config is not None
        cfg = self._config
        system_prompt: Any = None
        if cfg.system_prompt:
            system_prompt = cfg.system_prompt
        elif cfg.append_system_prompt:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": cfg.append_system_prompt,
            }
        kwargs: dict[str, Any] = {
            "model": self._model,
            "cwd": cfg.cwd,
            "add_dirs": list(cfg.add_dirs),
            "env": dict(cfg.env),
            "system_prompt": system_prompt,
            "permission_mode": cfg.permission_mode or "default",
            "can_use_tool": self._can_use_tool,
            "hooks": {
                "PreToolUse": [HookMatcher(hooks=[self._pre_tool_use])],
                "PostToolUse": [HookMatcher(hooks=[self._post_tool_use])],
                "UserPromptSubmit": [HookMatcher(hooks=[self._user_prompt_submit])],
                "Stop": [HookMatcher(hooks=[self._stop])],
                "PreCompact": [HookMatcher(hooks=[self._pre_compact])],
            },
            "include_partial_messages": True,
        }
        if self._session_id:
            kwargs["resume"] = self._session_id
            if self._fork_next:
                kwargs["fork_session"] = True
        level = self._thinking_level
        if level == "off":
            kwargs["thinking"] = {"type": "disabled"}
        elif level in _EFFORT:
            kwargs["effort"] = _EFFORT[level]
        if self._tools:
            kwargs["mcp_servers"] = {"localcode": self._mcp_server()}
            kwargs["allowed_tools"] = [f"mcp__localcode__{t.name}" for t in self._tools]
        kwargs.update(cfg.extra.get("claude_options", {}))
        return ClaudeAgentOptions(**kwargs)

    def _mcp_server(self) -> Any:
        sdk_tools = []
        for definition in self._tools:
            sdk_tools.append(self._wrap_tool(definition))
        return create_sdk_mcp_server(name="localcode", version="1.0.0", tools=sdk_tools)

    def _wrap_tool(self, definition: ToolDefinition) -> Any:
        executor = self._executor

        @sdk_tool(definition.name, definition.description, definition.parameters)
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            if executor is None:
                return {
                    "content": [{"type": "text", "text": "tool executor missing"}],
                    "is_error": True,
                }
            result = await executor(definition.name, f"mcp-{now_ms()}", dict(args))
            out: dict[str, Any] = {"content": list(result.get("content", []))}
            if result.get("isError"):
                out["is_error"] = True
            return out

        return handler

    async def _ensure_client(self) -> None:
        if self._client is not None and not self._needs_reconnect:
            return
        # Our own extension tools are auto-approved by being in `allowed_tools`,
        # which the SDK warns about because it shadows `can_use_tool`. That is
        # the intent: we mounted those tools, so prompting for them would be
        # asking the user to approve our own plumbing. Extensions that *do*
        # want to gate them use the `tool_call` hook, which compiles to
        # PreToolUse and still fires.
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self._client = self._client_factory(self._options())
        await self._client.connect()
        self._needs_reconnect = False
        self._fork_next = False
        info = None
        try:
            info = await self._client.get_server_info()
        except Exception:  # noqa: BLE001 — optional
            info = None
        if isinstance(info, dict):
            sid = info.get("session_id") or info.get("sessionId")
            if sid:
                self._set_session_id(str(sid))

    def _set_session_id(self, sid: str) -> None:
        if sid != self._session_id:
            self._session_id = sid
            self._side.put_nowait({"type": "engine_session", "engine": self.name, "sessionId": sid})

    # ── hooks (SDK-facing) ─────────────────────────────────────────────

    async def _pre_tool_use(
        self, input_data: Any, tool_use_id: str | None, _ctx: Any
    ) -> dict[str, Any]:
        if self._hooks.before_tool is None:
            return {}
        name = input_data.get("tool_name", "")
        args = dict(input_data.get("tool_input") or {})
        decision = await self._hooks.before_tool(name, tool_use_id or "", args)
        out: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if decision and decision.get("block"):
            out["permissionDecision"] = "deny"
            out["permissionDecisionReason"] = decision.get("reason") or "blocked by extension"
        elif decision and decision.get("updated_input") is not None:
            out["permissionDecision"] = "allow"
            out["updatedInput"] = decision["updated_input"]
        else:
            return {}
        return {"hookSpecificOutput": out}

    async def _post_tool_use(
        self, input_data: Any, tool_use_id: str | None, _ctx: Any
    ) -> dict[str, Any]:
        if self._hooks.after_tool is None:
            return {}
        name = input_data.get("tool_name", "")
        args = dict(input_data.get("tool_input") or {})
        response = input_data.get("tool_response")
        content = [{"type": "text", "text": _stringify(response)}]
        override = await self._hooks.after_tool(name, tool_use_id or "", args, content, False)
        if override and override.get("content"):
            text = "".join(
                p.get("text", "") for p in override["content"] if p.get("type") == "text"
            )
            if text:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": text,
                    }
                }
        return {}

    async def _user_prompt_submit(
        self, input_data: Any, _id: str | None, _ctx: Any
    ) -> dict[str, Any]:
        if self._hooks.before_prompt is None:
            return {}
        result = await self._hooks.before_prompt(str(input_data.get("prompt", "")))
        if result and result.get("additional_context"):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": result["additional_context"],
                }
            }
        return {}

    async def _stop(self, _input: Any, _id: str | None, _ctx: Any) -> dict[str, Any]:
        if self._hooks.on_stop is not None:
            await self._hooks.on_stop()
        return {}

    async def _pre_compact(self, input_data: Any, _id: str | None, _ctx: Any) -> dict[str, Any]:
        self._side.put_nowait(
            {
                "type": "compaction",
                "summary": None,
                "tokensBefore": None,
                "trigger": input_data.get("trigger"),
            }
        )
        return {}

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        if self._hooks.permission_request is None:
            return PermissionResultDeny(message=_NO_APPROVAL_MSG, interrupt=False)
        ctx_info = {
            "tool_use_id": getattr(context, "tool_use_id", None),
            "title": getattr(context, "title", None),
            "description": getattr(context, "description", None),
            "decision_reason": getattr(context, "decision_reason", None),
        }
        decision = await self._hooks.permission_request(tool_name, dict(tool_input), ctx_info)
        if decision and decision.get("allow"):
            return PermissionResultAllow(updated_input=decision.get("updated_input"))
        return PermissionResultDeny(
            message=(decision or {}).get("message") or "denied",
            interrupt=bool((decision or {}).get("interrupt")),
        )

    # ── the run ────────────────────────────────────────────────────────

    async def prompt(
        self, message: dict[str, Any], hooks: EngineHooks
    ) -> AsyncIterator[EngineEvent]:
        self._hooks = hooks
        await self._ensure_client()
        assert self._client is not None
        while not self._side.empty():
            yield self._side.get_nowait()
        await self._client.query(_sdk_user_message(message))
        self._running = True
        yield {"type": "agent_start"}

        produced: list[dict[str, Any]] = []
        builder: AssistantMessageBuilder | None = None
        pending: dict[str, tuple[str, dict[str, Any]]] = {}
        tool_results: list[dict[str, Any]] = []
        turn_open = False
        aborted = False
        errored: str | None = None

        def new_builder() -> AssistantMessageBuilder:
            return AssistantMessageBuilder(
                api=self._api_label, provider="anthropic", model=self._model or ""
            )

        try:
            async for msg in self._client.receive_response():
                while not self._side.empty():
                    yield self._side.get_nowait()

                if isinstance(msg, StreamEvent):
                    if getattr(msg, "parent_tool_use_id", None):
                        continue  # subagent traffic stays out of the main stream
                    ev = msg.event or {}
                    kind = ev.get("type")
                    if kind == "message_start":
                        if turn_open and builder is not None and not pending:
                            yield {
                                "type": "turn_end",
                                "message": builder.message,
                                "toolResults": tool_results,
                            }
                            tool_results = []
                            turn_open = False
                        builder = new_builder()
                        turn_open = True
                        yield {"type": "turn_start"}
                        yield {"type": "message_start", "message": builder.message}
                    elif kind == "content_block_delta" and builder is not None:
                        delta = ev.get("delta") or {}
                        events: list[dict[str, Any]] = []
                        if delta.get("type") == "text_delta":
                            events = builder.text_delta(delta.get("text", ""))
                        elif delta.get("type") == "thinking_delta":
                            events = builder.thinking_delta(delta.get("thinking", ""))
                        for e in events:
                            yield {
                                "type": "message_update",
                                "message": builder.message,
                                "assistantMessageEvent": e,
                            }
                    continue

                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    if builder is None:
                        builder = new_builder()
                        turn_open = True
                        yield {"type": "turn_start"}
                        yield {"type": "message_start", "message": builder.message}
                    # Reconcile with the consolidated message: text was streamed;
                    # tool calls (with fully-formed input) come from here.
                    streamed_text = "".join(
                        c.get("text", "")
                        for c in builder.message["content"]
                        if c.get("type") == "text"
                    )
                    final_text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                    if final_text and not streamed_text:
                        for e in builder.text_delta(final_text):
                            yield {
                                "type": "message_update",
                                "message": builder.message,
                                "assistantMessageEvent": e,
                            }
                    for block in msg.content:
                        if isinstance(block, ThinkingBlock) and not any(
                            c.get("type") == "thinking" for c in builder.message["content"]
                        ):
                            for e in builder.thinking_delta(block.thinking):
                                yield {
                                    "type": "message_update",
                                    "message": builder.message,
                                    "assistantMessageEvent": e,
                                }
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            for e in builder.tool_call(block.id, block.name, dict(block.input)):
                                yield {
                                    "type": "message_update",
                                    "message": builder.message,
                                    "assistantMessageEvent": e,
                                }
                            pending[block.id] = (block.name, dict(block.input))
                    if msg.error:
                        errored = str(msg.error)
                    usage = _usage_from_sdk(msg.usage)
                    stop = "toolUse" if pending else ("error" if msg.error else "stop")
                    for e in builder.finish(stop, usage, errored if stop == "error" else None):
                        yield {
                            "type": "message_update",
                            "message": builder.message,
                            "assistantMessageEvent": e,
                        }
                    yield {"type": "message_end", "message": builder.message}
                    produced.append(builder.message)
                    for call_id, (name, args) in pending.items():
                        yield {
                            "type": "tool_execution_start",
                            "toolCallId": call_id,
                            "toolName": name,
                            "args": args,
                        }
                    if not pending:
                        yield {"type": "turn_end", "message": builder.message, "toolResults": []}
                        turn_open = False
                    continue

                if isinstance(msg, UserMessage):
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    content = msg.content if isinstance(msg.content, list) else []
                    for block in content:
                        if not isinstance(block, ToolResultBlock):
                            continue
                        name, args = pending.pop(block.tool_use_id, ("", {}))
                        text = _stringify(block.content)
                        result_content = [{"type": "text", "text": text}]
                        is_error = bool(block.is_error)
                        yield {
                            "type": "tool_execution_end",
                            "toolCallId": block.tool_use_id,
                            "toolName": name,
                            "result": {"content": result_content, "details": {}},
                            "isError": is_error,
                        }
                        tr = {
                            "role": "toolResult",
                            "toolCallId": block.tool_use_id,
                            "toolName": name,
                            "content": result_content,
                            "isError": is_error,
                            "timestamp": now_ms(),
                        }
                        tool_results.append(tr)
                        produced.append(tr)
                    if turn_open and builder is not None and not pending and tool_results:
                        yield {
                            "type": "turn_end",
                            "message": builder.message,
                            "toolResults": tool_results,
                        }
                        tool_results = []
                        turn_open = False
                    continue

                if isinstance(msg, SystemMessage):
                    data = msg.data or {}
                    if data.get("session_id"):
                        self._set_session_id(str(data["session_id"]))
                        while not self._side.empty():
                            yield self._side.get_nowait()
                    if msg.subtype == "compact_boundary":
                        yield {
                            "type": "compaction",
                            "summary": None,
                            "tokensBefore": data.get("pre_tokens"),
                        }
                    continue

                if RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
                    info = msg.rate_limit_info
                    yield {
                        "type": "rate_limit",
                        "info": {
                            "status": getattr(info, "status", None),
                            "resetsAt": getattr(info, "resets_at", None),
                            "type": getattr(info, "rate_limit_type", None),
                            "utilization": getattr(info, "utilization", None),
                        },
                    }
                    continue

                if isinstance(msg, ResultMessage):
                    if msg.session_id:
                        self._set_session_id(msg.session_id)
                        while not self._side.empty():
                            yield self._side.get_nowait()
                    if msg.terminal_reason in ("aborted_streaming", "aborted_tools"):
                        aborted = True
                    if msg.is_error:
                        errored = msg.result or (msg.errors[0] if msg.errors else msg.subtype)
                        yield {"type": "error", "message": errored, "willRetry": False}
                    if turn_open and builder is not None:
                        if builder.message["stopReason"] == "pending":
                            for e in builder.finish("aborted" if aborted else "stop"):
                                yield {
                                    "type": "message_update",
                                    "message": builder.message,
                                    "assistantMessageEvent": e,
                                }
                            yield {"type": "message_end", "message": builder.message}
                            produced.append(builder.message)
                        yield {
                            "type": "turn_end",
                            "message": builder.message,
                            "toolResults": tool_results,
                        }
                        tool_results = []
                        turn_open = False
                    break
        finally:
            self._running = False
        yield {"type": "agent_end", "messages": produced, "aborted": aborted}

    # ── controls ───────────────────────────────────────────────────────

    async def follow_up(self, message: dict[str, Any]) -> None:
        # The session owns the follow-up queue and calls prompt() when idle.
        return None

    async def steer(self, message: dict[str, Any]) -> bool:
        return False  # the session falls back to interrupt + re-prompt

    async def interrupt(self) -> None:
        if self._client is not None and self._running:
            await self._client.interrupt()

    async def fork(self) -> str:
        if not self._session_id:
            raise NotSupportedError("nothing to fork yet: the engine has no session id")
        self._fork_next = True
        self._needs_reconnect = True
        await self._ensure_client()
        if not self._session_id:
            raise NotSupportedError("fork did not yield a session id")
        return self._session_id

    async def set_model(self, model: str) -> None:
        self._model = model
        if self._client is not None and not self._needs_reconnect:
            await self._client.set_model(model)

    async def set_thinking_level(self, level: str) -> None:
        if level != self._thinking_level:
            self._thinking_level = level
            self._needs_reconnect = True  # effort is a connect-time option

    async def compact(self, custom_instructions: str | None = None) -> None:
        await self._ensure_client()
        assert self._client is not None
        text = "/compact" + (f" {custom_instructions}" if custom_instructions else "")
        await self._client.query(text)
        async for msg in self._client.receive_response():
            if isinstance(msg, ResultMessage):
                break

    async def mount_tools(self, tools: list[ToolDefinition], executor: ToolExecutor) -> None:
        self._tools = list(tools)
        self._executor = executor
        self._needs_reconnect = True

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None


# ── helpers ────────────────────────────────────────────────────────────────


def _sdk_user_message(message: dict[str, Any]) -> dict[str, Any] | str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.get("mimeType", "image/png"),
                        "data": part.get("data", ""),
                    },
                }
            )
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return blocks[0]["text"]
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("stdout", "output", "content", "text"):
            if key in value:
                return _stringify(value[key])
        return str(value)
    return str(value)


def _usage_from_sdk(usage: dict[str, Any] | None) -> Any:
    if not usage:
        return usage_from_counts()
    return usage_from_counts(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
        cache_write=int(usage.get("cache_creation_input_tokens", 0) or 0),
    )
