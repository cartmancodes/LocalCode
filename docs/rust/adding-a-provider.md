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
    steer: false,                   // true if a running turn takes more input
    inline_images: false,           // true if images go in the prompt as base64
    effort_live: false,             // true if effort is sent per turn, not at launch
    efforts: &[],                   // the effort levels it takes; empty passes any word
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
    async fn send_prompt(&mut self, core: &mut Core, text: &str, images: &[ImageAttachment])
        -> Result<(), DriverError> { … }
    async fn compact(&mut self, core: &mut Core) -> Result<(), DriverError> { … }
    async fn interrupt(&mut self, core: &mut Core) -> Result<(), DriverError> { … }
    async fn set_mode(&mut self, core: &mut Core, target: Mode) -> Result<(), DriverError> { … }
    async fn on_frame(&mut self, core: &mut Core, frame: Value) -> Result<(), DriverError> { … }
    fn answer(wire: &Value, allow: bool) -> Value { … }
    fn stray_reply(request: &Value) -> Option<Value> { … }
    // Optional: is_progress, mode_change_pending, deadline, on_deadline, turn_started, steer.
}
```

**The catalog parser.** If the vendor reports models, write a parser for its
format, built on `model_catalog_with`.

### 2. Add the row to the table

```rust
pub const ALL: &'static [Engine] = &[Engine::CODEX, Engine::CLAUDE, Engine::DEMO,
    Engine(&gemini::PROVIDER)];
```

`ALL` lives in the same module as `Engine`, so it can build the row
directly. Add a named constant (`Engine::GEMINI`) only if code needs to
name the vendor. Code should almost never need to: it asks `engine.offline()`, or it
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
| `initialize` | Send the handshake. When its reply arrives (in `on_frame`): set `core.phase = Phase::Idle`, emit `Event::Ready` with the session ID, and pass the mode the vendor reports to `core.adopt_mode`, which notes a mismatch and emits `Event::ModeChanged`. A two-step handshake passes through `Phase::Handshaken`. **Only move the phase forward**: ignore a repeated reply, and treat a step that arrives early as a protocol error, as Codex and Claude do. |
| `launch_args` (more) | Also: `config.effort` when the vendor takes effort at launch, and `config.fork` (open `resume` as a new session). |
| `send_prompt` | Send the user's text, then its images (`ImageAttachment`: path, media type, name; `read_base64` reads the bytes). The driver has already set `Phase::InTurn` and emitted `User` and `Started`. If an image cannot be read, fail the turn with `core.finish_turn(Outcome::Failed, Some(message))`. |
| `compact` | Ask the vendor to compact its context, as a turn: the driver has set `Phase::InTurn` and emitted `User("/compact")` and `Started`. |
| `steer` | Add text to the running turn; `Ok(false)` (the default) means the vendor cannot, and the interface queues the text instead. Set the row's `steer` to match. |
| `interrupt` | Ask the vendor to stop the turn. The driver has set `Phase::Interrupting`, and denies pending approvals after this returns. |
| `set_mode` | Switch a live session to `target`. The driver has refused full access, an unready session, a pending switch and a no-op. Emit `Event::ModeChanged` once the vendor confirms. |
| `on_frame` | Turn vendor output into `Text`, `Tool`, `Usage`, `Approval` (through `core.queue_approval`, which denies a request too large to show or beyond the 8 already waiting) and, at the turn's end, `core.finish_turn(outcome, error)`, which closes the turn's approvals and makes the session idle. Ignore output for other sessions or turns. |
| `answer` | The reply that allows or denies an approval request. The driver uses it for user answers, timeouts and oversized requests. |
| `stray_reply` | The reply to a request Octet will not show the user. Deny permissions; report anything else as unsupported. |
| `mode_change_pending` | True while a mode switch waits for the vendor to confirm it. The driver then refuses another switch. |
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
