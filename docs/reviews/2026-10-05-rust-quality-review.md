# Rust workspace quality review and refactor — 2026-10-05

## Scope and result

This review covered the whole Rust workspace at `70b2ee3`, focusing on code
quality and idiomatic Rust rather than features. It found no unsound `unsafe`,
no leaked tasks, and no blocking calls on async threads. The transport,
resource bounds and test suite were already sound. Two other problems made the
code riskier to change than it needed to be:

- The two control-flow hubs were too dense for the tooling.
- Engine identity, turn outcomes and error classification were plain strings.

The refactor on branch `refactor/rust-quality` addresses both. It follows
`docs/superpowers/plans/2026-10-05-rust-quality-refactor.md`. Journals and
vendor wire traffic are unchanged. User-facing text changed in three
deliberate ways, each described below:

- Goal persistence failures are always reported.
- Several error messages now name the step that failed.
- A Codex app-server that answers its startup requests out of order is
  stopped at once.

## Findings and changes

### The vendor driver was invisible to rustfmt and clippy

`lc-engine/src/live.rs` ran both provider protocols in one 270-line
`tokio::select!` block. That block held about 25 shared mutable locals, and
both protocols were interleaved behind `if claude`. rustfmt does not format
inside the macro, so 58 lines exceeded 140 characters while `fmt --check`
still passed. Clippy's length lints could not see the block either.

`live.rs` is now a `live/` module:

- **`mod.rs`**: public types, event emission, `spawn`.
- **`mode.rs`**: the only copy of the permission-mode vendor mapping.
- **`driver.rs`**: a `Driver` struct holds the connection and turn state. Each
  `select!` arm is now a one-line call to `on_cancel`, `on_timer`,
  `on_command` or `on_frame`. Shared helpers replace repeated code: approval
  queueing, the watchdog restart, and closing or denying pending approvals.
- **`claude.rs` / `codex.rs`**: each protocol's frame handler and requests,
  written as `impl Driver` blocks.
- **`demo.rs`**: the offline engine.

Only 7 lines now exceed 140 characters, and each contains a JSON or string
literal.

### Strings stood in for types

- **Engine.** Engine names were string literals throughout the code. Any value
  other than `"claude"` silently ran the Codex protocol. `Engine` is now an
  enum, and `Config::new` replaces hand-written configs in tests. Matching is
  exhaustive, including `Mode::describe`.
- **Turn outcome.** Outcomes were strings, and Codex's raw status passed
  straight through. `Outcome` has `Completed`, `Interrupted`, `Failed` and
  `Other(String)`; `Other` keeps an unknown vendor status verbatim. Goal
  prompts take a `GoalStep` instead of two booleans that could contradict
  each other.
- **Errors.** Whether the vendor's stderr was attached to an error depended on
  matching three message prefixes, so rewording a message changed behaviour.
  `DriverError` now classifies failures by variant, with identical display
  text. `Handle::send` returns `SendError`.

### Goal orchestration lived in the TUI event loop

"Pause the goal and save it" was written 11 times with three different error
policies. Two call sites dropped the error with `let _ =`. Two others
propagated it with `?`, which closed the terminal UI whenever the goal file
could not be written during a reconnect or model switch.

`lc_core::goal::GoalRunner` now owns the goal, its store, the running goal turn
and its bounded output. It is unit-tested without a PTY.

**Deliberate behaviour change:** every goal persistence failure is now shown
as a notice. None is dropped, and none is fatal. A failed `/goal pause` says
"Goal paused, but saving it failed: …". A failed `/goal clear` shows only the
failure, where it used to say "Goal cleared" as well.

The TUI changed in three smaller ways:

- An `Exit` enum, which the compiler checks exhaustively, replaces the
  `_ => break` that treated every unknown `Action` as quit.
- Ctrl+C and Esc share one `cancel_turn` helper.
- `App::is_busy` and `App::is_connecting` replace repeated boolean
  expressions.

### Smaller items

- **Test fixtures.** The fake-vendor fixture was copied three times. It is now
  `lc_testkit::protocol_child()`, which builds for the running profile, so
  `cargo test --release` works. `TempDir` cleans up after a failing test.
  `lc-testkit` no longer declares an unused `libc` dependency.
- **Unix only.** LocalCode is stated as Unix-only with `compile_error!`. Dead
  non-Unix branches are removed.
- **`unsafe`.** Every `unsafe` block has a `SAFETY` comment. Process-group
  signals use `killpg` with a typed `pid_t`, and suspend uses `raise`.
- **Shared helpers.** Six hand-written char-boundary loops now use
  `floor_char_boundary` or `ceil_char_boundary`. The model-identifier check
  (repeated five times) and private-file creation (repeated three times) are
  shared helpers.
- **Sanitizer.** Its state is a named enum instead of the numbers 0–4.
- **`#[must_use]`.** Added to `Process::shutdown` and `Editor::insert`. Tests
  now assert the result of `insert` instead of ignoring it.
- **Error context.** These messages now name the step that failed:
  - **Session start:** "Cannot write transcript journal: …" (this error stops
    the TUI from starting, as before).
  - **`/export`:** "Cannot read journal *path*: …", "Cannot create *path*: …"
    and "Cannot write *path*: …".
  - **Goal file:** "Cannot save goal: …" and "Cannot clear goal: …", inside
    the goal persistence notices.
- **Codex startup order.** The driver checks that initialization finished
  before the session became ready. That check now runs after every Codex
  frame. Previously it ran only after notifications for the session, never
  after replies.
  - If an app-server answers "open session" before "initialize", it now stops
    at once with "Unexpected protocol initialization order".
  - Previously it limped on and could open a second thread.
  - A well-behaved vendor never reaches either path.

## Deferred, with reasons

- **Protocol-gate binary restructure** (`Scenario` enum, statistics struct,
  moving fixtures out of `lc-engine`'s root): it is a developer tool and
  belongs in a separate plan.
- **`App` phase and overlay enums, and an `app.rs` split:** the predicates
  removed the duplication; an enum rewrite would be churn without a defect
  behind it.
- **Typed `lc-core` errors:** every one is shown verbatim and no caller
  branches on them, so they stay `String` with added context.
- **The `claude: bool` parameter on `model_catalog`:** it is internal and has
  one call site per protocol.
- **Byte-at-a-time frame reading in `lc-proc`:** no measurement shows it
  matters.

## Verification evidence

- Baseline at `70b2ee3`: `make rust-check` passed 119 tests, with three opt-in
  live tests ignored.
- After the refactor: `make rust-check` passes formatting, clippy
  `-D warnings` and 134 tests (three opt-in tests ignored). The new tests
  cover:
  - engine parsing;
  - outcome spelling, including unknown vendor statuses;
  - journal spelling of engine and outcome;
  - stderr attachment over typed errors;
  - send-error wording;
  - goal prompts and `GoalRunner`: a failed turn is counted once, the goal
    continues until the completion marker, output is bounded on a char
    boundary, a persistence failure is reported, and cancelling pauses only a
    running goal turn;
  - a failed `/goal pause` still saying the goal stopped;
  - private-file creation and identifier bounds;
  - the CLI rejecting an unknown engine.
- `cargo test --release -p lc-proc --test transport` passes. The fixture is
  built under `target/release`.
- The PTY suite exercises both providers in all four modes, including goal
  continuation, pause and cancel counting, and malformed-goal recovery.
- Live vendors were not rerun for this refactor.
