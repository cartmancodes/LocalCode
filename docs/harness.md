# The harness

LocalCode drives three vendor coding agents through one chat surface. This
document is about the layer that makes that a single system rather than three
integrations sitting next to each other: what every provider is held to, where
each guarantee is enforced, and which tests would fail if it stopped being true.

Read [architecture.md](architecture.md) for what each module *is*. This file is
about what the harness *promises* — and what it does not.

---

## 1. The invariant

**LocalCode never reads a credential store and never holds a vendor key.**

Each vendor CLI authenticates itself: `claude login` writes an OAuth token to
`~/.claude/` (or the platform keychain), `codex login` to `~/.codex/`, and
`opencode auth login` to `~/.local/share/opencode/auth.json`. The orchestrator
spawns the CLI and never looks inside any of those files. It also never assigns
an `*_API_KEY`, `*_OAUTH_TOKEN` or `*_SESSION_KEY` from anything, and never
shells out to the keychain.

This is not a convention. `backend/app/invariants.py` is a source scanner, and
`backend/tests/test_auth_invariant.py` runs it over the whole of
`backend/app/`: a credential-store path in a string literal, an
`os.environ["ANTHROPIC_API_KEY"] = ...`, an `env={...}` kwarg carrying a key
name, or a `security find-generic-password` call each fails the suite. The
scanner exempts no file by name — including its own pattern table, which is
exempt only at the level of the specific AST nodes that hold it.

Why a static gate rather than a code-review convention: Anthropic's February
2026 terms make automating a Claude subscription through anything but the
vendor CLI a violation, and a provider that grew a credential read to "optimize
away a subprocess spawn" would look like an innocuous refactor in a diff.

---

## 2. One approval bus

Two vendors, two permission callbacks, one gate. `claude-agent-sdk` calls
`can_use_tool(tool_name, tool_input, context)` and expects a
`PermissionResultAllow` / `PermissionResultDeny` back; the codex app-server
sends an `execCommandApproval` or `applyPatchApproval` *request* over JSON-RPC
and expects `{"decision": "approved" | "denied" | ...}`. Neither shape appears
anywhere near the decision.

```
claude can_use_tool ─┐
                     ├─▶ approvals.evaluate_tool_request(name, args, policy, mode,
codex execCommand… ──┘        sink, approval_channel, timeout)
                                   │
                     permissions.decide(...) ── allow / deny / ask
                                   │
                        ask ──▶ pipeline.awaiting_approval  ──▶ the one card the UI renders
                                   │                              ▲
                        answer ◀── approval_channel ◀─────────────┘ (WS → SessionRunner)
                                   │
                        pipeline.approval_received ──▶ card cleared, on every path
```

`evaluate_tool_request` is provider-neutral by signature: a tool name, a plain
mapping, a `ToolPolicy`, a mode, the sink and the queue. It returns only
`allow` or `deny` — it resolves `ask` itself by publishing the card and waiting.
`build_can_use_tool` is a *translation* adapter and holds no policy; a branch
added there would be a branch Codex does not get, which is the failure the
split exists to prevent.

Three rules worth knowing:

- **The headless answer to `ask` is deny.** A fleet step runs in a worker
  process with no approval channel, so there is nobody to say yes. A refusal
  the model can read costs one wasted tool call; the escalation this replaced
  granted filesystem writes on every headless step.
- **One exception, keyed on "no channel attached":** exec tools
  (`Bash`/`BashOutput`/`KillBash`) are allowed headlessly when the role's own
  policy already grants exec — otherwise every `pytest` and `git diff` a fleet
  role runs would refuse. This lives in the gate, not in `decide`'s
  `acceptEdits` branch, because `ctx.role` is unset for interactive sessions too
  and a mode-keyed version would auto-approve shell commands in a plain chat.
- **A mode is not an override.** `bypassPermissions` means "stop asking the
  human", never "ignore what this role may touch". The role gates run first.

The matrix asserts the card is byte-identical in shape across vendors:
`{id, kind, tool, input, reason, timeout_s}`, `kind == "tool"`, an id prefixed
`approval.tool.` — for a `can_use_tool` callback and an `execCommandApproval`
request alike (`backend/tests/test_matrix.py`).

---

## 3. The providers

| | `claude` | `codex` | `opencode` |
| :-- | :-- | :-- | :-- |
| Transport | `claude-agent-sdk` → `claude` CLI | JSON-RPC over stdio → `codex app-server` | HTTP + SSE → `opencode serve` |
| Auth | `claude login` | `codex login` | `opencode auth login` |
| Streaming text | yes (token deltas) | yes (item updates, suffix-only) | yes (part deltas) |
| Tool cards | yes | yes (commands, patches, MCP, web search) | yes |
| Approvals through the one bus | yes (`can_use_tool`) | yes (`execCommandApproval`, `applyPatchApproval`) | **no** — OpenCode resolves permissions itself via `opencode.json` |
| Role policy enforced | yes | yes (same `policy_for_role`) | partial — tool policy is not forwarded |
| `additional_dirs` | yes (`add_dirs`) | yes (`additionalDirectories`) | **no** — a session is bound to one project |
| Persistent session across turns | yes (one `ClaudeSDKClient` per session) | yes (one app-server per workspace, thread resumed) | yes (upstream session id) |
| Vendor-reported rate limits | yes (`RateLimitEvent`) | unverified — read if present, see `codex/protocol.py` A11 | no |
| Metered by the quota governor | yes | yes | no (locally estimated only) |

The codex column is pinned to a protocol read from documentation rather than
observed against a live binary — every assumption is listed in
`backend/app/orchestrator/codex/protocol.py` with the cost of each one being
wrong, and `make codex-schema` regenerates the vendor schema for
reconciliation. See [codex.md](codex.md).

---

## 4. Persistent sessions and prompt-prefix discipline

The Claude provider holds **one connected `ClaudeSDKClient` per LocalCode
session**, reused across turns. The one-shot `query()` this replaced spawned a
fresh CLI per message: startup cost every turn, and a cold server-side prompt
cache every turn — a long system prompt plus a growing transcript re-read at
full price.

Two rules keep the reuse honest, and both are about the prompt *prefix*:

- **Rebuild, never mutate.** A client is discarded and rebuilt when the model,
  cwd, extra dirs, system prompt, permission mode or tool set changes
  (`claude._signature`). Mutating a live client's prompt prefix or tool list is
  what invalidates the cache the design exists to keep. There is no setter call
  in the module.
- **Only a turn whose `ResultMessage` went past leaves a reusable client.**
  Every turn's messages arrive on one per-connection stream, so a turn that
  ended early leaves an unread tail that the *next* turn would read first.
  "The loop ended" is not the test — `receive_response()` also returns at
  stream EOF — the result message itself is.

The same discipline is why large output is evicted rather than inlined: a
single oversized turn changes the transcript prefix for every turn after it,
so the cost of one 2 MB tool result is paid on every subsequent request.
`backend/tests/test_claude_client_reuse.py` and `test_usage.py` (cache hit
rate) are where this is observable.

---

## 5. The artifact store

`backend/app/artifacts.py`. Content-addressed text blobs under
`~/.localcode/artifacts/<sha[:2]>/<sha>.txt`, written tmp-then-`os.replace`.

`store_if_large(text, kind=..., max_bytes=...)` returns `(text_or_summary,
ref_or_None)`: under the threshold nothing is written and the text passes
through unchanged; over it, the bytes are stored once and the caller gets a
head/tail summary (2:1, the more informative start gets more room) with a
marker line naming the artifact. Budgets are counted in **bytes** with the cut
moved to a UTF-8 character boundary — a character-count budget silently assumes
one byte per character and overshoots 3-4x on CJK or emoji.

Eviction happens once, on the way out of a step (`fleet/collect.py`), never at
each consumer. The full text is fetched back exactly where a document is owed
rather than a context: `dispatch._full_output` reads the artifact to write the
plan file, because the planner's own description promises the whole plan is on
disk and the coder is told to read it.

---

## 6. The worker pool and its budgets

Fleet steps run in **separate OS processes**. This is not an optimisation:
driving a second `claude_agent_sdk.query()` from inside the orchestrator's own
MCP tool callback deadlocks the SDK (its async-generator state is
process-global, so a thread with its own loop does not help), and a child
process is also the only thing that lets the parent truly kill a wedged vendor
CLI.

The processes are **pooled** and long-lived — one spawn per
`(session, provider, model, cwd)` key — because interpreter start + SDK import
+ CLI spawn was 0.7-1.0 s per step. What is reused is the *process*; each
request builds a fresh sub-provider, so one role's context cannot leak into the
next.

Budgets, and what each bounds:

| Budget | Default | Bounds |
| :-- | :-- | :-- |
| `fleet_startup_grace_s` | 90 s | zero output from a backend → "produced NO output", abort loudly |
| `fleet_step_timeout_s` | 1200 s | a step that streams but never finishes |
| `HEARTBEAT_INTERVAL_S` | 30 s | how often the UI is told what is happening |
| `DISPATCH_HARD_FAIL_CAP` | 2 | re-dispatches of a role that hard-failed |
| `TurnBudget.max_dispatches` | `max(8, 2×roles)` | total dispatches in one turn |
| `fleet_turn_token_budget` | 0 (off) | tokens spent across a turn's sub-steps |
| `fleet_max_workers` | 4 | live worker processes |
| `fleet_worker_idle_s` | 300 s | how long an idle worker survives |

Three reclamation rules: a worker is spawned with `start_new_session=True` and
killed with `killpg` on the pgid captured at spawn (the vendor CLI is its
child, and killing the leader alone re-parents a paid CLI to `launchd`); a
step that was still *queued* is not charged against the retry cap, because it
demonstrated nothing about its backend; and each worker writes a pidfile so a
`SIGKILL`ed backend's orphans are swept on the next pool start.

The heartbeat wording is load-bearing. "Still working" is only said once the
worker has produced output; before that it is "no response yet", and a step
queued behind another says so. A comforting lie is a lie the user acts on.

---

## 7. Conditional routing

`fleet/router.py` decides how many agents a prompt deserves, with **no model
call** — regex and length only, so the decision is deterministic, free, and
printable into a log line an operator can argue with.

| Class | Test | Agents | Example |
| :-- | :-- | :-- | :-- |
| `lookup` | question-shaped, no mutation verb, short, single-step | 1, read-only by preference (`reviewer` → `coder` → `developer` → `planner`) | "How many retries does the uploader do?" |
| `simple` | exactly one mutation verb, not feature-scale, short, single-step, **not shaped like a question** | `coder` + `reviewer` | "fix the typo in README.md" |
| `standard` | everything else, including the empty prompt | the whole registered crew | "Implement retry-with-backoff, then review it" |

Three commitments, each recording a way this went wrong:

- **Ambiguity resolves upward.** Over-spending on a borderline task wastes
  tokens and is recoverable; under-planning a real implementation ships broken
  code and is not.
- **A question is never a simple change.** `MUTATION_VERBS` is deliberately
  broad and includes ordinary nouns (`handle`, `run`, `set`). Without the
  question-shape veto, "how is the retry handle passed to the worker?" read as
  "exactly one mutation verb, not asking" and was *demoted* from the full crew
  to the coder pair — no planner, and a rationale telling the model it had an
  edit to make.
- **A prompt carrying both an ask and a mutation verb is `standard`.** Two
  units of work, and collapsing them to the coder pair is the same
  under-planning.

Code is stripped before matching — fenced blocks *and* inline spans — so a
pasted diff containing `+ add_route(...)` is not read as a request to add
something. `always_full_crew` in `fleet.yaml` restores the pre-routing
behaviour verbatim, character for character.

The decision is rendered into the orchestrator's system prompt as an
instruction with both directions pinned: escalation is explicitly allowed (the
classifier can undershoot), de-escalation is explicitly forbidden.

---

## 8. The quota governor

`backend/app/quota.py`. Per-turn cost in USD bills nobody under a
subscription; the number that actually steers work is **how much of each plan's
window is left**, and that is what the governor keeps.

- **Windows are a table, not a branch.** Claude gets a 5-hour window; Codex
  gets a 5-hour window *and* a weekly cap. A provider with no row still gets
  one rather than silently having no ledger.
- **Two sources in strict precedence.** A vendor-reported payload (Claude's
  `RateLimitEvent`, whatever Codex's `turn/completed` carries) replaces
  `used`/`limit`/`resets_at` and sets `confidence="reported"`. Absent that,
  tokens accumulate locally against an unknown limit and `headroom()` reads
  `1.0` with `confidence="unknown"` — the meter must not draw a full bar it
  never measured.
- **One writer.** `quota.json` is a read-modify-write file, so only the main
  server process records: `session_runner/turn.py` for a direct turn and
  `orchestrator/dispatch.py` for a fleet sub-step. Providers *emit*; they never
  write.
- **`provider: "auto"`** in a role's config is resolved at dispatch time by
  remaining headroom, never by a branch on a provider name. Below
  `QUEUE_THRESHOLD` (5 %) the governor refuses rather than silently downgrading
  to a spent subscription.

Surfaced at `GET /api/system/quota` and drawn by the meter in `Topbar.tsx`.

### Known limitations — true today, stated rather than fixed

- **`"auto"` is accepted in `fleet.yaml` but is not offered by the fleet
  editor's provider dropdown.** It works when written by hand; the UI simply
  does not list it yet.
- **The fleet orchestrator's own model loop is unmetered.** Its
  `assistant.done` carries no `usage`, so the tokens its planning loop spends
  are invisible to the governor. Sub-step tokens *are* metered, via
  `StepResult.usage`.
- **A `quota.limit` observed inside a fleet WORKER is lost.** The worker's only
  channels to the parent are `@@FIRST@@` and a framed `StepResult`, and
  `collect.py` consumes the event stream in the child with no branch for it.
  Relaying it would mean a third marker on the wire protocol. Because
  rate-limit state is per-account, the next transition the main process sees —
  a direct turn, or the orchestrator's own loop, which *does* read them —
  supersedes it. **Claude headroom is therefore measured from the main-process
  loops only.**

---

## 9. The three evaluation layers

Everything above is a claim. These are what make the claims falsifiable.

### Replay — `backend/tests/test_replay.py`, fixtures in `backend/tests/replay/`

Four JSON fixtures of recorded provider messages: `claude_basic`,
`claude_tools`, `codex_basic`, `codex_approval`. Each is asserted to produce an
exact ordered list of Event types, the text its deltas assemble into, tool_use
ids matched by tool_result ids, and the keys of the single terminal event —
then the same Event stream goes through the real `execute_turn` and the
assertions continue on the persisted blocks.

**The fixtures are hand-authored, not recordings.** Neither vendor CLI is
installed on the machine this was written on, and a recording made on one would
be a snapshot of one CLI build (and a paid subscription) rather than a
statement about the contract. Each file is written from the shapes the
translator consumes: for Claude the `StreamEvent` / `AssistantMessage` /
`UserMessage` / `ResultMessage` dataclasses, tagged with `__type__` and
rehydrated by `backend/tests/fakes/providers.py`; for Codex the raw JSON-RPC
item notifications, whose every wire spelling comes from `codex/protocol.py`.
What they cannot do is discover that a vendor changed its shape. What they do
is make sure *we* did not.

### Long-horizon — `backend/tests/test_long_horizon.py`

Five cases, each through the real `execute_turn` with a fake provider and a
temp storage root: a turn cancelled mid-tool (the dangling `tool_use` gets a
synthetic result, and the next turn on that session continues); approval
branching (approve, deny-with-feedback, timeout — each asserted on both what
the user saw and what the model was told); a subagent handoff where the plan
exceeds the inline budget and the coder gets a pointer that resolves; a wedged
backend that produces nothing; and a 2 MB tool result that must not reach the
orchestrator's context.

### Golden traces — `backend/tests/test_golden_traces.py`, goldens in `backend/tests/golden/`

A whole fleet turn, reduced to everything observable about it: the routing
decision, per step the ordered tool calls with a stable hash of each call's
arguments and the gate's verdict on each, the approval cards with their
decisions, and the files that appeared under the temp cwd. The hash is
`sha256` of the arguments as sorted compact JSON, first 12 hex characters.

Two goldens, and together they are the regression net for the routing rules in
§7: `fleet_standard.json` (a feature request buys the full crew, the reviewer's
attempted write is refused and leaves no file) and `fleet_lookup.json` (a
question buys exactly one read-only agent out of a registry of three).

To refresh them after an intended behaviour change:

```bash
UPDATE_GOLDEN=1 .venv/bin/pytest backend/tests/test_golden_traces.py
```

…then **read the diff before committing it**. A golden regenerated without
being read is a file that agrees with whatever the code now does, which is the
one thing a golden must never be.

### The matrix — `backend/tests/test_matrix.py`

`{claude, codex} × {single, fleet}`, every cell asserted against the same four
invariants: an ordered event stream, exactly one terminal `assistant.done`,
persisted blocks that round-trip with their JSON types intact, and approvals
surfacing through the one bus. Plus the paging-parity assertion carried forward
from the storage work: one fixture larger than the tail window, read windowed
and read streaming, byte-identical.

### What the net does not cover

Stated here rather than hidden behind a weakened assertion:

- **`opencode` is absent from the matrix.** It speaks HTTP + SSE to a
  host-side server; standing one up would be a third fake, unlike the two that
  already exist. It is also the provider that forwards neither the tool policy
  nor `additional_dirs`, so a matrix row for it would fail several cells
  honestly rather than pass them.
- **The fleet cells do not cross the worker *process* boundary.**
  `fakes/providers.FakeWorkerPool` serves the pool's contract in-process,
  running the same `collect_step` call `fleet/subproc.py` makes in the child.
  The process boundary itself — framing, `killpg` reaping, queueing, pidfile
  sweeps — is `test_worker_pool.py`'s subject, against real subprocesses.
- **The codex fixtures agree with `protocol.py` by construction.** The fake
  app-server implements the same documented reading, so the suite cannot
  falsify a wrong guess about the vendor's schema. Only `make codex-schema` and
  a reconciliation can.
- **No test needs a vendor CLI.** The one that does is marked `requires_cli`
  and is deselected by default (`pyproject.toml`).

---

## 10. Phase 6 (ACP) — deliberately not built

The roadmap's Phase 6 was an Agent Client Protocol adapter: speaking a
standardised agent protocol so a LocalCode session could be driven by an
external client (an editor, another orchestrator) instead of only by this UI.
It is **out of scope by plan**, and this paragraph exists so the option stays
open rather than being silently dropped.

What reshaping it would involve, from where the code now stands: the `Provider`
protocol and the unified `Event` stream are already the right shape for it —
an ACP server would be a *consumer* of `SessionRunner` + `EventBus`, not a
fourth provider. Three things would have to move. The approval bus would need a
second front door: today a card is published to an `EventSink` and answered on
an `asyncio.Queue` fed by the WebSocket handler, so an ACP client would need
its own adapter onto `approval_channel` rather than a new gate. Session
identity would need to be addressable from outside — the store is keyed by a
LocalCode session id under `<cwd>/.localcode/sessions/`, which is fine, but
`get_runner` is process-local and an external client reconnecting after a
restart would want the replay ring's `?since=` semantics exposed on whatever
transport ACP uses. And the tool-policy layer would need a way to express "this
external client is the operator" so that `ask` has somewhere to go — today the
absence of an approval channel *means* headless, which is exactly the signal an
ACP session would be violating.

None of that is blocked by anything in the current design. It is a transport
plus an adapter, not a refactor.

---

## See also

- [architecture.md](architecture.md) — module-by-module responsibilities.
- [codex.md](codex.md) — the codex app-server integration, its unverified
  protocol assumptions, and how to reconcile them.
- [storage.md](storage.md) — the filesystem session store: paths, file shapes,
  atomicity, checkpoint throttling, cleanup.
- [fleet.md](fleet.md) / [fleet-config.md](fleet-config.md) — the fleet
  concept and its configuration UX.
