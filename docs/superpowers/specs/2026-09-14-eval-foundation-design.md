# Eval foundation — design

**Status:** approved in brainstorming, 2026-09-14. First sub-project of "evolve
LocalCode into a coding-agent harness that beats pi.dev on task outcome quality".

## Why this first

"Better task outcome quality" is unfalsifiable without an instrument. This
sub-project builds the instrument and records the baseline; every later
sub-project (quality levers, the TUI that replaces the web UI, branching
sessions and mid-session model switching, extensions) is judged against the
number it produces.

Decisions taken during brainstorming that bind this and later sub-projects:

- **Quality is measured on a public benchmark**: a fixed 50-instance subset of
  `SWE-bench/SWE-bench_Verified`, scored by SWE-bench's own harness.
- **Subscriptions only, governor-paced.** Eval runs go through the same vendor
  binaries as normal use and are paced by the quota governor. The auth
  invariant ("never hold the token; drive the vendor's own binary") is
  untouched — no API-key exception, not even for eval.
- **The agent runs on the host; only scoring runs in Docker.** Terminal-Bench
  was rejected because its harness runs the agent inside the container, which
  would mean mounting vendor credentials — holding the token by another name.
- **Host-only scoring (no Docker) was rejected**: it would re-implement
  SWE-bench's environments and make the number incomparable.

Out of scope here, deferred to their own sub-projects in this order: quality
levers (verify-before-done loop, context engineering, planner/coder tuning);
the terminal TUI that replaces the React web UI and VS Code extension;
branching sessions and mid-session model switching; extensions/plugins.

## 1. Architecture and components

Everything lives in the existing repository under `backend/app/eval/`, plus
`make eval` targets. No new service. One new dev dependency: `swebench`
(pinned; currently 5.0.2).

- **`eval/headless.py`** — `run_headless_turn(prompt, cwd, *, provider_name,
  model, fleet_override, permission_mode, budget) -> TurnRecord`. Creates a
  throwaway session on disk through the same storage call the create-session
  route uses, builds the provider via `registry.get_provider`, and drives the
  real `session_runner.turn.execute_turn` with a fresh `EventBus` and an
  approval queue whose reader answers from a fixed policy: approve edits and
  commands whose paths resolve inside the checkout, deny everything else with
  a recorded reason. Returns event count, token sums, cost where reported,
  wall clock, denial count, and the terminal outcome. This is the harness's
  headless mode; a future RPC surface wraps it.
- **`eval/instances.py`** — loads the committed standing subset
  (`evals/swebench_verified_subset.json`) and fetches dataset rows through
  `swebench`'s dataset helpers, cached under `~/.localcode/eval/datasets/`.
- **`eval/runner.py`** — the generate phase (§2), sequential, governor-paced
  (§3), resumable.
- **`eval/score.py`** — the score phase: invokes SWE-bench's evaluation
  through the `swebench.harness.run_evaluation` module entry point (the
  `swebench eval` CLI wraps it; the module path is what the pinned version
  guarantees) on `predictions.jsonl` with a `run_id`, `--task-repo` local
  Buildx builds, `max_workers` 1-2; parses
  `logs/evaluation/<run_id>/results.json` and per-instance `report.json`.
- **`eval/report.py`** — writes the run's result JSON and appends one row to
  `evals/HISTORY.md`.
- **Makefile**: `make eval CONFIG=<config> [N=50]` (both phases),
  `make eval-score RUN=<run_id>` (re-score), `make eval-probe` (rebuild the
  subset, §4), `make eval-selftest` (the Docker-backed slow test).

**Configurations** are named cells: `claude-single`, `claude-fleet`,
`codex-single`, `codex-fleet`, and `opencode-single` when configured — the
same matrix the test suite already names. A configuration fixes provider,
model (from the catalog), and whether the fleet provider or a single-agent
session serves the turn.

## 2. Per-instance data flow

Each instance passes through six steps; each leaves a file, so a crash
resumes at the first unfinished step.

1. **Checkout.** A bare mirror per repository under
   `~/.localcode/eval/repos/<owner>__<name>.git`, fetched once per run; a temp
   worktree at `base_commit` under `~/.localcode/eval/work/<instance_id>/`.
   Never under the user's projects; `allowed_cwd_roots` already permits `~`.
2. **Prompt.** The problem statement verbatim inside a fixed preamble that
   names the repository, forbids modifying tests, and says to stop when done.
   No `hints_text`, and the gold patch is never present in the process. The
   preamble is versioned by a `PROMPT_VERSION` constant recorded in every
   result, so a later lever changes it deliberately.
3. **Turn.** `run_headless_turn` with `cwd` = the worktree and
   `permission_mode = "acceptEdits"`, bounded by §3's budget. Events stream to
   `events.jsonl` per instance (the same event shapes the UI consumed) so a
   failed instance can be read back.
4. **Patch.** `git add -A && git diff --cached --binary` in the worktree, with
   paths SWE-bench classifies as test files for that instance removed. An
   empty diff records `no_patch` and is not scored. One line appended to
   `predictions.jsonl`: `{instance_id, model_name_or_path: "<config>@<sha>",
   model_patch}`.
5. **Cleanup.** Worktree removed; mirror kept. `TurnRecord` written to
   `records/<instance_id>.json`.
6. **Score** (separate phase): per-instance verdicts `resolved` /
   `unresolved` / `eval_error` joined with the records into the run result.

Instances run in id order within the subset. `--resume <run_id>` skips
instances with a record. A run id is `<config>@<short-sha>-<UTC timestamp>`.
A dirty working tree refuses to run: the number would name a commit it does
not describe.

## 3. Pacing and budgets

**Pacing belongs to the governor.** Before each instance the runner reads
`Governor.headroom()` for every provider the configuration uses. If any is
below `EVAL_HEADROOM_FLOOR` (default `0.15`; above the governor's own
`QUEUE_THRESHOLD` of `0.05` because a benchmark must not consume the last of
the user's working headroom) it sleeps until that provider's `resets_at` and
records the wait. `confidence == "unknown"` proceeds — an unmeasured limit is
not a reason to stall. Generate runs one instance at a time so headroom
arithmetic stays honest and cost stays attributable.

**Per-instance budget** — three limits; any one ends the turn through the
same cancellation path Stop uses:

| Limit | Constant | Default | Source |
|---|---|---|---|
| wall clock | `EVAL_TURN_TIMEOUT_S` | 1200 s | same as `fleet_step_timeout_s` |
| tokens (all steps) | `EVAL_TURN_TOKEN_BUDGET` | 400 000 | fleet turn budget for fleet configs; runner's own `usage` sum for single |
| tool calls | `EVAL_TURN_MAX_TOOL_CALLS` | 200 | counted from `tool.use` events |

A budgeted-out turn still has its partial diff captured and scored; the
record names the limit that fired.

**Cost accounting.** Each `TurnRecord` sums input / output / cache-read /
cache-creation tokens from `usage` events and carries `cost_usd` where the
provider reports it. The run summary reports tokens/task and cost/task next
to the resolve rate: a lever that lifts the rate by paying double is a
different result.

Deliberately absent: retries inside a run, cross-run caching of agent
output, parallel instances, scoring concurrency above `max_workers = 2`.

## 4. The standing subset and arm64

**Selection, run once and committed.** Order the 500 Verified instances by a
fixed seed (`20260914`). Walk the order; for each candidate build its
environment image locally (`--task-repo`, Docker Buildx, `cache_level =
instance`); keep it if the build succeeds on this arm64 machine, skip it
otherwise; stop at **50** kept. Commit `evals/swebench_verified_subset.json`
with the ids, the seed, the skip list (id + first line of the build error),
the `swebench` version, and the date. The subset never changes silently: a
re-probe writes a new file with a new date, and every `HISTORY.md` row names
the subset it ran on. Fifty is where one instance moves the rate by two
points — visible levers, and a run fits in a few five-hour windows.

**Why local builds.** Prebuilt images are x86_64; SWE-bench calls arm64
support experimental and recommends local Buildx builds on M-series. The
probe makes "does it build" a recorded fact instead of a runtime surprise.

**Pre-flight, enforced by the runner:** Docker daemon reachable; Docker
memory allocation ≥ 8 GiB (refuse below; warn below 12 GiB — SWE-bench
recommends 16 GB; the fix is Docker Desktop → Resources); ≥ 60 GiB free disk;
`swebench` importable at the pinned version; the vendor binaries for the
configuration on `PATH` and logged in, checked the way `warm_up` does — never
by reading a credential.

**Known bias:** instances that only build on x86_64 can never enter the
subset. Scores remain directly comparable to anyone running the same 50 ids,
which are public in the repository.

## 5. Failure handling

Every failure is a recorded outcome; a run never crashes on an instance.
`TurnRecord.outcome` is the full vocabulary: `patched`, `no_patch`,
`budget_wall_clock`, `budget_tokens`, `budget_tool_calls`, `provider_error`,
`checkout_error`, `harness_error`. Scoring adds `resolved`, `unresolved`,
`eval_error`. The summary counts each.

- **Checkout fails** → `checkout_error`, skip, continue. Three consecutive
  mirror-fetch failures abort the run (that is the network).
- **Provider error or silent turn** → the existing `error` + `assistant.done`
  path ends the turn; the partial diff is captured as usual. No retry.
- **Budget hit** → cancellation through the Stop path, then patch capture. The
  runner waits the cancel grace and verifies through the pool's own reaping
  that no worker tree survives.
- **Undecidable approval** (unresolvable path) → denied with a reason; the
  agent continues; denials are counted in the record.
- **Governor says stop mid-run** → sleep to `resets_at`, resume. A wait longer
  than `EVAL_MAX_WAIT_S` (default 6 h) checkpoints the run and exits 3 with
  the `--resume` hint — a benchmark must not sleep unattended by accident.
- **Ctrl-C** → the current instance is cancelled through the Stop path, its
  record written with the budget outcome and `interrupted: true`, the worktree
  removed; the run is resumable.
- **Scoring** → per-instance harness errors become `eval_error` with the
  `run_instance.log` path; a harness-level failure exits non-zero with
  `predictions.jsonl` intact, and a re-score reuses finished instances because
  SWE-bench caches by `run_id` + `instance_id`.
- **Disk** → the free-space threshold is re-checked between instances; below
  20 GiB the generate phase pauses with a clear message.

Deliberately absent: automatic retries of any class, repair of malformed
patches, scoring of an instance with no record.

## 6. Testing and outputs

**Tests — default suite, fake-driven, no Docker, CLI or network:**

- `headless.py`: a `ScriptedProvider` turn through the real `execute_turn`
  yields a `TurnRecord` with correct sums, counts and outcome; the approval
  reader approves an `Edit` inside the worktree and denies one outside, each
  with its reason; each budget limit (wall clock via an injected clock,
  tokens, tool calls) ends the turn through the Stop path and captures the
  partial diff; no worker process survives (the Task 16 leak assertions,
  reused).
- `runner.py`: against a temp git repository — checkout at `base_commit`;
  patch capture excludes the instance's test files; an empty diff records
  `no_patch`; `--resume` skips completed records; a dirty tree is refused;
  the governor gate sleeps to an injected `resets_at` and records the wait;
  `EVAL_MAX_WAIT_S` exits 3 with the resume hint.
- `score.py`: the evaluation call is a seam; the parser is tested against a
  committed sample `results.json` and per-instance `report.json` captured
  once from a real SWE-bench run, including `eval_error` and a missing
  instance.
- `instances.py`: the committed subset validates (50 unique ids present in a
  cached dataset snapshot fixture, seed recorded); the selection walk is
  deterministic for a given seed and skip list.
- `report.py`: the `HISTORY.md` row is byte-stable for a fixed record set;
  the result JSON round-trips.
- One `@pytest.mark.requires_cli` end-to-end test through the real `claude`
  binary on one instance (skipped by default); one `@pytest.mark.slow`
  scoring test that needs Docker (excluded by default; `make eval-selftest`).

**Outputs:**

- `evals/results/<run_id>.json` (git-ignored): every `TurnRecord`, every
  verdict, every wait, the configuration, `PROMPT_VERSION`, the subset file
  name, the `swebench` version, the tree sha.
- `evals/HISTORY.md` (committed): one row per run — date, sha, config,
  subset, n, resolved/n, tokens/task, cost/task, wall clock, notes. This file
  is the answer to "are we better than last week".
- `evals/swebench_verified_subset.json` (committed).
- Console: one line per instance as it finishes (id, outcome, tokens,
  seconds) and a final summary; `--json` for machines.

**First deliverable: the baseline.** `make eval CONFIG=claude-single` and
`make eval CONFIG=claude-fleet` on the current tree, producing the first two
rows of `HISTORY.md`. The quality-levers sub-project is judged against them.

## Interfaces this depends on (as they stand)

- `session_runner.turn.execute_turn(*, session_id, bus, approval_q, provider,
  provider_name, model, cwd, additional_dirs, upstream_id, fleet_override,
  permission_mode, prompt)`.
- `orchestrator.registry.get_provider(name)`; `orchestrator.base.RunContext`.
- `quota.Governor.headroom(provider)`, `snapshot()` (per-provider
  `resets_at`, `confidence`), `QUEUE_THRESHOLD`.
- `orchestrator.fleet.pool` reaping and the Task 14 cancel grace.
- `backend/tests/fakes/providers.py` (`ScriptedProvider`, `claude_provider`).
- `swebench` 5.x: `SWE-bench/SWE-bench_Verified`, `swebench eval` /
  `run_evaluation`, `--task-repo` local builds, results under
  `logs/evaluation/<run_id>/`.
