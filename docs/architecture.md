# Architecture — Orchestrator-as-Agent + Provider-Agnostic Dispatch

This is the deep technical reference for LocalCode's fleet orchestration. For the user-facing concept doc see [docs/fleet.md](fleet.md); for the configuration UX see [docs/fleet-config.md](fleet-config.md).

---

## At a glance

```text
                        user prompt
                             │
                ┌────────────▼────────────┐
                │ FleetProvider.run()     │  thin router
                │   • loads fleet config  │
                │   • merges UI override  │
                │   • delegates           │
                └────────────┬────────────┘
                             │
                ┌────────────▼─────────────────────┐
                │ OrchestratorAgent                │  ReAct loop (claude-agent-sdk)
                │   model: claude-sonnet-4-6        │
                │   max_turns: 30                   │
                │   mcp_servers: { fleet_dispatch }  │
                │   allowed_tools: [                 │
                │     dispatch_subagent,              │
                │     request_plan_approval ]         │
                └─┬──────────┬───────────────────────┘
                  │          │              ▲
            tool call       tool call       │ orchestrator's narrative text
                  │          │              │ + sub-agent events from EventSink
   ┌──────────────▼─┐  ┌─────▼──────────┐   │
   │dispatch_subagent│  │request_plan_…  │   └──► merged asyncio.Queue ──► WS
   │ (Task equivalent)│  │ (HITL gate)    │
   └────────┬─────────┘  └─────┬──────────┘
            │                  │
            │                  └─► awaits ctx.approval_channel
            │                       (WS handler routes inbound `{type:"approval"}`
            │                        frames to the channel during the turn)
            ▼
   _run_step_with_role(step, role_cfg, ctx, outputs)
   ├── yields tool_use ─────────────────────────────────┐
   ├── concurrent _collect_text(role) + heartbeat ticker│ events pushed to EventSink
   ├── per-step timeout (STEP_TIMEOUT_S = 600s)         │
   ├── on success: yields tool.result                   │
   └── on timeout: yields error result + raises StepTimeoutError
                                                        ▼
                                            (sink → merged → WS)
```

One control path. The orchestrator is itself an LLM agent — it makes routing decisions, not Python code.

---

## Module layout

| Module | Responsibility |
| :----- | :------------- |
| [agent_def.py](../backend/app/orchestrator/agent_def.py) | `AgentDef` dataclass — one entry in the orchestrator's registry. Mirrors Claude Code's `AgentDefinition` and OpenCode's frontmatter shape. Helpers convert legacy `RoleConfig` and render the registry into the orchestrator's system prompt. |
| [dispatch.py](../backend/app/orchestrator/dispatch.py) | In-process MCP server with two tools: `dispatch_subagent(name, prompt)` (Task-equivalent) and `request_plan_approval(plan_summary)` (HITL gate). Exposes `EventSink`, `save_plan`, `await_approval`. |
| [orchestrator.py](../backend/app/orchestrator/orchestrator.py) | `OrchestratorAgent` — wraps a claude-agent-sdk session, registers the dispatch MCP server, merges its own model output with sub-agent events from the sink into one outgoing event stream. |
| [fleet.py](../backend/app/orchestrator/fleet.py) | `FleetProvider`, `FleetConfig`, `RoleConfig`, `Step`. The per-step runner `_run_step_with_role` (heartbeats + timeout + classifier) lives here — it's called from inside the dispatch tool. Plus config loader, merger, and built-in defaults. |
| [base.py](../backend/app/orchestrator/base.py) | `Provider` protocol, `RunContext` (carries cwd, additional_dirs, approval_channel), `Event` and `EventType` literals. |
| [routes/sessions.py](../backend/app/routes/sessions.py) | WebSocket handler. Drains the merged event stream, persists assistant blocks at every checkpoint, routes inbound approval messages to `RunContext.approval_channel`. |

---

## Agent registry

```python
@dataclass
class AgentDef:
    name: str                  # unique key, e.g. "planner"
    description: str           # what the orchestrator reads to decide WHEN to dispatch
    provider: str              # "claude" | "opencode"
    model: str
    system_prompt: str
    permission_mode: str | None = None  # passed through where the provider supports it
    max_turns: int | None = None        # bound on inner ReAct iterations
    metadata: dict[str, Any] = field(default_factory=dict)
```

The registry is a `dict[str, AgentDef]` built per-turn from `cfg.roles` via `registry_from_role_library()`. The orchestrator reads it once at the start of the turn and references it for every dispatch decision.

`render_registry_for_prompt(registry)` produces a Markdown bullet list — one line per agent with name + description + (provider:model). This is interpolated into the orchestrator's system prompt so it knows what's available.

---

## The dispatch MCP server

The MCP server is built fresh per-turn (closures capture this turn's registry/ctx/sink) and registered with `ClaudeAgentOptions.mcp_servers={"fleet_dispatch": …}`. The orchestrator sees two tools (wire names):

| Tool wire name | Body |
| :------------- | :--- |
| `mcp__fleet_dispatch__dispatch_subagent` | Looks up `name` in registry → builds a `Step` + `RoleConfig` → drives `_run_step_with_role(step, role_cfg, ctx, outputs)` and pushes every event onto `EventSink`. Returns the subagent's final output text. When `name == "planner"`, additionally writes the result to `<cwd>/.localcode/plans/<timestamp>-<slug>.md` and appends the path to the returned text. |
| `mcp__fleet_dispatch__request_plan_approval` | Pushes a `pipeline.awaiting_approval` event onto the sink, awaits a decision from `ctx.approval_channel` (WS back-channel) with `APPROVAL_TIMEOUT_S = 300s`, pushes a `pipeline.approval_received` event, returns the decision as text the orchestrator can reason about. |

Both tools use closures over `registry`, `ctx`, `sink`, and `run_step_fn` (a bound method on `FleetProvider` passed in to avoid a circular import).

---

## Event-stream merging

Two producers, one consumer:

1. **`_pump_orchestrator`** drains `claude_agent_sdk.query()` (the orchestrator's own SDK iterator), translating each message via `_translate_orchestrator_message()` and pushing to the merged queue. Translation suppresses the raw `mcp__fleet_dispatch__*` tool_use / tool_result blocks because the dispatch tool's body emits proper per-role cards through the sink — we don't want duplicate cards for the same call.
2. **`_pump_sink`** drains the `EventSink` queue and pushes to the merged queue.

A `_seal_when_drained` task `gather()`s both pumps and pushes a `_DONE` sentinel onto the merged queue when both finish. The consumer (the OrchestratorAgent's `run()` async generator) breaks out on the sentinel.

On consumer exit (WS close, exception), the `finally` block cancels all three tasks and awaits their cleanup. The dispatch tool's currently-running `_run_step_with_role` is cancelled via the same path — its inner `try/finally` cancels its `_collect_text` Task, which propagates to the sub-provider's process / HTTP stream.

---

## The per-step runner: `_run_step_with_role`

Every dispatch goes through this function. It's the workhorse that the dispatch tool delegates to.

```python
async def _run_step_with_role(self, step, role_cfg, ctx, outputs):
    yield Event(type="assistant.tool_use", data={...})

    collect = asyncio.create_task(_collect_text(role_cfg, step.prompt, ctx.cwd, ctx.additional_dirs))
    elapsed_s = 0
    output, error_text, timed_out = None, None, False

    try:
        while True:
            try:
                output = await asyncio.wait_for(asyncio.shield(collect), timeout=HEARTBEAT_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                elapsed_s += int(HEARTBEAT_INTERVAL_S)
                if elapsed_s >= STEP_TIMEOUT_S:
                    timed_out = True
                    error_text = f"{step.role} step exceeded {int(STEP_TIMEOUT_S)}s budget"
                    break
                yield Event(type="assistant.text", data={
                    "text": f"_…{step.role} still working ({elapsed_s}s)…_\n",
                    "heartbeat": True,
                })
    except Exception as exc:
        error_text = str(exc) or repr(exc)
    finally:
        if not collect.done():
            collect.cancel()
            try: await collect
            except (asyncio.CancelledError, Exception): pass

    if error_text is not None:
        yield Event(type="tool.result", data={"tool_use_id": step.id, "content": error_text, "is_error": True})
        if timed_out:
            raise StepTimeoutError(error_text)
        return

    outputs[step.id] = output
    is_error = step.role in ("reviewer", "tester") and _classify_gate(output, step.role) != "lgtm"
    yield Event(type="tool.result", data={"tool_use_id": step.id, "content": output, "is_error": is_error})
```

Key constants:

| Constant | Value | Why |
| :------- | :---- | :-- |
| `HEARTBEAT_INTERVAL_S` | 30s | Chat heartbeat cadence so the UI never goes silent for >30s during a slow opus turn. |
| `STEP_TIMEOUT_S` | 600s (10min) | Hard cap on a single sub-provider step. opus on a complex plan can legitimately take 3-4 min; 10 min covers worst-case while bounding hung subprocesses. |

`asyncio.shield()` protects the inner `_collect_text` task from `wait_for`'s cancel-on-timeout — we want the timeout to fire the heartbeat, not abort the work.

`StepTimeoutError` (subclass of `RuntimeError`) propagates up through `_safe_run` in `sessions.py` so the WS gets a clean `error` + `assistant.done` close-out.

---

## Gate classifier

Two of our roles use a strict last-line classifier protocol:

```python
def _classify_gate(output: str, role: str) -> str:
    """Returns "lgtm" / "nack" / "nack_code" / "nack_tests"."""
    lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
    if not lines:
        return "nack" if role != "tester" else "nack_code"
    last = lines[-1].upper()
    if role == "tester":
        if last.startswith("LGTM") or last.startswith("TESTS_OK"): return "lgtm"
        if last.startswith("NACK_TESTS"): return "nack_tests"
        return "nack_code"  # bare NACK or unclassified → fail-safe to NACK_CODE
    if last.startswith("LGTM"): return "lgtm"
    return "nack"  # anything else → fail-safe NACK
```

Reads the LAST non-empty line — both system prompts instruct models to put the verdict there.

Earlier we used `output.startswith("NACK")`, which only ever inspected the FIRST line. A reviewer that prefaced its verdict with descriptive prose ("The project directory is empty…") looked like an LGTM and the workflow advanced past clear failures. Last-line + fail-safe-to-NACK closes that hole.

The `is_error` flag on the tool_result drives the UI's red-card rendering and also signals the orchestrator (via the tool result content + `is_error` field) that the gate failed.

---

## HITL — the approval back-channel

```text
WS handler                      RunContext         OrchestratorAgent      request_plan_approval tool
    │                               │                       │                        │
    │ creates approval_q             │                       │                        │
    │                               ◄─────── attached  ─────│                        │
    │                                                        │ orchestrator decides   │
    │                                                        │ to call the tool ─────►│
    │                                                        │                        │ pushes pipeline.awaiting_approval
    │                                                        │                        │ → EventSink → WS
    │ receives {type:"approval",                              │                        │
    │           id:"approval.plan",                           │                        │ awaits approval_q.get()
    │           value:"yes"|"no",                             │                        │  (with APPROVAL_TIMEOUT_S deadline)
    │           feedback?:"..."}                              │                        │
    │  ─── routes to approval_q ────────────────────────────────────────────────────►│
    │                                                        │                        │ pushes pipeline.approval_received
    │                                                        │                        │ returns text decision to orchestrator
    │                                                        │ orchestrator reads     │
    │                                                        │ decision text, decides │
    │                                                        │ to continue or abort   │
```

Stale approvals (different `id`) are dropped — this lets a second approval gate in the same turn ignore a late click on a previous gate's button. On disconnect, the WS handler cancels the inbound reader; the approval tool's `await asyncio.wait_for(...)` catches `CancelledError` via the surrounding orchestrator-task cancellation cascade.

The HITL block in the orchestrator's system prompt is only injected when `cfg.require_plan_approval=True`. It's templated into a placeholder between "Dispatch planner" and "Dispatch coder" so the orchestrator can't miss the gate.

---

## Mid-turn persistence

Every `tool_use` and `tool.result` event triggers a checkpoint:

```python
async def _checkpoint() -> None:
    """INSERT the assistant message on first call, UPDATE thereafter."""
    flushed = list(assistant_blocks)
    if text_buf:
        flushed.append({"type": "text", "text": "".join(text_buf)})
    if not flushed: return
    async with session_scope() as db:
        if assistant_message_id is None:
            msg = Message(session_id=..., role="assistant", content=flushed, ...)
            db.add(msg); await db.flush()
            assistant_message_id = msg.id
        else:
            await db.execute(update(Message).where(Message.id == assistant_message_id).values(content=flushed, ...))
```

A page refresh during a multi-minute workflow shows the latest checkpoint — every completed step is in the DB. Without this the user only saw their own prompt until the entire turn finished.

Heartbeat events (`assistant.text` with `heartbeat: True`) are filtered from `text_buf` so they reach the live WS but not persisted history. Result: live chat is responsive, persisted chat is clean.

---

## Frontend reconnect handling

[ChatPane.tsx](../frontend/src/components/ChatPane.tsx) calls `loadMessages(sessionId)` on every WS open beyond the first. The hydration logic detects "mid-turn state" — an assistant turn whose last `tool_use` has no matching `tool_result` — and marks it `inProgress: true`. The `ensureAssistant()` helper then merges live events from the new WS into that turn instead of pushing a duplicate.

This means a full page refresh (or auto-reconnect after a network blip) shows the partial workflow correctly: completed agent cards plus a still-spinning current card.

---

## Cancellation cascade

When the WS disconnects mid-turn:

1. `sessions.py` catches `WebSocketDisconnect` on the next `send_json`.
2. The `_run_one_turn` finally calls `events.aclose()` on the `_safe_run` generator.
3. `_safe_run`'s `async for` over `provider.run()` is cancelled.
4. `FleetProvider.run`'s `_run_orchestrated` is cancelled.
5. `OrchestratorAgent.run`'s consumer loop is cancelled. Its `finally` cancels `_pump_orchestrator`, `_pump_sink`, and `_seal_when_drained`.
6. `_pump_orchestrator`'s cancellation propagates into `query()` → cancels the SDK's iteration → SDK's underlying spawn process gets SIGTERM.
7. Any in-flight `dispatch_subagent` body is interrupted at its `await sink.put(...)`. Its `_run_step_with_role` runs its own `finally` which cancels the inner `_collect_text` Task. That cancellation propagates to the sub-provider (claude-agent-sdk's spawned `claude` CLI gets SIGTERM, opencode HTTP stream is closed).
8. The DB checkpoint in `sessions.py:finally` writes whatever assistant_blocks were accumulated so the user sees partial state on reload.

---

## Why custom MCP rather than the SDK's native `Task` + `AgentDefinition`

`claude-agent-sdk` exposes `AgentDefinition` and `agents` parameter — when you populate the latter, the orchestrator gets a native `Task` tool that dispatches the named subagents. For pure-claude workflows that's the cleanest path.

We don't use it because **we want the orchestrator to dispatch opencode-backed subagents too**. `AgentDefinition` only knows about claude-agent-sdk subagents. Routing through our own `dispatch_subagent` MCP tool gives us the unified provider-agnostic dispatch.

The cost: we re-implement the parts of native Task we need (per-subagent context, summary return, parallel safety). The benefit: a coder running on `opencode/openai/gpt-5.3-codex` (cheap, code-tuned) is dispatchable from the same orchestrator that dispatches `claude-opus-4-7` for planning. This is the architectural unlock.

---

## Heritage

Earlier iterations of this code shipped a fixed linear pipeline (planner emits JSON-step list → Python loop runs them → fixed retry topology). See [docs/orchestration-proposals.md](orchestration-proposals.md) for the design rationale, and [docs/superpowers/plans/2026-05-10-orchestrator-architecture.md](superpowers/plans/2026-05-10-orchestrator-architecture.md) for the planning artifact behind the rewrite.

The rewrite (May 2026) replaced the linear pipeline with the orchestrator-as-agent pattern documented here, achieving structural parity with Claude Code's main-session-with-Task and OpenCode's primary-agent-with-subagents architectures while preserving provider-agnostic dispatch.
