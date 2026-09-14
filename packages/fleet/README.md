# localcode-fleet

The multi-agent workflow, as a package. It was core; it is not any more —
pi ships no subagents in core and neither do we.

```bash
localcode install ./packages/fleet          # user scope
localcode install ./packages/fleet --local  # this project only
```

Installing registers one tool, `dispatch`, with the engine. The model calls
it to hand a focused task to a named subagent; each subagent is a full
`AgentSession` on its own engine, with its own context window and its own
tool limits.

## What changed from the old fleet provider

| Old | Now |
| :--- | :--- |
| A `fleet` provider selected at chat creation | An extension; any session can dispatch |
| One subprocess per dispatch (`python -m …fleet.subproc`) | A nested `AgentSession` — engines are already out of process |
| Roles enforced by prose in a 200-line system prompt | `sandbox` / `permission_mode` per role at the engine |
| Verdicts parsed from the last line (`LGTM` / `NACK:`) | A fenced JSON verdict, with the last-line form as fallback |
| Every turn ran planner → coder → reviewer | A complexity gate; simple turns cost one call |

## Configuration

`.localcode/fleet.yaml` (or `fleet.json`), unchanged in spirit:

```yaml
name: default
roles:
  planner:  { engine: claude, model: claude-opus-4-7 }
  coder:    { engine: codex,  model: gpt-5.5 }
  reviewer: { engine: claude, model: claude-sonnet-4-6, sandbox: read-only }
  tester:   { engine: claude, model: claude-haiku-4-5 }
gate:
  enabled: true        # route trivial turns to a single agent
  always_full: false   # true restores the old "every role, every turn"
```

`engine` accepts `claude`, `codex`, or anything an extension registered with
`api.register_provider`. Omit a role to drop it from the workflow.

## Commands

- `/fleet` — show the active workflow and which roles are registered
- `/fleet full <task>` — force the full crew, skipping the gate
