# Codebase review fixes: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans
> (chosen: inline). Steps use checkbox (`- [ ]`) syntax.

**Goal:** fix every Important and Minor finding of the whole-codebase review
of `a0c97a9`, and apply the patterns it recommended.

**Architecture:** turn invariants move into the engine's `Core`; the TUI gets
one turn-start path, a `Vendor` seam, registry-level guards and a pure
reconnect plan; the CLI gets a pure argument parser and typed exit codes;
test infrastructure is shared through `octet-testkit`.

**Tech stack:** Rust 1.98.1 workspace, tokio, ratatui/crossterm, serde_json,
thiserror. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-07-codebase-review-fixes-design.md`

## Global constraints

- Run cargo through `scripts/rust-env.sh`; the gate is `make rust-check`
  (fmt, all tests, clippy `-D warnings`), green after every task.
- Workspace lints apply (`unwrap_used`, `missing_docs`,
  `missing_errors_doc`, `needless_pass_by_value`, …); test files carry
  `#![allow(clippy::unwrap_used)]`.
- Product code lines ≤ 120 columns (`crates/octet/tests/source.rs`).
- Behaviour changes: failing test first. Refactors: suite green before and
  after, plus the unit tests the task names.
- Every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Out of scope: typestate `Phase`, `Config` builder, event-sink trait, full
  `App::event` state machine, demo as `Protocol`, gate `Scenario` enum,
  unrelated nits.

## Review focus

1. A Claude approval answered after its turn ended, or after a cancel
   request, must not reach Claude twice or reach the wrong request.
2. The approval cap must deny over-cap requests without dropping the ones
   already shown.
3. `begin_turn` must not mark a turn started when the send fails.
4. A reconnect plan must never turn a resume into a fork, or carry an effort
   the provider does not take.
5. Headless modes must shut the session down on every exit path, including
   a closed stdout.

---

## Phase A: engine

### Task 1: Turn invariants in `Core`; approval tests

**Files:** `crates/octet-engine/src/live/{protocol.rs,codex.rs,claude.rs,mode.rs,driver.rs}`,
`crates/octet-testkit/src/bin/protocol-child.rs`, `crates/octet-engine/tests/live.rs`.

**Produces:** `Core::finish_turn(&mut self, Outcome, Option<String>) -> Result<(), DriverError>`;
`Core::adopt_mode(&mut self, reported: Option<(Option<Mode>, String)>) -> Result<(), DriverError>`
(shape follows `confirm_mode`'s current arguments); `MAX_PENDING_APPROVALS`.

- [ ] Fake Claude: `approval` sends `{"type":"control_request","request_id":"perm-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"echo fixture"}}}`
  and, on the `control_response`, replies with text `"{behavior}:{updatedInput.command}"`
  then a result; `approval-cancel` sends the request then
  `{"type":"control_cancel_request","request_id":"perm-1"}` and finishes the turn;
  `approvals-9` sends nine requests.
- [ ] Failing live tests: `claude_approval_allowed_passes_the_input_back`,
  `claude_approval_denied`, `claude_cancel_request_closes_the_approval`,
  `approvals_over_the_cap_are_denied_with_a_notice` (both vendors),
  `an_unanswered_approval_times_out_and_is_denied` (both vendors, short
  `approval_timeout`), `claude_result_without_is_error_is_failed_once`.
- [ ] Implement `finish_turn`, the cap in `queue_approval`, `adopt_mode`; move
  vendor mode mappings out of `mode.rs`; read `is_error` once.
- [ ] Gate; commit "Keep turn endings, the approval cap and mode adoption in Core".

### Task 2: Codex request map; lost steer; shutdown error

**Files:** `codex.rs`, `protocol.rs`, `driver.rs`, `tests/live.rs`.

- [ ] Failing tests: `a_steer_held_for_a_cancelled_turn_is_reported`,
  `a_steer_held_for_a_refused_turn_is_reported`,
  `an_unverified_shutdown_keeps_the_vendor_error` (unit test of the error
  combining function).
- [ ] `Outstanding` map with `request()`; `Core.request_id` for approvals only.
- [ ] Gate; commit "Codex: one map of outstanding requests; report held steers".

### Task 3: Bounds and one approval timeout

**Files:** `image.rs`, `claude.rs`, `codex.rs`, `mod.rs`, tests.

- [ ] Failing tests: `a_huge_reply_is_cut_not_fatal` (fake Codex `huge`:
  one 5 MiB `agentMessage` completion), `late_modes_are_bounded`,
  `held_steers_are_bounded`, `an_image_that_grew_is_refused_at_send`.
- [ ] Implement; remove `Limits.approval`, read `Config.approval_timeout`.
- [ ] Gate; commit "Bound replies, images, late modes and held steers; one approval window".

### Task 4: `TurnGate`

- [ ] Unit tests of `TurnGate` (counts only turn commands; a cancel covers
  turns sent before it; marks the cancel seen).
- [ ] Use it in the driver and the demo; shared notice constants.
- [ ] Gate; commit "Share the cancel gate and notices between the driver and the demo".

### Task 5: Engine test infrastructure

- [ ] `octet_testkit::scenario` constants; fixture builders; `live.rs` helpers;
  replace fixed sleeps with marker waits (`transport.rs` flood marker); move
  vendor tests out of `driver.rs`; fix stale comments; client version from
  `CARGO_PKG_VERSION`.
- [ ] Gate (test count unchanged or higher); commit "Engine tests: shared scenarios, builders and helpers".

## Phase B: TUI

### Task 6: `Vendor`, `Prompt`, `begin_turn`

- [ ] `trait Vendor` + `impl Vendor for Handle` + `RecordingVendor` (tests).
- [ ] Failing tests: `begin_turn_marks_nothing_when_the_send_fails`,
  `idle_steer_sends_attachments_like_enter` (the drift the review found).
- [ ] `Prompt`, queue of `Prompt`, every turn start through `begin_turn`;
  handlers take the vendor; `Action` shrinks.
- [ ] Gate; commit "TUI: one way to start a turn, through a Vendor seam".

### Task 7: Registry guards and reporting

- [ ] Failing test: `every_command_needing_an_idle_session_refuses_the_same_way`
  (iterates the registry).
- [ ] `requires` on `Spec`; `try_command` checks it; `status_line`, `note`,
  `hint`, `error`.
- [ ] Gate; commit "TUI: guards in the registry; one reporting rule".

### Task 8: Reconnect plan

- [ ] Unit tests per `Exit` variant of `reconnect::plan`.
- [ ] `plan()`; `Connection::new`; `ConnPhase::Running { cancelling }`.
- [ ] Gate; commit "TUI: reconnects as a tested plan".

### Task 9: TUI fixes

- [ ] Failing tests: `no_color_renders_without_colour`,
  `sessions_listing_runs_off_the_loop` (the loop handles an event while it
  runs), `one_tab_listing_at_a_time`.
- [ ] `Theme`; SIGINT once; background `/sessions` and `/export`; Tab gate.
- [ ] Gate; commit "TUI: honour NO_COLOR; keep SIGINT; move disk work off the loop".

### Task 10: TUI structure and text

- [ ] Splits; `submit()`; limits from constants; message constants; plurals;
  test split and helpers; timing margins; `mode_command` guard; remote
  problem count.
- [ ] Gate; commit "TUI: split along its seams; one source for limits and messages".

## Phase C: core, store, CLI, CI

### Task 11: Headless

- [ ] Failing tests: `print_shuts_down_when_stdout_closes` (journal ends with
  `stopped`, child reaped), `an_argument_prompt_over_the_limit_is_refused_before_opening`.
- [ ] `open_headless`, loops in functions, always shut down; public
  `starts_turn`.
- [ ] Gate; commit "Headless: always shut the session down".

### Task 12: CLI

- [ ] Unit tests of `parse_args` (every error message, exit codes, `-p`,
  `--rpc`); failing: `usage_errors_exit_2`, `a_non_utf8_cwd_is_refused`.
- [ ] `Args`, `CliError`, `..Config::new()`.
- [ ] Gate; commit "CLI: a tested argument parser and distinct exit codes".

### Task 13: Core and store

- [ ] Failing tests: `a_closed_receiver_stops_the_pump`,
  `a_failed_goal_save_leaves_no_temp_file`, `journal_directories_are_private`.
- [ ] `pump`/`deliver`; `GoalRunner` shape; `catalog_hint` to TUI; `FORMAT`;
  0700 directories; shared journal fixture in testkit.
- [ ] Gate; commit "Core and store: one pump, simpler goals, private directories".

### Task 14: Test infrastructure

- [ ] Failing test: the credential guard catches `"_API_KEY"` and
  `format!("{x}_API_KEY")`, and scans `scripts/` and `.github/`.
- [ ] Line-length walker over every product crate; shared helpers in testkit;
  CLI tests out of the PTY file; timing fixes.
- [ ] Gate; commit "Tests: shared helpers; wider guards".

### Task 15: CI and docs

- [ ] `check.yml` permissions and concurrency; SHA-pinned actions; smoke run
  of each built binary; actionlint and shellcheck clean.
- [ ] Stale text; `docs/rust/protocol-gate.md`; `make gate`.
- [ ] Gate; commit "CI and docs: least privilege, pinned actions, smoke-run releases".

## Finish

- Final whole-branch review (fresh reviewer, most capable model); one fix pass.
- Live tmux run; tiny real-Claude check (approval allow/deny through Octet).
- Merge only when the user says.
