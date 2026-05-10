# Configuring the Fleet

The fleet ([docs/fleet.md](fleet.md)) is an **agent registry** — a named set of specialist agents the orchestrator can dispatch. Available agents:

| Agent       | Job                                                                       |
| :---------- | :------------------------------------------------------------------------ |
| **Planner**   | Produces a Markdown implementation plan, persisted to `.localcode/plans/`. |
| **Developer** *(optional)* | Extra design pass before the coder. No code is written. |
| **Coder**     | Executes the plan task-by-task using file-edit and bash tools. |
| **Reviewer**  | Verifies plan compliance + code quality. Returns `LGTM` or `NACK`. |
| **Tester**    | Final gate. Writes + runs tests. Returns `LGTM`, `NACK_CODE`, or `NACK_TESTS`. |

> **Registry IS the workflow.** Only agents you include are eligible for dispatch. Adding or removing one literally adds or removes a key — there's no "disabled" flag to keep in sync. The orchestrator reads the registry, decides which to dispatch, and in what order.

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

| Preset                    | Members                                          | Best for                                              |
| :------------------------ | :----------------------------------------------- | :---------------------------------------------------- |
| **Full crew**             | planner + coder + reviewer + tester              | Hard, multi-phase tasks                               |
| **Plan + code + review + test** | planner + coder + reviewer + tester        | Same as Full crew; explicit name                      |
| **Plan + code + test**    | planner + coder + tester                         | Skip the review gate — tester is the only check       |
| **Plan + code**           | planner + coder                                  | Concrete tasks where review + tests would be overkill |
| **Design + code**         | planner + developer + coder                      | Architectural ambiguity; no review needed             |
| **Design only**           | developer                                        | Produce a design doc, no code                         |
| **Code + review**         | planner + coder + reviewer                       | Mechanical work with correctness verification         |
| **Code only**             | coder                                            | Single-shot — fastest                                 |
| **Plan only**             | planner                                          | "Show me a plan" — no execution                       |
| **Review only**           | reviewer                                         | Paste content into the prompt for an LGTM/NACK pass   |

4. Optionally toggle individual agent cards or change provider/model per agent.
5. Configure the meta-fields:
   - **Max steps** *(legacy, advisory only)* — a hint that propagates through the registry.
   - **Max review retries** *(visible when reviewer is in the workflow)* — the budget the orchestrator uses for NACK retry chains. The orchestrator's system prompt is told this number.
   - **Require plan approval (HITL)** *(visible when planner is in the workflow)* — when on, the orchestrator's system prompt instructs it to call `request_plan_approval` after the planner. The chat shows an Approve/Reject card; rejection ends the turn.
6. **Start chat.** The choice is saved on the session.

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
curl -fsS http://localhost:8080/api/fleet/config | jq .config.config_source
# "/Users/you/Projects/LocalCode/.localcode/fleet.yaml"
```

---

## File config schema

```yaml
name: my-workflow                # informational
max_steps: 6                     # advisory hint, propagates to the orchestrator
max_review_retries: 3            # NACK retry budget the orchestrator self-bounds
require_plan_approval: false     # HITL gate after the planner
entry_role: coder                # which role runs first when there's no planner

roles:                           # REGISTRY — only these agents are eligible to dispatch
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
  # developer absent → not in this workflow
```

| Field                  | Required | Notes                                                                 |
| :--------------------- | :------- | :-------------------------------------------------------------------- |
| `name`                 | no       | Informational                                                          |
| `max_steps`            | no       | 1–12; default 6. Advisory — the orchestrator interprets it.           |
| `max_review_retries`   | no       | 0–5; default 1. The orchestrator's NACK retry budget.                 |
| `require_plan_approval`| no       | bool; default false. HITL gate after the planner.                     |
| `entry_role`           | no       | Must be a key in `roles`. Default: `coder` if present, else first role |
| `roles`                | yes      | Mapping `<role> → {provider, model, system_prompt?}`                   |
| `roles.<role>.provider`| no       | `claude` or `opencode`. Default: from role library                    |
| `roles.<role>.model`   | no       | Free-form model id. Default: from role library                        |
| `roles.<role>.system_prompt` | no | Override the role's default prompt                              |

**Per-field defaulting:** within a present role, fields fall back to the **role library** (the built-in default per role). So `coder: { model: openai/gpt-5.5-mini }` is enough — provider and system_prompt keep their defaults.

**Role removal:** delete the key. There's no "enabled: false" flag to set.

**Empty workflow:** if `roles:` is empty or missing, the loader falls back to defaults (planner + coder + reviewer + tester). Validation is permissive — typos in one role drop that role with a warning rather than failing the whole load.

---

## Picking models

**Claude.** Anything the SDK accepts: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`. Spend hits your Claude Pro / Max subscription; rate-limit windows apply.

**OpenCode.** Run on the host to see what `opencode auth login` exposes:

```bash
~/.opencode/bin/opencode models
# openai/gpt-5.5-pro, openai/gpt-5.4-mini, openai/gpt-5.3-codex,
# opencode/big-pickle (free), opencode/minimax-m2.5-free, ...
```

Format the value as `<providerID>/<modelID>` exactly as printed.

**Cost-aware defaults.** The shipped role library reflects an "expensive thinking, cheap doing" split:

| Role     | Provider:Model                  | Why                                                                   |
| :------- | :------------------------------ | :-------------------------------------------------------------------- |
| planner  | `claude:claude-opus-4-7`        | Plan quality dominates downstream success. Worth the cost once.       |
| developer| `claude:claude-sonnet-4-6`      | Optional design pass; mid-tier reasoning.                             |
| coder    | `opencode:openai/gpt-5.3-codex` | Mechanical execution — a code-tuned model on a cheap subscription.    |
| reviewer | `claude:claude-sonnet-4-6`      | LGTM/NACK gate needs real judgement.                                  |
| tester   | `claude:claude-haiku-4-5`       | Writes + runs tests; doesn't need deep reasoning.                     |
| **(orchestrator)** | `claude:claude-sonnet-4-6` (DEFAULT_ORCHESTRATOR_MODEL) | Meta-routing decisions — sonnet is plenty smart at ~5× cheaper than opus. |

---

## How a turn flows

**Single path: orchestrator-as-agent.** Whatever's in the registry, the orchestrator handles it.

1. Your prompt + the registry go to the orchestrator (claude-agent-sdk session with the dispatch MCP server registered).
2. The orchestrator's ReAct loop calls `dispatch_subagent(name, prompt)` to delegate. Each dispatch:
   - Runs the named subagent via [_run_step_with_role](../backend/app/orchestrator/fleet.py) (heartbeats, per-step timeout, gate classifier).
   - Streams per-subagent events to the WS in real time via an `EventSink` queue.
   - Returns the subagent's final text to the orchestrator.
3. The orchestrator reads the result and decides next: dispatch another agent, retry on NACK, or summarise + stop.
4. **HITL** *(when `require_plan_approval` is set)*: the orchestrator calls `request_plan_approval` after the planner; the chat shows an Approve/Reject card; the user's decision feeds back as the tool result.

For trivial tasks the orchestrator's system prompt instructs it to skip the planner and dispatch the coder directly.

For details on the dispatch loop, MCP tool wiring, and event-stream merging, see [docs/architecture.md](architecture.md).

---

## Inspecting active config

```bash
curl -fsS http://localhost:8080/api/fleet/config | jq
```

```json
{
  "config": {
    "name": "default",
    "roles": {
      "planner":   { "provider": "claude",   "model": "claude-opus-4-7", "system_prompt": "..." },
      "coder":     { "provider": "opencode", "model": "openai/gpt-5.3-codex", "system_prompt": "..." },
      "reviewer":  { "provider": "claude",   "model": "claude-sonnet-4-6", "system_prompt": "..." },
      "tester":    { "provider": "claude",   "model": "claude-haiku-4-5", "system_prompt": "..." }
    },
    "entry_role": "coder",
    "max_steps": 6,
    "max_review_retries": 1,
    "require_plan_approval": false,
    "config_source": "/Users/you/Projects/LocalCode/.localcode/fleet.yaml"
  },
  "is_default": false,
  "valid_providers": ["claude", "opencode"],
  "valid_roles": ["planner", "developer", "coder", "reviewer", "tester"],
  "presets":      { "full": {...}, "plan-and-code": {...}, "code-only": {...}, ... },
  "role_library": { "planner": {...}, "developer": {...}, "coder": {...}, "reviewer": {...}, "tester": {...} },
  "defaults":     { ... }
}
```

`is_default: true` → no file resolved, built-ins are active.
`presets` → preset chips the UI offers.
`role_library` → defaults the UI uses to pre-fill a newly-added role card.

---

## Troubleshooting

**"My override isn't taking effect."** Hit `/api/fleet/config` and look at `config.config_source`. If it's `null`, the loader didn't find your file. Most common causes:

- File at a different cwd than the orchestrator's → set `LOCALCODE_FLEET_CONFIG=/abs/path` to override.
- Wrong filename: case matters on some filesystems; `fleet.YAML.example` won't load.
- You edited `.example` instead of the real file.

**"My role config didn't apply."** Check the backend log for warnings like:

```
fleet config: 'roles.coder.provider'='openai' invalid (allowed: ('claude', 'opencode')); using default 'opencode'
```

`provider` must be `claude` or `opencode` — not a model id.

**"The reviewer NACKed because nothing was implemented."** The coder agent returned narrative without using tools. The orchestrator should detect this and re-dispatch with an explicit "you MUST use file-edit + bash tools" preface. If it doesn't, the coder's system prompt may have been overridden — re-check the role config.

**"The reviewer says LGTM but the work is wrong."** The reviewer's system prompt requires it to inspect files on disk (it's instructed to use `ls` / `cat` / `git status` to verify the coder's claims). If your override removed those instructions, restore the default prompt.

**"Approval card never shows."** Confirm `require_plan_approval: true` is in your config (`/api/fleet/config | jq .config.require_plan_approval`). If it's set, the orchestrator's system prompt has the HITL block injected; check that the planner is also in your registry (HITL is meaningless without one).

**"Workflow keeps NACKing in a loop."** The orchestrator self-bounds via `max_review_retries`. After the budget hits, it should emit a final summary describing the terminal state. If you're seeing endless retries, verify the orchestrator model has access to the dispatch MCP tool (`/api/fleet/config` is unaffected by this; check the chat for the `mcp__fleet_dispatch__*` tool_use cards).

---

## Related

- [docs/fleet.md](fleet.md) — fleet architecture, role semantics, event protocol.
- [docs/architecture.md](architecture.md) — deep technical reference (orchestrator + dispatch + event flow).
- [.localcode/fleet.yaml.example](../.localcode/fleet.yaml.example) — annotated YAML starter.
- [.localcode/fleet.json.example](../.localcode/fleet.json.example) — JSON starter.
- [backend/app/orchestrator/fleet.py](../backend/app/orchestrator/fleet.py) — config loader, validator, runner.
- [frontend/src/components/FleetConfigEditor.tsx](../frontend/src/components/FleetConfigEditor.tsx) — UI modal that emits the per-session override.
