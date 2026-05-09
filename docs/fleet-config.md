# Configuring the Fleet

The fleet ([docs/fleet.md](fleet.md)) is a **workflow** — an ordered set of specialist agents that handle one user prompt. Available agents:

| Agent       | Job                                                                       |
| :---------- | :------------------------------------------------------------------------ |
| **Planner**   | Decomposes the request into ordered steps. Outputs JSON.                  |
| **Developer** | Designs the approach for a step — interfaces, files, edge cases. No code. |
| **Coder**     | Implements the step. Edits files, runs bash.                              |
| **Reviewer**  | Gates the previous step (LGTM / NACK + reason).                           |

> **Workflow IS its agents.** Only the agents you include in a workflow run. There is no "disabled" state — adding or removing an agent literally adds or removes a key. This eliminates a class of bugs where a stale "disabled" flag could let an agent leak through.

Three configuration layers, applied in order (later overrides earlier):

| Layer            | Where                                                | When to use                                         |
| :--------------- | :--------------------------------------------------- | :-------------------------------------------------- |
| Built-in defaults | code in [fleet.py](../backend/app/orchestrator/fleet.py) | Always available — full crew, no setup required    |
| File config       | `.localcode/fleet.{yaml,yml,json}` (or env override) | Per-project, version-controllable                   |
| **UI override**   | Modal that pops up when you click **+ New chat** with a fleet model selected | Per-session, stored on the session row in the DB    |

---

## Quick start: presets in the UI

1. Pick `fleet:default` in the sidebar.
2. Click **+ New chat**. The **Configure Fleet** modal opens.
3. Click a preset chip at the top:

| Preset             | Members                                | Best for                                              |
| :----------------- | :------------------------------------- | :---------------------------------------------------- |
| **Full crew**       | planner + developer + coder + reviewer | Hard, multi-phase tasks                               |
| **Plan + code**     | planner + coder                        | Concrete tasks where design is obvious                |
| **Design + code**   | planner + developer + coder            | Architectural ambiguity, but no review needed         |
| **Code + review**   | planner + coder + reviewer             | Mechanical work where correctness must be verified    |
| **Code only**       | coder                                  | Single-shot — fastest, no orchestration              |
| **Plan only**       | planner                                | "Show me a plan" — no execution                       |
| **Review only**     | reviewer                               | Paste content into the prompt, get LGTM/NACK         |

4. Optionally toggle individual agent cards (the `+` / `×` button) or change provider/model per agent.
5. **Start chat.** The choice is saved on the session.

The chat header shows the workflow's agents as colored chips. Anything you overrode shows a `●` indicator with a tooltip.

---

## Quick start: file config

```bash
cp .localcode/fleet.yaml.example .localcode/fleet.yaml
$EDITOR .localcode/fleet.yaml
# Add or remove role keys to shape the workflow. Save. Next prompt picks it up.
```

To verify what's active:

```bash
curl -fsS http://localhost:8080/api/fleet/config | jq .config_source
# "/Users/you/Projects/LocalCode/.localcode/fleet.yaml"
```

---

## File config schema

```yaml
name: my-workflow         # informational
max_steps: 6              # cap on plan length
entry_role: coder         # which role runs first when there's no planner

roles:                    # WORKFLOW MEMBERSHIP — only these agents run
  planner:
    provider: claude
    model: claude-sonnet-4-6
  coder:
    provider: opencode
    model: openai/gpt-5.3-codex
  # developer + reviewer absent → not in this workflow
```

| Field           | Required | Notes                                                                 |
| :-------------- | :------- | :-------------------------------------------------------------------- |
| `name`          | no       | Informational                                                          |
| `max_steps`     | no       | 1–12; default 6                                                        |
| `entry_role`    | no       | Must be a key in `roles`. Default: `coder` if present, else first role |
| `roles`         | yes      | Mapping `<role> → {provider, model, system_prompt?}`                   |
| `roles.<role>.provider` | no | `claude` or `opencode`. Default: from role library                  |
| `roles.<role>.model`    | no | Free-form model id. Default: from role library                      |
| `roles.<role>.system_prompt` | no | Override the role's default prompt                              |

**Per-field defaulting:** within a present role, fields fall back to the **role library** (the built-in default RoleConfig per role). So `coder: { model: gpt-5.4-mini }` is enough — provider and system_prompt keep their defaults.

**Role removal:** delete the key. There's no "enabled: false" flag to set.

**Empty workflow:** if `roles:` is empty or missing entirely, the loader falls back to defaults (full crew). Validation is permissive — typos in one role drop that role with a warning rather than failing the whole load.

---

## Picking models

**Claude.** Anything the SDK accepts: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`. Spend hits your Claude Pro / Max subscription; rate-limit windows apply.

**OpenCode.** Run on the host to see what your `opencode auth login` exposes:

```bash
~/.opencode/bin/opencode models
# openai/gpt-5.5-pro, openai/gpt-5.4-mini, openai/gpt-5.3-codex,
# opencode/big-pickle (free), opencode/minimax-m2.5-free, ...
```

Format the value as `<providerID>/<modelID>` exactly as printed.

---

## How a turn flows

**With planner present + at least one worker** (most workflows):
1. Planner gets the user prompt and emits `{"steps": [{"id", "role", "prompt", "depends_on"}]}`. Steps are filtered to roles present in this workflow — if you removed the developer, any developer step the planner emits is dropped with a warning.
2. Steps run linearly. Each step's prompt is augmented with `## Output of <prior step>` blocks for declared dependencies (or, by default, the immediately previous step).
3. Final reply: last `coder` output → assistant text. Falls through to last `developer` output if the workflow had no coder. Reviewer outputs are never the final answer.
4. Fallback: planner unreachable / returns garbage → run a single `entry_role` step on the raw prompt.

**Without a planner** (e.g. Code only, Review only, or planner failed):
1. The user prompt goes straight to the `entry_role` agent.
2. Its output becomes the assistant text.

---

## Inspecting active config

```bash
curl -fsS http://localhost:8080/api/fleet/config | jq
```

```json
{
  "config": {
    "name": "localcode-default",
    "roles": {
      "planner":   { "provider": "claude",   "model": "claude-sonnet-4-6", "system_prompt": "..." },
      "developer": { "provider": "claude",   "model": "claude-opus-4-7", "system_prompt": "..." },
      "coder":     { "provider": "opencode", "model": "openai/gpt-5.3-codex", "system_prompt": "..." },
      "reviewer":  { "provider": "claude",   "model": "claude-haiku-4-5", "system_prompt": "..." }
    },
    "entry_role": "coder",
    "max_steps": 6,
    "config_source": "/Users/you/Projects/LocalCode/.localcode/fleet.yaml"
  },
  "is_default": false,
  "valid_providers": ["claude", "opencode"],
  "valid_roles": ["planner", "developer", "coder", "reviewer"],
  "presets":      { "full": {...}, "plan-and-code": {...}, "code-only": {...}, ... },
  "role_library": { "planner": {...}, "developer": {...}, ... },
  "defaults":     { ... }
}
```

`is_default: true` → no file resolved, built-ins are active.
`presets` → preset chips the UI offers.
`role_library` → defaults the UI uses to pre-fill a newly-added role card.

---

## Troubleshooting

**"My override isn't taking effect."** Hit `/api/fleet/config` and look at `config_source`. If it's `null`, the loader didn't find your file. Most common causes:

- File at a different cwd than the orchestrator's → set `LOCALCODE_FLEET_CONFIG=/abs/path` to override.
- Wrong filename: case matters on some filesystems; `fleet.YAML.example` won't load.
- You edited `.example` instead of the real file.

**"The fleet loaded but my role config didn't apply."** Check the backend log for warnings like:

```
fleet config: 'roles.coder.provider'='openai' invalid (allowed: ('claude', 'opencode')); using default 'opencode'
```

`provider` must be `claude` or `opencode` — not a model id.

**"Planning fails on Code-only workflow."** It shouldn't — Code-only has no planner so planning never runs. If you see "planning failed" in your stream, the planner is in your workflow. Either remove it or pick a stronger planner model.

**"Reviewer NACKs because the file doesn't exist."** The Coder ran tool calls but lacked permission to write. For OpenCode in `serve` mode this isn't always granted. Workaround: switch the Coder to a Claude provider for steps that need file edits — Claude's `permission_mode="acceptEdits"` is honored.

---

## Related

- [docs/fleet.md](fleet.md) — fleet design, role semantics, event protocol.
- [.localcode/fleet.yaml.example](../.localcode/fleet.yaml.example) — annotated YAML starter.
- [.localcode/fleet.json.example](../.localcode/fleet.json.example) — JSON starter.
- [backend/app/orchestrator/fleet.py](../backend/app/orchestrator/fleet.py) — config loader, validator, runner.
- [frontend/src/components/FleetConfigEditor.tsx](../frontend/src/components/FleetConfigEditor.tsx) — UI modal that emits the per-session override.
