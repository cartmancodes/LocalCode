# LocalCode

A provider-agnostic abstraction over **Claude Code** and **OpenCode**, with a Claude-Code-like web UI, every model call funneled through **LiteLLM** for unified budgeting and local-model routing.

```
            ┌────────────────────────┐
            │   React + Vite UI      │
            │   chat / model picker  │
            │   budget bar           │
            └──────────┬─────────────┘
                       │ REST + WS
            ┌──────────▼─────────────┐
            │  FastAPI orchestrator  │
            │  ─ Provider protocol   │
            │   ├ ClaudeProvider     │  claude-agent-sdk
            │   └ OpenCodeProvider   │  opencode serve  (HTTP + SSE)
            └──────┬───────┬─────────┘
                   │       │
                   │       │ both providers route every model
                   ▼       ▼ call through one LiteLLM proxy
            ┌────────────────────────┐
            │  LiteLLM proxy (4000)  │  budgets, virtual keys, logs
            └─┬──────────┬──────────┬┘
              ▼          ▼          ▼
           Anthropic   OpenAI    Ollama (local)
```

## Why

- **One UI for two agents.** Claude Code is fast and tightly integrated; OpenCode is open-source and multi-provider. This lets you switch per chat without leaving the app.
- **One bill.** LiteLLM sits in front of every provider so the budget bar in the UI tracks *all* spend, including local Ollama (free, just shown for symmetry) and anything you wire up later.
- **Hot-swap models.** Each session pins a `provider:model` — start a turn on Sonnet, follow up on Llama 3.1 in a sibling chat.

## Layout

| Path | Role |
|---|---|
| [pyproject.toml](pyproject.toml) | Backend Python deps |
| [docker-compose.yml](docker-compose.yml) | postgres + litellm + opencode services |
| [Makefile](Makefile) | `make up`, `make backend`, `make frontend`, `make litellm-keygen` |
| [litellm/config.yaml](litellm/config.yaml) | Model registry + budget settings for the proxy |
| [opencode/opencode.json](opencode/opencode.json) | Tells OpenCode to use LiteLLM as its only provider |
| [backend/app/orchestrator/base.py](backend/app/orchestrator/base.py) | `Provider` protocol + unified `Event` |
| [backend/app/orchestrator/claude.py](backend/app/orchestrator/claude.py) | `claude-agent-sdk` adapter |
| [backend/app/orchestrator/opencode.py](backend/app/orchestrator/opencode.py) | OpenCode HTTP/SSE adapter |
| [backend/app/routes/sessions.py](backend/app/routes/sessions.py) | REST + WebSocket for chat |
| [frontend/src/App.tsx](frontend/src/App.tsx) | UI entrypoint |

## Setup

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY / OPENAI_API_KEY (omit any you don't use).

make install     # backend + frontend deps
make up          # postgres + litellm + opencode (Docker)

# Mint a virtual key with a daily budget — paste it back into LITELLM_API_KEY in .env.
make litellm-keygen BUDGET=10

make db-init     # create tables
make backend     # FastAPI on :8080
make frontend    # Vite dev server on :5173
```

Open `http://localhost:5173`, pick a model from the dropdown, hit **+ New chat**, and start typing. ⌘+↵ to send.

## How model selection works

`MODEL_CATALOG` in `.env` is a comma-separated list of `provider:model` pairs. Each is shown in the UI's model picker. When you create a chat, that pair is pinned to the session.

- `provider=claude` → backend uses `claude-agent-sdk`. By default (`CLAUDE_USE_NATIVE_AUTH=true`) the spawned `claude` CLI uses your host OAuth token from `claude login` — no API key needed. **Tradeoff:** these turns bypass LiteLLM, so they don't appear in the budget bar (you're billed against your Claude subscription). Set `CLAUDE_USE_NATIVE_AUTH=false` to force routing through LiteLLM with `ANTHROPIC_API_KEY` and put Claude spend back on the budget bar.
- `provider=opencode` → backend POSTs to `opencode serve`'s `/session/:id/prompt_async` and streams `/event` SSE. OpenCode's only provider is also LiteLLM, so the same mapping applies.

Adding a new model = one entry in [litellm/config.yaml](litellm/config.yaml), one entry in [opencode/opencode.json](opencode/opencode.json) if you want it on the OpenCode side, plus a token in `MODEL_CATALOG`.

## Budget

The budget bar polls `/api/budget`, which calls LiteLLM `/spend/logs?summarize=true` for today's UTC date. Cap is `DAILY_BUDGET_USD`. If the bar goes red (>= 80%) you're close to the cap; LiteLLM itself enforces hard limits via the virtual key's `max_budget`.

## Local-only via Ollama

```bash
ollama pull qwen2.5-coder:7b
# Pick `opencode:qwen2.5-coder-local` in the UI — calls go Ollama → LiteLLM → OpenCode → backend → UI.
```

No outbound traffic, $0 spend, same chat surface as Claude on Sonnet.

## What's next (good first issues)

- **Multi-turn for Claude provider.** Use `ClaudeSDKClient` + persistent SDK session instead of one-shot `query()`.
- **Per-model context limits.** Surface them on the model picker tooltip.
- **Auth.** Right now anyone on localhost can spend. For multi-user, mint per-user virtual keys and gate sessions.
- **Alembic migrations.** `db_init.py` uses `metadata.create_all` for now.
