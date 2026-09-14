# The harness

LocalCode drives two vendor coding agents through one chat surface. This
document is about the layer that makes that a single system rather than two
integrations sitting next to each other: what every provider is held to, where
each guarantee is enforced, and which tests would fail if it stopped being true.

Read [architecture.md](architecture.md) for what each module *is*. This file is
about what the harness *promises* — and what it does not.

---

## 1. The invariant

**LocalCode never reads a credential store and never holds a vendor key.**

Each vendor CLI authenticates itself: `claude login` writes an OAuth token to
`~/.claude/` (or the platform keychain), and `codex login` to `~/.codex/`. The
orchestrator spawns the CLI and never looks inside either of those files. It
also never assigns an `*_API_KEY`, `*_OAUTH_TOKEN` or `*_SESSION_KEY` from
anything, and never shells out to the keychain.

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

| | `claude` | `codex` |
| :-- | :-- | :-- |
| Transport | `claude-agent-sdk` → `claude` CLI | JSON-RPC over stdio → `codex app-server` |
| Auth | `claude login` | `codex login` |
| Streaming text | yes (token deltas) | yes (item updates, suffix-only) |
| Tool cards | yes | yes (commands, patches, MCP, web search) |
| Approvals through the one bus | yes (`can_use_tool`) | yes (`execCommandApproval`, `applyPatchApproval`) |
| Role policy enforced | yes | **only when the app-server asks** — see below |
| `additional_dirs` | yes (`add_dirs`) | yes (`additionalDirectories`) |
| Persistent session across turns | yes (one `ClaudeSDKClient` per session) | yes (one app-server per workspace, thread resumed) |
| Vendor-reported rate limits | yes (`RateLimitEvent`) | unverified — read if present, see `codex/protocol.py` A11 |
| Metered by the quota governor | yes | yes |

The codex column is pinned to a protocol read from documentation rather than
observed against a live binary — every assumption is listed in
`backend/app/orchestrator/codex/protocol.py` with the cost of each one being
wrong, and `make codex-schema` regenerates the vendor schema for
reconciliation. See [codex.md](codex.md).

Three limits of the codex column, true today and stated rather than fixed:

- **The role policy binds only the requests the app-server chooses to send.**
  `thread/start` forwards `{cwd, model, additionalDirectories}` and no approval
  or sandbox policy, so whether `execCommandApproval` / `applyPatchApproval`
  fire at all is decided by the user's own `~/.codex` configuration. When one
  arrives it goes through the same `policy_for_role` table Claude's tools do,
  with the same card and the same deny — but a server configured to ask about
  nothing is a server LocalCode never gets to refuse. Sending an explicit
  policy on `thread/start` is a follow-up gated on reconciling the real schema
  (`make codex-schema`); guessing a field name here would be a policy silently
  ignored, which reads exactly like one enforced.
- **An errored turn is unmetered.** A silent turn or an `error` item ends the
  turn with an `error` event and no `assistant.done`, so there are no token
  counts to record and no row is written to `usage.jsonl`. A successful turn
  is always exactly one row, as a Claude turn is — that file is the whole of
  `GET /api/system/usage`.
- **The app-server outlives its sessions.** One process serves a *workspace*,
  so `close_session` is deliberately a no-op: closing it when one LocalCode
  session rooted there goes away would kill the agent of every other session
  in that directory. The process (and its group) is reclaimed by
  `provider.aclose()` at shutdown, not before.

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
| `fleet_max_workers` | 4 | live worker processes in **idle steady state**, not a concurrency limit: a burst is served in full (evicting a busy worker would fail a live step) and the next spawn evicts least-recently-used *idle* workers back to the cap |
| `fleet_worker_idle_s` | 300 s | how long an idle worker survives |

Three reclamation rules: a worker is spawned with `start_new_session=True` and
killed with `killpg` on the pgid captured at spawn (the vendor CLI is its
child, and killing the leader alone re-parents a paid CLI to `launchd`); a
step that was still *queued* is not charged against the retry cap, because it
demonstrated nothing about its backend; and each worker writes a pidfile so a
`SIGKILL`ed backend's orphans are swept on the next pool start.

That sweep is paid by the **first fleet step of a backend**, not by startup:
`_ensure_started` runs `sweep_stale_workers` under the pool lock before
anything is spawned, so a first step can wait on it. It is off the event loop
(`asyncio.to_thread`), but it stats each leftover pidfile and shells out to
`ps` to verify the pid still names our worker module — up to ~10 s per stale
record on the `ps` timeout. In practice there are no stale records unless a
backend was `SIGKILL`ed, which is exactly when paying for the check is worth
it; it is under the lock because reclaiming a previous backend's workers has
to finish before this pool spawns its own.

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
- **One verification step is still manual: Ctrl-C on a live fleet turn.**
  Task 14's process-group reaping was signed off partly by hand — start a fleet
  turn against a real `claude`, Ctrl-C the backend, and check with
  `pgrep -fl "fleet.subproc|claude"` that neither the worker nor the vendor CLI
  survived. Nothing here can automate that: it needs an installed CLI and a
  paid subscription. The automated stand-in is
  `test_leak_containment.py::TestAbandonedSteps::test_a_step_cancelled_mid_flight_leaves_no_process_behind`,
  which cancels a real step through the same `finally` and asserts on a real
  grandchild's pid — the fake worker spawns one precisely so the assertion is
  about the process that used to survive. What the stand-in cannot prove is
  that the *vendor's own* CLI reacts to `SIGKILL` on its group the way the
  stand-in's `sleep` does.
- **A deferred tail that arrives after a turn ends is undetectable.** The SDK
  defers on backgrounded agent work (`DEFERRING_TASK_TYPES`), so a result can
  arrive with that work still in flight and the follow-up frames land on the
  same connection afterwards. `ClaudeProvider` now checks for that tail once,
  after the turn's result, and drops the client rather than serving it to the
  next turn (`test_claude_client_reuse.py::TestADeferredTailOnTheConnection`).
  A tail the CLI emits *after* that check and before the next turn is still
  invisible — the SDK's own comment says closing the gap "needs a run-boundary
  signal from the CLI rather than an inference from task bookkeeping", and no
  test here can manufacture one. One frame narrower and the same in kind: a
  frame delivered at the exact moment the check's per-frame window expires can
  be consumed and lost, because anyio assigns the item to the receiver before
  waking it and the cancellation `wait_for` then raises drops it. The check
  walks past benign frames under a whole-loop deadline (`_TAIL_CHECK_DEADLINE_S`)
  rather than a per-frame one, so a stream of post-turn `system` task-lifecycle
  frames cannot stall the turn. Running out of that budget is *not* on this
  list: it costs a reconnect, not a risk. An expiry means the check never
  reached the end of the trickle, so the client is dropped and the next turn
  starts a fresh CLI — the frames that decide reuse may have been behind the
  ones it did see.

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
third provider. Three things would have to move. The approval bus would need a
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

## 11. Soak, latency and leak verification

§9's layers ask whether the harness behaves. These ask what it costs to keep
behaving, and what it leaves behind when it does not — the class of regression
that is invisible at turn 3 and fatal at turn 300.

| Suite | Subject | In `pytest -q`? |
| --- | --- | --- |
| `backend/tests/test_soak_long_session.py` | 200 turns on one session: disk, memory, tasks, descriptors, paging | no — `slow` |
| `backend/tests/test_latency_budgets.py` | event-loop stalls, bus throughput, checkpoint cost, first-event latency | yes |
| `backend/tests/test_leak_containment.py` | real worker processes and their grandchildren; session churn | yes |
| `backend/tests/test_failure_injection.py` | every failure mode the audit named, end to end | yes |

**Why exactly one of them is excluded.** A reliability test nobody runs is
decoration, so the injections and the budgets are in the default suite — they
cost about four seconds between them. The soak is not excluded for its cost: it
runs in 1.5 s. It is excluded because what it produces is a *trend* to be read,
not a gate to be passed — ten thousand events of measurements belong in a run
someone is looking at, and its budgets are deliberately loose enough that
failing one means a redesign rather than a bad afternoon. `make soak` runs it,
and runs everything else with it:

```bash
make soak       # the whole suite + the soak, -W error::UserWarning, 300 s per test
.venv/bin/pytest -q -m slow     # the soak alone
```

`make soak` adds two things the default run does not. Warnings are errors, so
an unregistered marker or a new vendor-SDK deprecation fails rather than
scrolls past. And every test gets a hard wall clock
(`LOCALCODE_TEST_TIMEOUT_S`, honoured by an autouse fixture in
`backend/tests/conftest.py`): on expiry a watchdog thread dumps every thread's
stack, and if the test is still wedged five seconds later the run aborts with
exit code 99 rather than hanging. That exists because a full-suite run under
`-W error::UserWarning` hung exactly once during Task 5, at the third test in
collection order, with a second pytest running beside it — and eight bounded
reruns never reproduced it. A hang that rare is only ever caught by a run that
cannot hang.

### How to read a failure

Every budget in these suites is a named module constant whose comment says what
it protects and where the number came from, marked `audit-derived` (a figure
the audit or an earlier task measured) or `chosen-here` (picked from a
measurement these tests print). Read that comment first: it is the difference
between "this is a regression" and "this machine is busy".

* **A ratio failed** (disk bytes vs content bytes, checkpoint bytes vs final
  message) — that is a regression. Ratios do not move with machine load; they
  moved because the code writes more than it used to. `[soak] disk … (1.17x)`
  and `[latency] … (1.76x)` are the measured values against 3x budgets.
* **A count failed** (tasks, descriptors, write count, registered workers) —
  also a regression, for the same reason.
* **The stall case failed** — read the printed baseline. The verdict is
  load-relative by construction (see below); if the *baseline* is the number
  that is large, the machine could not schedule the watchdog and the test
  skips rather than failing.
* **A throughput floor or a latency budget failed** — check the printed value
  against the constant's comment. These carry an order of magnitude of
  headroom (bus: 50 000 events/s floor against ~500 000 measured; first event:
  250 ms budget against ~1 ms), so a failure is a change in kind, not a busy
  laptop.

**On the stall budget specifically.** An absolute "worst loop gap < 50 ms"
assertion was tried first and failed roughly one run in ten with no defect
present — under concurrent load the watchdog coroutine itself went unscheduled
for 158 ms. `test_storage_offload.py` had already met this; both suites now
reach the verdict the same way: the watchdog must *tick* during the operation
(work that blocks the loop produces no ticks at all), and the worst gap is
compared against a baseline measured through the same watchdog on an idle loop,
best-of-3. The 50 ms figure survives as a sanity ceiling on the *baseline*: if
an idle loop cannot tick inside it, the test skips with the number it measured.

### What the numbers were when this was written

On the development machine, from a clean `make soak`:

```
[soak] 200 turns x 50 events = 10000 events in 0.90 s (4.5 ms/turn)
[soak] disk 4418978 bytes for 3761332 content bytes (1.17x, budget 3x)
[soak] tracemalloc peak growth 3.02 MiB (budget 8 MiB), ru_maxrss growth 0.9 MiB
[soak] tasks 1 -> 1, fds 9 -> 9
[soak] first page: 50 messages in 2.6 ms, read 983040 of 4418978 bytes on disk
[latency] 2000-event turn in 40.3 ms: worst loop gap 6.89 ms, idle baseline 6.41 ms
[latency] bus: 10000 events to 3 subscribers = 506,960 events/s
[latency] 500 tool boundaries: 4 writes, 1.76x the final message
[latency] first event reached a subscriber after 0.71 ms
```

They are printed on success, not only on failure, and `make soak` passes `-s`
so they reach the log of a passing run. A soak whose numbers are invisible
until it breaks teaches nobody the trend.

### The fakes these suites add

`backend/tests/fakes/load.py` — synthetic turns that are their own ledger. A
`TurnScript` yields the exact events `execute_turn` consumes *and* accumulates
the UTF-8 size of everything that will land in a persisted block, so the soak's
disk budget is a ratio against a measured denominator rather than a guess.
Nothing else is added: the stall detector is Task 13's, the scripted providers,
worker pool and vendor fakes are Task 12's and Task 7's, and the leak suite
uses the *real* `WorkerPool` — the one fake it deliberately refuses, because
`FakeWorkerPool` stays in-process and a process leak is the subject.

---

## See also

- [architecture.md](architecture.md) — module-by-module responsibilities.
- [codex.md](codex.md) — the codex app-server integration, its unverified
  protocol assumptions, and how to reconcile them.
- [storage.md](storage.md) — the filesystem session store: paths, file shapes,
  atomicity, checkpoint throttling, cleanup.
- [fleet.md](fleet.md) / [fleet-config.md](fleet-config.md) — the fleet
  concept and its configuration UX.
