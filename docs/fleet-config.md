# Configuring the Fleet

The fleet ([docs/fleet.md](fleet.md)) runs four roles — **Planner**, **Developer**, **Coder**, **Reviewer** — each with its own `{provider, model, system_prompt}`. There are three layers of configuration, applied in this order (later layers override earlier ones):

| Layer            | Where                                                | When to use                                         |
| :--------------- | :--------------------------------------------------- | :-------------------------------------------------- |
| Built-in defaults | code in [fleet.py](../backend/app/orchestrator/fleet.py) | Always available — no setup required                |
| File config       | `.localcode/fleet.{yaml,yml,json}` (or env override) | Per-project, version-controllable                   |
| **UI override**   | Modal that pops up when you click **+ New chat** with a fleet model selected | Per-session, stored on the session row in the DB    |

> If you just want it to work: don't write any config. The built-in defaults assume you've run `./setup.sh login` and use Claude (Sonnet/Opus/Haiku) + OpenCode (gpt-5.3-codex). Pick `fleet:default` in the model dropdown and send a prompt.

---

## Quick start: UI override (no files)

1. Pick `fleet:default` in the model dropdown.
2. Click **+ New chat**.
3. The **Configure Fleet** modal opens. It shows the current effective config (built-in defaults plus any file-level overrides) and lets you change provider/model per role inline. Roles you didn't touch keep inheriting whatever the layer below provided.
4. Click **Start chat** — the override is saved on the session and applied on every subsequent turn for that session. New chats start fresh from the file/built-in defaults.

The chat header shows role chips like `PLANNER claude:claude-sonnet-4-6` per turn — anything you overrode is highlighted in accent purple, with a tooltip noting "(UI override)".

For project-wide changes (every chat in a repo) prefer the file path below. The UI is for one-off "just for this chat" tweaks.

## Quick start: file config (60 seconds)

```bash
cp .localcode/fleet.yaml.example .localcode/fleet.yaml
$EDITOR .localcode/fleet.yaml
# Edit the role you care about, save. Next prompt picks it up — no restart.
```

To verify your edit landed:

```bash
curl -fsS http://localhost:8080/api/fleet/config | jq .config_source
# "/Users/you/Projects/LocalCode/.localcode/fleet.yaml"
```

---

## Where to put the file

The fleet looks for a config in this order, **first hit wins**:

| Order | Source                                                        | When to use                                          |
| :---- | :------------------------------------------------------------ | :--------------------------------------------------- |
| 1     | `$LOCALCODE_FLEET_CONFIG` (absolute path, YAML or JSON)       | CI, dotfile mgmt, sharing one config across projects |
| 2     | `<chat session cwd>/.localcode/fleet.{yaml,yml,json}`         | Project-local — different fleet per repo            |
| 3     | `<orchestrator cwd>/.localcode/fleet.{yaml,yml,json}`         | The default for `./setup.sh` users                   |
| —     | built-in defaults                                             | If none of the above resolves                        |

```bash
# Override for one shell:
export LOCALCODE_FLEET_CONFIG=~/dotfiles/localcode/cheap-fleet.yaml
```

---

## YAML or JSON?

Either. The loader detects format from the file extension. **Pick YAML** if you'll override `system_prompt`s — multiline strings are nicer. **Pick JSON** if you're generating the config from a tool or hate YAML's whitespace rules.

```yaml
# fleet.yaml
roles:
  coder:
    provider: opencode
    model: openai/gpt-5.3-codex
    system_prompt: |
      You are the Coder. Always run `pytest -q` after edits.
      Use `ruff format` on Python you touch.
```

```json
{
  "roles": {
    "coder": {
      "provider": "opencode",
      "model": "openai/gpt-5.3-codex",
      "system_prompt": "You are the Coder. Always run `pytest -q` after edits."
    }
  }
}
```

---

## Field reference

```text
name:        string   — informational; shown in /api/fleet/config
max_steps:   int      — hard cap on plan length (default 6, min 1)
roles:
  planner:   { provider, model, system_prompt }
  developer: { provider, model, system_prompt }
  coder:     { provider, model, system_prompt }
  reviewer:  { provider, model, system_prompt }
```

| Field           | Type   | Valid values                                                 | What happens if missing / invalid                                       |
| :-------------- | :----- | :----------------------------------------------------------- | :----------------------------------------------------------------------- |
| `provider`      | string | `claude` or `opencode`                                       | Reverts to the default provider for that role; logs a warning            |
| `model`         | string | Anything the provider can resolve. See "Picking models" below | Reverts to the default model for that role; logs a warning              |
| `system_prompt` | string | Any non-empty string                                         | Inherits the built-in role prompt (which already enforces JSON / no-code rules) |

**Partial overrides are fine.** Anything you omit inherits the default. So this is valid:

```yaml
roles:
  coder:
    model: openai/gpt-5.4   # override one field, keep provider + prompt
```

---

## Picking models

Available models depend on which provider you pick.

**Claude.** Anything the SDK accepts: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`. Spend hits your Claude Pro / Max subscription; rate-limit windows apply.

**OpenCode.** Run this on the host to see what your `opencode auth login` exposes:

```bash
~/.opencode/bin/opencode models
# openai/gpt-5.5-pro
# openai/gpt-5.5
# openai/gpt-5.5-fast
# openai/gpt-5.4
# openai/gpt-5.4-mini
# openai/gpt-5.4-mini-fast
# openai/gpt-5.3-codex
# openai/gpt-5.3-codex-spark
# opencode/big-pickle               ← free / unlimited tier
# opencode/hy3-preview-free
# opencode/minimax-m2.5-free
# opencode/nemotron-3-super-free
```

Format the value as `<providerID>/<modelID>` exactly as printed.

---

## Recipes

### All-Claude (no OpenAI subscription)

```yaml
name: all-claude
roles:
  planner:   { provider: claude, model: claude-sonnet-4-6 }
  developer: { provider: claude, model: claude-opus-4-7   }
  coder:     { provider: claude, model: claude-sonnet-4-6 }
  reviewer:  { provider: claude, model: claude-haiku-4-5  }
```

### Free (no subscription cost on either side)

```yaml
name: free-tier
roles:
  planner:   { provider: opencode, model: opencode/big-pickle }
  developer: { provider: opencode, model: opencode/big-pickle }
  coder:     { provider: opencode, model: opencode/big-pickle }
  reviewer:  { provider: opencode, model: opencode/big-pickle }
```

Quality won't match Opus, but it's $0.

### Codex-led (Coder is the bottleneck, not the Planner)

```yaml
name: codex-led
max_steps: 4
roles:
  planner:   { provider: claude,   model: claude-haiku-4-5      }   # cheap, fast plans
  developer: { provider: claude,   model: claude-sonnet-4-6     }
  coder:     { provider: opencode, model: openai/gpt-5.3-codex  }
  reviewer:  { provider: claude,   model: claude-haiku-4-5      }
```

### Opus-everything (max quality, slow)

```yaml
name: opus-everywhere
roles:
  planner:   { provider: claude, model: claude-opus-4-7 }
  developer: { provider: claude, model: claude-opus-4-7 }
  coder:     { provider: claude, model: claude-opus-4-7 }
  reviewer:  { provider: claude, model: claude-opus-4-7 }
```

### Project-aware coder (override system prompt)

```yaml
roles:
  coder:
    provider: opencode
    model: openai/gpt-5.3-codex
    system_prompt: |
      You are the Coder for the LocalCode repo.
      - Backend lives at backend/app/. Use FastAPI conventions.
      - After Python edits, run `ruff check . && pytest -q`.
      - For TypeScript, follow the patterns in frontend/src/components.
      - Never edit pyproject.toml or package.json without an explicit ask.
      End every response with a "Changes:" summary of files touched.
```

The default coder prompt is replaced entirely — the role's job (don't add features, run tools, end with a Changes summary) needs to remain in your override or the role will drift.

---

## Live editing workflow

**File config:**
1. Edit `.localcode/fleet.yaml`.
2. Send the next prompt in the UI.
3. The fleet reloads the file at the start of every turn.
4. Hit `GET /api/fleet/config` to confirm what's active.

No backend restart required. Malformed YAML logs a warning and falls back to defaults; the chat still works.

**UI override:**
1. Open the editor when creating a new chat (it auto-opens on fleet provider selection).
2. Tweak roles, click **Start chat**.
3. The override is stored on the session row (`sessions.fleet_config_override`) — it's persisted across backend restarts and applies for every turn in that session.
4. To change a session's override later: delete the chat and create a new one. Per-turn override editing is on the to-do list.

The two layers compose: file config sets the project default, UI override tweaks one chat. Roles you didn't touch in the UI inherit from the file (or built-in if no file).

---

## Inspecting the active config

```bash
curl -fsS http://localhost:8080/api/fleet/config | jq
```

```json
{
  "config": {
    "name": "localcode-default",
    "planner":   { "provider": "claude",   "model": "claude-sonnet-4-6", "system_prompt": "..." },
    "developer": { "provider": "claude",   "model": "claude-opus-4-7",   "system_prompt": "..." },
    "coder":     { "provider": "opencode", "model": "openai/gpt-5.3-codex", "system_prompt": "..." },
    "reviewer":  { "provider": "claude",   "model": "claude-haiku-4-5",  "system_prompt": "..." },
    "max_steps": 4,
    "config_source": "/Users/you/Projects/LocalCode/.localcode/fleet.yaml"
  },
  "is_default": false,
  "valid_providers": ["claude", "opencode"],
  "valid_roles": ["planner", "developer", "coder", "reviewer"],
  "defaults": { ... }
}
```

`is_default: true` means no file resolved and built-ins are active.
`config_source: null` means same as `is_default`.

---

## Troubleshooting

**"My override isn't taking effect."**

Hit `/api/fleet/config` — the `config_source` field tells you which file was loaded. If it's `null`, the loader didn't find your file. Most common causes:

- File at `<some-other-cwd>/.localcode/fleet.yaml` instead of the orchestrator's cwd → set `LOCALCODE_FLEET_CONFIG=/abs/path` to override.
- Wrong filename: `fleet.YAML` (case matters on some filesystems), `fleet.yml.example`, etc.
- You edited `.example` instead of the real file.

**"The fleet loaded but my role config didn't apply."**

Check the backend log (`./setup.sh logs` or `.run/backend.log`) for warnings like:

```
fleet config: 'roles.coder.provider'='openai' invalid (allowed: ('claude', 'opencode')); using default 'opencode'
```

`provider` must be `claude` or `opencode` — not `openai` or a model id.

**"Planning fails every turn."**

The planner has to return strict JSON. Some models (especially weaker ones) leak prose around the JSON block. The loader extracts the first balanced `{...}` so leading prose is OK, but if the JSON itself is malformed the fleet falls back to a single coder step on the raw prompt.

If that's happening repeatedly, switch the planner to a stronger instruction-follower (Sonnet / Opus / gpt-5.5) and avoid Haiku / opencode/big-pickle as planner.

**"Reviewer keeps NACKing because the file doesn't exist."**

The Coder tool-calls (Edit / Write) happen inside the sub-provider, but they need permission to write. For OpenCode in `serve` mode this isn't always granted automatically. Track this issue in [docs/fleet.md#limitations-v1](fleet.md). Workaround: set the Coder to a Claude provider for any step that needs file edits — Claude's `permission_mode="acceptEdits"` is honored.

**"The final answer is the developer's design, not the coder's code."**

`_final_summary` falls back to the last developer output when the coder produced empty text — usually because the coder's work was all tool calls and emitted no narrative. This is annoying but informative: it's telling you the coder ran but didn't surface a "Changes:" summary. Either tighten the coder's system prompt to *always* end with a textual summary, or wait for sub-step event forwarding (see `What's next` in [README.md](../README.md)).

---

## Related

- [docs/fleet.md](fleet.md) — fleet design, role semantics, event protocol.
- [.localcode/fleet.yaml.example](../.localcode/fleet.yaml.example) — annotated YAML starter.
- [.localcode/fleet.json.example](../.localcode/fleet.json.example) — JSON starter.
- [backend/app/orchestrator/fleet.py](../backend/app/orchestrator/fleet.py) — config loader, validator, runner.
- [frontend/src/components/FleetConfigEditor.tsx](../frontend/src/components/FleetConfigEditor.tsx) — UI modal that emits the per-session override.
