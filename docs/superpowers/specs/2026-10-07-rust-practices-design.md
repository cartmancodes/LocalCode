# Rust practices review: design

**Date:** 2026-10-07. **Branch:** `refactor/rust-practices`.
**Input:** a five-reviewer senior Rust review of master `ec2d9e4`, every file
read in full, with pedantic and nursery clippy as leads. The findings were
checked against the code; the reviewers' probes are kept under the session
scratchpad.
**Scope (chosen by the user):** every finding, structural refactors
included, plus `clippy::pedantic` workspace-wide and edition 2024.

## Goal

Fix every correctness and robustness finding test-first, then bring the
workspace to one consistent senior-Rust standard: narrow public APIs,
`Debug` on public types, no unexplained `#[allow]`, functions short enough
to reason about, and a lint policy that keeps it that way.

## Principles

- **Behaviour changes are test-first:** a failing test, then the fix.
  Refactors keep the suite green and add the unit tests named here.
- **Decided and out of scope:** the earlier review's "don't" list (typestate
  `Phase`, a `Config` builder, an event-sink trait, a full `App::event` state
  machine, the demo as a `Protocol`, the gate's `Scenario` enum). Also
  decided: approvals fail closed, one TUI job at a time, and existing
  journal directories are not chmodded.
- **Gate after every task:** `make rust-check` passes and `git status
  --porcelain` is empty.

## Important findings

| ID | Finding | Decided fix | Test |
| --- | --- | --- | --- |
| E1 | A burst of small deltas while the consumer stalls about 1 s overflows the 128-event queue and stops the session (`emit` only does `try_send`). | Backpressure: while `tx.capacity() <= HEADROOM` (32), the driver stops polling frames and waits on `tx.reserve_many(HEADROOM + 1)`. Commands, cancel and timers stay live. `ConsumerOverloaded` remains only for one frame that expands past `HEADROOM`. | 200 one-character Claude deltas with a 1 s consumer pause: all arrive, then `Finished`. |
| E2 | Claude subagent `assistant` and `stream_event` frames (`parent_tool_use_id` set) are emitted as reply text and change `ModelSelected`. | A frame with a non-null `parent_tool_use_id` adds no reply text and no model change. Its tool activity still shows as `Tool`. | Fixture: a main message then a subagent message; the reply holds only main text and the model stays the main one. |
| T1 | A keystroke that lands as an approval arrives answers it (`a` allows). | Each approval records when it was shown. For 400 ms answer keys are swallowed and a hint says the dialog is opening. | `a` straight after `Event::Approval` sends nothing; after the delay it allows. |
| T2 | The panic hook restores the terminal for a panic on any thread, including jobs and index threads that Octet survives. | The hook restores only on the thread that owns the event loop. | Unit test: the hook's thread check (owner thread vs other). |
| T3 | `run_session` is 212 lines. Its arms do the work, suspend is written twice, and `App` mirrors state the loop owns (`shell_running`, `Files::Building`), which `App::connection` repairs on every reconnect. | `Wake` enum: `select!` arms only return a `Wake`. A `Loop` struct holds terminal, input, editor, paint state and signals, with named handlers (`handle`, `apply`, `start_editor`, `finish_editor`, `suspend`). A `maybe()` helper replaces the four "await an Option" blocks. `ShellTask` moves into `App` next to `job`, `Files::Building` holds its receiver, `shell_running()` is derived, and `connection()` loses its repairs. | Suite green, plus unit tests for `maybe` and for derived `shell_running`. |
| V1 | A `!` command's process group survives a reconnect (the `JoinHandle` is dropped and detached) and a quit (`kill_on_drop` kills only the shell). The transcript says it stopped. | In `shell.rs`, a `Group` drop guard kills the group while the shell is still unreaped, declared so it drops before `child`. `impl Drop for ShellTask` aborts the task. | Dropping or aborting `run("sleep 30 \| cat")` leaves no `sleep` running. |
| C1 | An RPC `answer` waits behind a pipelined prompt, so the approval times out and is denied. | Once `ready`, `answer` is sent at once, like `interrupt`. | Headless test: prompt `approval`, prompt `hello`, then `answer allow` gives the allowed reply with no timeout. |
| C2 | tokio `write_all` only queues bytes, and `sync_data` returns `Ok` even when the queued write failed. Journal records can be lost while reported durable, and a goal save can install a truncated file. | `write_all(..).await?; flush().await?` on every journal append, and `flush` before `sync_data` in the goal save. | A child process with `RLIMIT_FSIZE` sees `append` fail, and a goal save fails, leaving the old file. |
| C3 | `--rpc` does not exit after `quit` while the client keeps stdin open: the blocking stdin read holds the runtime on drop. | Read RPC stdin on a dedicated `std::thread` feeding a channel. The runtime then never waits on it. | `quit` with stdin held open exits 0 within 5 s. |
| P1 | `shutdown()` aborts the stderr task straight after reaping, so the vendor's final error line is lost about 0.1% of the time. | Await the stderr task for up to 100 ms (`STDERR_DRAIN`), then abort. | Deterministic test: the child writes stderr, closes stdout and exits; `shutdown()` without draining frames; plus an `#[ignore]` stress test of 500 runs. |
| P2 | The gate builds its own Claude command line: it lacks `--include-partial-messages`, and it uses `--resume=ID` where Octet passes `--resume ID`. It also copies the process limits by hand. | The engine exposes `launch_args(engine, &Config)` and `vendor_process(executable, args, cwd) -> ProcessConfig`. The gate uses both and appends only its own extras. The gate doc's `initialize` row is corrected. | Unit test: the gate's argv for a scenario begins with the engine's argv. |

## Minor findings

### Engine

- **E3:** `Instant + Duration` overflow panics, with no `Stopped`. Add a
  helper `deadline_after(d)` using `checked_add` (saturating far in the
  future), and have `spawn` turn a driver panic into `Error` plus `Stopped`.
  Test: `approval_timeout = Duration::MAX`.
- **E4:** `Handle::send` races `interrupt`. Reserve the queue slot
  (`try_reserve`) before counting the turn.
- **E5:** Esc while interrupting resends the interrupt and pushes the
  deadline out. Return early in `Interrupting`. Test: a second cancel keeps
  the deadline.
- **E6:** Vendor strings skip the checks:
  - a session ID that fails `valid_identifier` is ignored, with a notice;
  - the Codex unknown status and `Request declined: {method}` go through
    `limited`.

  Test: a session ID with an escape sequence never reaches `Ready`.
- **E7:** The image read inside the select body has no time limit. Wrap it
  in a 10 s `timeout`; a timeout fails the turn with a message.
- **E8:** In the demo, cancelling during the approval dialog ends
  `Completed`. `approval()` returns allowed, denied or cancelled, and a
  cancel ends `Interrupted`.
- **E9:** API and idiom:
  - `Provider.start`, `StartFn`, `BoxFuture` and `Channels` become
    `pub(crate)`;
  - `Protocol::stray_reply` becomes per-vendor free functions;
  - add a comment on why `Core` holds function pointers;
  - merge the `prompt_bytes` arms;
  - `write!` instead of `push_str(&format!)`;
  - check `contains` before the cap;
  - `push_bounded` uses a `VecDeque`.
- **E10:** Tests:
  - the handshake test waits on a marker file;
  - the chatter test's timing assert gets a wider bound, based on the limit;
  - add tests for unusual vendor output: non-object frames, fields with the
    wrong type, control characters in IDs.

### TUI core

- **T4:** Signals are registered once in `run` (a `Signals` struct passed to
  each session). They are checked before the next session opens, so a
  signal during a reconnect quits.
- **T5:** A job no longer blocks a reconnect. It moves into the next `App`,
  kept or fresh, and its arm picks it up there. Only Quit waits, up to
  `GRACE`, and only for an export; other jobs are aborted. This replaces
  the 5 s freeze.
- **T6:** Esc while connecting leaves "Cancelling…" on the status line.
  `Stopped` clears it. A `StatusKind` enum (`Plain`, `QuitHint`,
  `Cancelling`) replaces comparing status strings.
- **T7:**
  - `/image` resolves and opens its file off the loop (`off_loop`, with
    the Tab wait);
  - `cancel_turn` interrupts the vendor before saving the paused goal.
- **T8:** Paste and the external editor's text both go through `strip`,
  which keeps tabs. The composer renders a tab as spaces to the next stop
  of four, and the cursor measures it the same way.
- **T9:** `/export PATH` resolves against the workspace and expands `~/`,
  through a shared `resolve_path` (renamed from `image_path`).
- **T10:** One `can_reconnect(app)` check is used by `try_command` and by
  the reconnecting branches of `/model`, `/mode` and `/effort`.
- **T11:** Tests:
  - `write_terminal` writes to a test sink, so no real OSC 52 reaches your
    terminal;
  - `next_user_text` has a 5 s timeout;
  - key tests use `RecordingVendor` unless they need a session;
  - tests drive phases with events, not by setting fields;
  - the reconnect helpers are tested only in `reconnect.rs`;
  - the job and grace tests use tokio `start_paused`.
- **T12:** Small items:
  - `/resume` with no argument gets a usage message;
  - a queued prompt whose send fails goes back to the front, with an error;
  - a paste while an overlay is open gets a hint;
  - one `APPROVAL_DEMO` constant shared with the engine;
  - `Editor` fields become private;
  - `#[must_use]` on `Action`;
  - commands split on any whitespace;
  - every status write goes through `hint`, `note` or `error`;
  - `size_label` says KiB;
  - `composer_key` is split into `submit_draft` and `tab_key`.

### TUI view and IO

- **V2:** `wrap` measures words as the sum of their grapheme widths, as
  ratatui draws them. The editor's layout gets the same check. Tests: CJK,
  emoji ZWJ, combining marks, lam-alef; no row exceeds the width.
- **V3:** The sanitizer:
  - add an `EscIntermediate` state (0x20–0x2F stay in it, 0x30–0x7E end
    it);
  - the C1 string introducers (0x90, 0x98, 0x9E, 0x9F) start a string;
  - a string ends at `\n` or after 4 KiB;
  - merge the identical arms.

  Tests: `tput sgr0` output, an unterminated OSC, C1 strings.
- **V4:** Scrolling clamps so the page is never shorter than the view when
  there is history to fill it.
- **V5:** The external editor:
  - its file lives in a private 0700 directory with a random name, removed
    on drop;
  - `$VISUAL`/`$EDITOR` run as `sh -c '<editor> "$@"' octet-editor PATH`,
    as git does;
  - the unreachable `NoEditor` goes.
- **V6 (ruling: not changed):** re-wrapping the streaming entry each frame
  costs about 8% of a core while a long reply streams (measured). That is
  acceptable, and an incremental wrap adds state for little gain.
  - The cheap per-frame fix is taken: render visible lines by reference.
  - The mascot's grid is built once.
- **V7:** One wrap function decides rows for both help and the approval
  dialog, and both are drawn unwrapped. `approval_scroll` is clamped in the
  view.
- **V8:** `git ls-files` gets a 10 s watchdog, then falls back to the walk.
  The walk counts directories against its limit.
- **V9:**
  - move the misplaced `report()` doc comment;
  - explain the up-to-100 ms join in `InputReader::drop`;
  - remove the `unix_signal` wrapper;
  - `read_tail` uses a `VecDeque`.

### CLI, core, store

- **C4:** A consumer stall that ends the pump journals an `error` record
  first. Headless reports "the session ended without finishing the turn"
  when events close without `Stopped`.
- **C5:** `Session::shutdown(self)` consumes the session.
- **C6:** Goal evidence keeps the text just before the marker (its last
  4096 characters).
- **C7:** `observe_text` trims only past twice the limit, then back to the
  limit.
- **C8:** If `GoalRunner::start`'s save fails, the previous goal stays in
  memory.
- **C9:** `GoalStore::load` reads at most the limit plus one byte.
- **C10:** Fsync the parent directory after creating a journal and after
  renaming the goal file. The docs say durable.
- **C11:** Journal names add a process-wide counter.
- **C12:** `export_journal` removes a partial target on failure.
- **C13:** `--print` takes the next argument as the prompt whatever it
  starts with, and accepts `--print=TEXT`. `--version` is listed in
  `--help`.
- **C14:** A relative `HOME` is ignored, as a relative `XDG_DATA_HOME`
  already is.
- **C15:** Error variants whose message includes the cause drop
  `#[source]`, so chain reporters don't print it twice.
- **C16:** API:
  - `Goal`'s fields become private, with getters and constructors (the
    `testing` feature hooks remain);
  - `Session::journal()` and `Journal::path()` become accessors;
  - add `Debug` to `Session`, `GoalStore`, `GoalRunner`, `Process`,
    `ProcessSender`, `GateProcess`;
  - `model::or_list` becomes private.
- **C17:** `pump` drops the engine's receiver before waiting for the driver
  to stop.
- **C18:** Tests:
  - the PTY `screen()` skips OSC strings;
  - `goal()` only matches `.json`;
  - `run()` ignores a write error to a child that exited;
  - file walks don't follow symlinked directories;
  - the credential guard reads bytes lossily;
  - pids use `pid_t::try_from`;
  - the remote-control test's window is widened;
  - `wait_child` kills its child on timeout.

### Process, testkit, gate

- **P3:** The gate reports a protocol error instead of panicking on an
  approval without an ID. Its workspace is a `TempDir` guard, so every exit
  path cleans up.
- **P4:** Whitespace-only lines are skipped. Test: `{"a":1}`, a blank line,
  then `{"b":2}` gives two frames.
- **P5:** The fake vendor becomes a directory binary: `main.rs`,
  `wire.rs`, `codex.rs`, `claude.rs`.
  - Each vendor gets a state struct with one method per wire method.
  - Scenarios are matched on constants.
  - `scenario` gains the goal prefix, the reply texts and the approval IDs.
  - The goal prefix is a constant that `octet-core` pins.
- **P6:**
  - remove the unused `ProcessError::QueueClosed`;
  - derive `shutdown_complete` from the report;
  - `Drop` kills the group once, alongside `kill_on_drop`.
- **P7:**
  - a frame keeps its buffer and copies out the exact bytes;
  - the stderr ring takes the tail of each read at once.
- **P8:** `Process::spawn` documents that it needs a Tokio runtime.
- **P9:** The gate:
  - `codex()` is split into `handshake`, `open_thread`, `run_turn` and
    `session_ops`;
  - the counters become an `Evidence` struct;
  - `receive` dispatches once;
  - the trace flag is read once;
  - `GateProcess` exposes less.
- **P10:** CI and build:
  - `timeout-minutes: 30` on each job;
  - a weekly audit run;
  - `cargo deny check licenses bans sources` in CI with a `deny.toml`;
  - `cargo doc --no-deps` with `-D warnings` in `rust-check`;
  - clippy before the tests;
  - `publish = false` for the whole workspace;
  - internal path dependencies in `[workspace.dependencies]`.
- **P11:** Transport tests:
  - wait for the queue to fill instead of sleeping;
  - add tests: drop without shutdown kills the group; a send after exit
    gives `BrokenPipe`, then `StdinClosed`; invalid JSON after valid
    frames.

## Toolchain and lint policy

- **Edition 2024** for every crate, via `cargo fix --edition` and review.
  `unsafe_op_in_unsafe_fn` becomes the default.
- **`[workspace.lints.clippy]`:**
  - `pedantic = { level = "warn", priority = -1 }`, with allow
    `must_use_candidate`, `module_name_repetitions`, `similar_names` and
    `struct_excessive_bools`; add `#[must_use]` by hand where dropping a
    value is a bug;
  - `allow_attributes_without_reason`, `allow_attributes`, `dbg_macro` and
    `todo` at warn;
  - `too_many_lines` stays on. A function still over the threshold after
    T3, P9 and T12 carries `#[expect(clippy::too_many_lines, reason =
    "…")]`.
- **`[workspace.lints.rust]`:** `missing_debug_implementations` and
  `unreachable_pub`. The crates' style becomes `pub(crate)` for crate-only
  items; `redundant_pub_crate` stays off, since it conflicts.
- **`#![forbid(unsafe_code)]`** in octet, octet-core, octet-store,
  octet-engine and octet-gate.
- **Left off, as noise or contested:** `use_self`, `redundant_pub_crate`,
  `missing_const_for_fn`, `option_if_let_else`,
  `significant_drop_tightening` (false positives here), `future_not_send`
  (the loop runs on the main thread), `expect_used`.

## Order

1. Edition 2024.
2. octet-proc.
3. Engine fixes.
4. Engine API and launch contract.
5. Gate.
6. Testkit.
7. Store and core.
8. CLI.
9. TUI safety fixes.
10. TUI event loop.
11. TUI view and IO.
12. Tests across crates.
13. Lint policy.
14. CI and build.
15. Docs.

Bug fixes come before refactors, so each refactor runs against tests that
already pin the fixed behaviour. The lint policy comes last, so it sweeps
the final code once.

## Done when

- Every row above has a test or a ledgered ruling.
- `make rust-check` passes under the new lint set, `cargo doc` and
  `cargo deny` are clean, and the tree is clean.
- A live sweep passes: the fake vendors in tmux, plus a tiny real-Claude
  and real-Codex check (approval allow and deny, `!`, `/export`).
- A fresh final reviewer has reviewed the whole branch.
