# The core — pi's shape, official engines

`backend/app/core/` is a rebuild of LocalCode's harness to
[pi.dev](https://pi.dev)'s architecture, with one deliberate substitution:
the agent loop is never ours. It runs inside the vendor's official binary —
`claude` through the Agent SDK, `codex` through its app-server — so each
subscription's auth stays where the vendor put it. See
[harness-roadmap.md](harness-roadmap.md) for why.

## Module map

| pi (`packages/coding-agent/src/core`) | LocalCode (`backend/app/core`) | Notes |
| :--- | :--- | :--- |
| `session-manager.ts` | `session_manager.py` | Session format v3, byte-for-byte: header + `id`/`parentId` entries, branch in place, fork with `parentSession`, labels, compaction and branch-summary entries. Files under `~/.localcode/agent/sessions/--<cwd>--/`. |
| `messages.ts`, pi-ai `types.ts` | `messages.py` | pi-ai message shapes (`user`/`assistant`/`toolResult` plus `custom`, `compactionSummary`, `branchSummary`, `bashExecution`). |
| `extensions/types.ts` | `extensions/types.py` | Event and result contracts, `ToolDefinition`, `ctx.ui`, the `UIBridge`. |
| `extensions/runner.ts` | `extensions/runner.py` | Dispatch with pi's merge rules; errors isolated per extension. |
| `extensions/loader.ts` | `extensions/loader.py` | Discovery: `<agent_dir>/extensions`, `<cwd>/.localcode/extensions` (trusted projects only), `-e` paths. Module contract: `def setup(api)`. |
| `agent.ts` (pi-agent-core) | `engines/` | **The substitution.** `Engine` wraps a binary and emits pi's `AgentEvent`s. |
| `agent-session.ts` | `agent_session.py` | prompt / steer / follow_up / abort / subscribe / fork / navigate_tree / set_model / compact; entry recording; queues. |
| `modes/rpc/*`, `modes/json-event.ts` | `rpc/` | Same commands, responses, events and `extension_ui_request` sub-protocol; same `message_update` thinning. |
| `modes/print`, `--mode json` | `modes.py`, `cli.py` | `localcode -p "…"`, `localcode --mode json "…"`, `localcode --mode rpc`. |

Not ported: the TUI (the web UI is the interactive surface), packages
(`pi install`), skills/prompt-template discovery, themes. Those are the next
slice.

## Engines

```text
                    AgentSession
                        │  EngineHooks: before_tool · after_tool · permission_request · ask_user
              ┌─────────┴──────────┐
        ClaudeEngine          CodexEngine
   ClaudeSDKClient (stream)   codex app-server (JSON-RPC, stdio)
```

| Extension event | Claude engine | Codex engine |
| :--- | :--- | :--- |
| `tool_call` (block / edit input) | `PreToolUse` hook → deny / `updatedInput` | `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` → decline. **Cannot rewrite input** (`capabilities.tool_input_rewrite=False`). |
| permission prompt | `can_use_tool` | same approval requests |
| `tool_result` | `PostToolUse` → `additionalContext` | observe only |
| `before_agent_start` | context prepended to the prompt | context prepended to the prompt |
| `agent_end` | `Stop` hook | `turn/completed` |
| compaction | `PreCompact` hook → `compaction` entry | `thread/compacted` → `compaction` entry |
| steer | not native → interrupt, then the queued text runs next | `turn/steer` |
| fork | `fork_session=True` | `thread/fork` |
| quota | `RateLimitEvent` | `account/rateLimits/read` + `account/rateLimits/updated` |
| custom tools | in-process MCP server | `dynamicTools` (experimental; off by default) |

Engine session ids are recorded as `custom` entries
(`customType: "engine_session"`) on the branch and resumed on restart.

## Permissions

pi ships no permission popups in core; approvals are an extension's job.
Here the binary asks (`can_use_tool` / `requestApproval`) and the session
resolves it in this order:

1. `session.permission_policy` if an extension installed one;
2. the `tool_call` chain's verdict for that call (a block is a denial);
3. `default_permission`: `ask` → `ctx.ui.confirm` (RPC/WebSocket clients
   answer an `extension_ui_request`; with no UI attached this is a denial),
   `allow`, or `deny`.

`--permission-mode acceptEdits` (Claude) / a permissive sandbox (Codex)
sidestep the prompt at the binary, exactly as before.

## Known limits (stated, not hidden)

- **No `context` hook.** pi rewrites the message array before every LLM
  call; an engine-owned loop never exposes that. Registering for it raises.
- **`navigate_tree` moves the shell's leaf only.** The engine keeps its own
  context; the next prompt carries a "conversation rewound" note.
- **Compaction is the engine's.** The shell records that it happened; the
  summary text is the engine's, not ours.
- **Python SDK hook subset.** `SessionStart`/`SessionEnd` are TypeScript-only
  callbacks; the session emits its own.

## Running

Three transports, one protocol:

- **stdio** — `localcode --mode rpc` (editors, scripts, CI).
- **WebSocket** — `ws://host/api/core/rpc?engine=claude&cwd=/repo&session=new` on the FastAPI
  app (`backend/app/routes/core_rpc.py`); first frame is `{"type":"ready","state":…}`.
- **one-shot** — `-p` print and `--mode json`.

```bash
localcode --mode rpc --engine claude            # pi's RPC on stdio
localcode -p "what does setup.sh do?"           # print mode
localcode --mode json "summarise README.md"     # JSONL events
localcode --engine codex --session continue     # resume the latest session
localcode --trust-project -e ./my_ext.py        # project + explicit extensions
```

An extension:

```python
# ~/.localcode/agent/extensions/guard.py
def setup(api):
    async def guard(event, ctx):
        if event["toolName"] == "bash" and "rm -rf" in event["input"].get("command", ""):
            ok = await ctx.ui.confirm("Destructive command", event["input"]["command"])
            if not ok:
                return {"block": True, "reason": "declined by user"}
    api.on("tool_call", guard)
    api.register_command("name", handler=lambda args, ctx: api.set_session_name(args),
                         description="rename this session")
```

## Tests

`./.venv/bin/python -m pytest backend/tests` — session tree, extensions,
engines (Claude against scripted SDK messages; Codex against
`tests/fake_codex_server.py`, which speaks the real line protocol), the
session, RPC, CLI, and the architecture invariants in `test_invariants.py`
that fail the build on any credential-store read or token forwarding.
