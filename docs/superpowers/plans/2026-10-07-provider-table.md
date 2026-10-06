# Provider Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adding a vendor CLI becomes one file plus one table row. Claude,
Codex and the demo behave exactly as before.

**Architecture:**
- `Engine` becomes a `Copy` handle onto a `Provider` row.
- The vendor driver splits into a shared `Core` plus one `Protocol` trait
  implementation per vendor.
- The driver's four flags and the TUI's three become two phase enums.

**Tech Stack:** Rust 1.98.1, tokio, serde_json, thiserror. No new
dependencies.

**Spec:** `docs/superpowers/specs/2026-10-07-provider-table-design.md`

## Global Constraints

- **Toolchain:** run every cargo command through `scripts/rust-env.sh`.
- **Gate:** `make rust-check` passes after every task (fmt, all tests including
  doctests, clippy with `-D warnings`). The workspace lints include
  `unwrap_used`, `missing_docs`, `missing_errors_doc` and
  `undocumented_unsafe_blocks`.
- **Behaviour is frozen:** wire messages, event order, journal records,
  notice text and timings are unchanged. Move method bodies verbatim and
  change only the receiver paths (`self.` → `core.` / `self.`).
- **Dependencies:** none new; `async-trait` is not used.
- **Rules:** follow `.claude/skills/rust-engineer/SKILL.md` (docs on public
  items, `expect` with reasons, no `unwrap` in product code).
- **Commits:** every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A Codex frame from another thread during a turn** must not reset the
   silence watchdog: `is_progress` is false for it. Test: the live `chatter`
   script already covers it, and must still pass.
2. **Claude reporting its session ID mid-turn** must not move the TUI phase
   out of `Running`. Task 6, `ready_mid_turn_keeps_running`.
3. **Cancelling during the Codex handshake** (before `Idle`) must still end
   with `DriverError::Cancelled`. Task 4,
   `cancel_while_starting_is_cancelled`.
4. **An approval's oversized detail** must still be denied using the
   vendor's own deny shape after `answer` moves behind the trait. The
   existing live test for oversized approvals must still pass.
5. **`Engine` equality, hashing and debug output** must use the name, so the
   TUI's per-provider binary map and `assert_eq!` keep working. Task 1,
   `engines_compare_and_hash_by_name`.

---

### Task 1: `Engine` is a handle onto the provider table

**Files:**
- Modify: `crates/octet-engine/src/live/mod.rs` (`Engine`, a new `Provider`,
  `Channels` and `BoxFuture`, `spawn_with_limits`)
- Modify: `crates/octet-engine/src/live/{codex,claude,demo}.rs` (a `PROVIDER`
  const each)
- Modify: `crates/octet-engine/src/live/mode.rs` (`describe` reads the row)
- Modify: `crates/octet-core/src/model.rs` (`configure` uses `default_binary`)
- Modify: every test that names `Engine::Codex`, `Engine::Claude` or
  `Engine::Demo` → `Engine::CODEX`, `Engine::CLAUDE`, `Engine::DEMO`

**Interfaces:**
- Produces:
  - `pub struct Provider`, with fields `name`, `title`, `default_binary`,
    `offline`, `modes: [&'static str; 4]` and `start: StartFn`;
  - `pub type StartFn = fn(Config, Limits, Channels) -> BoxFuture<Result<(), String>>`;
  - `pub type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send>>`;
  - `pub struct Channels { commands, cancel, stopping, events }`;
  - `Engine` methods `CODEX`, `CLAUDE`, `DEMO`, `ALL: &[Engine]`,
    `new(&'static Provider)`, `provider()`, `offline()`, `title()`, plus the
    existing `parse`, `as_str` and `is_vendor`.

- [ ] **Step 1: Write the failing tests** in the `live/mod.rs` tests:

```rust
#[test]
fn engines_compare_and_hash_by_name() {
    use std::collections::HashMap;
    let copy = Engine::parse("codex").unwrap();
    assert_eq!(copy, Engine::CODEX);
    assert_ne!(Engine::CODEX, Engine::CLAUDE);
    let mut binaries = HashMap::new();
    binaries.insert(Engine::CODEX, "a");
    assert_eq!(binaries.get(&copy), Some(&"a"));
    assert_eq!(format!("{:?}", Engine::CLAUDE), "claude");
}
#[test]
fn the_table_is_complete_and_unambiguous() {
    let names: Vec<&str> = Engine::ALL.iter().map(|e| e.as_str()).collect();
    assert_eq!(names, ["codex", "claude", "demo"]);
    for engine in Engine::ALL {
        assert_eq!(Engine::parse(engine.as_str()), Some(*engine));
        assert!(engine.provider().modes.iter().all(|m| !m.is_empty()));
        assert!(!engine.title().is_empty());
    }
    assert_eq!(Engine::ALL.iter().filter(|e| e.offline()).count(), 1);
    assert!(Engine::DEMO.offline() && !Engine::DEMO.is_vendor());
}
```

- [ ] **Step 2: Run them.** Command:
  `scripts/rust-env.sh cargo test -p octet-engine --lib engines_compare`.
  Expected: they don't compile (`Engine::CODEX` doesn't exist).
- [ ] **Step 3: Implement it** in `live/mod.rs`. Replace the `Engine` enum
  and its `impl` with:

```rust
/// One backend Octet can drive. A new vendor is one of these plus its
/// `Protocol` file (see docs/rust/adding-a-provider.md).
pub struct Provider {
    /// The CLI flag, journal and `/model` spelling.
    pub name: &'static str,
    /// The name in notices ("Codex reports …").
    pub title: &'static str,
    /// The command to run when `--binary` is absent.
    pub default_binary: &'static str,
    /// No vendor process and no model (the demo).
    pub offline: bool,
    /// `/mode` descriptions, in `Mode::ALL` order.
    pub modes: [&'static str; 4],
    /// Runs one session until it stops.
    pub start: StartFn,
}
/// Starts a provider's session.
pub type StartFn = fn(Config, Limits, Channels) -> BoxFuture<Result<(), String>>;
/// A boxed future that can move between threads.
pub type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + Send>>;
/// The channels a session runs on.
pub struct Channels {
    /// Commands from the interface.
    pub commands: mpsc::Receiver<Command>,
    /// Bumped to cancel the running turn.
    pub cancel: watch::Receiver<u64>,
    /// Set to stop the session.
    pub stopping: watch::Receiver<bool>,
    /// Events to the interface.
    pub events: mpsc::Sender<Event>,
}
/// A provider, by its row in the table. Compares, hashes and prints by name.
#[derive(Clone, Copy)]
pub struct Engine(&'static Provider);
impl Engine {
    /// Codex.
    pub const CODEX: Engine = Engine(&codex::PROVIDER);
    /// Claude Code.
    pub const CLAUDE: Engine = Engine(&claude::PROVIDER);
    /// The offline demo.
    pub const DEMO: Engine = Engine(&demo::PROVIDER);
    /// Every provider, in the order help lists them. This is the table.
    pub const ALL: &'static [Engine] = &[Engine::CODEX, Engine::CLAUDE, Engine::DEMO];
    /// A handle onto a provider row.
    pub const fn new(provider: &'static Provider) -> Engine { Engine(provider) }
    /// The provider's row.
    pub fn provider(self) -> &'static Provider { self.0 }
    // parse (same doctest as today), as_str (self.0.name), is_vendor (!offline),
    // offline (self.0.offline), title (self.0.title).
}
impl PartialEq for Engine { fn eq(&self, other: &Self) -> bool { self.0.name == other.0.name } }
impl Eq for Engine {}
impl std::hash::Hash for Engine { fn hash<H: std::hash::Hasher>(&self, h: &mut H) { self.0.name.hash(h) } }
impl std::fmt::Debug for Engine { fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str(self.0.name) } }
```

  `Display` stays as it is today (`as_str`).

  In `spawn_with_limits`, replace the demo/vendor `if` with:

```rust
let channels = Channels { commands: rx, cancel, stopping, events: events.clone() };
let result = (config.engine.provider().start)(config, limits, channels).await;
```

  The final `Error`/`Stopped` sends stay on the original `events`.
- [ ] **Step 4: Add the rows.**
  - `codex.rs`: `pub(super) const PROVIDER: Provider` with `name: "codex"`,
    `title: "Codex"`, `default_binary: "codex"`, `offline: false`, `modes`
    (the four Codex strings moved verbatim from `mode.rs`), and `start: start`.
  - `codex.rs` also gets
    `fn start(config: Config, limits: Limits, channels: Channels) -> BoxFuture<Result<(), String>> { Box::pin(async move { vendor(config, limits, channels).await }) }`.
  - `claude.rs`: the same with the Claude strings.
  - `demo.rs`: `name: "demo"`, `title: "Demo"`, `default_binary: "demo"`,
    `offline: true`, the demo strings, and a `start` wrapping
    `demo(config.mode, limits.approval, channels.commands, channels.cancel, channels.stopping, &channels.events)`
    with `.map_err(|e| e.to_string())`.
  - Change `vendor`'s signature to `vendor(config, limits, channels: Channels)`
    and destructure inside. Its `match config.engine` picks the protocol
    with `if config.engine == Engine::CLAUDE { Protocol::Claude } else { Protocol::Codex }`
    until Task 3.
- [ ] **Step 5: Mode descriptions.** `Mode::describe` becomes
  `engine.provider().modes[self.index()]`, with `fn index(self) -> usize`
  matching `ALL`'s order. Delete the 10-arm match. Its strings now live in
  the rows.
- [ ] **Step 6: Callers.**
  - `Selection::configure`: `PathBuf::from(self.provider.provider().default_binary)`.
  - The TUI and core `== Engine::Demo` comparisons stay as `== Engine::DEMO`
    for now; Task 6 changes them.
  - Rename across all crates with
    `perl -pi -e 's/Engine::Codex\b/Engine::CODEX/g; s/Engine::Claude\b/Engine::CLAUDE/g; s/Engine::Demo\b/Engine::DEMO/g'`
    over `crates/**/*.rs`.
  - `confirm_mode`'s `"Claude"`/`"Codex"` vendor argument comes from
    `Engine::CLAUDE.title()`/`Engine::CODEX.title()`, the same strings.
- [ ] **Step 7: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  previous count + 2. The `/mode` tests that compare descriptions pass
  unchanged.
- [ ] **Step 8: Commit.** Message: "Make Engine a handle onto a provider
  table".

### Task 2: `Core` and the `Protocol` trait; Codex moves first

**Files:**
- Create: `crates/octet-engine/src/live/protocol.rs` (the trait and `Core`)
- Modify: `crates/octet-engine/src/live/driver.rs` (becomes
  `Driver<P: Protocol>` and the generic `run`)
- Modify: `crates/octet-engine/src/live/codex.rs` (`CodexProtocol`)

**Interfaces:**
- Produces:
  - `pub(crate) trait Protocol` as in the spec, with these methods:
    - required: `launch_args`, `initialize`, `send_prompt`, `interrupt`,
      `set_mode`, `on_frame`, `answer`, `stray_reply`;
    - with defaults: `is_progress`, `deadline`, `on_deadline`,
      `turn_started`.
  - `pub(crate) struct Core`, with fields `config`, `limits`, `process`, `tx`,
    `session`, `mode`, `initialized`, `ready`, `running`, `interrupt_pending`,
    `deadline`, `request_id`, `pending` and `answer: fn(&Value, bool) -> Value`.
    The flags remain until Task 4.
  - `Core`'s methods: `send`, `emit`, `queue_approval`, `resume_watchdog`,
    `close_all_pending`, `deny_all_pending` and `answer_wire(wire, allow)`
    (which calls `self.answer`).
  - `pub(super) async fn run<P: Protocol>(config, limits, channels) -> Result<(), String>`:
    today's `vendor` body, generic over `P`.

- [ ] **Step 1: Run the safety net.** Before any change, run
  `scripts/rust-env.sh cargo test -q -p octet-engine` and record the count.
  The 34 live tests plus the unit tests are this task's tests: the change is
  a move with no behaviour change.
- [ ] **Step 2: Write `protocol.rs`.** Define the trait with the signature in
  the spec, each method declared
  `fn x(&mut self, core: &mut Core, …) -> impl Future<Output = Result<(), DriverError>> + Send;`.
  Move these from `Driver` into `Core`, verbatim:
  - the shared fields;
  - `send`, `emit`, `resume_watchdog`, `queue_approval`, `deny_all_pending`
    and `close_all_pending`, where `self.answer(..)` becomes
    `(self.answer)(..)`.
- [ ] **Step 3: Make the driver generic.**
  - `driver.rs` becomes `struct Driver<P> { core: Core, protocol: P }`.
  - `run`, `next_wake`, `timers_armed`, `on_cancel`, `on_timer`,
    `watchdog_error`, `on_command`, `start_turn`, `answer_approval`,
    `set_mode` and `on_frame` keep their bodies, with every
    `match self.protocol { … }` replaced by the trait call on
    `self.protocol` with `&mut self.core`:
    - `initialize` → `self.protocol.initialize(&mut self.core)`;
    - `interrupt`, `send_prompt` and `set_mode` likewise;
    - `on_frame`: `ours` becomes `self.protocol.is_progress(&self.core, &frame)`;
    - `start_turn` resets `turn`, `streamed` and `text_items` through
      `self.protocol.turn_started()`;
    - `next_wake` and `timers_armed` read `self.protocol.deadline()`;
      `on_timer`'s mode-request block becomes
      `self.protocol.on_deadline(&mut self.core).await?`.
  - `launch_args` comes from `P::launch_args(&config)`.
  - `with_stderr` and `seconds` stay as free functions.
- [ ] **Step 4: Move Codex.** In `codex.rs`, define
  `#[derive(Default)] pub(super) struct CodexProtocol { turn, start_request, interrupt_request, text_items, catalog, catalog_pages, catalog_cursors }`
  and `impl Protocol for CodexProtocol`. Each `codex_*` method moves
  verbatim:
  - `codex_initialize` → `initialize`;
  - `codex_send_prompt` → `send_prompt`;
  - `codex_interrupt` → `interrupt`;
  - `codex_set_mode` → `set_mode`;
  - `codex_frame` → `on_frame`;
  - the helpers `codex_response`, `codex_catalog_page`,
    `codex_server_request` and `codex_request_interrupt` become private
    methods taking `core: &mut Core`;
  - `answer` → `Protocol::answer`;
  - `codex_stray_reply` → `stray_reply`, keeping a public wrapper fn
    `codex_stray_reply` for the gate.

  Receiver paths: shared fields go to `core.…`, Codex fields to `self.…`.
  The Codex `is_progress` is today's `ours` test for Codex:
  `frame.pointer("/params/threadId").and_then(Value::as_str).is_none_or(|id| id == core.session)`.
  Codex `start` becomes `Box::pin(async move { driver::run::<CodexProtocol>(config, limits, channels).await })`.
- [ ] **Step 5: Bridge Claude for this commit.** Claude still uses the old
  methods, so for this commit give it a temporary
  `impl Protocol for ClaudeProtocol` that wraps them. Simplest and
  recommended: do Task 3's move in the same working session, and commit
  Tasks 2 and 3 together if a clean intermediate state costs more than it
  shows. Ruling allowed; record it.
- [ ] **Step 6: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  same count as Step 1 (+0).
- [ ] **Step 7: Commit.** Message: "Split the vendor driver into a shared core
  and a Codex Protocol".

### Task 3: Claude moves to `ClaudeProtocol`; the old enums go

**Files:**
- Modify: `crates/octet-engine/src/live/claude.rs` and `driver.rs`, and
  `mod.rs` (`model_catalog`)

- [ ] **Step 1: Move Claude.**
  `#[derive(Default)] pub(super) struct ClaudeProtocol { selected_model, streamed, mode_request, late_modes, mode_seq }`
  and `impl Protocol for ClaudeProtocol`, moved verbatim:
  - `claude_initialize`, `claude_interrupt`, `claude_send_prompt`,
    `claude_request_mode` (→ `set_mode`) and `claude_frame` (→ `on_frame`);
  - `claude_control_response` and `claude_result` become private methods;
  - `deadline()` returns `self.mode_request.as_ref().map(|r| r.deadline)`;
  - `on_deadline` holds the mode-request block moved out of
    `Driver::on_timer`;
  - `turn_started` resets `streamed`;
  - `is_progress` uses the default (`true`).
- [ ] **Step 2: Remove the old enums.**
  - Delete `enum Protocol` (the old enum) and `Driver`'s Claude- and
    Codex-only fields.
  - Split `model_catalog(value, claude: bool)` into `codex::catalog(value)`
    and `claude::catalog(value)`. Both share a private
    `model_catalog_with(value, selection_key, id_key)` in `mod.rs`.
  - Update its test so it calls both.
- [ ] **Step 3: Check that no vendor is matched any more.** Command:
  `grep -nE 'Protocol::(Claude|Codex)|claude: bool' crates/octet-engine/src`.
  Expected: no output.
- [ ] **Step 4: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  same count.
- [ ] **Step 5: Commit.** Message: "Move Claude to its Protocol and drop the
  vendor matches".

### Task 4: The driver's `Phase`

**Files:**
- Modify: `crates/octet-engine/src/live/protocol.rs` (`Core.phase`) and the
  flag sites in `driver.rs`, `codex.rs` and `claude.rs`

- [ ] **Step 1: Write the failing tests.** In `protocol.rs` tests, use a
  `Core` built over the fake vendor, or test the pure helpers below directly:

```rust
#[test]
fn phase_answers_the_old_flag_questions() {
    use Phase::*;
    for (phase, ready, running, interrupting) in [
        (Starting, false, false, false),
        (Handshaken, false, false, false),
        (Idle, true, false, false),
        (InTurn, true, true, false),
        (Interrupting, true, true, true),
    ] {
        assert_eq!(phase.is_ready(), ready, "{phase:?}");
        assert_eq!(phase.is_running(), running, "{phase:?}");
        assert_eq!(phase == Interrupting, interrupting, "{phase:?}");
        assert_eq!(phase.watchdog_applies(), phase != Idle, "{phase:?}");
    }
}
```

  In the live tests, add `cancel_while_starting_is_cancelled`:
  - spawn a Codex session over a fake child that never answers `initialize`
    (the protocol-child `hold` mode, or a script that sleeps);
  - call `handle.interrupt()`;
  - expect `Event::Error` containing "Connection cancelled".
- [ ] **Step 2: Run them.** Expected: they don't compile (`Phase` doesn't
  exist).
- [ ] **Step 3: Implement it.** Add to `protocol.rs`:

```rust
/// Where a vendor connection is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum Phase { Starting, Handshaken, Idle, InTurn, Interrupting }
impl Phase {
    pub(crate) fn is_ready(self) -> bool { self >= Phase::Idle }
    pub(crate) fn is_running(self) -> bool { matches!(self, Phase::InTurn | Phase::Interrupting) }
    /// The connect/turn silence watchdog applies (paused while an approval waits).
    pub(crate) fn watchdog_applies(self) -> bool { self != Phase::Idle }
}
```

  Replace `Core`'s four flags with `phase: Phase` (starting at `Starting`)
  and rewrite each site:

| Site | Before | After |
| --- | --- | --- |
| next_wake | `(!ready \|\| running) && pending.is_empty()` | `phase.watchdog_applies() && pending.is_empty()` |
| timers_armed | `!ready \|\| running \|\| …` | `phase.watchdog_applies() \|\| …` |
| on_cancel | `if !ready` / `if !running` / `interrupt_pending = true` | `!phase.is_ready()` / `!phase.is_running()` / `phase = Interrupting` |
| on_timer, watchdog_error | `!ready`, `interrupt_pending` | `!phase.is_ready()`, `phase == Interrupting` |
| start_turn | `!ready \|\| running` guard; `running = true`, `interrupt_pending = false` | `!phase.is_ready() \|\| phase.is_running()`; `phase = InTurn` |
| set_mode | `!ready` | `!phase.is_ready()` |
| on_frame | `running && !interrupt_pending && ours` | `phase == InTurn && …` |
| on_frame | "Unexpected protocol initialization order" check | delete |
| resume_watchdog | `pending.is_empty() && running && !interrupt_pending` | `pending.is_empty() && phase == InTurn` |
| codex set_mode | `if running` | `if core.phase.is_running()` |
| codex `turn/started` | `&& running`, then `if interrupt_pending` | `&& phase.is_running()`, then `phase == Interrupting` |
| codex frames | `if !running` | `if !core.phase.is_running()` |
| codex turn end, start error | `running = false` | `phase = Idle` |
| codex id 1 | `initialized = true` | `phase = Handshaken` |
| codex id 2 | `ready = true` | `phase = Idle` |
| codex server request | `running && !interrupt_pending` | `phase == InTurn` |
| claude permission | `running && !interrupt_pending` | `phase == InTurn` |
| claude frames | `if !running` | `if !core.phase.is_running()` |
| claude init | `initialized = true; ready = true` | `phase = Idle` |
| claude result | reads `interrupt_pending` twice, then `running = false` | read `let interrupted = phase == Interrupting;` first, use it, then `phase = Idle` |

- [ ] **Step 4: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  previous count + 2.
- [ ] **Step 5: Commit.** Message: "Track a vendor connection as one Phase".

### Task 5: The demo is a provider row (completes Task 1's wiring)

Task 1 already routes the demo through `PROVIDER.start`. This task removes
what is left of the special-casing in `octet-engine`.

- [ ] **Step 1: Find the leftovers.** Command:
  `grep -n 'Engine::DEMO' crates/octet-engine/src`. Expected: only tests and
  the `DEMO` const itself. Any product site found becomes `.offline()`.
- [ ] **Step 2: Run the gate and commit**, only if Step 1 changed something.
  Message: "Treat the demo as just another provider". If nothing changed,
  record "Task 5: nothing left after Task 1" in the ledger.

### Task 6: The TUI `ConnPhase`

**Files:**
- Modify: `crates/octet-tui/src/app.rs` (`Connection`), and the sites in
  `input.rs`, `commands.rs`, `lib.rs`, `view.rs` and the tests

- [ ] **Step 1: Write the failing tests** in `app/tests.rs`:

```rust
#[test]
fn ready_mid_turn_keeps_running() {
    let config = octet_core::Config::new(octet_core::Engine::CLAUDE, "claude", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.event(Event::Ready { session: String::new() });
    assert!(app.is_idle());
    app.event(Event::Started);
    app.event(Event::Ready { session: "s-1".into() });
    assert!(app.conn.is_running());
    app.event(Event::Finished { outcome: octet_core::Outcome::Completed });
    assert!(app.is_idle());
    app.event(Event::Stopped);
    app.event(Event::Ready { session: "late".into() });
    assert!(app.conn.is_stopped() && !app.is_busy());
}
```

- [ ] **Step 2: Run it.** Expected: it doesn't compile (`is_idle` doesn't
  exist).
- [ ] **Step 3: Implement it.**

```rust
/// Where the vendor connection is, as the interface sees it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ConnPhase { Connecting, Idle, Running, Stopped }
```

  `Connection` loses `ready`, `running` and `stopped` and gains
  `phase: ConnPhase`. It also gets these helpers:
  - `is_ready()`: `matches!(phase, Idle | Running)`;
  - `is_running()`: `phase == Running`;
  - `is_stopped()`: `phase == Stopped`;
  - `start_turn()`: `if phase != Stopped { phase = Running }`;
  - `end_turn()`: `if phase == Running { phase = Idle }`.

  `App` gets `is_idle()`: `phase == Idle`. `is_connecting` and `is_busy`
  keep their meanings, rewritten on `phase`.

  Rewrite each site:
  - **Events:**
    - `Ready`: `if phase == Connecting { phase = Idle }` (and today's status
      update when not running);
    - `Started`: `start_turn()`;
    - `Finished`: `end_turn()`;
    - `Stopped`: `phase = Stopped`;
    - `connection()`: `phase = Connecting`.
  - **Writes:**
    - `app.conn.running = true` (prompt sent, goal prompt, continuation):
      `app.conn.start_turn()`;
    - in tests, `app.conn.ready = true`: `app.conn.phase = ConnPhase::Idle`;
    - `app.conn.running = true`: `app.conn.phase = ConnPhase::Running`.
  - **Reads:**
    - `conn.ready` → `conn.is_ready()`;
    - `conn.running` → `conn.is_running()`;
    - `conn.stopped` → `conn.is_stopped()`;
    - `!running && ready && !stopped` → `app.is_idle()`;
    - `!ready || running || stopped` → `!app.is_idle()`.
- [ ] **Step 4: Replace the demo checks.** The three `== Engine::DEMO` sites
  in `app.rs`, `view.rs` and `lib.rs` become `.offline()`.
- [ ] **Step 5: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  previous count + 1.
- [ ] **Step 6: Commit.** Message: "Track the interface's connection as one
  ConnPhase".

### Task 7: The CLI and the gate read the table; the doc

**Files:**
- Modify: `crates/octet/src/main.rs`,
  `crates/octet-gate/src/bin/protocol-gate.rs` and
  `crates/octet/tests/terminal.rs`
- Create: `docs/rust/adding-a-provider.md`
- Modify: `README.md` and `docs/tui.md` (link)

- [ ] **Step 1: Write the failing test** in `terminal.rs`:

```rust
#[test]
fn help_and_errors_name_exactly_the_providers() {
    let names: Vec<&str> = octet_core::Engine::ALL.iter().map(|e| e.as_str()).collect();
    let help = Command::new(env!("CARGO_BIN_EXE_octet")).arg("--help").output().unwrap();
    assert!(String::from_utf8_lossy(&help.stdout).contains(&format!("--engine {}", names.join("|"))));
    let bad = Command::new(env!("CARGO_BIN_EXE_octet")).args(["--engine", "nope"]).output().unwrap();
    assert!(String::from_utf8_lossy(&bad.stderr).contains("Engine must be codex, claude or demo"));
}
```

  It passes on today's text. Make it fail first by checking it against a
  temporarily added fourth fake row is not possible without a provider, so
  this test pins the wording while the strings become generated. Watch it
  pass before and after, and record that.
- [ ] **Step 2: Implement it.**
  - `main.rs` builds the usage line's `codex|claude|demo` from `Engine::ALL`
    names joined by `|`.
  - The error text joins the names as "a, b or c". A helper `fn or_list(&[&str]) -> String`
    turns `["codex","claude","demo"]` into "codex, claude or demo".
  - The gate's `engine != "claude" && engine != "codex"` becomes
    `octet_engine::live::Engine::parse(&engine).filter(|e| e.is_vendor()).is_none()`.
- [ ] **Step 3: Write the doc.** `docs/rust/adding-a-provider.md`:
  - the four steps from the spec's section 7;
  - a skeleton `impl Protocol for XProtocol` listing each method and what
    it must do;
  - a reminder that each method moves the core's `phase` as Codex and
    Claude do.

  Link it from `README.md`'s crate table (the `octet-engine` row) and from
  `docs/tui.md`'s provider section.
- [ ] **Step 4: Run the gate.** Command: `make rust-check`. Expected: exit 0.
- [ ] **Step 5: Commit.** Message: "Build the CLI's provider list from the
  table; document adding a provider".

---

## Finish

- [ ] Run the final whole-branch review, per executing-plans.
- [ ] Do a live tmux run over the demo and the fake Codex and Claude vendors,
  repeating the 2026-10-06 checklist.
- [ ] Update the CHANGELOG with a "Changed" line: provider table internals,
  no user-visible change.
- [ ] Hand off with superpowers:finishing-a-development-branch.
