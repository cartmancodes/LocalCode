"""OpenCode provider — speaks to a running `opencode serve` over HTTP + SSE.

OpenCode is configured (via `opencode/opencode.json`) to use a single
`@ai-sdk/openai-compatible` provider pointed at the LiteLLM proxy. So the
`model` we pass through here is a LiteLLM model name, prefixed with the
opencode provider id (`litellm/<model-name>`).
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import get_settings
from .base import Event, RunContext


OPENCODE_PROVIDER_ID = "litellm"  # must match key in opencode/opencode.json


class OpenCodeProvider:
    name = "opencode"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = httpx.AsyncClient(
            base_url=self._settings.opencode_base_url,
            timeout=httpx.Timeout(60.0, read=None),  # SSE stream is open-ended
        )

    async def open_session(self, ctx: RunContext) -> str:
        if ctx.upstream_session_id:
            return ctx.upstream_session_id
        resp = await self._client.post("/session", json={})
        resp.raise_for_status()
        return resp.json()["id"]

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = await self.open_session(ctx)
        body: dict[str, Any] = {
            "model": f"{OPENCODE_PROVIDER_ID}/{ctx.model}",
            "parts": [{"type": "text", "text": ctx.prompt}],
        }
        if ctx.system_prompt:
            body["system"] = ctx.system_prompt

        # Open the SSE channel BEFORE firing the prompt so we don't miss events.
        try:
            async with self._client.stream("GET", "/event") as stream:
                # Fire the prompt asynchronously — server returns 204 immediately.
                fire = await self._client.post(
                    f"/session/{session_id}/prompt_async", json=body
                )
                if fire.status_code >= 400:
                    yield Event(
                        type="error",
                        data={"message": f"opencode rejected prompt: {fire.text}"},
                    )
                    return

                done = False
                async for raw_line in stream.aiter_lines():
                    if not raw_line or not raw_line.startswith("data:"):
                        continue
                    payload_str = raw_line[5:].strip()
                    if not payload_str:
                        continue
                    try:
                        payload = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue

                    for ev in _translate(payload, session_id):
                        yield ev
                        if ev.type == "assistant.done":
                            done = True
                    if done:
                        break
        except httpx.HTTPError as exc:
            yield Event(type="error", data={"message": str(exc), "provider": self.name})

    async def aclose(self) -> None:
        await self._client.aclose()


def _translate(payload: dict[str, Any], session_id: str) -> list[Event]:
    """Translate an opencode SSE event into our unified events.

    OpenCode emits a small zoo of bus events (`message.part.updated`,
    `message.updated`, etc.). We pick the ones that carry text/tool deltas
    and the message-finished signal.
    """
    out: list[Event] = []
    event_type = payload.get("type") or payload.get("event")
    properties = payload.get("properties", {}) or {}

    if event_type in ("message.part.updated", "message.part.added"):
        part = properties.get("part") or {}
        if part.get("sessionID") and part["sessionID"] != session_id:
            return out
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                out.append(Event(type="assistant.text", data={"text": text}))
        elif ptype == "tool":
            state = part.get("state", {}) or {}
            status = state.get("status")
            if status in ("running", "pending"):
                out.append(
                    Event(
                        type="assistant.tool_use",
                        data={
                            "id": part.get("id"),
                            "name": part.get("tool"),
                            "input": state.get("input", {}),
                        },
                    )
                )
            elif status == "completed":
                out.append(
                    Event(
                        type="tool.result",
                        data={
                            "tool_use_id": part.get("id"),
                            "content": state.get("output"),
                            "is_error": False,
                        },
                    )
                )
            elif status == "error":
                out.append(
                    Event(
                        type="tool.result",
                        data={
                            "tool_use_id": part.get("id"),
                            "content": state.get("error"),
                            "is_error": True,
                        },
                    )
                )
    elif event_type == "message.updated":
        info = properties.get("info") or {}
        if info.get("sessionID") and info["sessionID"] != session_id:
            return out
        if info.get("time", {}).get("completed"):
            out.append(
                Event(
                    type="assistant.done",
                    data={
                        "cost_usd": info.get("cost"),
                        "tokens": info.get("tokens"),
                    },
                )
            )
    return out
