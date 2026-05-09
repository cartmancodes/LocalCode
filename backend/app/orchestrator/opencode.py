"""OpenCode provider — speaks to a running `opencode serve` over HTTP + SSE.

OpenCode runs on the host (not in Docker) so that `opencode auth login` can open
a browser and persist OAuth tokens at ~/.local/share/opencode/auth.json. The
backend therefore passes models in OpenCode's native `<provider>/<model>` form,
e.g. `openai/gpt-5-codex` or `openai/gpt-4o`. OpenCode resolves the credential
from its own auth store — we don't pass any key.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import get_settings
from .base import Event, RunContext


class OpenCodeProvider:
    name = "opencode"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = httpx.AsyncClient(
            base_url=self._settings.opencode_base_url,
            timeout=httpx.Timeout(60.0, read=None),  # SSE stream is open-ended
            # Cap the connection pool so a runaway reconnect storm against a
            # flapping OpenCode server can't exhaust file descriptors.
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def open_session(self, ctx: RunContext) -> str:
        if ctx.upstream_session_id:
            return ctx.upstream_session_id
        # Pass the user-chosen working directory so the spawned coder's
        # file/bash tools are rooted there. OpenCode tolerates a missing
        # `directory` (uses its server-side cwd), so this is safe to send
        # unconditionally when ctx.cwd is set.
        body: dict[str, Any] = {}
        if ctx.cwd:
            body["directory"] = ctx.cwd
        resp = await self._client.post("/session", json=body)
        resp.raise_for_status()
        return resp.json()["id"]

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        try:
            session_id = await self.open_session(ctx)
        except httpx.HTTPError as exc:
            yield Event(
                type="error",
                data={
                    "message": f"could not reach opencode at {self._settings.opencode_base_url}: {exc}",
                    "provider": self.name,
                },
            )
            return

        # OpenCode's current schema expects model as {providerID, modelID}, not
        # "provider/model". Our catalog stores "openai/gpt-5-codex" — we split
        # on `/`. We do NOT silently default to "openai" for unprefixed names:
        # users who type `gpt-5.4` (forgetting the prefix) get a clear error
        # here instead of an opaque "model not found" from OpenCode later.
        provider_id, _, model_id = ctx.model.partition("/")
        if not model_id:
            yield Event(
                type="error",
                data={
                    "message": (
                        f"opencode model {ctx.model!r} must be in 'provider/model' "
                        f"form (e.g. 'openai/gpt-5.4-mini'). Run "
                        f"'~/.opencode/bin/opencode models' for the list."
                    ),
                    "provider": self.name,
                },
            )
            return
        body: dict[str, Any] = {
            "model": {"providerID": provider_id, "modelID": model_id},
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

                state = _TurnState()
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

                    for ev in _translate(payload, session_id, state):
                        yield ev
                        if ev.type == "assistant.done":
                            done = True
                    if done:
                        break
        except httpx.HTTPError as exc:
            yield Event(type="error", data={"message": str(exc), "provider": self.name})

    async def aclose(self) -> None:
        await self._client.aclose()


class _TurnState:
    """Per-turn correlation state for the SSE translator. OpenCode emits
    user-message parts and assistant-message parts on the same stream, so we
    track which message IDs are user-role and skip their parts (the UI already
    has the user's prompt — re-emitting it produces an echo)."""

    __slots__ = ("user_msg_ids", "text_seen_per_part")

    def __init__(self) -> None:
        self.user_msg_ids: set[str] = set()
        # OpenCode sends `message.part.updated` with the *full* current text on
        # each update, not a delta. Track the running length per part so we can
        # emit only the new tail to the UI.
        self.text_seen_per_part: dict[str, int] = {}


def _translate(
    payload: dict[str, Any], session_id: str, state: _TurnState
) -> list[Event]:
    """Translate an opencode SSE event into our unified events. See _TurnState
    for the correlation we maintain across events."""
    out: list[Event] = []
    event_type = payload.get("type") or payload.get("event")
    properties = payload.get("properties", {}) or {}

    if event_type == "message.updated":
        info = properties.get("info") or {}
        if info.get("sessionID") and info["sessionID"] != session_id:
            return out
        if info.get("role") == "user" and info.get("id"):
            state.user_msg_ids.add(info["id"])
        # Assistant message completion → end of turn.
        if info.get("role") == "assistant" and info.get("time", {}).get("completed"):
            out.append(
                Event(
                    type="assistant.done",
                    data={"cost_usd": info.get("cost"), "tokens": info.get("tokens")},
                )
            )
        return out

    if event_type in ("session.error",):
        err = properties.get("error") or {}
        msg = (err.get("data") or {}).get("message") or err.get("name") or "session error"
        out.append(Event(type="error", data={"message": msg, "provider": "opencode"}))
        # Follow with done so the UI clears its working indicator.
        out.append(Event(type="assistant.done", data={}))
        return out

    if event_type in ("message.part.updated", "message.part.added"):
        part = properties.get("part") or {}
        if part.get("sessionID") and part["sessionID"] != session_id:
            return out
        # Skip parts that belong to the user's own message — those are the
        # prompt being echoed back, not the assistant's response.
        if part.get("messageID") in state.user_msg_ids:
            return out

        ptype = part.get("type")
        if ptype == "text":
            full = part.get("text", "") or ""
            pid = part.get("id") or ""
            seen = state.text_seen_per_part.get(pid, 0)
            delta = full[seen:]
            if delta:
                state.text_seen_per_part[pid] = len(full)
                out.append(Event(type="assistant.text", data={"text": delta}))
        elif ptype == "tool":
            tstate = part.get("state", {}) or {}
            status = tstate.get("status")
            if status in ("running", "pending"):
                out.append(
                    Event(
                        type="assistant.tool_use",
                        data={
                            "id": part.get("id"),
                            "name": part.get("tool"),
                            "input": tstate.get("input", {}),
                        },
                    )
                )
            elif status == "completed":
                out.append(
                    Event(
                        type="tool.result",
                        data={
                            "tool_use_id": part.get("id"),
                            "content": tstate.get("output"),
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
                            "content": tstate.get("error"),
                            "is_error": True,
                        },
                    )
                )
    return out
