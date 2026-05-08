# Fleet — multi-agent orchestration (Proposal G)

The fleet provider treats your prompt as a workflow rather than a single agent call. It decomposes the request with one model (the **Planner**) and runs the resulting steps through specialist models (**Developer**, **Coder**, **Reviewer**). The fleet itself implements the same `Provider` protocol as `claude` and `opencode`, so the UI renders it identically — each sub-step is a tool-use card you can expand.

## Roles

| Role          | Job                                                                       | Default model            |
| :------------ | :------------------------------------------------------------------------ | :----------------------- |
| **Planner**   | Decomposes user prompt into 1–6 steps; emits JSON.                        | `claude-sonnet-4-6`      |
| **Developer** | Designs the approach for a step — interfaces, files, edge cases. No code. | `claude-opus-4-7`        |
| **Coder**     | Implements the step. May use file-edit / bash tools.                      | `openai/gpt-5.3-codex`   |
| **Reviewer**  | Gates the previous step (LGTM / NACK + reason). Used sparingly.           | `claude-haiku-4-5`       |

The Planner picks `developer | coder | reviewer` per step. Developer steps emit a design only; the Coder reads it as context for the next step. Reviewer NACKs are surfaced as failed cards but do not (yet) trigger auto-retry.

## When to use it

- Multi-phase tasks (plan → design → implement → check).
- Cost optimization: keep Opus / Sonnet on planning + design, Codex on the bulk work, Haiku on review.
- Anytime you'd otherwise hand-prompt one agent to "first plan, then code, then review."

When **not** to use it: short factual questions, one-shots, or tasks where you'd rather keep the conversational thread inside one model. Fleet adds 1–3 LLM hops and is slower on trivial prompts.

## Configuring (YAML or JSON)

Drop a config at `.localcode/fleet.yaml` (or `.json` / `.yml`) in your project. Resolution order, first hit wins:

1. `$LOCALCODE_FLEET_CONFIG` — absolute path override (YAML or JSON, detected by extension).
2. `<cwd>/.localcode/fleet.{yaml,yml,json}`
3. `<orchestrator-cwd>/.localcode/fleet.{yaml,yml,json}`

Every field is optional — anything you omit inherits the default. Invalid fields (bad provider name, empty model) log a warning and revert to the default for that field, so a typo in one role doesn't sink the whole fleet.

```yaml
# .localcode/fleet.yaml
name: opus-led
max_steps: 4
roles:
  planner:   { provider: claude,   model: claude-opus-4-7 }
  developer: { provider: claude,   model: claude-opus-4-7 }
  coder:     { provider: opencode, model: openai/gpt-5.3-codex }
  reviewer:  { provider: claude,   model: claude-haiku-4-5 }
```

Equivalent JSON:

```json
{
  "name": "opus-led",
  "max_steps": 4,
  "roles": {
    "planner":   { "provider": "claude",   "model": "claude-opus-4-7" },
    "developer": { "provider": "claude",   "model": "claude-opus-4-7" },
    "coder":     { "provider": "opencode", "model": "openai/gpt-5.3-codex" },
    "reviewer":  { "provider": "claude",   "model": "claude-haiku-4-5" }
  }
}
```

You can also override the `system_prompt` per role to inject project conventions:

```yaml
roles:
  coder:
    provider: opencode
    model: openai/gpt-5.3-codex
    system_prompt: |
      You are the Coder. Always run `pytest -q` after edits and paste the
      result. Use `ruff format` on any Python you touch.
```

Config is loaded **per turn**, so editing the file takes effect on the next prompt without a backend restart.

## Inspecting the active config

```bash
curl -fsS http://localhost:8080/api/fleet/config | jq
```

Returns:

```json
{
  "config": { "name": "...", "planner": {...}, "developer": {...}, ... },
  "is_default": false,
  "config_source": "/Users/you/Projects/foo/.localcode/fleet.yaml",
  "valid_providers": ["claude", "opencode"],
  "valid_roles": ["planner", "developer", "coder", "reviewer"],
  "defaults": { ... }
}
```

`is_default: true` means no file was found and the built-in defaults are in effect.

## How a turn flows

1. **Plan.** Planner gets the user prompt, emits JSON: `{"steps": [{"id", "role", "prompt", "depends_on"}]}`.
2. **Execute.** Steps run linearly. Each step's prompt is augmented with `## Output of <prior step>` blocks for declared dependencies (or, if none declared, the immediately previous step).
3. **Final reply.** Last `coder` output → assistant text. If the plan was design-only, last `developer` output is used. Reviewer outputs are never the final answer.
4. **Fallback.** If planning fails (model unreachable, JSON unparseable), the fleet runs a single coder step on the raw prompt — you still get an answer.

## Event protocol

Each fleet turn produces this on the WebSocket:

```text
session.started        provider=fleet model=default
assistant.tool_use     id=fleet.plan name="planner [claude:claude-sonnet-4-6]"
tool.result            id=fleet.plan content=<bullet list of steps>
  ┌─ for each step ─┐
  assistant.tool_use   id=s1 name="developer [claude:claude-opus-4-7]"
  tool.result          id=s1 content=<design / Approach: ...>
  assistant.tool_use   id=s2 name="coder [opencode:openai/gpt-5.3-codex]"
  tool.result          id=s2 content=<changes summary>
  └─────────────────┘
assistant.text         <final answer = last coder step's output>
assistant.done         duration_ms=…
```

The UI's existing tool-use cards render this without any front-end changes.

## Limitations (v1)

- **Linear plans only.** No parallel branches or DAG fan-out — `depends_on` is parsed but execution is sequential.
- **No mid-step streaming visibility.** Sub-providers stream tokens internally; we collect the full text per step before emitting it.
- **No auto-retry on reviewer NACK.** A NACK is surfaced as a failed card; the user has to ask for a re-do.
- **Per-turn state only.** Multi-turn chats don't replay prior fleet outputs into a new planner — each user message starts fresh planning.
- **Tool calls (Edit / Bash) inside a step are silent in the UI.** They still happen — the step's text output usually summarizes them.

## Source

- [backend/app/orchestrator/fleet.py](../backend/app/orchestrator/fleet.py) — provider, config loader, plan parser, runner.
- [backend/app/routes/fleet.py](../backend/app/routes/fleet.py) — `GET /api/fleet/config`.
- [backend/app/orchestrator/registry.py](../backend/app/orchestrator/registry.py) — adds `fleet` to the singleton registry.
- [.localcode/fleet.yaml.example](../.localcode/fleet.yaml.example) and [.localcode/fleet.json.example](../.localcode/fleet.json.example) — drop-in starter configs.
- [docs/orchestration-proposals.md](orchestration-proposals.md) — design rationale.
