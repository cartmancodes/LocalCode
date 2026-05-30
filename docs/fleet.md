# Fleet — orchestrator-driven multi-agent workflow

The `fleet` provider treats your prompt as a **workflow** rather than a single agent call. An LLM-driven **Orchestrator** reads your request, decides which specialist subagents to dispatch, runs them in isolated contexts, reasons about their results, and re-dispatches as needed. The orchestrator itself is a [claude-agent-sdk](https://docs.anthropic.com/en/api/agent-sdk) session running its own ReAct loop; subagents are dispatched via a custom MCP tool, so the same orchestrator can reach both Claude- and OpenCode-backed workers.

This is the same shape Claude Code and OpenCode use for their main-session-with-Task-tool pattern, with one difference: our `dispatch_subagent` MCP tool routes provider-agnostically, so a single orchestrator can mix providers in one workflow.

For the deep technical reference (event streams, cancellation, MCP layer, design tradeoffs), see [docs/architecture.md](architecture.md).

## Roles

| Role         | Job                                                                         | Default model            |
| :----------- | :-------------------------------------------------------------------------- | :----------------------- |
| **Planner**  | Produces a comprehensive Markdown implementation plan with file paths, complete code, test commands, and bite-sized steps. The plan is committed to `.localcode/plans/<timestamp>-<slug>.md`. | `claude-opus-4-7`        |
| **Developer** *(optional)* | Extra design pass — interfaces, files, edge cases. No code. Used only when the plan needs more architectural detail. | `claude-sonnet-4-6`      |
| **Coder**    | Executes the plan task-by-task using file-edit and bash tools. MUST use tools, not just describe intent. | `openai/gpt-5.3-codex` (via OpenCode) |
| **Reviewer** | Verifies plan compliance + code quality on disk. Replies `LGTM` or `NACK: <reason>`. | `claude-sonnet-4-6`      |
| **Tester**   | Final gate. Writes executable tests and runs them. Replies `LGTM`, `NACK_CODE` (impl bug), or `NACK_TESTS` (test bug). | `claude-haiku-4-5`       |

The orchestrator always dispatches the core agents in order when they are registered, even for trivial or read-only tasks:

```
planner → coder → reviewer → tester
              ↑      ↓ NACK         ↓ NACK_CODE / NACK_TESTS
              └──────┘                       │
                          retry              │
                ┌──────────────────────────────┘
                ↓
              coder again (with feedback)
```

If one of the core roles is not registered, the orchestrator continues with the remaining registered roles in the same order. It can also dispatch the developer when architectural ambiguity warrants it. For read-only tasks, the coder executes inspection/verification commands instead of editing files unless the user explicitly requested changes.

## When to use it

- **Multi-phase tasks** (plan → code → review → test).
- **Cost optimization**: keep `claude-opus-4-7` on planning, cheaper opencode-routed `gpt-5.3-codex` on the bulk implementation work, mid-tier sonnet on review, haiku on the test pass.
- **Anything you'd hand-prompt one agent through "first plan, then code, then review, then write tests."**

When **not** to use it: short factual questions, one-shots, or tasks where you'd rather keep the conversational thread inside one model. Fleet adds 1–4 LLM hops; trivial prompts feel slower with it on. Pick a direct `claude:…` or `opencode:…` model in those cases.

## How a turn flows

1. **Orchestrator entry.** Your prompt + the registered agent registry go to the orchestrator (`claude-sonnet-4-6` by default — smart enough for delegation, cheap enough to use every turn).
2. **Dispatch loop.** The orchestrator's ReAct loop calls `dispatch_subagent(name, prompt)` to delegate. Each dispatch:
   - Spawns the named subagent in an isolated context window with its own system prompt and tools.
   - Streams per-subagent events (tool_use card → heartbeats → tool_result) to the chat in real time via an `EventSink` queue.
   - Returns the subagent's final summary text to the orchestrator.
3. **Reasoning between dispatches.** The orchestrator reads the subagent's output and decides next steps:
    - Coder returned only narrative without tool calls? Re-dispatch with "you MUST use tools to execute the plan." For read-only tasks this means inspection commands; for implementation tasks this means real changes.
   - Reviewer NACKed? Re-dispatch the coder with the NACK feedback prepended.
   - Tester returned `NACK_CODE`? Re-dispatch coder + reviewer + tester. `NACK_TESTS`? Re-dispatch tester only with a "fix the test files" instruction.
4. **HITL gate** *(optional)*. When `require_plan_approval` is set, the orchestrator's system prompt instructs it to call `request_plan_approval` after the planner. The chat shows an Approve/Reject card; the user's decision feeds back to the orchestrator.
5. **Termination.** When all gates are green or the orchestrator's turn budget is exhausted, it emits a one-paragraph summary as the final assistant text.

## What you see in the chat

For each dispatched subagent the WS shows:

```
assistant.tool_use     name="planner [claude:claude-opus-4-7]"     input={"prompt": "<truncated>"}
assistant.text         "_…planner still working (30s)…_"           heartbeat: True   ← every 30s
assistant.text         "_…planner still working (60s)…_"           heartbeat: True
tool.result            tool_use_id=orch.planner.1                  content=<full markdown plan>
assistant.text         "_Plan saved to_ `<...>/.localcode/plans/<timestamp>-<slug>.md`"
                       ↓
                       (orchestrator decides what's next, possibly with its own narrative)
                       ↓
assistant.tool_use     name="coder [opencode:openai/gpt-5.3-codex]"
…
assistant.done         duration_ms=…
```

Heartbeats are filtered out of persisted message history (live UI only); everything else is checkpointed mid-turn so a refresh shows the latest progress.

## Reliability features

| Feature | Where | Behaviour |
| :------ | :---- | :-------- |
| Per-step timeout | `STEP_TIMEOUT_S = 600s` in `fleet/provider.py:_run_step_with_role` | Hung sub-providers raise `StepTimeoutError`; orchestrator sees `is_error=True` and decides whether to retry or abort. |
| Heartbeats | Every 30s during a long sub-provider call | Streams "still working" pings so the UI doesn't look frozen during opus's 2–3 min thinking. |
| Mid-turn persistence | UPSERT on every `tool_use` and `tool.result` | A page refresh during a multi-minute workflow shows the latest checkpoint, not just the user prompt. |
| WS reconnect refetch | Frontend `loadMessages()` runs on WS reopen | New WS replaces stale local state with the latest persisted blocks; mid-flight `tool_use` without matching result is marked `inProgress`. |
| Last-line classifier | `fleet/gate.py:classify_gate(output, role)` | Reads the LAST non-empty line of a gate output. Unclassified output is treated as a fail-safe NACK rather than silently advancing. |
| Bounded retry budget | `cfg.max_review_retries` | The orchestrator's system prompt tells it the budget; orchestrator self-bounds. |

## Config

```yaml
# .localcode/fleet.yaml
name: my-workflow
max_review_retries: 3
require_plan_approval: false

roles:                              # registry — only these agents run
  planner:
    provider: claude
    model: claude-opus-4-7
  coder:
    provider: opencode
    model: openai/gpt-5.3-codex
  reviewer:
    provider: claude
    model: claude-sonnet-4-6
  tester:
    provider: claude
    model: claude-haiku-4-5
```

Drop the file into `.localcode/fleet.yaml` (or `.json` / `.yml`); resolution order, first hit wins:

1. `$LOCALCODE_FLEET_CONFIG` — absolute path override.
2. `<cwd>/.localcode/fleet.{yaml,yml,json}`
3. `<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}`
4. Built-in defaults (`planner + coder + reviewer + tester`).

For configuration UX, presets, troubleshooting, see [docs/fleet-config.md](fleet-config.md).

## Why this shape

We previously shipped a **fixed linear pipeline** (planner JSON-decomposes → linear step execution → fixed retry topology). That worked but was rigid: adding an agent meant a code change, the orchestrator's "intelligence" was hardcoded Python, and parallel dispatch was impossible.

The orchestrator-as-agent rewrite (May 2026) brings us structurally identical to Claude Code's main-session-with-Task and OpenCode's primary-agent-with-subagents architectures. Specifically:

- The orchestrator is itself an LLM agent running a ReAct loop — it makes routing decisions, not Python code.
- Subagents are dispatched via a Task-equivalent tool. Each gets an isolated context.
- Provider-agnostic dispatch (the missing piece in upstream models) is achieved through a custom in-process MCP server that routes by registry lookup.
- Reuse: the dispatch tool body delegates to `_run_step_with_role`, which means heartbeats, per-step timeout, the gate classifier, and mid-turn persistence all just work.

Sources for the design:
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [OpenCode agents](https://opencode.ai/docs/agents/)
- [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk)
- [How Coding Agents Actually Work — OpenCode internals](https://cefboud.com/posts/coding-agents-internals-opencode-deepdive/)

## Source

- [backend/app/orchestrator/fleet/](../backend/app/orchestrator/fleet/) — `FleetProvider` package: `provider.py` (per-step runner `_run_step_with_role`), `loader.py` (config resolution), `gate.py` (classifier), `models.py` / `defaults.py` / `presets.py` / `prompts.py`. `__init__.py` re-exports the public API.
- [backend/app/orchestrator/orchestrator.py](../backend/app/orchestrator/orchestrator.py) — `OrchestratorAgent` (claude-agent-sdk session + merged event stream).
- [backend/app/orchestrator/dispatch.py](../backend/app/orchestrator/dispatch.py) — in-process MCP server with `dispatch_subagent` and `request_plan_approval` tools.
- [backend/app/orchestrator/agent_def.py](../backend/app/orchestrator/agent_def.py) — `AgentDef` (registry shape), conversion from legacy `RoleConfig`.
- [backend/app/routes/fleet.py](../backend/app/routes/fleet.py) — `GET /api/fleet/config` for inspection.
- [.localcode/fleet.yaml.example](../.localcode/fleet.yaml.example) — drop-in starter.
- [docs/architecture.md](architecture.md) — deep technical reference.
