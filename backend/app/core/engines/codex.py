"""CodexEngine — the official ``codex`` CLI through its app-server.

The CLI holds the ChatGPT-subscription auth (``codex login``); this module
only speaks JSON-RPC to it. One app-server process per engine.

Projection of Codex's item stream onto pi's turn model: Codex runs tools
*before* the agent message that reports on them, so each tool item becomes a
``toolCall`` on the current assistant message plus a ``tool_execution_*``
pair, and an ``agentMessage`` completing closes the pi turn.

Compile-down of hooks:

    tool_call / permission ask → item/commandExecution/requestApproval,
                                 item/fileChange/requestApproval (accept / decline;
                                 Codex cannot rewrite tool input)
    ask_user                   → item/tool/requestUserInput
    custom tools               → item/tool/call (dynamic tools, experimental)
    steer / interrupt / fork / compact → turn/steer, turn/interrupt, thread/fork,
                                         thread/compact/start (all native)

``account/chatgptAuthTokens/refresh`` is answered with an error on purpose:
this client never holds tokens.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

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
from .codex_rpc import CodexAppServerClient, CodexRpcError
from .stream import AssistantMessageBuilder, usage_from_counts

ClientFactory = Callable[..., CodexAppServerClient]

_SANDBOX = {
    None: "workspace-write",
    "default": "workspace-write",
    "acceptEdits": "workspace-write",
    "plan": "read-only",
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "bypassPermissions": "danger-full-access",
    "danger-full-access": "danger-full-access",
}
_EFFORT = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}
_NO_APPROVAL_MSG = "no approval handler: install an approval extension or use a permissive sandbox"


class CodexEngine:
    name = "codex"

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        client_factory: ClientFactory | None = None,
        experimental_api: bool = False,
    ) -> None:
        self._command = command
        self._client_factory = client_factory or CodexAppServerClient
        self._experimental = experimental_api
        self._client: CodexAppServerClient | None = None
        self._config: EngineConfig | None = None
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._model: str | None = None
        self._thinking_level = "off"
        self._hooks = EngineHooks()
        self._tools: list[ToolDefinition] = []
        self._executor: ToolExecutor | None = None
        self._side: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self._running = False
        self._server_info: dict[str, Any] = {}

    # ── properties ─────────────────────────────────────────────────────

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            steer=True,
            follow_up=True,
            fork=True,
            compact=True,
            tool_input_rewrite=False,
            custom_tools=self._experimental,
            thinking_levels=("off", "minimal", "low", "medium", "high", "xhigh"),
        )

    @property
    def session_id(self) -> str | None:
        return self._thread_id

    @property
    def model(self) -> str | None:
        return self._model

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self, config: EngineConfig) -> None:
        self._config = config
        self._model = config.model
        self._thinking_level = config.thinking_level or "off"
        self._client = self._client_factory(
            self._command, cwd=config.cwd, on_server_request=self._on_server_request
        )
        await self._client.start()
        self._server_info = await self._client.initialize(experimental_api=self._experimental)
        params: dict[str, Any] = {
            "cwd": config.cwd,
            "sandbox": _SANDBOX.get(config.permission_mode, "workspace-write"),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        }
        if config.model:
            params["model"] = config.model
        if config.system_prompt:
            params["baseInstructions"] = config.system_prompt
        if config.append_system_prompt:
            params["developerInstructions"] = config.append_system_prompt
        if self._tools and self._experimental:
            params["dynamicTools"] = [_dynamic_tool(t) for t in self._tools]
        params.update(config.extra.get("codex_thread", {}))
        if config.session_id:
            params["threadId"] = config.session_id
            result = await self._client.request_with_retry("thread/resume", params)
        else:
            result = await self._client.request_with_retry("thread/start", params)
        thread = result.get("thread") or {}
        self._thread_id = str(thread.get("id") or config.session_id or "")
        if result.get("model"):
            self._model = result["model"]
        self._side.put_nowait(
            {"type": "engine_session", "engine": self.name, "sessionId": self._thread_id}
        )
        try:
            limits = await self._client.request("account/rateLimits/read", {}, timeout_s=10)
            if limits:
                self._side.put_nowait(
                    {
                        "type": "rate_limit",
                        "info": _rate_limit_info(limits.get("rateLimits") or limits),
                    }
                )
        except Exception:  # noqa: BLE001 — informational
            pass

    # ── server → client requests ───────────────────────────────────────

    async def _on_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "item/commandExecution/requestApproval":
            args = {
                "command": params.get("command"),
                "cwd": params.get("cwd"),
                "reason": params.get("reason"),
            }
            allowed = await self._approve("bash", params.get("itemId", ""), args)
            return {"decision": "accept" if allowed else "decline"}
        if method == "item/fileChange/requestApproval":
            args = {"grantRoot": params.get("grantRoot"), "reason": params.get("reason")}
            allowed = await self._approve("apply_patch", params.get("itemId", ""), args)
            return {"decision": "accept" if allowed else "decline"}
        if method == "item/permissions/requestApproval":
            args = {"permissions": params.get("permissions"), "reason": params.get("reason")}
            allowed = await self._approve("permissions", params.get("itemId", ""), args)
            return {"decision": "accept" if allowed else "decline"}
        if method in ("execCommandApproval", "applyPatchApproval"):  # legacy v1 shapes
            name = "bash" if method == "execCommandApproval" else "apply_patch"
            allowed = await self._approve(name, params.get("callId", ""), dict(params))
            return {"decision": "approved" if allowed else "abort"}
        if method == "item/tool/requestUserInput":
            if self._hooks.ask_user is None:
                return {"answers": {}}
            answers = await self._hooks.ask_user(list(params.get("questions") or []))
            return answers or {"answers": {}}
        if method == "item/tool/call":
            return await self._dynamic_tool_call(params)
        if method == "account/chatgptAuthTokens/refresh":
            raise NotSupportedError("this client never holds ChatGPT tokens; run `codex login`")
        raise NotSupportedError(f"unsupported server request {method}")

    async def _approve(self, tool_name: str, call_id: str, args: dict[str, Any]) -> bool:
        if self._hooks.before_tool is not None:
            decision = await self._hooks.before_tool(tool_name, call_id, args)
            if decision and decision.get("block"):
                return False
        if self._hooks.permission_request is None:
            self._side.put_nowait(
                {"type": "error", "message": _NO_APPROVAL_MSG, "willRetry": False}
            )
            return False
        result = await self._hooks.permission_request(tool_name, args, {"tool_use_id": call_id})
        return bool(result and result.get("allow"))

    async def _dynamic_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("tool") or params.get("name") or ""
        args = params.get("arguments") or {}
        call_id = params.get("callId") or params.get("itemId") or f"dyn-{now_ms()}"
        if self._executor is None:
            return {
                "contentItems": [{"type": "inputText", "text": "no tool executor"}],
                "success": False,
            }
        result = await self._executor(name, call_id, dict(args))
        text = "".join(
            p.get("text", "") for p in result.get("content", []) if p.get("type") == "text"
        )
        return {
            "contentItems": [{"type": "inputText", "text": text}],
            "success": not result.get("isError"),
        }

    # ── the run ────────────────────────────────────────────────────────

    async def prompt(
        self, message: dict[str, Any], hooks: EngineHooks
    ) -> AsyncIterator[EngineEvent]:
        self._hooks = hooks
        if self._client is None or not self._thread_id:
            raise RuntimeError("engine not started")
        while not self._side.empty():
            yield self._side.get_nowait()
        if hooks.before_prompt is not None:
            await hooks.before_prompt(_text_of(message))
        params: dict[str, Any] = {"threadId": self._thread_id, "input": _user_input(message)}
        if self._model:
            params["model"] = self._model
        effort = _EFFORT.get(self._thinking_level)
        if effort and effort != "none":
            params["effort"] = effort
        result = await self._client.request_with_retry("turn/start", params)
        self._turn_id = str((result.get("turn") or {}).get("id") or "")
        self._running = True
        yield {"type": "agent_start"}

        produced: list[dict[str, Any]] = []
        builder: AssistantMessageBuilder | None = None
        turn_open = False
        tool_results: list[dict[str, Any]] = []
        tool_items: dict[str, dict[str, Any]] = {}
        last_usage: dict[str, Any] | None = None
        aborted = False
        pending_close: list[EngineEvent] = []
        pending_message: dict[str, Any] | None = None
        # The real app-server can send a standalone "error" notification AND
        # turn/completed's own turn.error for the SAME failure, byte-identical
        # message (see codex_real_trace_2026-09-14.json). Only dedupe on that
        # exact match — never assume the two are always redundant in general,
        # since nothing in the protocol guarantees "error" is turn-scoped.
        last_error_message: str | None = None

        def open_turn() -> list[EngineEvent]:
            nonlocal builder, turn_open
            if turn_open and builder is not None:
                return []
            builder = AssistantMessageBuilder(
                api="openai-codex-responses", provider="openai", model=self._model or ""
            )
            turn_open = True
            return [{"type": "turn_start"}, {"type": "message_start", "message": builder.message}]

        def close_turn(stop: str) -> list[EngineEvent]:
            nonlocal builder, turn_open, tool_results
            if builder is None:
                return []
            out: list[EngineEvent] = []
            if builder.message["stopReason"] == "pending":
                usage = _usage(last_usage)
                for e in builder.finish(stop, usage):  # type: ignore[arg-type]
                    out.append(
                        {
                            "type": "message_update",
                            "message": builder.message,
                            "assistantMessageEvent": e,
                        }
                    )
                out.append({"type": "message_end", "message": builder.message})
                produced.append(builder.message)
            out.append(
                {"type": "turn_end", "message": builder.message, "toolResults": tool_results}
            )
            tool_results = []
            builder = None
            turn_open = False
            return out

        try:
            while True:
                note = await self._client.notifications.get()
                method, p = note.get("method"), note.get("params") or {}
                if method == "__closed__":
                    yield {
                        "type": "error",
                        "message": "codex app-server exited",
                        "willRetry": False,
                    }
                    break
                while not self._side.empty():
                    yield self._side.get_nowait()
                if p.get("threadId") not in (None, self._thread_id) and method not in (
                    "account/rateLimits/updated",
                ):
                    continue

                if method == "turn/started":
                    continue
                if method == "item/started":
                    item = p.get("item") or {}
                    kind = item.get("type")
                    if kind in ("agentMessage", "reasoning", "plan"):
                        for e in open_turn():
                            yield e
                    elif kind in (
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                        "dynamicToolCall",
                        "webSearch",
                    ):
                        for e in open_turn():
                            yield e
                        assert builder is not None
                        name, args = _tool_call_of(item)
                        tool_items[item["id"]] = {"name": name, "args": args}
                        for e in builder.tool_call(item["id"], name, args):
                            yield {
                                "type": "message_update",
                                "message": builder.message,
                                "assistantMessageEvent": e,
                            }
                        yield {
                            "type": "tool_execution_start",
                            "toolCallId": item["id"],
                            "toolName": name,
                            "args": args,
                        }
                    continue
                if method == "item/agentMessage/delta":
                    for e in open_turn():
                        yield e
                    assert builder is not None
                    for e in builder.text_delta(p.get("delta", "")):
                        yield {
                            "type": "message_update",
                            "message": builder.message,
                            "assistantMessageEvent": e,
                        }
                    continue
                if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
                    for e in open_turn():
                        yield e
                    assert builder is not None
                    for e in builder.thinking_delta(p.get("delta", "")):
                        yield {
                            "type": "message_update",
                            "message": builder.message,
                            "assistantMessageEvent": e,
                        }
                    continue
                if method == "item/commandExecution/outputDelta":
                    info = tool_items.get(p.get("itemId", ""), {})
                    yield {
                        "type": "tool_execution_update",
                        "toolCallId": p.get("itemId"),
                        "toolName": info.get("name", "bash"),
                        "args": info.get("args", {}),
                        "partialResult": {
                            "content": [{"type": "text", "text": p.get("delta", "")}]
                        },
                    }
                    continue
                if method == "item/completed":
                    item = p.get("item") or {}
                    kind = item.get("type")
                    if kind == "agentMessage":
                        for e in open_turn():
                            yield e
                        assert builder is not None
                        streamed = "".join(
                            c.get("text", "")
                            for c in builder.message["content"]
                            if c.get("type") == "text"
                        )
                        if item.get("text") and not streamed:
                            for e in builder.text_delta(item["text"]):
                                yield {
                                    "type": "message_update",
                                    "message": builder.message,
                                    "assistantMessageEvent": e,
                                }
                        pending_message = builder.message
                        pending_close = close_turn("stop")
                    elif kind == "reasoning" and builder is not None:
                        for e in builder.close_block():
                            yield {
                                "type": "message_update",
                                "message": builder.message,
                                "assistantMessageEvent": e,
                            }
                    elif item.get("id") in tool_items:
                        name = tool_items[item["id"]]["name"]
                        content, is_error = _tool_result_of(item)
                        yield {
                            "type": "tool_execution_end",
                            "toolCallId": item["id"],
                            "toolName": name,
                            "result": {"content": content, "details": {}},
                            "isError": is_error,
                        }
                        tr = {
                            "role": "toolResult",
                            "toolCallId": item["id"],
                            "toolName": name,
                            "content": content,
                            "isError": is_error,
                            "timestamp": now_ms(),
                        }
                        tool_results.append(tr)
                        produced.append(tr)
                    continue
                if method == "thread/tokenUsage/updated":
                    last_usage = p.get("tokenUsage")
                    if pending_message is not None and not pending_message["usage"]["totalTokens"]:
                        pending_message["usage"] = _usage(last_usage)
                    for e in pending_close:
                        yield e
                    pending_close, pending_message = [], None
                    continue
                if method == "thread/compacted":
                    yield {"type": "compaction", "summary": None, "tokensBefore": None}
                    continue
                if method == "account/rateLimits/updated":
                    yield {
                        "type": "rate_limit",
                        "info": _rate_limit_info(p.get("rateLimits") or {}),
                    }
                    continue
                if method == "error":
                    err = p.get("error") or {}
                    message = err.get("message", "error")
                    last_error_message = message
                    yield {
                        "type": "error",
                        "message": message,
                        "willRetry": bool(p.get("willRetry")),
                    }
                    continue
                if method == "turn/completed":
                    turn = p.get("turn") or {}
                    status = turn.get("status")
                    aborted = status == "interrupted"
                    if status == "failed" and turn.get("error"):
                        message = (turn["error"] or {}).get("message", "turn failed")
                        if message != last_error_message:
                            yield {"type": "error", "message": message, "willRetry": False}
                    for e in close_turn(
                        "aborted" if aborted else ("error" if status == "failed" else "stop")
                    ):
                        yield e
                    break
        finally:
            self._running = False
            self._turn_id = None
        for e in pending_close:
            yield e
        if hooks.on_stop is not None:
            await hooks.on_stop()
        yield {"type": "agent_end", "messages": produced, "aborted": aborted}

    # ── controls ───────────────────────────────────────────────────────

    async def follow_up(self, message: dict[str, Any]) -> None:
        return None  # the session owns the follow-up queue

    async def steer(self, message: dict[str, Any]) -> bool:
        if self._client is None or not self._running or not self._turn_id:
            return False
        try:
            await self._client.request(
                "turn/steer",
                {
                    "threadId": self._thread_id,
                    "expectedTurnId": self._turn_id,
                    "input": _user_input(message),
                },
            )
        except CodexRpcError:
            return False
        return True

    async def interrupt(self) -> None:
        if self._client is None or not self._running or not self._turn_id:
            return
        with contextlib.suppress(CodexRpcError):
            await self._client.request(
                "turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id}
            )

    async def fork(self) -> str:
        if self._client is None or not self._thread_id:
            raise NotSupportedError("engine not started")
        result = await self._client.request_with_retry("thread/fork", {"threadId": self._thread_id})
        self._thread_id = str((result.get("thread") or {}).get("id") or self._thread_id)
        self._side.put_nowait(
            {"type": "engine_session", "engine": self.name, "sessionId": self._thread_id}
        )
        return self._thread_id

    async def set_model(self, model: str) -> None:
        self._model = model  # applied per turn

    async def set_thinking_level(self, level: str) -> None:
        self._thinking_level = level

    async def compact(self, custom_instructions: str | None = None) -> None:
        if self._client is None or not self._thread_id:
            raise NotSupportedError("engine not started")
        await self._client.request_with_retry("thread/compact/start", {"threadId": self._thread_id})

    async def mount_tools(self, tools: list[ToolDefinition], executor: ToolExecutor) -> None:
        self._tools = list(tools)
        self._executor = executor

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


# ── helpers ────────────────────────────────────────────────────────────────


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


def _user_input(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out: list[dict[str, Any]] = []
    for part in content or []:
        if part.get("type") == "text":
            out.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image":
            data = part.get("data", "")
            if isinstance(data, bytes):
                data = base64.b64encode(data).decode()
            out.append(
                {"type": "image", "url": f"data:{part.get('mimeType', 'image/png')};base64,{data}"}
            )
    return out or [{"type": "text", "text": ""}]


def _dynamic_tool(t: ToolDefinition) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "inputSchema": t.parameters}


def _tool_call_of(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = item.get("type")
    if kind == "commandExecution":
        return "bash", {"command": item.get("command", ""), "cwd": item.get("cwd")}
    if kind == "fileChange":
        return "apply_patch", {
            "changes": [
                {"path": c.get("path"), "kind": c.get("kind")} for c in item.get("changes") or []
            ]
        }
    if kind == "mcpToolCall":
        return f"{item.get('server', 'mcp')}/{item.get('tool', '')}", dict(
            item.get("arguments") or {}
        ) if isinstance(item.get("arguments"), dict) else {"arguments": item.get("arguments")}
    if kind == "dynamicToolCall":
        return str(item.get("tool", "")), dict(item.get("arguments") or {}) if isinstance(
            item.get("arguments"), dict
        ) else {}
    if kind == "webSearch":
        return "web_search", {"query": item.get("query", "")}
    return str(kind), {}


def _tool_result_of(item: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    kind = item.get("type")
    status = item.get("status")
    failed = status in ("failed", "declined")
    if kind == "commandExecution":
        text = item.get("aggregatedOutput") or ""
        code = item.get("exitCode")
        if code not in (None, 0):
            text = f"{text}\n[exit code {code}]".strip()
            failed = True
        return [{"type": "text", "text": text}], failed
    if kind == "fileChange":
        text = "\n".join(
            f"{c.get('kind', 'update')} {c.get('path', '')}\n{c.get('diff', '')}"
            for c in item.get("changes") or []
        )
        return [{"type": "text", "text": text}], failed
    if kind == "mcpToolCall":
        res = item.get("result")
        err = item.get("error")
        text = (
            str(err.get("message"))
            if isinstance(err, dict)
            else str(res)
            if res is not None
            else ""
        )
        return [{"type": "text", "text": text}], failed or err is not None
    if kind == "dynamicToolCall":
        items = item.get("contentItems") or []
        text = "".join(str(c.get("text", "")) for c in items if isinstance(c, dict))
        return [{"type": "text", "text": text}], failed or item.get("success") is False
    if kind == "webSearch":
        return [{"type": "text", "text": str(item.get("results") or "")}], failed
    return [{"type": "text", "text": ""}], failed


def _usage(token_usage: dict[str, Any] | None) -> Any:
    if not token_usage:
        return usage_from_counts()
    last = token_usage.get("last") or token_usage.get("total") or {}
    return usage_from_counts(
        input_tokens=int(last.get("inputTokens", 0) or 0),
        output_tokens=int(last.get("outputTokens", 0) or 0),
        cache_read=int(last.get("cachedInputTokens", 0) or 0),
    )


def _rate_limit_info(snapshot: dict[str, Any]) -> dict[str, Any]:
    def window(w: dict[str, Any] | None) -> dict[str, Any] | None:
        if not w:
            return None
        return {
            "usedPercent": w.get("usedPercent"),
            "windowMinutes": w.get("windowDurationMins"),
            "resetsAt": w.get("resetsAt"),
        }

    return {
        "plan": snapshot.get("planType"),
        "primary": window(snapshot.get("primary")),
        "secondary": window(snapshot.get("secondary")),
        "reached": snapshot.get("rateLimitReachedType"),
    }
