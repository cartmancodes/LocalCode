# Adding a provider

Octet drives each vendor CLI through two pieces:

- **A row in the provider table:** `Engine::ALL` in
  `crates/octet-engine/src/live/mod.rs`. The CLI, `/model`, `/mode`, the
  sidebar and the journals all read it.
- **A `Protocol` implementation** in the vendor's own file. The shared driver
  (`live/driver.rs`) owns the loop, the timers, approvals and commands, and
  calls the protocol for everything vendor-specific.

So adding a vendor is one new file and one table row. The driver, the TUI and
`/model` do not change.

## Steps

### 1. Write `crates/octet-engine/src/live/<name>.rs`

Declare it next to the others in `live/mod.rs` (`mod <name>;`). The file
holds three things.

**The row:**

```rust
pub(super) const PROVIDER: Provider = Provider {
    name: "gemini",                 // --engine gemini, journals, /model gemini …
    title: "Gemini",                // "Gemini reports …" in notices
    default_binary: "gemini",       // run when --binary is absent
    offline: false,
    modes: [                        // /mode descriptions, in Mode::ALL order
        "…ask…", "…accept-edits…", "…auto…", "…full-access…",
    ],
    start,
};

fn start(config: Config, limits: Limits, channels: Channels) -> BoxFuture<Result<(), String>> {
    Box::pin(async move { super::driver::run::<GeminiProtocol>(config, limits, channels).await })
}
```

Write `start` for the concrete type, as shown. That lets the compiler prove
the session can move between threads.

**The protocol and its state.** Keep only this vendor's state here; the
shared state is in `Core`.

```rust
#[derive(Default)]
pub(super) struct GeminiProtocol { /* this vendor's state only */ }

impl Protocol for GeminiProtocol {
    fn launch_args(config: &Config) -> Vec<OsString> { … }
    async fn initialize(&mut self, core: &mut Core) -> Result<(), DriverError> { … }
    async fn send_prompt(&mut self, core: &mut Core, text: &str) -> Result<(), DriverError> { … }
    async fn interrupt(&mut self, core: &mut Core) -> Result<(), DriverError> { … }
    async fn set_mode(&mut self, core: &mut Core, target: Mode) -> Result<(), DriverError> { … }
    async fn on_frame(&mut self, core: &mut Core, frame: Value) -> Result<(), DriverError> { … }
    fn answer(wire: &Value, allow: bool) -> Value { … }
    fn stray_reply(request: &Value) -> Option<Value> { … }
    // Optional: is_progress, mode_change_pending, deadline, on_deadline, turn_started.
}
```

**The catalog parser.** If the vendor reports models, write a parser for its
format, built on `model_catalog_with`.

### 2. Add the row to the table

```rust
pub const ALL: &'static [Engine] = &[Engine::CODEX, Engine::CLAUDE, Engine::DEMO,
    Engine::new(&gemini::PROVIDER)];
```

Add a named constant (`Engine::GEMINI`) only if code needs to name the
vendor. Code should almost never need to: it asks `engine.offline()`, or it
reads the row.

### 3. Add a fake-vendor script and live tests

Teach `crates/octet-testkit/src/bin/protocol-child.rs` to speak enough of the
protocol to run scripted scenarios. Then add tests to
`crates/octet-engine/tests/live.rs` for:

- connect;
- a turn;
- an approval allowed, denied and timed out;
- a cancel;
- a mode switch;
- a vendor error.

The existing Codex and Claude tests are the pattern.

### 4. Optionally, add gate scenarios

`crates/octet-gate` records contract evidence against the real CLI. Its
scenarios are per vendor.

## What each `Protocol` method must do

| Method | Contract |
| --- | --- |
| `launch_args` | The CLI's arguments for `config`: model, resume ID, the permission mode at launch. |
| `initialize` | Send the handshake. When the session is open: set `core.phase = Phase::Idle`, emit `Event::Ready` with the session ID, confirm the mode (`confirm_mode`) and emit `Event::ModeChanged`. A two-step handshake passes through `Phase::Handshaken`. |
| `send_prompt` | Send the user's text. The driver has already set `Phase::InTurn` and emitted `User` and `Started`. |
| `interrupt` | Ask the vendor to stop the turn. The driver has set `Phase::Interrupting`, and denies pending approvals after this returns. |
| `set_mode` | Switch a live session to `target`. The driver has refused full access, an unready session, a pending switch and a no-op. Emit `Event::ModeChanged` once the vendor confirms. |
| `on_frame` | Turn vendor output into `Text`, `Tool`, `Usage`, `Approval` (through `core.queue_approval`) and, at the turn's end, `core.close_all_pending()`, `phase = Idle` and `Event::Finished`. Ignore output for other sessions or turns. |
| `answer` | The reply that allows or denies an approval request. The driver uses it for user answers, timeouts and oversized requests. |
| `stray_reply` | The reply to a request Octet will not show the user. Deny permissions; report anything else as unsupported. |
| `is_progress` | Whether a frame shows the turn is alive. Return false for another thread's output. |
| `deadline` / `on_deadline` | An optional protocol timer, such as Claude's mode confirmation. |
| `turn_started` | Clear per-turn state. |

## Rules that apply

- **Never answer a vendor approval yourself.** Queue it for the user, or deny
  it when it is too large to show.
- **Keep every buffer bounded,** and fail visibly on overload (`emit` returns
  `ConsumerOverloaded` when the interface falls behind).
- **Never read vendor credentials.** The CLI authenticates itself.
- **Follow `.claude/skills/rust-engineer/SKILL.md`,** and pass `make rust-check`.
