# Rust practices review: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** fix every finding of the senior Rust review of `ec2d9e4` test-first,
then move the workspace to edition 2024 and a pedantic lint policy.

**Architecture:**
- The engine gains backpressure and a shared vendor launch contract, which
  the gate uses.
- The TUI's event loop becomes a `Wake` enum plus a `Loop` struct with named
  handlers, and the state it owns moves into `App`.
- Store, core and CLI fix durability and headless lifecycle.
- The fake vendor and the gate are split along their seams.

**Tech stack:** Rust 1.98.1 (edition 2024 after Task 1), tokio,
ratatui/crossterm, serde_json, thiserror. One new tool: `cargo-deny`, in CI
only.

**Spec:** `docs/superpowers/specs/2026-10-07-rust-practices-design.md`. IDs
such as E1 and T3 refer to its rows.

## Global constraints

- **Running cargo:** always through `scripts/rust-env.sh`.
- **The gate:** `make rust-check` (fmt, all tests, clippy `-D warnings`)
  passes after every task, with an empty `git status --porcelain` before
  each commit, and the exit code is checked rather than grepped.
- **Lints:** workspace lints apply. Test files carry
  `#![allow(clippy::unwrap_used)]`, which becomes
  `#![expect(clippy::unwrap_used, reason = "…")]` or a reasoned allow in
  Task 13.
- **Line length:** product code lines ≤ 120 columns (`crates/octet/tests/source.rs`).
- **Behaviour changes:** a failing test first, watched failing. Refactors
  keep the suite green and add the unit tests the task names.
- **Commits:** every message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Real vendors:** Codex runs need `--model gpt-5.5`, and prompts stay tiny.
  Never edit the user's Codex config.
- **Installing:** never install system software. cargo-deny runs only in
  CI, through a SHA-pinned action.
- **Out of scope:** the spec's "decided" list, and V6's incremental wrap
  (ruled out).

## Review focus

1. Backpressure (E1) must never stop cancel, stop or command handling while
   output waits, and must never deadlock when the consumer has gone.
2. Moving `ShellTask` and the index receiver into `App` (T3) and carrying a
   job across a reconnect (T5) must not orphan a process, or apply a
   result to the wrong session's state.
3. The approval delay (T1) must never answer, and never lose, an approval;
   Esc/D after the delay still denies.
4. Durability fixes (C2, C10) must report every write failure and leave the
   previous goal file intact.
5. The edition and lint migration (Tasks 1 and 13) must not change
   behaviour. That is especially true where edition 2024 rescopes `if let`
   temporaries, in lock guards (`octet-proc`, `clipboard`, `RecordingVendor`).

---

## Phase A: toolchain base

### Task 1: Edition 2024

**Files:** `Cargo.toml` (`[workspace.package] edition`), every crate
(through `cargo fix --edition`).

- [ ] Run `scripts/rust-env.sh cargo fix --edition --workspace --all-targets --allow-dirty`.
  Expected: it completes. Review each change it makes, especially
  `unsafe` blocks inside `unsafe fn`, `if let` rescoping and `impl Trait`
  capture.
- [ ] Set `edition = "2024"`. Expected: the build passes.
- [ ] Wrap the bodies of any `unsafe fn` in `unsafe {}` blocks with
  `// SAFETY:` comments (`unsafe_op_in_unsafe_fn`).
- [ ] Run `cargo fmt` (edition 2024 style).
- [ ] Gate. Commit "Edition 2024".

## Phase B: process, engine, gate, testkit

### Task 2: octet-proc (P1, P4, P6, P7, P8, P11)

**Files:** `crates/octet-proc/src/lib.rs`, `crates/octet-proc/tests/transport.rs`.

- [ ] Failing tests:
  - `stderr_is_complete_when_shutdown_does_not_drain_frames`: the child
    writes stderr, closes stdout and exits; `shutdown()` is called without
    `next_frame`. Loop 50 times in the test, plus
    `stderr_stress_500_runs` (`#[ignore]`).
  - `blank_lines_are_skipped`: `{"a":1}`, a blank line, then `{"b":2}`
    gives two frames, then `None`.
  - `invalid_json_after_valid_frames_ends_the_stream`.
  - `dropping_a_process_kills_its_group`.
  - `a_send_after_exit_is_broken_pipe_then_closed`.
- [ ] Implement:
  - `STDERR_DRAIN = 100 ms`, with a timeout around the stderr task before
    the abort;
  - skip whitespace-only frames;
  - remove `QueueClosed`;
  - `shutdown_complete()` derived from the report;
  - `Drop`: `killpg(SIGKILL)` plus `kill_on_drop`, without the extra
    `start_kill`;
  - frames keep their buffer and copy out the exact bytes
    (`frame.as_slice().into(); frame.clear()`);
  - the stderr ring takes each read's tail at once;
  - a manual `Debug` for `Process` and `ProcessSender` (pid and flags);
  - `# Panics` on `spawn` (it needs a Tokio runtime).
- [ ] The queue-full tests wait for the child to block, instead of sleeping
  80 ms.
- [ ] Gate. Commit "Process: complete stderr tails, skip blank lines, leaner frames".

### Task 3: Engine fixes (E1–E8)

**Files:** `crates/octet-engine/src/live/{mod,driver,protocol,claude,codex,demo,image}.rs`,
`crates/octet-testkit/src/bin/protocol-child.rs` (fixtures),
`crates/octet-engine/tests/live.rs`.

**Produces:** `fn deadline_after(d: Duration) -> Instant` in `mod.rs`;
`HEADROOM: usize = 32`; `enum DemoApproval { Allowed, Denied, Cancelled }`.

- [ ] Failing tests (in `live.rs` unless noted):
  - `a_burst_of_deltas_during_a_stall_is_delivered` (E1): fixture
    `delta-burst` sends 200 one-character `stream_event` deltas; the test
    pauses 1 s before reading, then expects all 200 characters and
    `Finished{Completed}`.
  - `claude_subagent_text_is_not_the_reply` (E2): fixture `subagent` sends
    a main assistant message, then one with `parent_tool_use_id`. Expect
    only the main text, and one `ModelSelected`.
  - `a_huge_approval_timeout_does_not_panic` (E3): `Duration::MAX`; the
    approval is shown, then interrupt, then `Finished`/`Stopped` arrive.
  - `a_second_cancel_keeps_the_interrupt_deadline` (E5): the fixture
    ignores interrupts. Two cancels 5 s apart give `Stopped` about 10 s
    after the first. Use `start_paused` where the driver's clock allows;
    otherwise shrink the interrupt limit.
  - `an_unsafe_session_id_is_ignored` (E6): an ID containing `\u001b` never
    reaches `Ready`.
  - Codex: a long unknown status and a long declined method are cut.
  - Unit test `send_counts_a_turn_only_when_queued` (E4): with the queue
    full, `send` fails and `interrupt` covers no phantom turn.
  - `an_image_read_that_hangs_fails_the_turn` (E7): a FIFO path, with the
    timeout shrunk through a test hook (const in `cfg(test)`).
  - `demo_cancel_in_the_dialog_is_interrupted` (E8).
- [ ] Implement:
  - backpressure in the driver's `select!` (an arm on
    `tx.reserve_many(HEADROOM + 1)` when the queue is short; the frame arm
    is gated on room);
  - `parent_tool_use_id` filtering;
  - `deadline_after` everywhere an `Instant + Duration` is computed;
  - `spawn` maps a driver panic to `Error` plus `Stopped`;
  - `try_reserve` before the turn count;
  - an early return in `Interrupting`;
  - `valid_identifier` on session IDs, and `limited` on status and method;
  - a 10 s `timeout` on the image read;
  - the demo's tri-state approval.
- [ ] Gate. Commit "Engine: backpressure, subagent filtering, bounded deadlines and vendor strings".

### Task 4: Engine API and launch contract (E9, E10, P2 engine side)

**Files:** engine `mod.rs`, `protocol.rs`, `claude.rs`, `codex.rs`,
`driver.rs`, `lib.rs`, and `tests/live.rs`.

**Produces:**
- `pub fn launch_args(engine: Engine, config: &Config) -> Vec<String>`;
- `pub fn vendor_process(executable: PathBuf, args: Vec<String>, cwd: PathBuf) -> octet_proc::ProcessConfig`;
- `pub fn claude_stray_reply(&Value) -> Option<Value>` and
  `pub fn codex_stray_reply(&Value) -> Option<Value>`, as free functions.

- [ ] Unit test `launch_args_match_what_the_driver_runs`: the driver spawns
  with exactly `launch_args`. The fake vendor echoes its argv into the
  journal, or the test compares against the one builder.
- [ ] Narrow the API:
  - `Provider.start`, `StartFn`, `BoxFuture` and `Channels` become
    `pub(crate)`;
  - remove `Protocol::stray_reply` from the trait;
  - add a comment on why `Core` keeps function pointers;
  - apply the idiom fixes (E9).
- [ ] Tests (E10):
  - the handshake test waits on a marker file;
  - the chatter test's bound becomes `limit * 2`;
  - add tests for unusual vendor output: non-object frames, fields with
    the wrong type, control characters in IDs;
  - the helper `wanted: impl Fn + Send`.
- [ ] Gate. Commit "Engine: narrower API; one vendor launch contract".

### Task 5: Gate (P2 gate side, P3, P9)

**Files:** `crates/octet-gate/src/{lib.rs,bin/protocol-gate.rs}`,
`crates/octet-gate/tests/*`, `docs/rust/protocol-gate.md`.

- [ ] Failing tests:
  - `gate_argv_starts_with_the_engines`;
  - `an_approval_without_an_id_is_a_protocol_failure` (a scripted vendor:
    exit code and evidence JSON, with no panic);
  - `the_workspace_is_removed_on_failure`.
- [ ] Implement:
  - use `launch_args` and `vendor_process`;
  - `GateError::Protocol`;
  - the `TempDir` guard;
  - split `codex()` into `handshake`, `open_thread`, `run_turn` and
    `session_ops`;
  - an `Evidence` struct;
  - `receive` dispatches once;
  - read the trace flag once;
  - private counters with accessors;
  - `Debug` on `GateProcess`.
- [ ] Fix the doc's `initialize` row.
- [ ] Gate. Commit "Gate: test the engine's own launch; no panics; split runs".

### Task 6: Testkit (P5, C18 `wait_child`)

**Files:** `crates/octet-testkit/src/bin/protocol-child.rs` becomes
`src/bin/protocol-child/{main,wire,codex,claude}.rs`;
`crates/octet-testkit/src/{lib.rs,scenario.rs}`; `crates/octet-core/src/goal.rs`
(the goal prefix const).

**Produces:** `scenario::{GOAL, GOAL_HOLD, GOAL_FAIL, CODEX_REPLY,
CLAUDE_REPLY, APPROVAL_ID, CANCELLED_APPROVAL_ID, CAP_PREFIX}`, and
`octet_core::goal::PROMPT_PREFIX`.

- [ ] Unit test `goal_prompts_start_with_the_pinned_prefix` (core).
- [ ] Unit test `wait_child_kills_on_timeout` (testkit; `should_panic`,
  then the pid is gone).
- [ ] Split the binary:
  - one state struct per vendor;
  - `match` on the scenario constants;
  - extract the prompt once.

  Expected: the suite stays green, and the test count is unchanged or
  higher.
- [ ] Gate. Commit "Testkit: the fake vendor split by vendor; shared scenario names".

## Phase C: store, core, CLI

### Task 7: Store and core (C2, C4, C5–C12, C15–C17)

**Files:** `crates/octet-store/src/lib.rs`,
`crates/octet-core/src/{lib.rs,goal.rs,error.rs,model.rs,sessions.rs}`, and callers.

- [ ] Failing tests:
  - `a_failed_append_is_reported` (store): a child process with
    `RLIMIT_FSIZE`; a helper binary or `#[test]` re-exec.
  - `a_failed_goal_save_keeps_the_old_file` (core): the same technique.
  - `journal_names_are_unique_within_a_process`.
  - `a_failed_export_leaves_no_file`.
  - `a_consumer_stall_is_journaled`.
  - `evidence_is_the_text_before_the_marker`.
  - `a_failed_start_keeps_the_previous_goal`.
  - `an_oversized_goal_file_is_not_read_whole` (the error appears before
    the whole file is read; a 10 MiB sparse file).
  - `observe_text_trims_amortized` (a unit test of the trim points).
- [ ] Implement:
  - `flush` after `write_all`;
  - fsync the parent directory after a journal create and after the goal
    rename;
  - a counter in journal names;
  - remove a partial export;
  - the pump journals the stall and drops `engine_events` before waiting;
  - `Session::shutdown(self)`;
  - the goal evidence tail, the amortized trim, keeping the previous goal
    on a failed start, and the bounded load;
  - drop the duplicated `#[source]`;
  - private `Goal` fields with getters and constructors;
  - `journal()` accessors;
  - `Debug` on `Session`, `GoalStore` and `GoalRunner`;
  - `or_list` private.
- [ ] Gate. Commit "Store and core: report every write failure; durable names and renames; safer goals".

### Task 8: CLI (C1, C3, C4 headless, C13, C14)

**Files:** `crates/octet/src/{main.rs,args.rs,headless.rs}`, `crates/octet/tests/headless.rs`.

- [ ] Failing tests:
  - `rpc_answers_go_ahead_of_queued_prompts`;
  - `rpc_quits_while_stdin_stays_open`;
  - `print_says_when_the_session_ends_unfinished` (fixture `vanish`: the
    vendor exits mid-turn);
  - `a_print_prompt_may_start_with_dashes` (both `-p "--x"` and
    `--print=--x`);
  - `help_lists_version`;
  - `a_relative_home_is_ignored`.
- [ ] Implement:
  - the answer bypasses the queue;
  - stdin is read on a `std::thread`;
  - the unfinished-session message;
  - `--print` takes the next argument verbatim, and `--print=`;
  - the `--version` help line;
  - the `HOME` filter.
- [ ] Gate. Commit "CLI: answers go first; quit exits; prompts may start with dashes".

## Phase D: TUI

### Task 9: TUI safety fixes (V1, T1, T2, T4–T10, T12)

**Files:** `crates/octet-tui/src/{lib.rs,app.rs,input.rs,commands.rs,shell.rs,jobs.rs,
composer.rs,editor.rs,text.rs,registry.rs,terminal.rs}` and the tests.

**Produces:**
- `const APPROVAL_ARM: Duration = 400 ms`;
- `enum StatusKind { Plain, QuitHint, Cancelling }`;
- `fn resolve_path(root, home, arg) -> PathBuf`;
- `fn can_reconnect(&App) -> Result<(), &'static str>`;
- `struct Signals`;
- `octet_engine::APPROVAL_DEMO`.

- [ ] Failing tests:
  - `dropping_a_shell_run_kills_its_group` (shell.rs);
  - `an_answer_key_right_after_an_approval_is_ignored`;
  - `the_panic_hook_restores_only_on_the_loop_thread` (unit test of the
    predicate);
  - `a_job_carries_into_the_next_session` (kept and fresh);
  - `quit_waits_only_for_an_export`;
  - `esc_while_connecting_does_not_leave_cancelling`;
  - `cancel_interrupts_before_saving_the_goal` (`RecordingVendor` order
    against a goal-store failure);
  - `paste_keeps_tabs`;
  - `export_resolves_against_the_workspace`;
  - `reconnecting_commands_share_one_guard`;
  - `resume_without_an_argument_shows_usage`;
  - `a_queued_prompt_that_fails_to_send_is_kept`;
  - `paste_into_an_overlay_hints`;
  - `commands_split_on_any_whitespace`;
  - `size_label_says_kib`.
- [ ] Implement the fixes as the spec rows say:
  - `Group` guard and `ShellTask::drop`;
  - approval timestamps;
  - the panic hook's thread check;
  - `Signals` built once in `run`;
  - jobs move into the next `App`, and Quit waits only for an export;
  - `StatusKind`;
  - `/image` off the loop;
  - interrupt before the goal save;
  - `strip` for paste and the editor's text, with tab rendering;
  - `resolve_path`;
  - `can_reconnect`;
  - the small items;
  - private `Editor` fields;
  - `#[must_use]` on `Action`;
  - status writes through helpers.
- [ ] Gate. Commit "TUI: approvals need a deliberate key; shells die with their session; jobs carry over".

### Task 10: TUI event loop (T3, T12 `composer_key`)

**Files:** `crates/octet-tui/src/lib.rs` (gets `Loop` and `Wake`; may split
into `event_loop.rs`), `app.rs`, `composer.rs`, `files.rs`, `input.rs`.

**Produces:**
- `enum Wake`;
- `struct Loop<'t>`;
- `async fn maybe<F: Future + Unpin>(f: Option<&mut F>) -> F::Output`;
- `App::shell: Option<ShellTask>`;
- `Files::Building(oneshot::Receiver<Index>)`;
- `Composer::shell_running()`.

- [ ] Unit tests: `maybe_waits_forever_on_none`,
  `shell_running_follows_the_task`.
- [ ] Refactor:
  - `select!` arms only return `Wake`;
  - handlers become methods;
  - suspend is written once;
  - remove `App::connection`'s repairs;
  - split `composer_key` into `submit_draft` and `tab_key`.

  Expected: `run_session` and `composer_key` are each under 100 lines, and
  the suite is green.
- [ ] Gate. Commit "TUI: the event loop as wake-ups and named handlers".

### Task 11: TUI view and IO (V2–V5, V6 cheap parts, V7–V9)

**Files:** `crates/octet-tui/src/{view.rs,view/transcript.rs,text.rs,editor.rs,external.rs,
files.rs,remote.rs,terminal.rs,shell.rs,mascot.rs}` and the tests.

- [ ] Failing tests:
  - `wrap_never_exceeds_the_width` (CJK, ZWJ, combining marks, lam-alef);
  - `editor_layout_measures_like_ratatui`;
  - `sanitizer_drops_intermediate_escapes`;
  - `an_unterminated_string_ends_at_newline`;
  - `c1_strings_are_dropped`;
  - `scrolling_up_keeps_the_view_full`;
  - `editor_file_is_in_a_private_directory`;
  - `an_editor_path_with_spaces_runs`;
  - `approval_scroll_is_clamped`;
  - `help_rows_match_what_is_drawn`;
  - `a_hung_git_falls_back_to_the_walk` (a `git` stand-in on PATH that
    sleeps);
  - `the_walk_counts_directories`.
- [ ] Implement the fixes:
  - visible lines render by reference;
  - the mascot grid is built once;
  - the V9 small items.
- [ ] Gate. Commit "TUI: grapheme-true wrapping, a stricter sanitizer, a private editor file".

### Task 12: Tests across crates (T11, C18)

- [ ] TUI tests:
  - `write_terminal` writes to a sink under `cfg(test)`;
  - timeouts on `next_user_text`;
  - convert key tests to `RecordingVendor`;
  - phases driven by events;
  - the reconnect helpers' tests move to `reconnect.rs`;
  - tokio `test-util` with `start_paused` for the job and grace tests.
- [ ] `octet` tests:
  - `screen()` skips OSC;
  - `goal()` matches `.json` only;
  - `run()` ignores EPIPE;
  - file walks don't follow symlinks;
  - the credential guard reads lossily;
  - pids via `pid_t::try_from`;
  - widen the remote-control window.
- [ ] Gate. Expected: the test count is unchanged or higher, and
  `cargo test` prints no OSC 52.
- [ ] Commit "Tests: no terminal side effects, bounded waits, events over fields".

## Phase E: policy, CI, docs

### Task 13: Lint policy

**Files:** root `Cargo.toml` `[workspace.lints]`, `clippy.toml`, the crate
roots, and every site the new lints flag.

- [ ] Add the lints the spec lists:
  - `pedantic` with its allow-list;
  - `allow_attributes`, `allow_attributes_without_reason`, `dbg_macro`,
    `todo`;
  - `missing_debug_implementations`;
  - `unreachable_pub`;
  - `#![forbid(unsafe_code)]` in the five crates.
- [ ] Run clippy. Fix each hit or justify it with
  `#[expect(lint, reason = "…")]`. Bare `#[allow]` is gone.
  `too_many_lines` offenders that remain get an `expect` with a reason.
- [ ] Gate. Expected: clippy is clean with the new set.
- [ ] Commit "Lints: pedantic, reasoned expectations, Debug everywhere, no stray pub".

### Task 14: CI and build (P10)

**Files:** `.github/workflows/{check.yml,release.yml}`, `Makefile`,
`Cargo.toml`, the crate manifests, `deny.toml` (new).

- [ ] Workflows:
  - `timeout-minutes: 30`;
  - a weekly cron for the audit;
  - a cargo-deny job (pinned action SHA, looked up with `gh api`);
  - `deny.toml` for licenses (MIT, Apache-2.0, BSD, ISC, Unicode-3.0,
    Zlib), bans (duplicates warn) and sources (crates.io only).
- [ ] Makefile:
  - `rust-check` runs fmt, then clippy, then `cargo doc --no-deps` with
    `RUSTDOCFLAGS=-D warnings`, then the tests.
- [ ] Manifests:
  - `publish = false` in `[workspace.package]`, inherited by every crate;
  - internal path dependencies in `[workspace.dependencies]`.
- [ ] Gate. Run actionlint and shellcheck if they are installed (do not
  install them). Commit "CI: timeouts, scheduled audit, license checks, doc build".

### Task 15: Docs

- [ ] Update every user-visible change:
  - `docs/tui.md`:
    - the approval delay;
    - jobs carry over, and only Quit waits for an export;
    - tabs kept on paste;
    - `/export` resolves against the workspace;
    - the external editor's private directory and `sh -c`;
  - `docs/tui.md` and the README:
    - `--print=TEXT` and prompts that start with dashes;
    - RPC answers go first;
    - `quit` exits at once;
  - `CONTRIBUTING.md`: the lint policy and `#[expect(reason)]`;
  - `docs/rust/protocol-gate.md`;
  - `CHANGELOG.md`.
- [ ] Gate. Commit "Docs: the review's user-visible changes and the lint policy".

## Finish

- Final whole-branch review: a fresh reviewer on the most capable model,
  then one fix pass.
- Live tmux sweep with the fake vendors, plus a tiny real-Claude and
  real-Codex check (approval allow and deny, `!`, `/export`, `--rpc` quit).
- Merge only when the user says.
