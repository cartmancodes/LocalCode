# Provider table: design

Date: 2026-10-07. Status: proposed.

## Intent

Octet will support more vendor CLIs soon (for example the Gemini CLI,
OpenCode or Aider). Adding one today means editing about 8 places in 5 files:

- the closed `Engine` enum;
- a second `Protocol` enum;
- eight `match self.protocol` sites in the driver;
- one `Driver` struct holding both Claude's and Codex's state;
- the `(Engine, Mode)` description table;
- a catalog parser switched by a `claude: bool` argument;
- the demo special-cased across the engine and the TUI.

**Goal:** a new provider is **one new file plus one row in a provider
table**. The shared driver, the TUI, `/model` and the CLI need no change.

**Behaviour:** Claude, Codex and the demo work exactly as they do now: the
same wire messages, events, journal records, notices and timings.

**Out of scope:**
- runtime plugins and providers loaded from configuration;
- new dependencies;
- the `protocol-gate` binary's per-vendor scenarios. A new vendor needs new
  contract evidence anyway, so only its name checks move to the table.

## Decisions

### 1. The provider table

`Engine` stays the type every crate uses: `Config.engine`, `--engine`,
journals and `/model` all keep working. It becomes a `Copy` handle onto a
row:

```rust
/// One backend Octet can drive. A new vendor is one of these plus its file.
pub struct Provider {
    pub name: &'static str,           // "codex": CLI flag, journals, /model
    pub title: &'static str,          // "Codex": in notices
    pub default_binary: &'static str, // used when --binary is absent
    pub offline: bool,                // the demo: no process, no model
    pub modes: [&'static str; 4],     // /mode descriptions, indexed by Mode
    pub start: fn(Config, Limits, Channels) -> BoxFuture<Result<(), String>>,
}

pub static PROVIDERS: &[&Provider] = &[&codex::PROVIDER, &claude::PROVIDER, &demo::PROVIDER];

#[derive(Clone, Copy)]
pub struct Engine(&'static Provider); // PartialEq, Eq, Hash, Debug by name
```

- **Lookups:** `Engine::parse`, `Engine::ALL`, `as_str`, `is_vendor` and
  `Display` read the table. Their results and order are unchanged:
  `ALL = [codex, claude, demo]`.
- **Constants:** `Engine::CODEX`, `Engine::CLAUDE` and `Engine::DEMO` replace
  the enum variants.
- **New accessors:** `engine.provider()`, `engine.offline()` and
  `engine.title()`.
- **Mode descriptions:** `Mode::describe(engine)` reads
  `engine.provider().modes`, and the 10-arm match goes.
- **Starting a session:** `spawn_with_limits` calls `(provider.start)(…)`.
  This is the one place a provider is chosen, so the `if config.engine ==
  Engine::Demo` branch goes.
- **Names elsewhere:** `"Claude"` and `"Codex"` in notices come from `title`.
- **`Channels`** bundles what `start` receives: the command receiver, the
  interrupt and stop watches, and the event sender. **`BoxFuture`** is
  `Pin<Box<dyn Future<Output = T> + Send>>`, from std.

The `ModeSwitch` capability shown earlier in chat is dropped (YAGNI). Nothing
outside a vendor's own file reads it: each `Protocol::set_mode` already
emits its own notice.

### 2. The `Protocol` trait

Each vendor file implements the trait and owns only its state.

```rust
pub(crate) trait Protocol: Default + Send {
    fn launch_args(config: &Config) -> Vec<OsString>;
    fn initialize(&mut self, core: &mut Core) -> impl Future<Output = Result<(), DriverError>> + Send;
    fn send_prompt(&mut self, core: &mut Core, text: &str) -> impl Future<Output = Result<(), DriverError>> + Send;
    fn interrupt(&mut self, core: &mut Core) -> impl Future<Output = Result<(), DriverError>> + Send;
    fn set_mode(&mut self, core: &mut Core, target: Mode) -> impl Future<Output = Result<(), DriverError>> + Send;
    fn on_frame(&mut self, core: &mut Core, frame: Value) -> impl Future<Output = Result<(), DriverError>> + Send;
    fn answer(wire: &Value, allow: bool) -> Value;
    fn stray_reply(request: &Value) -> Option<Value>;
    /// A frame that resets the turn's silence watchdog.
    fn is_progress(&self, core: &Core, frame: &Value) -> bool { true }
    /// A protocol timer (Claude's mode confirmation), if armed.
    fn deadline(&self) -> Option<Instant> { None }
    fn on_deadline(&mut self, core: &mut Core) -> impl Future<Output = Result<(), DriverError>> + Send { async { Ok(()) } }
    /// Called when a turn starts, to reset per-turn protocol state.
    fn turn_started(&mut self) {}
}
```

**Why `-> impl Future + Send`:** declaring each method this way, with
implementations written as `async fn`, lets a driver spawn on the
multi-threaded runtime on stable Rust 1.98, without the `async-trait` crate.

**State moves out of the shared struct:**
- `ClaudeProtocol`: `selected_model`, `streamed`, `mode_request`,
  `late_modes`, `mode_seq`.
- `CodexProtocol`: `turn`, `start_request`, `interrupt_request`,
  `text_items`, `catalog`, `catalog_pages`, `catalog_cursors`.

**Each vendor file holds:**
- its `PROVIDER` row;
- `fn start(…) -> BoxFuture<…> { Box::pin(run::<XProtocol>(…)) }`, written
  for the concrete type so the `Send` check needs no generic bound;
- its catalog parser. This replaces `model_catalog(value, claude: bool)`.

`claude_stray_reply` and `codex_stray_reply` stay public: the gate uses
them. They become thin wrappers over `ClaudeProtocol::stray_reply` and
`CodexProtocol::stray_reply`.

### 3. The shared driver core and its phases

`Driver<P: Protocol> { core: Core, protocol: P }`.

**`Core` holds:**
- `config`, `limits`, `process` and `tx`;
- `session` and `mode`;
- `phase`, `deadline`, `request_id` and `pending`;
- the helpers every protocol needs: `send`, `emit`, `queue_approval`,
  `answer` dispatch (through `P::answer`), `resume_watchdog`,
  `close_all_pending` and `deny_all_pending`.

**The loop:** `run`, `next_wake`, `on_timer`, `on_command`, `on_frame`,
`start_turn`, `answer_approval` and `set_mode`'s shared guards stay in
`driver.rs` and call `P`. They match on no vendor.

**One phase enum replaces four flags** (`initialized`, `ready`, `running`,
`interrupt_pending`):

```rust
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum Phase { Starting, Handshaken, Idle, InTurn, Interrupting }
```

| Former check | Becomes |
| --- | --- |
| `ready` | `phase >= Phase::Idle` |
| `running` | `matches!(phase, InTurn \| Interrupting)` |
| `interrupt_pending` | `phase == Interrupting` |
| `initialized` | `phase >= Handshaken` |
| watchdog armed | `phase != Idle && pending.is_empty()` |

Claude moves `Starting → Idle` in one step. Codex goes `Starting →
Handshaken → Idle`. A finished turn returns to `Idle`. The runtime check
"Unexpected protocol initialization order" goes, because the state it
guarded can no longer occur.

### 4. The demo as a provider

`demo.rs` gains its `PROVIDER` row (`offline: true`) and a `start` that runs
today's `demo(...)` loop. That loop is unchanged.

### 5. The TUI connection phase

`App.conn`'s `ready`, `running` and `stopped` (49 uses) become:

```rust
pub(crate) enum ConnPhase { Connecting, Idle, Running, Stopped }
```

**Helpers:** `is_connecting`, `is_busy`, `is_idle` (ready, no turn, not
stopped) and `is_running`.

**Transitions:**

| Event | New phase |
| --- | --- |
| `Ready` | `Idle`, unless the phase is `Running` or `Stopped` (Claude can report its session ID mid-turn) |
| `Started` | `Running` |
| `Finished` | `Idle` |
| `Stopped` | `Stopped` |
| A new connection | `Connecting` |
| A sent prompt or goal prompt | `Running` |

**Demo checks:** the three `== Engine::Demo` checks in `app.rs`, `view.rs`
and `lib.rs` become `engine.offline()`.

### 6. `/model`, the CLI and the gate

- **`Selection::parse`:** unchanged behaviour; its name lookups go through
  the table.
- **`Selection::configure`:** takes a new provider's binary from
  `provider.default_binary`, which equals today's name for Claude and Codex.
- **`--help` and the invalid-engine error** ("Engine must be codex, claude or
  demo") are built from `PROVIDERS`. The wording stays identical.
- **The `protocol-gate` binary's name checks** use `Engine::parse(...)` and
  `is_vendor()`. Its scenarios stay per vendor.

### 7. Docs

`docs/rust/adding-a-provider.md` lists the steps:

1. Write `live/<name>.rs` with the `Protocol` implementation and its
   `PROVIDER` row.
2. Add the row to `PROVIDERS`.
3. Add a fake-vendor script to `protocol-child`, and the live tests.
4. Optionally, add gate scenarios.

`README.md` and `docs/tui.md` link to it.

## Testing

**The existing suite** (270 tests) must pass unchanged, apart from mechanical
renames (`Engine::Codex` → `Engine::CODEX`).

**New tests:**
- **The table:** names are unique; every row has 4 non-empty mode
  descriptions; `parse` round-trips every name; `ALL` order is unchanged;
  exactly one row is offline.
- **The CLI:** `--help`'s engine list and the invalid-engine error name
  exactly the table's providers.
- **Driver phases:** the transitions for each protocol (a Codex handshake
  passes through `Handshaken`; a cancel goes `InTurn → Interrupting → Idle`),
  and the watchdog arms only outside `Idle` with no pending approval.
- **TUI phases:** a mid-turn `Ready` keeps `Running`; `Stopped` wins over
  every event.

**Live:** a tmux run against the demo and the fake Codex and Claude vendors
repeats the 2026-10-06 checklist.

## Order

Each step is its own commit, with `make rust-check` passing:

1. `Engine` becomes a handle onto `PROVIDERS`; the mode descriptions move
   into the rows.
2. `Core` and `Protocol` split out; Codex moves to `CodexProtocol`.
3. Claude moves to `ClaudeProtocol`; the `Protocol` enum and the
   `claude: bool` parser go.
4. The driver's `Phase` enum.
5. The demo becomes a provider row.
6. The TUI's `ConnPhase`.
7. The CLI and the gate read the table; the "Adding a provider" doc.

## Risks

| Risk | Mitigation |
| --- | --- |
| The split changes timing or the order of wire messages | Each method body moves verbatim; the 34 live driver tests and the gate's contract tests cover the order |
| A trait method's future isn't `Send` | Declared as `impl Future + Send` in the trait, so the compiler rejects it where it's written |
| Phase mapping misses a flag combination | One table of former checks (above); compile errors at every former flag use; phase transition tests |
| A provider row's mode descriptions drift from the old table | They are moved text; the `/mode` tests compare the strings |
