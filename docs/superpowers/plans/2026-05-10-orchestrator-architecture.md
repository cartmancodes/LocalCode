# Orchestrator-as-Agent + Provider-Agnostic Task Dispatch — Implementation Plan

**Goal:** Replace the fixed-pipeline FleetProvider with an LLM-driven orchestrator
that dispatches subagents via a custom MCP tool, matching Claude Code / OpenCode
architecture while staying provider-agnostic.

**Architecture:** The orchestrator is itself a claude-agent-sdk agent running its
own ReAct loop. Its only tool is `dispatch_subagent(name, prompt)` exposed via an
in-process MCP server. When the orchestrator dispatches a subagent, our Python
implementation routes to either ClaudeProvider or OpenCodeProvider based on the
subagent's registered config — this is what makes the system provider-agnostic.

**Tech Stack:** Python 3.11, FastAPI, claude-agent-sdk (`create_sdk_mcp_server`,
`tool` decorator, `query`), existing OpenCodeProvider HTTP+SSE adapter.

---

## Why this shape

Claude Code's actual architecture (per [SDK docs](https://code.claude.com/docs/en/sub-agents)):
1. Main session runs a ReAct loop
2. `Task` tool dispatches subagents into isolated context windows
3. Each subagent runs its own ReAct loop and returns a summary
4. Orchestrator decides workflow dynamically based on user prompt

For us:
- **Orchestrator** = main claude session with our custom MCP tool
- **Task tool** = `dispatch_subagent` MCP tool (provider-agnostic)
- **Subagent execution** = existing `_run_step_with_role` (already has heartbeats + timeout)
- **Context isolation** = each dispatch runs in a fresh sub-context

The `_run_step_with_role` is already a "Tier 3" loop — it consumes the sub-provider's
event stream until completion or timeout. We don't need to rebuild it; we wrap it in
the dispatch tool.

## File Structure

- **Create** `backend/app/orchestrator/agent_def.py` — `AgentDef` dataclass (the
  registry entry shape), conversion from existing `RoleConfig` for backward compat
- **Create** `backend/app/orchestrator/dispatch.py` — in-process MCP server with
  the `dispatch_subagent` tool; calls `_run_step_with_role` under the hood
- **Create** `backend/app/orchestrator/orchestrator.py` — `OrchestratorAgent` class
  that wraps a claude-agent-sdk session + the dispatch MCP server
- **Modify** `backend/app/orchestrator/fleet.py` — `FleetConfig` gets a new
  `orchestrator_mode: bool` field; `FleetProvider.run` branches: orchestrator mode
  → `OrchestratorAgent`, legacy mode → existing `_run_planned`
- **Modify** `backend/app/orchestrator/base.py` — no changes needed; existing
  `assistant.tool_use` / `tool.result` events cover orchestrator dispatch

## Subagent dispatch protocol

Each subagent in the registry has:

```python
@dataclass
class AgentDef:
    name: str                  # unique key, e.g. "planner"
    description: str           # help text shown to orchestrator
    provider: str              # "claude" | "opencode"
    model: str
    system_prompt: str
    permission_mode: str | None = None  # passed to provider when supported
    max_turns: int | None = None        # bounded ReAct loop within agent
```

The `dispatch_subagent` MCP tool takes `{name: str, prompt: str}`, looks up the
agent in the registry, and runs it via `_run_step_with_role`. The tool returns
the subagent's complete output as the tool result content. The orchestrator sees
this as a normal tool result and decides next actions.

## Orchestrator system prompt (sketch)

```
You are the Orchestrator in a multi-agent coding fleet. You DO NOT write code
yourself. Your job: analyze the user's request and dispatch the right
specialized subagents in the right order.

Available subagents (call `dispatch_subagent` to invoke):

  planner   — produces a detailed Markdown plan committed to disk. Dispatch
              this FIRST for any non-trivial task.
  coder     — executes a plan task-by-task using file/bash tools. Pass the
              full plan in the prompt.
  reviewer  — verifies plan compliance. Returns LGTM or NACK: <reason>.
  tester    — writes + runs tests. Returns LGTM, NACK_CODE, or NACK_TESTS.

Decision rules:
- Trivial 1-line task (e.g. "rename X to Y"): dispatch coder directly, no planner.
- Non-trivial: planner → coder → reviewer (gate) → tester (gate).
- On reviewer NACK: dispatch coder again with the NACK feedback prepended.
- On tester NACK_CODE: dispatch coder again.
- On tester NACK_TESTS: dispatch tester again with feedback.
- If the coder reports no actual file changes (look at its output), re-dispatch
  with explicit "you MUST use file-edit + bash tools" — don't assume the work
  was done just because the agent wrote prose.

Bounded by max_turns. When done, emit a one-paragraph summary.
```

## Tier 3 hardening — "did the agent actually do work?"

The orchestrator's reasoning naturally handles this: it sees the coder's output
in the tool result and decides whether to retry. To make this robust we surface
tool activity in `_collect_text` (already done — coder output now includes both
narrative + tool call digest). The orchestrator can scan for "files written" /
"commands run" lines and re-dispatch on absence.

## Backward compatibility

- `FleetConfig.orchestrator_mode: bool = False` (default off — existing fleets
  keep their linear pipeline)
- New preset `"orchestrator"` enables the new mode
- Both modes share `_run_step_with_role`, so heartbeats + timeouts behave
  identically

## Tasks

### Task 1: AgentDef schema + registry

**Files:**
- Create: `backend/app/orchestrator/agent_def.py`

Define `AgentDef` dataclass and a converter from existing `RoleConfig`. Pure
data — no provider calls.

### Task 2: dispatch_subagent MCP tool

**Files:**
- Create: `backend/app/orchestrator/dispatch.py`

Use `claude_agent_sdk.tool` decorator + `create_sdk_mcp_server`. The tool's body
calls `_run_step_with_role` with a Step constructed from the AgentDef. Capture
the Step's tool_result content as the dispatch output.

The tricky bit: `_run_step_with_role` is an async generator yielding events.
The dispatch tool needs to consume those events AND surface them to the user
(so the chat shows planner / coder / reviewer cards, not just one big
"dispatch_subagent" card). Solution: pass an event sink (asyncio.Queue) into
the dispatch tool; the orchestrator runner drains the sink concurrently with
the model's output.

### Task 3: OrchestratorAgent class

**Files:**
- Create: `backend/app/orchestrator/orchestrator.py`

Wraps a claude-agent-sdk session. Builds `ClaudeAgentOptions` with:
- The dispatch MCP server registered
- `allowed_tools=["mcp__fleet_dispatch__dispatch_subagent"]`
- `system_prompt = ORCHESTRATOR_SYSTEM` listing available subagents
- `max_turns = ORCHESTRATOR_MAX_TURNS` (e.g. 30)
- `include_partial_messages=True` for streaming text to UI

Yields a unified `Event` stream merging the orchestrator's own text with the
sub-events from the event sink.

### Task 4: FleetProvider integration

**Files:**
- Modify: `backend/app/orchestrator/fleet.py`

Add `FleetConfig.orchestrator_mode` field; in `FleetProvider.run`, when this is
true, instantiate `OrchestratorAgent` and yield from it. Otherwise fall back to
existing `_run_planned`.

### Task 5: Verify + smoke test

Restart, send a turn, check that the orchestrator dispatches subagents and the
chat shows their cards.

## Risks / things to watch

1. **Streaming sub-events to the UI while the orchestrator is in a tool call.**
   The naive `await dispatch_subagent(...)` blocks until completion. We must
   pump sub-agent events to the WS *during* the dispatch. Solution: an
   asyncio.Queue between the dispatch tool and the OrchestratorAgent's outer
   yield loop.

2. **Cost.** Each turn now has the orchestrator (claude-opus default) PLUS the
   dispatched subagents. Mitigation: use claude-sonnet for the orchestrator
   (it's smart enough for delegation decisions), opus only for the planner.

3. **Loop bounds.** Orchestrator could in theory loop indefinitely. We have
   `max_turns` from claude-agent-sdk + `STEP_TIMEOUT_S` per dispatched step +
   `cfg.max_review_retries` semantics enforced via the orchestrator's prompt.

4. **MCP tool name.** SDK exposes the tool as `mcp__<server>__<tool>` — we
   must whitelist that exact name in `allowed_tools`.

5. **Backward compat.** Existing sessions / fleet configs default to the
   linear pipeline. New sessions can opt into orchestrator mode.
