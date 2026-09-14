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
| `package-manager.ts` | `packages.py` | `localcode install npm:\|git:\|path`, `remove`, `list`, `update`. A `localcode` manifest key in `package.json` (or `localcode.json`) declares what a package provides; without one, the `extensions/`, `skills/` and `prompts/` conventions apply. |
| — | `quota.py` | **New.** Normalises both vendors' rate-limit reports into one rolling-window view and answers "which engine should take this?". |

| `skills.ts`, `prompt-templates.ts`, `resource-loader.ts` | `resources/` | Skills (`SKILL.md` dirs and `.md` files; `/skill:name`), prompt templates (`/name $1 $@`), `AGENTS.md`/`CLAUDE.md` discovery up the parent chain, `SYSTEM.md` / `APPEND_SYSTEM.md`. Discovery: `<agent_dir>/{skills,prompts}`, `<cwd>/.localcode/{skills,prompts}` and `<cwd>/.agents/skills` (trusted only), plus extension `resources_discover` paths. |

Not ported: the TUI (the web UI is the interactive surface) and themes.
Context files are listed but not injected by default — both engines read them
natively; pass `inject_context_files=True` to `create_agent_session` for one
that does not.

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

## Packages

A package bundles extensions, skills and prompts. The fleet is one
(`packages/fleet`), which is the whole point of phase 5: multi-agent work is
no longer core.

```bash
localcode install ./packages/fleet          # user scope
localcode install ./packages/fleet --local  # this project only
localcode install npm:@scope/tools@1.2.3
localcode install git:github.com/user/repo@v1
localcode list
localcode update
localcode remove ./packages/fleet [--purge]
```

Installs are recorded in `settings.json` — `<agent_dir>/settings.json` for
user scope, `<cwd>/.localcode/settings.json` for project scope. A path source
is referenced where it lies rather than copied, so a package you are
developing stays live. Project-scope packages load only for a trusted project,
the same rule project extensions follow.

## Quota

Under OAuth the per-turn dollar figure is decoration: nobody is billed per
token. What runs out is the rolling window, so that is what the core tracks.

Both engines already report it; they just disagree on shape. Claude sends a
status plus a utilization fraction; Codex sends a plan type and up to two
windows as used-percent with a duration (Plus is a five-hour primary window
with a weekly secondary). `quota.py` normalises both to *how much headroom is
left and when it comes back*:

```python
governor.choose(["claude", "codex"], prefer="claude")
# → ("codex", "claude is down to 5%; routing to codex at 60%")
```

Over the wire the `get_quota` command returns that report, `get_state`
carries `quotaStatus` and `quotaHeadroom`, and the UI renders a badge.

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

## The fleet, as a package

Multi-agent work lives in `packages/fleet` and arrives through
`localcode install`. Installing registers one tool, `dispatch`; each subagent
is a nested `AgentSession` on its own engine.

Four things changed when it stopped being core:

| Old fleet | The package |
| :--- | :--- |
| A `fleet` provider chosen at chat creation | An extension — any session can dispatch |
| One subprocess per dispatch (`python -m …fleet.subproc`) | A nested session; engines are already out of process, so the SDK re-entrancy deadlock that forced the subprocess is gone |
| Roles limited by prose in a 200-line prompt | `sandbox` / `permission_mode` per role, enforced by the engine |
| Verdicts string-matched on the last line | A fenced JSON verdict, with the line form as fallback |
| Every turn ran planner → coder → reviewer | A complexity gate; a direct question costs one call |

The gate is a cheap lexical classifier, not a model call — spending a model
call to decide whether to spend model calls is its own tax. Its verdict rides
along as a non-displayed context message, so the transcript still shows what
the user actually typed.

Configuration is `.localcode/fleet.yaml`; see `packages/fleet/README.md`.

## The legacy stack

`backend/app/orchestrator/`, `backend/app/session_runner/` and
`/api/sessions/**` are the pre-core implementation. They still run, and the
React app still offers them, so nothing that worked before has stopped
working. New work belongs in `backend/app/core/`; the legacy tree is kept
only until the UI's remaining users have moved across.

In the UI, engines listed as **(core)** open a session on `/api/core/rpc`;
everything else goes through the old provider path.
