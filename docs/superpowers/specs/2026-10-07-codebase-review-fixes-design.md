# Whole-codebase review fixes: design

Date: 2026-10-07. Status: approved in conversation ("Looks good proceed with
implementation"). Source: the consolidated review of `master` at `a0c97a9`
(three reviewers: engine/proc/testkit; TUI; core/store/gate/CLI/CI).

## Intent

Fix every Important and Minor finding of that review, and apply the patterns
it recommended. Leave the patterns it advised against.

**Constraints**
- No new dependencies.
- `make rust-check` passes after every task.
- A behaviour change gets a failing test first; a refactor keeps the suite
  green and adds the unit tests named here.
- Claude, Codex and the demo behave as before, except where a finding is a
  bug.

**Out of scope** (the review's "don't" list, and the reviewers' raw nits):
typestate for `Phase`; a `Config` builder; an event-sink trait for print and
RPC; a full state machine for `App::event`; the demo as a `Protocol`; the
gate's `Scenario` enum; pedantic-clippy and naming nits not on the same lines
as a fix.

## Phase A: engine, process, testkit

### A1. Turn invariants live in `Core`

- `Core::finish_turn(outcome, error: Option<String>)`: phase `Idle`, close
  pending approvals, emit `Error` if given, emit `Finished`. Every turn end
  uses it: Codex `turn/completed`, a rejected Codex request, Claude `result`,
  Claude's failed send, the driver's cancel-before-start.
- `MAX_PENDING_APPROVALS = 8`, enforced in `Core::queue_approval` for every
  vendor. Over the cap: deny with the vendor's reply and emit a notice. The
  Claude deny message says why ("too many approvals waiting"), not "user or
  timeout".
- `Core::adopt_mode(requested, reported)`: confirm and emit `ModeChanged`.
  `mode.rs` keeps only provider-neutral code; Claude's and Codex's mappings
  move into their own files.
- Claude `result`: whether it failed is read once from `is_error`.

**Tests:** the fake Claude gains `approval` (sends `can_use_tool`, echoes the
answer's `behavior` and `updatedInput`) and `approval-cancel` (sends
`can_use_tool`, then `control_cancel_request`). Live tests, both vendors:
allow, deny, timeout, the cap; Claude: cancel request.

### A2. Codex correlation and lost text

- `CodexProtocol` owns its wire IDs: `next_id` and
  `requests: HashMap<u64, Outstanding>` with
  `Outstanding { Initialize, OpenThread, ModelList, Start, Interrupt, Steer(String) }`.
  One `request(kind, method, params)` sends and records. `Core.request_id`
  numbers approvals only.
- A steer held for `turn/started` that never goes out (the turn is cancelled,
  or `turn/start` is refused) becomes a notice "This steer was not sent: …".
- When the vendor cannot be confirmed stopped, the vendor's own error is
  kept and the verification failure is added to it.

### A3. Bounds and one source of truth

- `ImageAttachment::read_base64` reads through `take(IMAGE_LIMIT + 1)`.
- Claude `late_modes` and Codex `pending_steer` keep at most 8 entries.
- One frame's text is cut at 2 MiB with a visible marker, so one reply cannot
  fill the 128-event queue and stop a healthy session.
- The approval window comes from `Config.approval_timeout` only;
  `Limits.approval` goes.

### A4. One cancel gate

`TurnGate { taken }` with `cancelled(&mut self, command, &mut watch::Receiver<u64>) -> bool`
(using `borrow_and_update`), used by the driver and the demo. Notice texts the
demo shares with the driver become constants.

### A5. Test infrastructure

- The fake vendor: JSON builders (`codex_turn_completed`,
  `claude_control_response`, `claude_init`, `claude_result`), and
  `octet_testkit::scenario` constants for every script prompt, used by the
  fixture and the tests.
- `live.rs` helpers: `stop(handle, task)` (always with a timeout),
  `claude_config()`, `expect_error(events)`, `wait_for_within`. Fixed sleeps
  become waits on markers the fixture emits.
- Vendor tests move from `driver.rs` into the vendor files. Stale comments
  ("IDs 1–4", "the old flags", the hard-coded client version) are fixed.

## Phase B: TUI

### B1. One way to start a turn

- `trait Vendor { fn send(&self, Command) -> Result<(), SendError>; fn interrupt(&self); }`,
  implemented for `Handle`; a recording fake for tests.
- `Prompt { wire, display, images }` is what the queue holds.
- `App::begin_turn(vendor, command, By::User | By::Goal, status)` is the only
  path that sends a turn and marks it started.
- Command handlers receive the vendor, so `Action` keeps only effects the
  loop owns (exit, suspend, editor, shell, background jobs, keep-draft).

### B2. One guard and one reporting rule

- The registry row says what a command needs:
  `requires: Requires::{Nothing, Connected, Idle}`; `try_command` checks it
  once, with one message per requirement.
- `App.notice` (status line only) becomes `status_line`. `app.note()`
  (transcript and status line) for information, `app.hint()` (status line)
  for refusals, `app.error()` for failures.

### B3. Reconnects as a plan

- `reconnect::plan(exit, &config, &connection, &mut binaries) -> Option<Plan>`
  is pure and unit-tested per `Exit` variant; the loop applies the plan.
- `Connection::new(config, journal)`; a reconnect rebuilds the connection with
  struct update, keeping only what must survive.
- `cancelling` becomes `ConnPhase::Running { cancelling }`.

### B4. Fixes

- NO_COLOR: a `Theme` maps every colour to `Reset` when set.
- SIGINT is registered once, like SIGTERM, SIGHUP and SIGTSTP.
- `/sessions` and `/export` run as background jobs; one Tab listing runs at a
  time; the file index uses `off_loop`.

### B5. Structure and text

- `lib.rs` → `terminal.rs` and `reconnect.rs`; transcript rendering moves from
  `app.rs` to `view/transcript.rs`; `commands.rs` → `registry.rs` and the
  handlers; Enter's arm → `submit()`; `tests.rs` splits by module.
- Limits in messages are formatted from their constants; repeated messages
  become constants; plurals are right.
- Test helpers: `app_for(engine)`, state set through events, the recording
  vendor. Timing assertions get wider upper bounds.

## Phase C: core, store, CLI, CI

### C1. Headless

`open_headless` registers signals then opens the session; `print` and `rpc`
run their loops in functions and always shut the session down. An argument
prompt over the limit is refused before a session opens. RPC uses the
engine's now-public `Command::starts_turn`.

### C2. CLI

`parse_args(args) -> Result<Args, CliError>`; `CliError::Usage` exits 2,
`CliError::Run` exits 1. Options are listed once. `Config` is built with
`..Config::new()`. A `--cwd` that is not UTF-8 is refused. Startup-error tests
become unit tests of `parse_args`.

### C3. Core and store

- `Session`'s journaling task becomes `pump`, with `deliver()` treating a
  closed receiver as undelivered; its timeouts are named.
- `GoalRunner`: `turn: Option<String>` replaces `running` and `output`; one
  `pause_and_save`; `goal` is private with an accessor; messages come from
  `GoalError`.
- A failed goal save removes its temp file.
- `catalog_hint` moves to the TUI.
- `octet_store::FORMAT`; journal and goal directories are created 0700.
- One journal-fixture writer in `octet-testkit`.

### C4. Test infrastructure

- The credential guard flags any string literal containing a secret suffix,
  and also scans `scripts/` and `.github/`.
- The line-length check walks every product crate.
- `write_script`, `prepend_path`, `wait_child` and `line_reader` move to
  `octet-testkit`; CLI tests move out of the PTY file.

### C5. CI and docs

- `check.yml`: `permissions: contents: read` (the audit job also
  `checks: write`) and a concurrency group.
- The release workflow pins actions to commit SHAs and runs each built binary
  (`--version`, and a demo `-p`) before upload.
- Stale "preview", "v3" and "Python" text is corrected; the journal directory
  name stays and is documented as legacy.
- `docs/rust/protocol-gate.md` and a `make gate` target.

## Finish

A fresh-context review of the whole branch, a fix pass, a live tmux run and a
tiny real-Claude check.
