# LocalCode

A provider-agnostic abstraction over **Claude Code** and **OpenCode** — one Claude-Code-style web UI, three backends, and OAuth-based subscription auth so you don't have to hand it API keys.

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
            │   ├ OpenCodeProvider   │ ──▶ opencode serve  (HTTP + SSE)
            │   └ FleetProvider      │ ──▶ planner → developer → coder → reviewer
            └──────┬───────┬─────────┘
                   │       │
                   ▼       ▼
        ~/.claude OAuth   ~/.local/share/opencode/auth.json
        (claude login)    (opencode auth login)
                   │       │
                   ▼       ▼
              Anthropic   OpenAI (ChatGPT subscription)
```

Postgres holds session + message state. Both providers authenticate via host-side OAuth (`claude login` / `opencode auth login`) and stream directly to their upstreams.

## Why

- **Two agents, one chat surface.** Claude Code is fast and tightly integrated; OpenCode is open-source and pluralistic. Pick per session — or hand both to the **fleet** and let a Planner orchestrate them per step.
- **No keys to manage.** `./setup.sh login` runs `claude login` and `opencode auth login` once; tokens persist on disk / keychain and auto-refresh.
- **Composable orchestration.** A `Provider` protocol turns "which agent answered" into an implementation detail. The fleet is itself a provider — UI doesn't need to know.

## Three providers

| Provider   | What it does                                                                                              | Auth                                              |
| :--------- | :-------------------------------------------------------------------------------------------------------- | :------------------------------------------------ |
| `claude`   | Spawns the `claude` CLI via `claude-agent-sdk`. Streams text deltas (token-level) and tool-use events.    | `claude login` (OAuth, on host)                   |
| `opencode` | Talks to `opencode serve` over HTTP + SSE. Sends model as `{providerID, modelID}`.                        | `opencode auth login` (OAuth, on host)            |
| `fleet`    | Decomposes a prompt into Planner / Developer / Coder / Reviewer steps and runs them through the other two. | Config file + the underlying providers' auth      |

See [docs/fleet.md](docs/fleet.md) for the fleet design and [docs/fleet-config.md](docs/fleet-config.md) for the configuration UX.

## Layout

| Path                                                                                          | Role                                                              |
| :-------------------------------------------------------------------------------------------- | :---------------------------------------------------------------- |
| [setup.sh](setup.sh)                                                                          | One-shot bring-up + `login` / `stop` / `down` / `status` / `logs` |
| [docker-compose.yml](docker-compose.yml)                                                      | Postgres (OpenCode runs on the host)                              |
| [pyproject.toml](pyproject.toml)                                                              | Backend deps                                                      |
| [.env.example](.env.example)                                                                  | Settings template (model catalog, defaults)                       |
| [.localcode/fleet.yaml](.localcode/fleet.yaml)                                                | Active fleet config — picks role → provider → model               |
| [.localcode/fleet.yaml.example](.localcode/fleet.yaml.example) / [.json.example](.localcode/fleet.json.example) | Drop-in starters                                                  |
| [backend/app/orchestrator/base.py](backend/app/orchestrator/base.py)                          | `Provider` protocol + unified `Event`                             |
| [backend/app/orchestrator/claude.py](backend/app/orchestrator/claude.py)                      | Claude SDK adapter (partial-message streaming, native auth)       |
| [backend/app/orchestrator/opencode.py](backend/app/orchestrator/opencode.py)                  | OpenCode HTTP/SSE adapter                                         |
| [backend/app/orchestrator/fleet.py](backend/app/orchestrator/fleet.py)                        | Multi-agent fleet (Proposal G — shipped)                          |
| [backend/app/routes/sessions.py](backend/app/routes/sessions.py)                              | REST + WebSocket chat, `_safe_run` wrapper                        |
| [backend/app/routes/fleet.py](backend/app/routes/fleet.py)                                    | `GET /api/fleet/config` for inspection                            |
| [frontend/src/components/ChatPane.tsx](frontend/src/components/ChatPane.tsx)                  | Streaming chat UI with WS auto-reconnect                          |
| [docs/](docs/)                                                                                | Fleet, orchestration proposals, fleet-config usability            |

## Setup

```bash
./setup.sh                # check deps, bring up postgres, install opencode on host,
                          # create DB schema, start backend + frontend
./setup.sh login          # one-time browser-based: claude login + opencode auth login
```

Open <http://localhost:5173>, pick a model from the dropdown (try **`fleet:default`** first), hit **+ New chat**, and start typing. ⌘+↵ to send.

Other subcommands: `./setup.sh status` / `logs` / `stop` / `down`.

## How model selection works

`MODEL_CATALOG` in `.env` is a comma-separated list of `provider:model` pairs. Each appears in the UI's model picker; whichever you pick at chat-creation pins the session. Three provider prefixes are valid:

- **`claude:<model>`** — `model` is the Anthropic model name (e.g. `claude-sonnet-4-6`). The spawned `claude` CLI uses your `claude login` OAuth token.
- **`opencode:<provider>/<model>`** — e.g. `opencode:openai/gpt-5.4-mini`. OpenCode resolves credentials from its own auth store; for ChatGPT models you need `opencode auth login` → OpenAI.
- **`fleet:<config>`** — e.g. `fleet:default`. The model name selects which fleet config to use; only `default` ships out of the box. The actual models invoked come from the fleet config.

Run `~/.opencode/bin/opencode models` to see what your ChatGPT subscription exposes after login (typically GPT-5.x family + a handful of free OpenCode-hosted models).

## Auth notes

Both providers authenticate via host-side OAuth: the spawned `claude` CLI reads its token from `~/.claude/`, and `opencode serve` reads its token from `~/.local/share/opencode/auth.json`. Run `./setup.sh login` once to mint both; tokens auto-refresh thereafter.

> **Heads up:** Anthropic blocked Claude OAuth tokens for *third-party* tools in early 2026. Native auth works only because the agent we spawn is the official `claude` CLI itself. Don't try to forward those tokens elsewhere.

## Cost reporting

Per-turn cost (USD) is reported by each provider in the `assistant.done` event and rendered inline at the bottom of every assistant turn. There is no daily aggregate — under OAuth-only mode that figure would always be a subscription, not a metered spend.

## Documentation

- [docs/fleet.md](docs/fleet.md) — fleet design, role semantics, event protocol.
- [docs/fleet-config.md](docs/fleet-config.md) — fleet configuration UX, recipes, troubleshooting.
- [docs/orchestration-proposals.md](docs/orchestration-proposals.md) — design exploration for multi-backend orchestration; status flags marking what shipped.

## What's next (good first issues)

- **Per-turn model switching in the UI.** The model picker still pins at chat creation. Surface a per-message override and a `/use <provider>:<model>` slash command (Proposal A in the orchestration doc).
- **Daily token meter.** Sum the tokens from each `assistant.done` over the day and render a per-day usage bar.
- **Multi-turn Claude session reuse.** Switch from one-shot `query()` to `ClaudeSDKClient` and persist the upstream session id.
- **Forward sub-step tool events.** Today the fleet's coder/developer steps stream their tool-use cards internally but the UI only sees the final text — wrap them with a `step_id` envelope.
- **Auto-retry on reviewer NACK.** Surface NACKs as a re-run on the failing step.
- **Alembic migrations.** `db_init.py` uses `metadata.create_all`.
