# LocalCode

A provider-agnostic abstraction over **Claude Code** and **Codex** — one Claude-Code-style web UI, two vendor backends plus a fleet that composes them, and OAuth-based subscription auth so you never hand it an API key. Both subscriptions are metered: the top bar shows how much of each plan's window is left, and work routes onto whichever has room.

```text
            ┌────────────────────────┐
            │   React + Vite UI      │  chat / model picker / sidebar
            │   :5173                │  ⌘+↵ to send, /clear-all to wipe
            └──────────┬─────────────┘
                       │ REST + WebSocket
            ┌──────────▼─────────────┐
            │  FastAPI orchestrator  │
            │  :8080                 │
            │  ─ Provider protocol   │     unified Event stream
            │   ├ ClaudeProvider     │ ──▶ claude-agent-sdk → `claude` CLI
            │   ├ CodexProvider      │ ──▶ `codex app-server` (JSON-RPC/stdio)
            │   └ FleetProvider      │ ──▶ OrchestratorAgent dispatches
            │                        │     planner / coder / reviewer / tester
            │                        │     subagents via in-process MCP
            │  ─ one approval bus    │     every vendor's permission callback
            │  ─ one quota ledger    │     remaining headroom per subscription
            └────┬──────┬──────┬─────┘
                 │      │      │
                 ▼      ▼      ▼
        ~/.claude          ~/.codex
        (claude login)     (codex login)
         login)      login)
                 │      │      │
                 ▼      ▼      ▼
           Anthropic   OpenAI (ChatGPT subscription)
```

Sessions are persisted as files on disk under `<session.cwd>/.localcode/sessions/<uuid>/` (no database) — same pattern Claude Code uses; see [docs/storage.md](docs/storage.md). Every provider authenticates via host-side OAuth (`claude login` / `codex login`) and streams directly to its upstream. The fleet uses the orchestrator-as-agent pattern; see [docs/architecture.md](docs/architecture.md) for the modules and [docs/harness.md](docs/harness.md) for what every provider is held to.

## Why

- **Two agents, one chat surface.** Claude Code is fast and tightly integrated; Codex brings the ChatGPT subscription with real approvals. Pick per session — or hand them both to the **fleet** and let an LLM orchestrator delegate to specialists per step.
- **No keys to manage, and that is enforced.** `./setup.sh login` runs the vendors' own login flows once; tokens persist on disk / keychain and auto-refresh. LocalCode never reads a credential store and never assigns an `*_API_KEY` / `*_OAUTH_TOKEN` / `*_SESSION_KEY` from anything — that is not a convention but a source scanner (`backend/app/invariants.py`) run over the whole backend by [`backend/tests/test_auth_invariant.py`](backend/tests/test_auth_invariant.py), which fails the suite on a credential path in a string literal, an `os.environ` key assignment, an `env=` kwarg carrying one, or a keychain lookup.
- **One approval bus.** Each vendor has its own permission callback; all of them resolve through a single provider-neutral gate, so you learn one Approve/Deny card and a deny can only be forgotten in one place.
- **Composable orchestration.** A `Provider` protocol turns "which agent answered" into an implementation detail. The fleet is itself a provider — UI doesn't need to know.
- **Provider-agnostic dispatch.** A custom `dispatch_subagent` MCP tool lets one orchestrator dispatch subagents backed by different vendors in the same workflow — the architectural unlock that lets you mix `claude-opus-4-7` for planning with `gpt-5.3-codex` for the bulk coding work.

## Four providers

| Provider   | What it does                                                                                              | Auth                                              |
| :--------- | :-------------------------------------------------------------------------------------------------------- | :------------------------------------------------ |
| `claude`   | Spawns the `claude` CLI via `claude-agent-sdk`, holding one connected client per session so the prompt cache stays warm. Streams token-level text deltas and tool-use events. | `claude login` (OAuth, on host)                   |
| `codex`    | Speaks the `codex app-server` JSON-RPC protocol over stdio to the official `codex` CLI, one app-server per workspace. Command and patch approvals surface on the same card Claude's do. **Requires the `codex` CLI on `PATH` and `codex login` completed.** | `codex login` (OAuth, on host)                    |
| `fleet`    | LLM-driven orchestrator dispatches **planner / developer / coder / reviewer / tester** subagents dynamically, routing each turn to as many agents as the prompt deserves. Subagents can be backed by any of the three vendors in the same workflow, and a role set to `auto` goes to whichever subscription has the most headroom left. | Config file + the underlying providers' auth      |

See [docs/fleet.md](docs/fleet.md) for the fleet concept, [docs/fleet-config.md](docs/fleet-config.md) for configuration UX, [docs/codex.md](docs/codex.md) for the Codex integration, [docs/harness.md](docs/harness.md) for what every provider is held to, and [docs/architecture.md](docs/architecture.md) for the technical deep-dive.

## Layout

| Path                                                                                          | Role                                                              |
| :-------------------------------------------------------------------------------------------- | :---------------------------------------------------------------- |
| [setup.sh](setup.sh)                                                                          | One-shot bring-up + `login` / `stop` / `down` / `status` / `logs` |
| [pyproject.toml](pyproject.toml)                                                              | Backend deps                                                      |
| [.env.example](.env.example)                                                                  | Settings template (model catalog, defaults, session retention)    |
| [.localcode/fleet.yaml](.localcode/fleet.yaml)                                                | Active fleet config — picks role → provider → model               |
| [.localcode/fleet.yaml.example](.localcode/fleet.yaml.example) / [.json.example](.localcode/fleet.json.example) | Drop-in starters                                                  |
| [backend/app/orchestrator/base.py](backend/app/orchestrator/base.py)                          | `Provider` protocol + `RunContext` + unified `Event` types        |
| [backend/app/orchestrator/claude.py](backend/app/orchestrator/claude.py)                      | Claude SDK adapter — one persistent client per session, partial-message streaming, native auth |
| [backend/app/orchestrator/codex/](backend/app/orchestrator/codex/)                            | Codex app-server package — every wire name in `protocol.py`, stdio JSON-RPC transport, one server per workspace |
| [backend/app/orchestrator/approvals.py](backend/app/orchestrator/approvals.py)                | The one approval bus — `evaluate_tool_request` is provider-neutral; each vendor gets a thin adapter |
| [backend/app/orchestrator/permissions.py](backend/app/orchestrator/permissions.py)            | The role policy table + the single `decide()` both vendors consult |
| [backend/app/artifacts.py](backend/app/artifacts.py)                                          | Content-addressed store for output too large to put back in context |
| [backend/app/usage.py](backend/app/usage.py)                                                  | Per-turn token log + the cache-hit-rate that makes client reuse observable |
| [backend/app/quota.py](backend/app/quota.py)                                                  | Quota governor — remaining headroom per subscription, and what `auto` routes on |
| [backend/app/orchestrator/fleet/](backend/app/orchestrator/fleet/)                            | `FleetProvider` package — config loader, defaults, gate verdicts, per-step runner (heartbeats + budgets) |
| [backend/app/orchestrator/fleet/router.py](backend/app/orchestrator/fleet/router.py)          | Conditional routing — `lookup` / `simple` / `standard`, decided without a model call |
| [backend/app/orchestrator/fleet/pool.py](backend/app/orchestrator/fleet/pool.py)              | Long-lived sub-provider worker processes, reaped by process group |
| [backend/app/orchestrator/fleet/envelope.py](backend/app/orchestrator/fleet/envelope.py)      | `StepResult` — the bounded envelope a step hands back to the orchestrator |
| [backend/app/orchestrator/orchestrator.py](backend/app/orchestrator/orchestrator.py)          | `OrchestratorAgent` — claude-agent-sdk session + merged event stream |
| [backend/app/orchestrator/dispatch.py](backend/app/orchestrator/dispatch.py)                  | In-process MCP server: `dispatch_subagent` + `request_plan_approval` tools |
| [backend/app/orchestrator/agent_def.py](backend/app/orchestrator/agent_def.py)                | `AgentDef` — registry entry shape (mirrors Claude Code's `AgentDefinition`) |
| [backend/app/storage/sessions.py](backend/app/storage/sessions.py)                            | Filesystem session store — atomic JSONL append, mid-turn checkpoints, cleanup |
| [backend/app/routes/sessions.py](backend/app/routes/sessions.py)                              | REST + WebSocket chat, `_safe_run` wrapper, mid-turn persistence  |
| [backend/app/routes/fleet.py](backend/app/routes/fleet.py)                                    | `GET /api/fleet/config` for inspection                            |
| [frontend/src/components/ChatPane.tsx](frontend/src/components/ChatPane.tsx)                  | Streaming chat UI with WS auto-reconnect + mid-turn refetch       |
| [frontend/src/components/CrewBar.tsx](frontend/src/components/CrewBar.tsx)                    | Per-agent status indicator (running / done / NACK)                |
| [frontend/src/components/FleetConfigEditor.tsx](frontend/src/components/FleetConfigEditor.tsx) | Modal that emits per-session fleet override                      |
| [vscode-extension/](vscode-extension/)                                                        | Optional VS Code extension that embeds the LocalCode UI in a webview beside your code (see [docs/vscode-integration.md](docs/vscode-integration.md)) |
| [backend/tests/](backend/tests/)                                                              | The evaluation net — replay fixtures, long-horizon cases, golden fleet traces, the provider x mode matrix |
| [docs/](docs/)                                                                                | Harness contract, fleet concept, configuration UX, architecture deep-dive, Codex, storage, VS Code integration |

## Setup

```bash
./setup.sh                # check deps,
                          # start backend + frontend (no database — sessions on disk)
./setup.sh login          # one-time browser-based: claude login (+ codex login)
```

To use the `codex` provider, install the official `codex` CLI yourself and run
`codex login` once — `setup.sh` does not install it, and a session pinned to a
`codex:` model reports one clear error naming the binary if it is missing.

Open <http://localhost:5173>, pick a model from the dropdown (try **`fleet:default`** first), hit **+ New chat**, and start typing. ⌘+↵ to send.

Other subcommands: `./setup.sh status` / `logs` / `stop` / `down`.

## How model selection works

`MODEL_CATALOG` in `.env` is a comma-separated list of `provider:model` pairs. Each appears in the UI's model picker; whichever you pick at chat-creation pins the session. Four provider prefixes are valid:

- **`claude:<model>`** — `model` is the Anthropic model name (e.g. `claude-sonnet-4-6`). The spawned `claude` CLI uses your `claude login` OAuth token.
- **`codex:<model>`** — e.g. `codex:gpt-5.3-codex`. Driven through `codex app-server`; needs the `codex` CLI on `PATH` and `codex login` completed.
- **`fleet:<config>`** — e.g. `fleet:default`. The model name selects which fleet config to use; only `default` ships out of the box. The actual models invoked come from the fleet config.

## Auth notes

Every provider authenticates via host-side OAuth: the spawned `claude` CLI reads its token from `~/.claude/` (or the platform keychain) and `codex app-server` reads its own from `~/.codex/`. Run `./setup.sh login` once to mint them; both auto-refresh thereafter.

LocalCode itself never opens any of those files. That is enforced rather than promised — see the invariant bullet under [Why](#why) and [docs/harness.md](docs/harness.md#1-the-invariant).

> **Heads up:** Anthropic blocked Claude OAuth tokens for *third-party* tools in early 2026. Native auth works only because the agent we spawn is the official `claude` CLI itself. Don't try to forward those tokens elsewhere.

## What a turn costs you

The prominent number is **remaining subscription headroom**, not dollars. Under
OAuth-only auth nobody is billed per token, so a dollar figure describes
nothing you can spend; what actually stops work is a plan's window running out.
The top bar shows a meter per governed subscription — Claude on its 5-hour
window, Codex on a 5-hour window and a weekly cap — served by
`GET /api/system/quota`.

The meter is honest about what it knows. A vendor-reported figure (Claude's
rate-limit events, whatever Codex's `turn/completed` carries) is authoritative
and marked as measured; absent one, tokens accumulate locally against an
unknown ceiling and the bar says so rather than drawing a full one it never
measured. Below 5 % the governor stops routing work to that subscription and
says which plan is spent instead of silently downgrading to it — which is also
what a fleet role set to `provider: auto` routes on.

Per-turn cost (USD) is still reported by each provider in the `assistant.done`
event and still rendered inline at the bottom of every assistant turn. It is
informational — useful for comparing models against each other, not a bill.

## Documentation

- [docs/harness.md](docs/harness.md) — what every provider is held to: the auth invariant and its test, the one approval bus, the provider table, persistent sessions and prompt-prefix discipline, the artifact store, the worker pool and its budgets, conditional routing, the quota governor, and the evaluation layers (plus the gaps they do not cover).
- [docs/core.md](docs/core.md) — the second harness, under `backend/app/core/`: a pi-shaped shell with a session tree, extension API, engines, RPC mode, packages, quota and a CLI.
- [packages/fleet/README.md](packages/fleet/README.md) — the multi-agent workflow as a core package (`localcode install ./packages/fleet`).
- [docs/harness-roadmap.md](docs/harness-roadmap.md) — why the core is shaped like pi.dev.
- [docs/architecture.md](docs/architecture.md) — the technical deep-dive: providers, runner, orchestrator + dispatch, event flow, storage, configuration.
- [docs/codex.md](docs/codex.md) — the Codex app-server integration, its unverified protocol assumptions, and how to reconcile them against the vendor schema.
- [docs/fleet.md](docs/fleet.md) — fleet concept: roles, when to use it, what you see in chat.
- [docs/fleet-config.md](docs/fleet-config.md) — configuration UX, presets, recipes, troubleshooting.
- [docs/storage.md](docs/storage.md) — filesystem session store: paths, file shapes, atomicity, cleanup.
- [docs/vscode-integration.md](docs/vscode-integration.md) — VS Code extension that embeds the LocalCode UI beside your code (sidebar + editor-panel surfaces, install steps, architecture).

## What's next (good first issues)

- **Per-turn model switching in the UI.** The model picker still pins at chat creation. Surface a per-message override and a `/use <provider>:<model>` slash command (Proposal A in the orchestration doc).
- **Parallel sub-agent dispatch.** The orchestrator can call `dispatch_subagent` multiple times in one turn — the SDK runs them concurrently. Today our dispatch tool body is sequential per call; teach the orchestrator to batch independent dispatches (e.g. reviewer + tester after a coder LGTM).
- **Alembic migrations.** `db_init.py` uses `metadata.create_all`.
