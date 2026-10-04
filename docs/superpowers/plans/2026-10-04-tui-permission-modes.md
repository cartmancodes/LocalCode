# TUI Permission Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-neutral permission-mode picker (`ask`, `accept-edits`, `auto`, `full-access`) to the Rust TUI, with each vendor's native auto mode, for Claude, Codex and the offline demo.

**Architecture:** A `Mode` enum and the only vendor mapping live in `octet-engine::live`. `Config.mode` drives launch arguments; `Command::SetMode` switches `ask`/`accept-edits`/`auto` live (Claude `set_permission_mode` control request, Codex per-turn overrides, demo in-process); `full-access` changes reconnect with the same vendor session through a new TUI `Action::Mode`. The engine reports the accepted mode with `Event::ModeChanged`, which the TUI shows as a header chip.

**Tech Stack:** Rust 1.98.1 workspace (`tokio`, `serde_json`, `ratatui`, `crossterm`), fake vendor `octet-testkit/src/bin/protocol-child.rs`.

**Spec:** `docs/superpowers/specs/2026-10-04-tui-permission-modes-design.md`

## Global Constraints

- Mode spellings, everywhere user-facing: `ask`, `accept-edits`, `auto`, `full-access`. Default `ask`.
- Mapping (the only copy lives in `crates/octet-engine/src/live.rs`):
  - Claude: `ask`→`default`, `accept-edits`→`acceptEdits`, `auto`→`auto`, `full-access`→`bypassPermissions` plus `--allow-dangerously-skip-permissions`.
  - Codex (sandbox, approvalPolicy, approvalsReviewer): `ask`→(`workspace-write`,`untrusted`,`user`), `accept-edits`→(`workspace-write`,`on-request`,`user`), `auto`→(`workspace-write`,`on-request`,`auto_review`), `full-access`→(`danger-full-access`,`never`,`user`).
- Shift+Tab cycles `ask → accept-edits → auto → ask`; it never reaches `full-access`.
- `full-access` is entered or left only by reconnect, on an idle, ready session.
- Unknown mode values are errors, never a fallback.
- Octet never auto-answers a real vendor approval; existing fail-closed approval handling is unchanged.
- Mode is not persisted between launches; it is carried across `/model`, `/new`, `/reconnect`.
- Run cargo through `scripts/rust-env.sh`. Gate for every task: `scripts/rust-env.sh cargo fmt --all`, then `make rust-check` (fmt check, workspace tests, clippy `-D warnings`) must pass before the commit.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A Claude mode switch is pending when the session stops → the header must not stay stuck on `auto…` (Task 4 test `stopped_session_clears_pending_mode`).
2. Shift+Tab or `/mode` pressed again while a Claude switch is pending → ignored with a notice, not a second control request (Task 4 test `cycle_is_ignored_while_a_switch_is_pending`); a driver-refused switch re-emits the current mode so the chip never sticks on `…` (Task 3 test `driver_refuses_live_full_access`).
3. `/mode full-access` typed while a turn runs or an approval is open → refused, no reconnect (Task 4 test `full_access_requires_idle_ready_session`).
4. `/model codex …` while in `auto` → the new provider also starts in `auto` (Task 1 test `configure_carries_mode`).
5. `octet --mode bogus` → exits non-zero with "Unknown mode", never starts in another mode (Task 4 test `unknown_mode_flag_is_a_startup_error`).

---

### Task 1: Mode type, mapping and plumbing

**Files:**
- Modify: `crates/octet-engine/src/live.rs` (add `Mode`, mapping functions, `Config.mode`, `Command::SetMode`, `Event::ModeChanged`; tests module)
- Modify: `crates/octet-core/src/lib.rs` (re-export `Mode`, journal `mode` in session record, `record()` arm)
- Modify: `crates/octet-core/src/model.rs` (`configure` carries `mode`; test)
- Modify: `crates/octet-tui/src/view.rs` (`App.mode`, `ModeChanged` arm, test `Config` literals)
- Modify: Config literals in `crates/octet-engine/tests/live.rs`, `crates/octet-core/tests/session.rs`, `crates/octet-tui/src/lib.rs` (tests), `crates/octet/src/main.rs`

**Interfaces:**
- Produces:
  - `pub enum octet_engine::live::Mode { Ask, AcceptEdits, Auto, FullAccess }` (`Clone, Copy, Debug, Default=Ask, PartialEq, Eq`), re-exported as `octet_core::Mode`.
  - `Mode::ALL: [Mode; 4]`, `Mode::parse(&str) -> Option<Mode>`, `Mode::label(self) -> &'static str`, `Mode::cycle(self) -> Mode`, `Mode::describe(self, engine: &str) -> &'static str`.
  - `pub fn claude_permission_args(mode: Mode) -> Vec<&'static str>`, `pub fn codex_thread_params(mode: Mode) -> serde_json::Value`, `pub fn codex_turn_overrides(mode: Mode) -> serde_json::Value`, private `fn claude_mode(mode: Mode) -> &'static str`.
  - `Config.mode: Mode`, `Command::SetMode(Mode)`, `Event::ModeChanged(Mode)`.
  - `octet_tui::view::App.mode: octet_core::Mode`.

- [ ] **Step 1: Write the failing unit tests**

Append to the end of `crates/octet-engine/src/live.rs`:

```rust
#[cfg(test)]
mod mode_tests {
    use super::*;
    #[test]
    fn mode_labels_parse_and_cycle() {
        for mode in Mode::ALL {
            assert_eq!(Mode::parse(mode.label()), Some(mode));
        }
        assert_eq!(Mode::default(), Mode::Ask);
        assert_eq!(Mode::parse("bypassPermissions"), None);
        assert_eq!(Mode::parse(""), None);
        assert_eq!(Mode::Ask.cycle(), Mode::AcceptEdits);
        assert_eq!(Mode::AcceptEdits.cycle(), Mode::Auto);
        assert_eq!(Mode::Auto.cycle(), Mode::Ask);
        assert_eq!(Mode::FullAccess.cycle(), Mode::FullAccess);
    }
    #[test]
    fn claude_mapping_matches_spec_table() {
        assert_eq!(claude_permission_args(Mode::Ask), ["--permission-mode", "default"]);
        assert_eq!(claude_permission_args(Mode::AcceptEdits), ["--permission-mode", "acceptEdits"]);
        assert_eq!(claude_permission_args(Mode::Auto), ["--permission-mode", "auto"]);
        assert_eq!(
            claude_permission_args(Mode::FullAccess),
            ["--permission-mode", "bypassPermissions", "--allow-dangerously-skip-permissions"]
        );
    }
    #[test]
    fn codex_mapping_matches_spec_table() {
        let expected = [
            (Mode::Ask, "workspace-write", "untrusted", "user"),
            (Mode::AcceptEdits, "workspace-write", "on-request", "user"),
            (Mode::Auto, "workspace-write", "on-request", "auto_review"),
            (Mode::FullAccess, "danger-full-access", "never", "user"),
        ];
        for (mode, sandbox, policy, reviewer) in expected {
            assert_eq!(
                codex_thread_params(mode),
                json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer})
            );
            assert_eq!(
                codex_turn_overrides(mode),
                json!({"approvalPolicy":policy,"approvalsReviewer":reviewer})
            );
        }
    }
    #[test]
    fn every_mode_is_described_for_every_engine() {
        for engine in ["claude", "codex", "demo"] {
            for mode in Mode::ALL {
                assert!(!mode.describe(engine).is_empty());
            }
        }
    }
}
```

Append to the `tests` module in `crates/octet-core/src/model.rs` (and add `mode: crate::Mode::Auto,` to that module's `config()` literal):

```rust
    #[test]
    fn configure_carries_mode() {
        let selection = Selection::parse("claude example", "codex").unwrap();
        assert_eq!(selection.configure(&config(), "thread", None).mode, crate::Mode::Auto);
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-engine --lib mode_tests`
Expected: compile errors — `Mode`, `claude_permission_args`, `codex_thread_params`, `codex_turn_overrides` not found.

- [ ] **Step 3: Implement the type and mapping**

In `crates/octet-engine/src/live.rs`, add `mode` to `Config`:

```rust
#[derive(Clone, Debug)]
pub struct Config {
    pub engine: String,
    pub binary: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub resume: Option<String>,
    pub mode: Mode,
}
```

Directly below `Config`, add:

```rust
/// Provider-neutral permission mode. The vendor mapping lives only in the
/// functions below; `Auto` delegates to each vendor's own reviewer and
/// Octet never answers a vendor approval by itself.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Mode {
    #[default]
    Ask,
    AcceptEdits,
    Auto,
    FullAccess,
}
impl Mode {
    pub const ALL: [Mode; 4] = [Mode::Ask, Mode::AcceptEdits, Mode::Auto, Mode::FullAccess];
    pub fn parse(value: &str) -> Option<Mode> {
        Self::ALL.into_iter().find(|mode| mode.label() == value)
    }
    pub fn label(self) -> &'static str {
        match self {
            Mode::Ask => "ask",
            Mode::AcceptEdits => "accept-edits",
            Mode::Auto => "auto",
            Mode::FullAccess => "full-access",
        }
    }
    /// Shift+Tab order. Full access is never reached by cycling.
    pub fn cycle(self) -> Mode {
        match self {
            Mode::Ask => Mode::AcceptEdits,
            Mode::AcceptEdits => Mode::Auto,
            Mode::Auto => Mode::Ask,
            Mode::FullAccess => Mode::FullAccess,
        }
    }
    pub fn describe(self, engine: &str) -> &'static str {
        match (engine, self) {
            ("claude", Mode::Ask) => "Claude asks before edits and commands (permission mode default)",
            ("claude", Mode::AcceptEdits) => "File edits proceed; other actions ask (acceptEdits)",
            ("claude", Mode::Auto) => "Claude's classifier approves or blocks each action (auto)",
            ("claude", Mode::FullAccess) => "No permission checks at all (bypassPermissions)",
            ("codex", Mode::Ask) => "Workspace sandbox; untrusted commands ask (untrusted)",
            ("codex", Mode::AcceptEdits) => "Workspace sandbox; asks only to escalate (on-request). Codex has no edits-only mode",
            ("codex", Mode::Auto) => "Workspace sandbox; Codex's auto-review agent decides escalations (auto_review)",
            ("codex", Mode::FullAccess) => "No sandbox; never asks (danger-full-access)",
            (_, Mode::Ask | Mode::AcceptEdits) => "Offline demo: /approval-demo shows the dialog",
            (_, Mode::Auto | Mode::FullAccess) => "Offline demo: /approval-demo is allowed without a dialog",
        }
    }
}
fn claude_mode(mode: Mode) -> &'static str {
    match mode {
        Mode::Ask => "default",
        Mode::AcceptEdits => "acceptEdits",
        Mode::Auto => "auto",
        Mode::FullAccess => "bypassPermissions",
    }
}
/// Claude refuses a live switch to bypassPermissions unless launched with this
/// allowance, so full access is only ever applied at launch.
pub fn claude_permission_args(mode: Mode) -> Vec<&'static str> {
    let mut args = vec!["--permission-mode", claude_mode(mode)];
    if mode == Mode::FullAccess {
        args.push("--allow-dangerously-skip-permissions");
    }
    args
}
pub fn codex_thread_params(mode: Mode) -> Value {
    let (sandbox, policy, reviewer) = match mode {
        Mode::Ask => ("workspace-write", "untrusted", "user"),
        Mode::AcceptEdits => ("workspace-write", "on-request", "user"),
        Mode::Auto => ("workspace-write", "on-request", "auto_review"),
        Mode::FullAccess => ("danger-full-access", "never", "user"),
    };
    json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer})
}
/// Codex applies these on the turn and every later turn. Live modes all share
/// the workspace-write sandbox, so no sandboxPolicy override is sent.
pub fn codex_turn_overrides(mode: Mode) -> Value {
    let params = codex_thread_params(mode);
    json!({"approvalPolicy":params["approvalPolicy"],"approvalsReviewer":params["approvalsReviewer"]})
}
```

Add the enum variants: `ModeChanged(Mode),` to `Event` (after `ModelSelected(String),`) and `SetMode(Mode),` to `Command` (after `Answer { .. }`). In `prompt_parts`, change the unreachable arm to `Command::Answer { .. } | Command::SetMode(_) => unreachable!("only prompt commands reach prompt_parts"),`.

The driver must still compile: in `vendor()`'s `commands.recv()` match and in `demo()`, `SetMode` is not yet handled — `vendor()` needs an explicit arm, add after the `Answer` arm:

```rust
                    Some(Command::SetMode(_)) => { emit(tx,Event::Notice("Mode switching is not available yet".into()))?; }
```

(`demo()` already has a `_=>{}` catch-all.) Task 3 replaces this arm.

- [ ] **Step 4: Plumb `mode` through the other crates**

`crates/octet-core/src/lib.rs`:
- Re-export: `pub use octet_engine::live::{Command, Config, Event, Handle, Mode, PROMPT_LIMIT};`
- Session record: `json!({"engine":config.engine,"cwd":config.cwd,"resume":config.resume,"model":config.model,"mode":config.mode.label()})`
- `record()`: add `Event::ModeChanged(mode) => ("mode", json!(mode.label())),`

`crates/octet-core/src/model.rs` `configure`: add `mode: current.mode,` to the returned `Config`.

`crates/octet-tui/src/view.rs`:
- `App` field `pub mode: octet_core::Mode,`; in `App::new` set `mode: config.mode,`; in `connection()` add `self.mode = config.mode;`.
- `App::event`: add `Event::ModeChanged(mode) => self.mode = mode,`.

Add `mode: octet_core::Mode::Ask,` (or `mode: Mode::Ask` / `mode: Default::default()` where the crate does not import it) to every remaining `Config { … }` literal: `crates/octet-engine/tests/live.rs` (`config()`), `crates/octet-core/tests/session.rs`, `crates/octet-tui/src/view.rs` tests (4 literals), `crates/octet-tui/src/lib.rs` tests (`app()`), and `crates/octet/src/main.rs` (`mode: octet_core::Mode::Ask,` — Task 4 replaces it with the flag value).

Extend `crates/octet-core/tests/session.rs` after reading `text`:

```rust
    let first: serde_json::Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
    assert_eq!(first["data"]["mode"], "ask");
```

(`serde_json` is already a dependency of `octet-core`; if the test target cannot see it, use `octet_core`'s re-export path or add `serde_json.workspace = true` to `[dev-dependencies]`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/rust-env.sh cargo fmt --all && make rust-check`
Expected: all tests pass, including `mode_tests::*`, `configure_carries_mode`, the session test; clippy clean.

- [ ] **Step 6: Commit**

```bash
git add crates
git commit -m "Add provider-neutral permission Mode and vendor mapping to the Rust engine

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Launch with the configured mode

**Files:**
- Modify: `crates/octet-engine/src/live.rs` (`vendor()` launch args, Codex thread params, `ModeChanged` after ready; `demo()` signature and initial `ModeChanged`)
- Modify: `crates/octet-testkit/src/bin/protocol-child.rs` (echo argv and params)
- Test: `crates/octet-engine/tests/live.rs`

**Interfaces:**
- Consumes: `Mode`, `claude_permission_args`, `codex_thread_params`, `codex_turn_overrides`, `Config.mode`, `Event::ModeChanged` (Task 1).
- Produces: after `Event::Ready`, every engine emits `Event::ModeChanged(config.mode)` once at connect; every Codex `turn/start` carries `codex_turn_overrides(current mode)`; fixture prompts `argv` (Claude) and `params` (Codex) reply with what the fake vendor received.

- [ ] **Step 1: Teach the fake vendor to echo what it received**

In `crates/octet-testkit/src/bin/protocol-child.rs`:

`interactive_codex()`: add `let mut thread_params = Value::Null;` beside `turn`/`active`; in the `thread/start | thread/resume` arm, first do `thread_params = v["params"].clone();` (make the arm a block). In the `turn/start` arm, after the `text == "approval"` block, add:

```rust
                if text == "params" {
                    let echo = json!({"thread":thread_params,"turn":v["params"]}).to_string();
                    emit(
                        &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"params","delta":echo}}),
                    );
                    emit(
                        &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"completed"}}}),
                    );
                    continue;
                }
```

`interactive_claude()`: at the top add `let argv: Vec<String> = env::args().skip(1).collect();`. In the `v["type"] == "user"` branch compute

```rust
            let text = v.pointer("/message/content/0/text").and_then(Value::as_str).unwrap_or("");
            let reply = if text == "argv" { argv.join(" ") } else { "Hello Claude".to_owned() };
```

and use `reply` in place of the three `"Hello Claude"` literals (stream delta text, assistant text, result).

- [ ] **Step 2: Write the failing integration tests**

Add to `crates/octet-engine/tests/live.rs` (extend the `use` line to `use octet_engine::live::{spawn, Command, Config, Event, Mode};`):

```rust
async fn wait_for(events: &mut mpsc::Receiver<Event>, wanted: impl Fn(&Event) -> bool) -> Event {
    loop {
        let event = next(events).await;
        if let Event::Error(error) = &event {
            panic!("{error}");
        }
        if wanted(&event) {
            return event;
        }
    }
}
async fn turn_text(events: &mut mpsc::Receiver<Event>) -> String {
    let mut text = String::new();
    loop {
        match next(events).await {
            Event::Text(t) => text.push_str(&t),
            Event::Finished { .. } => return text,
            Event::Error(e) => panic!("{e}"),
            _ => {}
        }
    }
}
#[tokio::test]
async fn codex_launches_and_turns_with_the_configured_mode() {
    let mut c = config();
    c.mode = Mode::Auto;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Auto))).await;
    handle.send(Command::Prompt("params".into())).unwrap();
    let echo: serde_json::Value = serde_json::from_str(&turn_text(&mut events).await).unwrap();
    assert_eq!(echo["thread"]["sandbox"], "workspace-write");
    assert_eq!(echo["thread"]["approvalPolicy"], "on-request");
    assert_eq!(echo["thread"]["approvalsReviewer"], "auto_review");
    assert_eq!(echo["turn"]["approvalPolicy"], "on-request");
    assert_eq!(echo["turn"]["approvalsReviewer"], "auto_review");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_launches_with_the_mapped_permission_flag() {
    let mut c = config();
    c.engine = "claude".into();
    c.mode = Mode::AcceptEdits;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::AcceptEdits))).await;
    handle.send(Command::Prompt("argv".into())).unwrap();
    let argv = turn_text(&mut events).await;
    assert!(argv.contains("--permission-mode acceptEdits"), "{argv}");
    assert!(!argv.contains("--permission-mode default"), "{argv}");
    assert!(!argv.contains("dangerously"), "{argv}");
    handle.shutdown();
    task.await.unwrap();
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-engine --test live`
Expected: the two new tests time out in `next()` waiting for `ModeChanged` (panic on `timeout(...).unwrap()`); existing tests pass.

- [ ] **Step 4: Implement launch wiring**

In `vendor()` in `crates/octet-engine/src/live.rs`:
- Remove `"--permission-mode",` and `"default",` from the Claude literal argument array. In the existing `if claude { … }` block that appends `--model`/`--resume`, first add:

```rust
        args.extend(claude_permission_args(config.mode).into_iter().map(OsString::from));
```

- Add driver state beside the other `let mut` lines: `let mode = config.mode;` (Task 3 makes it `mut`; declaring it `mut` now fails clippy's `-D warnings`).
- Claude init success: after `emit(tx,Event::Ready{…})?;` add `emit(tx,Event::ModeChanged(mode))?;`.
- Codex `id==1` branch: replace the params literal with

```rust
                            let mut params=codex_thread_params(mode);
                            params["cwd"]=json!(config.cwd);
```

- Codex `id==2` branch: after `emit(tx,Event::Ready{session:session.clone()})?;` add `emit(tx,Event::ModeChanged(mode))?;`.
- Codex `turn/start`: after building `params` (and the optional model), add

```rust
                            if let (Some(target),Value::Object(extra))=(params.as_object_mut(),codex_turn_overrides(mode)) { target.extend(extra); }
```

Demo: change the signature to `async fn demo(mode: Mode, mut commands: …, …)` (rename the local loop variable if it clashes — the demo has no other `mode`), call it as `demo(config.mode, rx, cancel, stopping, &events)` in `spawn`, and right after the initial `Event::Ready` emit add `emit(tx, Event::ModeChanged(mode))?;`. Mark `mode` as `let mut mode = mode;` only in Task 3 when it changes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/rust-env.sh cargo fmt --all && make rust-check`
Expected: all pass, including both new tests; clippy clean (if clippy flags `mode` as never reassigned, keep it `let mode` in this task).

- [ ] **Step 6: Commit**

```bash
git add crates
git commit -m "Launch Claude and Codex with the configured permission mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Live mode switching in the driver

**Files:**
- Modify: `crates/octet-engine/src/live.rs` (`SetMode` handling in `vendor()` and `demo()`, Claude `control_response` for mode, demo approval auto-allow)
- Modify: `crates/octet-testkit/src/bin/protocol-child.rs` (answer `set_permission_mode`)
- Test: `crates/octet-engine/tests/live.rs`

**Interfaces:**
- Consumes: Task 1 types; Task 2 `wait_for`, `turn_text`, fixture `argv`/`params` echoes; driver state `mode`.
- Produces: `Command::SetMode(target)` semantics —
  - `target` or current mode is `FullAccess` → `Event::Notice` containing `/mode`, then `ModeChanged(current)`.
  - not ready → `Event::Notice("Wait for the connection before changing modes")`, then `ModeChanged(current)`.
  - `target == mode` → `Event::ModeChanged(mode)` re-emitted.
  - Claude: sends `set_permission_mode`; success → `ModeChanged(target)`; error → `Notice("Mode change refused by Claude: …")` then `ModeChanged(old)`; a second request while one is pending → `Notice("A mode change is already pending")`.
  - Codex/demo: `ModeChanged(target)` immediately; Codex mid-turn also `Notice("Mode applies from the next turn")`.

- [ ] **Step 1: Teach the fake Claude to answer mode requests**

In `interactive_claude()`, add a branch before the `v["type"] == "user"` branch:

```rust
        } else if v["type"] == "control_request" && v["request"]["subtype"] == "set_permission_mode" {
            if argv.iter().any(|a| a == "reject-mode") {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"error","error":"Cannot set permission mode: fixture refusal"}}),
                );
            } else {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":v["request"]["mode"]}}}),
                );
            }
```

- [ ] **Step 2: Write the failing integration tests**

Add to `crates/octet-engine/tests/live.rs`:

```rust
#[tokio::test]
async fn codex_live_switch_applies_to_the_next_turn() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Auto))).await;
    handle.send(Command::Prompt("params".into())).unwrap();
    let echo: serde_json::Value = serde_json::from_str(&turn_text(&mut events).await).unwrap();
    assert_eq!(echo["thread"]["approvalsReviewer"], "user");
    assert_eq!(echo["turn"]["approvalPolicy"], "on-request");
    assert_eq!(echo["turn"]["approvalsReviewer"], "auto_review");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_live_switch_uses_set_permission_mode() {
    let mut c = config();
    c.engine = "claude".into();
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Auto))).await;
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_refusal_keeps_the_previous_mode() {
    let mut c = config();
    c.engine = "claude".into();
    c.model = Some("reject-mode".into());
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let notice = wait_for(&mut events, |e| matches!(e, Event::Notice(_) | Event::ModeChanged(_))).await;
    assert!(matches!(&notice, Event::Notice(text) if text.contains("refused")), "{notice:?}");
    assert!(matches!(next(&mut events).await, Event::ModeChanged(Mode::Ask)));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn driver_refuses_live_full_access() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::FullAccess)).unwrap();
    let event = wait_for(&mut events, |e| matches!(e, Event::Notice(_) | Event::ModeChanged(_))).await;
    assert!(matches!(&event, Event::Notice(text) if text.contains("/mode")), "{event:?}");
    assert!(matches!(next(&mut events).await, Event::ModeChanged(Mode::Ask)));
    handle.shutdown();
    task.await.unwrap();
}
```

Note: `wait_for` in `claude_refusal_keeps_the_previous_mode` stops at the first `Notice` or `ModeChanged`, so a stray `ModeChanged` before the notice fails the assertion — that is intended. The Codex fixture emits no notices before the first prompt, so `driver_refuses_live_full_access` cannot pick up an unrelated notice; if it does in practice (e.g. model catalog notice), filter with `text.contains("/mode")` inside the predicate instead.

- [ ] **Step 3: Run tests to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-engine --test live`
Expected: `codex_live_switch…`, `claude_live_switch…` and `claude_refusal…` fail (they get Task 1's "not available yet" notice or time out); `driver_refuses_live_full_access` fails its `/mode` assertion.

- [ ] **Step 4: Implement live switching**

In `vendor()`: make `let mut mode = config.mode;` mutable and add `let mut mode_request: Option<(String, Mode)> = None;` and `let mut mode_seq = 0u64;` beside it.

Replace Task 1's temporary `SetMode` arm with:

```rust
                    Some(Command::SetMode(target)) => {
                        // Refusals re-emit the current mode so the TUI clears its pending indicator.
                        if target==Mode::FullAccess || mode==Mode::FullAccess { emit(tx,Event::Notice("Full access is changed by reconnecting; use /mode".into()))?; emit(tx,Event::ModeChanged(mode))?; }
                        else if !ready { emit(tx,Event::Notice("Wait for the connection before changing modes".into()))?; emit(tx,Event::ModeChanged(mode))?; }
                        else if mode_request.is_some() { emit(tx,Event::Notice("A mode change is already pending".into()))?; }
                        else if target==mode { emit(tx,Event::ModeChanged(mode))?; }
                        else if claude {
                            mode_seq+=1;
                            let id=format!("octet-mode-{mode_seq}");
                            send(&process,json!({"type":"control_request","request_id":id,"request":{"subtype":"set_permission_mode","mode":claude_mode(target)}})).await?;
                            mode_request=Some((id,target));
                        } else {
                            mode=target;
                            emit(tx,Event::ModeChanged(mode))?;
                            if running { emit(tx,Event::Notice("Mode applies from the next turn".into()))?; }
                        }
                    }
```

In the Claude frame branch, directly after `let kind=…;`, add:

```rust
                        let response_id=v.pointer("/response/request_id").and_then(Value::as_str);
                        if kind=="control_response" && mode_request.as_ref().is_some_and(|(id,_)| Some(id.as_str())==response_id) {
                            let (_,target)=mode_request.take().unwrap();
                            if v.pointer("/response/subtype").and_then(Value::as_str)==Some("success") { mode=target; }
                            else { emit(tx,Event::Notice(format!("Mode change refused by Claude: {}",limited(v.pointer("/response/error").and_then(Value::as_str).unwrap_or("unknown error")))))?; }
                            emit(tx,Event::ModeChanged(mode))?;
                            continue;
                        }
```

In `demo()`: make the parameter `mut mode: Mode`, and add an arm to the `commands.recv()` match before `None=>break`:

```rust
                Some(Command::SetMode(target))=>{
                    if target==Mode::FullAccess || mode==Mode::FullAccess {emit(tx,Event::Notice("Full access is changed by reconnecting; use /mode".into()))?;}
                    else {mode=target;}
                    emit(tx,Event::ModeChanged(mode))?;
                },
```

and in the `/approval-demo` branch, before emitting `Event::Approval`, short-circuit when the mode allows it:

```rust
                    let reply=if text.trim()=="/approval-demo" && mode!=Mode::Ask && mode!=Mode::AcceptEdits {
                        emit(tx,Event::Notice(format!("Allowed without a dialog by {} mode (demo only)",mode.label())))?;
                        "Approved automatically. In a live session, the vendor's own reviewer decides.".to_owned()
                    } else if text.trim()=="/approval-demo" {
```

(the existing `if text.trim()=="/approval-demo" {` becomes the `else if` shown; its body and the final `else` are unchanged).

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/rust-env.sh cargo fmt --all && make rust-check`
Expected: all pass, including the four new tests and the existing PTY test (which runs demo in `ask`, so its approval dialog still appears).

- [ ] **Step 6: Commit**

```bash
git add crates
git commit -m "Switch permission modes live: Claude set_permission_mode, Codex per-turn overrides

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `/mode`, Shift+Tab, `--mode` and the full-access reconnect

**Files:**
- Modify: `crates/octet-tui/src/lib.rs` (`Action::SetMode`, `Action::Mode`, `/mode` command, BackTab, run loop)
- Modify: `crates/octet-tui/src/view.rs` (`App.mode_pending`, `mode_details()`, `ModeChanged`/`Stopped` arms)
- Modify: `crates/octet/src/main.rs` (`--mode` flag, help text)
- Test: `crates/octet-tui/src/lib.rs` (`model_tests` module), `crates/octet-tui/src/view.rs` (tests), `crates/octet/tests/terminal.rs`

**Interfaces:**
- Consumes: `octet_core::Mode`, `Command::SetMode`, `Event::ModeChanged`, `App.mode` (Tasks 1–3).
- Produces: `Action::SetMode(octet_core::Mode)`, `Action::Mode(octet_core::Mode)`; `App.mode_pending: Option<octet_core::Mode>`; `App::mode_details(&self) -> String`; `fn cycle_mode(app: &mut App) -> Action` in `lib.rs`.

- [ ] **Step 1: Write the failing tests**

Append to `mod model_tests` in `crates/octet-tui/src/lib.rs`:

```rust
    #[tokio::test]
    async fn mode_command_switches_live_modes_and_rejects_unknown() {
        let mut app = app();
        assert!(matches!(command(&mut app, "/mode auto").await, Action::SetMode(octet_core::Mode::Auto)));
        app.running = true;
        assert!(matches!(command(&mut app, "/mode accept-edits").await, Action::SetMode(octet_core::Mode::AcceptEdits)));
        assert!(matches!(command(&mut app, "/mode yolo").await, Action::Continue));
        assert!(app.notice.contains("Unknown mode"));
        assert!(matches!(command(&mut app, "/mode").await, Action::Continue));
        assert!(app.notice.contains("auto_review"));
    }
    #[tokio::test]
    async fn full_access_requires_idle_ready_session() {
        let mut app = app();
        app.running = true;
        assert!(matches!(command(&mut app, "/mode full-access").await, Action::Continue));
        app.running = false;
        app.approvals.push_back((1, "x".into()));
        assert!(matches!(command(&mut app, "/mode full-access").await, Action::Continue));
        app.approvals.clear();
        app.ready = false;
        assert!(matches!(command(&mut app, "/mode full-access").await, Action::Continue));
        app.ready = true;
        assert!(matches!(command(&mut app, "/mode full-access").await, Action::Mode(octet_core::Mode::FullAccess)));
        app.mode = octet_core::Mode::FullAccess;
        assert!(matches!(command(&mut app, "/mode auto").await, Action::Mode(octet_core::Mode::Auto)));
        assert!(matches!(command(&mut app, "/mode full-access").await, Action::Continue));
    }
    #[test]
    fn cycle_follows_order_and_never_reaches_full_access() {
        let mut app = app();
        assert!(matches!(cycle_mode(&mut app), Action::SetMode(octet_core::Mode::AcceptEdits)));
        app.mode = octet_core::Mode::Auto;
        assert!(matches!(cycle_mode(&mut app), Action::SetMode(octet_core::Mode::Ask)));
        app.mode = octet_core::Mode::FullAccess;
        assert!(matches!(cycle_mode(&mut app), Action::Continue));
        assert!(app.notice.contains("/mode"));
    }
    #[tokio::test]
    async fn cycle_is_ignored_while_a_switch_is_pending() {
        let mut app = app();
        app.mode_pending = Some(octet_core::Mode::AcceptEdits);
        assert!(matches!(cycle_mode(&mut app), Action::Continue));
        assert!(app.notice.contains("pending"));
        assert!(matches!(command(&mut app, "/mode auto").await, Action::Continue));
        assert!(app.notice.contains("pending"));
    }
```

Append to the tests module in `crates/octet-tui/src/view.rs`:

```rust
    #[test]
    fn stopped_session_clears_pending_mode() {
        let c = octet_core::Config {
            engine: "claude".into(),
            binary: "claude".into(),
            cwd: "/tmp".into(),
            model: None,
            resume: None,
            mode: octet_core::Mode::Ask,
        };
        let mut a = App::new(&c, "journal".into());
        a.mode_pending = Some(octet_core::Mode::Auto);
        a.event(Event::ModeChanged(octet_core::Mode::Auto));
        assert_eq!((a.mode, a.mode_pending), (octet_core::Mode::Auto, None));
        a.mode_pending = Some(octet_core::Mode::Ask);
        a.event(Event::Stopped);
        assert_eq!((a.mode, a.mode_pending), (octet_core::Mode::Auto, None));
    }
```

Append to `crates/octet/tests/terminal.rs` (no PTY needed — option parsing fails before the terminal check):

```rust
#[test]
fn unknown_mode_flag_is_a_startup_error() {
    let output = Command::new(env!("CARGO_BIN_EXE_octet"))
        .args(["--engine", "demo", "--mode", "bogus"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("Unknown mode bogus"));
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui -p octet`
Expected: compile errors — `Action::SetMode`, `Action::Mode`, `cycle_mode`, `mode_pending` not found.

- [ ] **Step 3: Implement `App` support in `view.rs`**

- Field `pub mode_pending: Option<octet_core::Mode>,`; `mode_pending: None,` in `App::new`; `self.mode_pending = None;` in `connection()`.
- `App::event`: `Event::ModeChanged(mode) => { self.mode = mode; self.mode_pending = None; }`; in the `Event::Stopped` arm add `self.mode_pending = None;`.
- Method on `App`:

```rust
    pub fn mode_details(&self) -> String {
        let mut text = format!("Permission mode: {}", self.mode.label());
        if let Some(pending) = self.mode_pending {
            text.push_str(&format!(" (switching to {})", pending.label()));
        }
        text.push_str(&format!("\n\n{} mapping:\n", self.engine));
        for mode in octet_core::Mode::ALL {
            let marker = if mode == self.mode { "●" } else { " " };
            text.push_str(&format!("{marker} {:<13} {}\n", mode.label(), mode.describe(&self.engine)));
        }
        text.push_str("\nShift+Tab cycles ask → accept-edits → auto. /mode full-access reconnects with every check off.");
        text
    }
```

- [ ] **Step 4: Implement commands, keys and the reconnect in `lib.rs`**

`Action` gains `SetMode(octet_core::Mode),` and `Mode(octet_core::Mode),`.

`command()` gains an arm after `"/model"`:

```rust
        "/mode"=>{
            use octet_core::Mode;
            if argument.is_empty(){app.notice(app.mode_details());}
            else if app.mode_pending.is_some(){app.notice("Mode change pending; wait for the vendor to confirm");}
            else {match Mode::parse(argument){
                None=>app.notice(format!("Unknown mode {argument}. Use ask, accept-edits, auto or full-access.")),
                Some(target) if target==app.mode && target==Mode::FullAccess=>app.notice("Already in full-access mode"),
                Some(target) if target==Mode::FullAccess || app.mode==Mode::FullAccess=>{
                    if app.running||!app.approvals.is_empty(){app.notice="Cancel or finish the current turn before changing full access".into();}
                    else if !app.ready{app.notice="Wait for a ready session before changing full access".into();}
                    else {return Action::Mode(target);}
                },
                Some(target)=>return Action::SetMode(target),
            }}
        },
```

Add the cycle helper next to `command()`:

```rust
fn cycle_mode(app: &mut App) -> Action {
    if app.mode == octet_core::Mode::FullAccess {
        app.notice = "Use /mode to leave full access".into();
    } else if app.mode_pending.is_some() {
        app.notice = "Mode change pending; wait for the vendor to confirm".into();
    } else {
        return Action::SetMode(app.mode.cycle());
    }
    Action::Continue
}
```

`key_action()`: in the main `match key.code`, add `KeyCode::BackTab => return cycle_mode(app),` (approval, help and palette states return earlier, so it is inactive there).

`run_session()`: in the `match action{ … }` add before `other=>return Ok(other)`:

```rust
                    Action::SetMode(mode)=>match session.handle.send(Command::SetMode(mode)) {
                        Ok(())=>app.mode_pending=Some(mode),
                        Err(e)=>app.notice(e),
                    },
```

`run()`: add an arm before `_ => break`:

```rust
            Action::Mode(mode) => {
                if let Some(goal) = &mut app.goal {
                    if goal.status == octet_core::goal::Status::Active {
                        goal.status = octet_core::goal::Status::Paused;
                        app.save_goal().await.map_err(io::Error::other)?;
                        app.notice("Goal paused for mode switch. Use /goal resume to continue.");
                    }
                }
                app.notice(if mode == octet_core::Mode::FullAccess {
                    "Full access: the agent can run any command and edit any file without asking. Reconnecting…".to_owned()
                } else {
                    format!("Leaving full access for {}. Reconnecting…", mode.label())
                });
                config.mode = mode;
                if !app.session.is_empty() && config.engine != "demo" {
                    config.resume = Some(app.session.clone());
                }
                retained_app = Some(app);
            }
```

- [ ] **Step 5: Implement `--mode` in `main.rs`**

- Add `let mut mode = octet_core::Mode::Ask;` with the other option variables.
- Match arm: `"--mode" => mode = octet_core::Mode::parse(&value).ok_or_else(|| format!("Unknown mode {value}. Use ask, accept-edits, auto or full-access."))?,`
- `Config { … mode, }` (replacing Task 1's literal).
- `HELP`: change the usage line to `[--model MODEL] [--mode MODE] [--resume VENDOR_SESSION_ID]`, add after the Defaults line: `Modes: ask (default) · accept-edits · auto (vendor auto-review) · full-access\n`, add `Shift+Tab mode` to the Keys line, and `/mode` to the Commands line.

- [ ] **Step 6: Run tests to verify they pass**

Run: `scripts/rust-env.sh cargo fmt --all && make rust-check`
Expected: all pass, including the five new tests.

- [ ] **Step 7: Commit**

```bash
git add crates
git commit -m "Add /mode, Shift+Tab cycling, --mode and full-access reconnect to the TUI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Header chip, discoverability, PTY acceptance and docs

**Files:**
- Modify: `crates/octet-tui/src/view.rs` (header chip, `COMMANDS`, help text, sidebar quick commands; render test)
- Modify: `crates/octet/tests/terminal.rs` (`Pty::spawn_with`, new PTY test)
- Modify: `docs/tui.md`

**Interfaces:**
- Consumes: `App.mode`, `App.mode_pending` (Task 4), demo auto-allow (Task 3), `--mode` (Task 4), journal `mode` record (Task 1).
- Produces: `fn mode_chip(app: &App) -> Span<'static>` in `view.rs`; `Pty::spawn_with(args: &[&str]) -> Pty`.

- [ ] **Step 1: Write the failing tests**

Append to the tests module in `crates/octet-tui/src/view.rs`:

```rust
    #[test]
    fn header_shows_confirmed_and_pending_mode() {
        let c = octet_core::Config {
            engine: "codex".into(),
            binary: "codex".into(),
            cwd: "/tmp/project".into(),
            model: None,
            resume: None,
            mode: octet_core::Mode::Auto,
        };
        let mut a = App::new(&c, "journal".into());
        let mut t = Terminal::new(TestBackend::new(120, 36)).unwrap();
        let screen = |t: &Terminal<TestBackend>| {
            t.backend().buffer().content().iter().map(|c| c.symbol()).collect::<String>()
        };
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("auto"));
        assert_eq!(mode_chip(&a).style.fg, Some(ACCENT));
        a.mode_pending = Some(octet_core::Mode::Ask);
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("ask…"));
        a.event(Event::ModeChanged(octet_core::Mode::FullAccess));
        assert_eq!(mode_chip(&a).style.fg, Some(AMBER));
        assert!(COMMANDS.iter().any(|(name, _)| *name == "/mode"));
    }
```

In `crates/octet/tests/terminal.rs`, rename `fn spawn() -> Self` to `fn spawn_with(args: &[&str]) -> Self`, add `.args(args)` right after `.arg(&directory)`, and add:

```rust
    fn spawn() -> Self {
        Self::spawn_with(&[])
    }
```

Then add the test:

```rust
#[test]
fn auto_mode_skips_the_demo_dialog_and_shift_tab_cycles() {
    let mut p = Pty::spawn_with(&["--mode", "auto"]);
    p.wait(|p| p.count("ready") == 1);
    p.wait(|p| p.records().iter().any(|v| v["type"] == "mode" && v["data"] == "auto"));
    p.send(b"/approval-demo\r");
    p.wait(|p| p.count("finished") == 1);
    assert_eq!(p.count("approval"), 0);
    p.send(b"\x1b[Z");
    p.wait(|p| p.records().iter().any(|v| v["type"] == "mode" && v["data"] == "ask"));
    p.send(b"\x11");
    p.finish();
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui -p octet`
Expected: `header_shows_confirmed_and_pending_mode` fails to compile (`mode_chip` missing). The PTY test may already pass (behavior landed in Tasks 3–4); that is acceptable — it is the acceptance check for the whole feature.

- [ ] **Step 3: Implement the chip and discoverability**

In `view.rs`:

```rust
fn mode_chip(app: &App) -> Span<'static> {
    let color = match app.mode {
        octet_core::Mode::Ask => MUTED,
        octet_core::Mode::AcceptEdits | octet_core::Mode::Auto => ACCENT,
        octet_core::Mode::FullAccess => AMBER,
    };
    let text = match app.mode_pending {
        Some(pending) => format!("{}…", pending.label()),
        None => app.mode.label().to_owned(),
    };
    Span::styled(text, Style::default().fg(color).bold())
}
```

In `draw()`, replace the header's second line

```rust
            Line::from(Span::styled(
                format!("{}  /  {}", app.engine, app.model),
                Style::default().fg(MUTED),
            )),
```

with

```rust
            Line::from(vec![
                Span::styled(format!("{}  /  {}  ·  ", app.engine, app.model), Style::default().fg(MUTED)),
                mode_chip(app),
            ]),
```

`COMMANDS` becomes `[(&str, &str); 9]` with `("/mode", "Permission mode: ask, accept-edits, auto, full-access"),` inserted after `/model`.

Help text: after the `/model [provider] <name> · /model default\n` segment insert `/mode [ask|accept-edits|auto|full-access] · Shift+Tab cycles\n`.

Sidebar quick commands: after `Line::from(" /model      Model / provider"),` add `Line::from(" /mode       Permission mode"),`.

- [ ] **Step 4: Update `docs/tui.md`**

- Key table: add a row `| Shift+Tab | Cycle permission mode: ask → accept-edits → auto |`.
- Commands line: add `/mode` to the list.
- Replace the sentence "Codex uses workspace-write plus untrusted approval policy; this is not a promise that every vendor action raises a dialog." with "The vendor policy depends on the permission mode below; this is not a promise that every vendor action raises a dialog."
- New section after "Interaction":

```markdown
## Permission modes

`--mode MODE` at launch, `/mode MODE` at runtime, Shift+Tab to cycle. The default
is `ask`; the mode is not remembered between launches but is kept across
`/model`, `/new` and `/reconnect`. `/mode` alone shows the table for the current
provider. The header shows the mode the vendor confirmed.

| Mode | Claude | Codex (sandbox · approval · reviewer) |
| --- | --- | --- |
| `ask` | `default` | workspace-write · untrusted · user |
| `accept-edits` | `acceptEdits` | workspace-write · on-request · user |
| `auto` | `auto` (Claude's classifier) | workspace-write · on-request · `auto_review` |
| `full-access` | `bypassPermissions` | danger-full-access · never · user |

`auto` hands approval decisions to the vendor's own reviewer; Octet never
answers a vendor approval by itself. Codex has no edits-only mode, so
`accept-edits` is its closest analogue: in-workspace edits already proceed under
workspace-write, and the model asks only to escalate.

`ask`, `accept-edits` and `auto` switch live. Claude applies the change
immediately, even mid-turn; Codex applies it from the next turn. A change Claude
refuses (for example, auto mode unavailable for the account) leaves the previous
mode in place with a notice. `full-access` is never reached by Shift+Tab: type
`/mode full-access` on an idle session, which reconnects to the same vendor
session with all checks off. Leaving it reconnects again.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `scripts/rust-env.sh cargo fmt --all && make rust-check`
Expected: all pass, including `header_shows_confirmed_and_pending_mode` and `auto_mode_skips_the_demo_dialog_and_shift_tab_cycles`.

- [ ] **Step 6: Commit**

```bash
git add crates docs/tui.md
git commit -m "Show the permission mode in the TUI header and document modes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Smoke against the installed vendor CLIs

No code unless a defect is found (then: systematic-debugging, failing test first, fix, commit).

- [ ] **Step 1: Claude launch flags are accepted (no inference)**

```bash
for m in auto bypassPermissions; do
  extra=""; [ "$m" = bypassPermissions ] && extra="--allow-dangerously-skip-permissions"
  ( printf '%s\n' '{"type":"control_request","request_id":"i","request":{"subtype":"initialize"}}'; sleep 3 ) \
    | claude --print --input-format stream-json --output-format stream-json --verbose \
      --permission-prompt-tool stdio --permission-mode $m $extra --setting-sources= --strict-mcp-config 2>&1 \
    | grep -o '"subtype":"[a-z]*"' | head -2
done
```

Expected: `"subtype":"success"` for both; no error output.

- [ ] **Step 2: Codex accepts `auto_review` on `thread/start` (no inference)**

```bash
( printf '%s\n' '{"id":1,"method":"initialize","params":{"clientInfo":{"name":"octet","version":"0.1.0"},"capabilities":{"experimentalApi":true}}}'; sleep 2;
  printf '%s\n' '{"method":"initialized","params":{}}';
  printf '%s\n' "{\"id\":2,\"method\":\"thread/start\",\"params\":{\"cwd\":\"$PWD\",\"sandbox\":\"workspace-write\",\"approvalPolicy\":\"on-request\",\"approvalsReviewer\":\"auto_review\"}}"; sleep 4 ) \
  | codex app-server 2>/dev/null | grep '"id":2' | head -c 400
```

Expected: a `result` containing `thread`, no `error`.

- [ ] **Step 3: Interactive check**

`make rust-build && ./target/release/octet --engine claude --mode auto` → header chip `auto`; `/mode` shows the Claude table; Shift+Tab → `ask` then `accept-edits`. Repeat with `--engine codex`. Report outcomes verbatim to the user.
