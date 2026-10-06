# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 14 findings of the 2026-10-07 review. Octet's behaviour does
not change, except that `@` ranking gets faster and stale wording is removed.

**Architecture:** The work is 11 refactoring commits on `refactor/review-fixes`,
each leaving `make rust-check` green. The new pieces are:

- an `octet-gate` crate;
- a command registry (`octet-tui/src/commands.rs`);
- grouped `App` state;
- typed `octet-core` errors.

Everything else edits code that already exists.

**Tech Stack:** Rust 1.98.1, tokio, ratatui 0.30, crossterm 0.29, thiserror 2,
GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-07-review-fixes-design.md`

## Global Constraints

- **Toolchain:** run every cargo command through `scripts/rust-env.sh`. The
  toolchain is pinned to 1.98.1 by `rust-toolchain.toml`.
- **Gate:** `make rust-check` passes after every task. It runs fmt check,
  `cargo test --locked --workspace` and clippy with `-D warnings`.
- **No behaviour change outside decisions 4 and 9 of the spec:**
  - user-visible messages keep their exact wording, except the stale lines
    removed in Task 1;
  - the help screen and CLI `--help` may change layout, but must list every
    command and key they list today.
- **Commits:** every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Existing patterns:** follow them. Doc comments say why. No new
  dependencies except `thiserror` in `octet-core` (already in the workspace)
  and `libc` in `octet-testkit` (already in the workspace).
- **Lines:** no line over 120 characters in product crates unless the line is
  only a string literal (enforced from Task 5).

## Review Focus

These are the input classes the spec implies but the existing tests don't
exercise. For each, the task that owns it adds a test.

1. **Help on a short terminal.** The help screen, now generated, must still
  show its last line ("Esc or F1 closes help") at 100×32. Task 5,
  `help_names_the_command_palette_key` (already exists).
2. **The `/exit` alias** must still quit after dispatch moves to the
  registry. Task 5, `aliases_dispatch_like_their_command`.
3. **`@` ranking with Unicode paths and queries** (é, 界) must match the old
  order. Task 9's property test includes non-ASCII paths.
4. **A frame split exactly at the size limit** must still be accepted at
  `max` bytes and rejected at `max + 1` after `read_frames` reads by slices.
  Task 10, `frame_at_the_limit_passes_and_one_more_byte_fails`.
5. **The `!` time limit** with a sub-second limit says "300 ms", not "0
  minutes". Task 1, `time_limit_wording_follows_the_limit`.

---

### Task 1: Hygiene (spec decisions 8, 9, 13, lints, packed lines)

**Files:**
- Modify: `crates/octet-tui/src/shell.rs`, `crates/octet-tui/src/view.rs`,
  `crates/octet/src/main.rs`, `docs/rust/remote-control-plan.md`
- Modify: `Cargo.toml` and every `crates/*/Cargo.toml`
- Modify the cast and lint sites: `crates/octet-tui/src/view.rs`,
  `crates/octet-proc/src/lib.rs`, `crates/octet-tui/src/lib.rs`,
  `crates/octet-engine/src/live/{claude,codex}.rs`,
  `crates/octet/tests/terminal.rs`
- Modify the packed lines: `crates/octet-core/src/{lib,goal,model}.rs`,
  `crates/octet-tui/src/{remote,lib,view}.rs`,
  `crates/octet-engine/src/live/{claude,codex,mode,demo,driver}.rs`

**Interfaces:**
- Produces: `shell::Status::TimedOut(Duration)` and
  `shell::duration_text(Duration) -> String`.

- [ ] **Step 1: Write the failing test.** Add it to the tests in `shell.rs`:

```rust
#[test]
fn time_limit_wording_follows_the_limit() {
    let summary = |limit| Ran {
        command: "x".into(),
        status: Status::TimedOut(limit),
        output: String::new(),
    }
    .summary();
    assert_eq!(summary(Duration::from_secs(600)), "timed out after 10 minutes");
    assert_eq!(summary(Duration::from_secs(60)), "timed out after 1 minute");
    assert_eq!(summary(Duration::from_secs(90)), "timed out after 90 seconds");
    assert_eq!(summary(Duration::from_millis(300)), "timed out after 300 ms");
}
```

- [ ] **Step 2: Run it.** Command: `scripts/rust-env.sh cargo test -p octet-tui
  time_limit_wording`. Expected: it doesn't compile, because `TimedOut` takes
  no value.
- [ ] **Step 3: Implement it.** In `shell.rs`:
  - change the variant to `TimedOut(Duration)`;
  - change the select arm to `Some(Status::TimedOut(limit))`;
  - in `times_out`, change the expected summary to `"timed out after 300 ms"`;
  - add:

```rust
/// A limit as people say it: whole minutes, else seconds, else milliseconds.
pub fn duration_text(limit: Duration) -> String {
    let seconds = limit.as_secs();
    match seconds {
        0 => format!("{} ms", limit.as_millis()),
        60 => "1 minute".into(),
        s if s % 60 == 0 => format!("{} minutes", s / 60),
        1 => "1 second".into(),
        s => format!("{s} seconds"),
    }
}
```

  and make `summary` use
  `Status::TimedOut(limit) => format!("timed out after {}", duration_text(limit))`.
- [ ] **Step 4: Run the test.** Expected: PASS. `Status` derives
  `PartialEq`, and `Duration` is `PartialEq`, so nothing else changes.
- [ ] **Step 5: Stale wording.**
  - In `main.rs` `HELP`, delete the line "Fleet, full plugin/hook parity, v3
    browsing and legacy RPC compatibility remain pending.\n".
  - In `view.rs` `sidebar`, delete the blank line and the two
    "Preview · core migration" / "remains in progress." lines.
  - In `docs/rust/remote-control-plan.md`, replace the status line with:
    "Planning date: 2026-10-04. Status: superseded. The web client this
    proposes was removed on 2026-10-04 (Rust terminal UI only); phone access
    is the SSH setup in [Use Octet from your phone](../remote-control.md)."
- [ ] **Step 6: tokio features and the release profile.**
  - In the root `Cargo.toml`, set
    `tokio = { version = "1", features = ["rt-multi-thread", "macros", "process", "signal", "sync", "time", "io-util", "fs", "net"] }`.
  - Add:

```toml
[profile.release]
lto = "thin"
codegen-units = 1
strip = true
```

  - Record `ls -l target/release/octet` before and after `make rust-build`.
    If the build reports a missing tokio feature, add exactly that feature.
- [ ] **Step 7: Workspace lints.** Add this to the root `Cargo.toml`:

```toml
[workspace.lints.clippy]
cast_possible_truncation = "warn"
needless_pass_by_ref_mut = "warn"
unused_async = "warn"
```

  and add `[lints]` with `workspace = true` to every crate. Then run
  `scripts/rust-env.sh cargo clippy --workspace --all-targets -- -D warnings`
  and fix every hit:
  - **`usize`→`u16` casts in `view.rs`:** use
    `u16::try_from(x).unwrap_or(u16::MAX)`, clamped where a value is already
    `.min(...)`.
  - **`octet-proc` `size.max(1) as u32`:** use
    `u32::try_from(size.max(1)).expect("frame size fits u32: queue_bytes is checked at spawn")`.
  - **`key_action(session: &mut Session)`:** change it to `&Session`.
  - **Claude and Codex `&mut self` that isn't mutated:** change it to `&self`.
  - **`Process::spawn`:** make it a plain `fn`, not `async`, and drop `.await`
    at each call site.
  - **Test casts in `terminal.rs`:** use `try_from(...).unwrap()`.
- [ ] **Step 8: Unpack hand-packed statements.**
  - Rewrite the one-line `json!`, `format!` and `let _=` statements as named
    locals with normal spacing, so rustfmt formats them. Affected lines:
    - `octet-core/src/lib.rs` 22, 55, 89, 101;
    - `view.rs` 1127, plus the long notice lines 233, 266, 269 and 289;
    - `lib.rs` 227, 961, 1237 and 1420;
    - `remote.rs` 282, 295, 325, 508 and 563;
    - `goal.rs` 70–73;
    - `model.rs` 15;
    - `mode.rs` 46, 47 and 142;
    - the long `json!` lines in `claude.rs`, `codex.rs`, `demo.rs` and
      `driver.rs`.
  - Put a long string literal on its own line, for example:

```rust
let message = format!(
    "Storage failure: {reason}. Session stopped; journal may have an incomplete tail."
);
let _ = timeout(Duration::from_secs(1), tx.send(Event::Error(message))).await;
```

  - Leave `view.rs:1040` (help) and `main.rs:3` (`HELP`) for Task 5, which
    rebuilds them.
- [ ] **Step 9: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  252 or more tests pass.
- [ ] **Step 10: Commit.** Message: "Tidy up: time-limit wording, stale copy,
  build settings, lints".

### Task 2: CI (spec decision 1)

**Files:**
- Create: `.github/workflows/check.yml`

- [ ] **Step 1: Write the workflow.**

```yaml
name: check
on:
  push:
    branches: [master]
  pull_request:
jobs:
  check:
    strategy:
      fail-fast: false
      matrix:
        os: [macos-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      # rustup installs the toolchain rust-toolchain.toml pins.
      - run: rustup show active-toolchain || rustup toolchain install
      - uses: Swatinem/rust-cache@v2
      - run: make rust-check
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: rustsec/audit-check@v2.0.0
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 2: Check it locally.** Run
  `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/check.yml'))"`
  (or `ruby -ryaml`). Expected: no error. If `actionlint` is installed, run it
  too.
- [ ] **Step 3: Commit.** Message: "Run the Rust checks and a dependency audit
  in CI".
- **Note:** the first real run happens when the branch is pushed. Pushing is
  the user's call, at the finishing step. Ledger this as a ruling.

### Task 3: The protocol gate gets its own crate (spec decision 2)

**Files:**
- Create: `crates/octet-gate/Cargo.toml` and `crates/octet-gate/src/lib.rs`
- Move: `crates/octet-engine/src/bin/protocol-gate.rs` →
  `crates/octet-gate/src/bin/protocol-gate.rs`
- Move: `crates/octet-engine/tests/{contracts,protocol_gate}.rs` →
  `crates/octet-gate/tests/`
- Modify: `crates/octet-engine/src/lib.rs`, which becomes only the module
  doc and `pub mod live;`
- Modify: `crates/octet-engine/src/live/{mod,codex,claude}.rs`, to make the
  stray-reply functions public
- Modify: the root `Cargo.toml` members, and `README.md` around line 204
  (the crate table)

**Interfaces:**
- Produces: `octet_engine::live::{codex_stray_reply, claude_stray_reply}`, as
  `pub fn(&Value) -> Option<Value>`.
- Produces: the `octet_gate` lib with `GateProcess`, `GateError` and the
  `*_fixture_*` functions. The two `*_response_for_request` functions are
  deleted.

- [ ] **Step 1: Create the crate.** `crates/octet-gate/Cargo.toml`:

```toml
[package]
name = "octet-gate"
version = "0.1.0"
publish = false
edition.workspace = true
license.workspace = true
repository.workspace = true
rust-version.workspace = true

[dependencies]
octet-engine = { path = "../octet-engine" }
octet-proc = { path = "../octet-proc" }
tokio.workspace = true
serde_json.workspace = true
thiserror.workspace = true

[lints]
workspace = true
```

- [ ] **Step 2: Move the code.**
  - Use `git mv` for the binary and both test files.
  - Move everything in `octet-engine/src/lib.rs` above `pub mod live;` into
    `octet-gate/src/lib.rs`, except `codex_response_for_request` and
    `claude_response_for_request`.
  - Change the module doc to "The protocol gate: drives a real vendor CLI
    through fixed scenarios and records contract evidence."
- [ ] **Step 3: Share the reply code.**
  - In `live/mod.rs`, add
    `pub use claude::claude_stray_reply; pub use codex::codex_stray_reply;`
    and make both functions `pub`.
  - In the gate binary and `contracts.rs`, replace every
    `codex_response_for_request` with `octet_engine::live::codex_stray_reply`,
    and every `claude_response_for_request` with
    `octet_engine::live::claude_stray_reply`.
  - Change the `use octet_engine::{...}` imports to `use octet_gate::{...}`.
- [ ] **Step 4: Run the moved tests.** Command:
  `scripts/rust-env.sh cargo test -p octet-gate`. Expected: PASS. If a
  `contracts.rs` assertion checks the old fixture wording ("protocol fixture
  denies tool calls"), change it to check the decision (`behavior == "deny"`)
  and record a ruling.
- [ ] **Step 5: Update the docs.** In the README crate table:
  - `octet-engine`: "The vendor drivers";
  - new row `crates/octet-gate`: "The protocol gate that records live
    contract evidence".
- [ ] **Step 6: Run the gate.** Command: `make rust-check`. Expected: exit 0.
  `git grep -n "response_for_request"` finds nothing.
- [ ] **Step 7: Commit.** Message: "Move the protocol gate into its own crate
  and reuse Octet's replies".

### Task 4: Small duplicates in `lib.rs` (spec decision 5)

**Files:**
- Modify: `crates/octet-tui/src/view.rs` (`App` helpers) and
  `crates/octet-tui/src/lib.rs`

**Interfaces:**
- Produces:
  - `App::remember(&mut self, draft: String)`;
  - `App::insert_or_warn(&mut self, text: &str) -> bool`;
  - `App::replace_or_warn(&mut self, start: usize, text: &str) -> bool`;
  - `const PROMPT_FULL: &str = "Prompt limit reached"`;
  - `fn resume_id(app: &App, config: &Config) -> Option<String>`;
  - `fn regain_terminal(...)`;
  - `fn fit(terminal)`.

- [ ] **Step 1: Write the failing tests.** In the `view.rs` tests:

```rust
#[test]
fn history_skips_repeats_and_keeps_fifty() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.remember("same".into());
    app.remember("same".into());
    assert_eq!(app.history.len(), 1);
    for i in 0..60 {
        app.remember(format!("p{i}"));
    }
    assert_eq!(app.history.len(), 50);
    assert_eq!(app.history.back().map(String::as_str), Some("p59"));
}
#[test]
fn a_full_prompt_warns_instead_of_inserting() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert!(!app.insert_or_warn(&"x".repeat(octet_core::PROMPT_LIMIT + 1)));
    assert_eq!(app.notice, "Prompt limit reached");
    assert!(app.insert_or_warn("ok"));
}
```

- [ ] **Step 2: Run them.** Command:
  `scripts/rust-env.sh cargo test -p octet-tui history_skips a_full_prompt`.
  Expected: doesn't compile, because the methods don't exist yet.
- [ ] **Step 3: Implement the `App` helpers.** In `view.rs`:

```rust
const HISTORY_LIMIT: usize = 50;
pub const PROMPT_FULL: &str = "Prompt limit reached";
impl App {
    /// A sent draft joins the history unless it repeats the last one.
    pub fn remember(&mut self, draft: String) {
        self.history_index = None;
        if self.history.back() != Some(&draft) {
            self.history.push_back(draft);
            if self.history.len() > HISTORY_LIMIT {
                self.history.pop_front();
            }
        }
    }
    /// Inserts at the cursor, or says the prompt is full.
    pub fn insert_or_warn(&mut self, text: &str) -> bool {
        let fits = self.editor.insert(text);
        if !fits {
            self.notice = PROMPT_FULL.into();
        }
        fits
    }
    /// Replaces the word before the cursor, or says the prompt is full.
    pub fn replace_or_warn(&mut self, start: usize, text: &str) -> bool {
        let fits = self.editor.replace(start, text);
        if !fits {
            self.notice = PROMPT_FULL.into();
        }
        fits
    }
}
```

- [ ] **Step 4: Use the helpers in `lib.rs`.**
  - Replace both history blocks (around lines 927–933 and 973–979) with
    `app.remember(draft)`. The `!` branch keeps `app.editor.take()` before it.
  - Replace the six `"Prompt limit reached"` sites with
    `insert_or_warn`/`replace_or_warn`.
  - Fold the Alt/Shift+Enter arm and the Ctrl+J arm into one arm, placed
    before `KeyCode::Enter`:

```rust
let newline = (key.code == KeyCode::Enter && (alt || key.modifiers.contains(KeyModifiers::SHIFT)))
    || (ctrl && key.code == KeyCode::Char('j'));
// …in the match:
_ if newline => {
    app.insert_or_warn("\n");
}
```

  - Add the session and terminal helpers:

```rust
/// The vendor session a reconnect resumes, once there is one.
fn resume_id(app: &App, config: &Config) -> Option<String> {
    (!app.session.is_empty() && config.engine.is_vendor()).then(|| app.session.clone())
}
/// Sizes ratatui to the terminal again after something else used it.
fn fit(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
    terminal.resize(terminal.size()?.into())
}
/// Takes the terminal back from the editor: raw mode, a fresh frame and a
/// new key reader.
fn regain_terminal(
    guard: &TerminalGuard,
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    input: &mut Option<InputReader>,
) -> io::Result<()> {
    guard.resume()?;
    fit(terminal)?;
    *input = Some(InputReader::new());
    Ok(())
}
```

  - Use `resume_id` in the `Exit::Mode` and `Exit::Reconnect` arms
    (`if let Some(id) = resume_id(&app, &config) { config.resume = Some(id); }`).
  - Use `regain_terminal` at the two editor sites. The finish site keeps
    `disable_raw_mode()` first.
  - Use `fit` at the remaining resize calls.
- [ ] **Step 5: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  including the two new tests.
- [ ] **Step 6: Commit.** Message: "Share the history, prompt-limit, resume
  and terminal-regain code".

### Task 5: One command registry (spec decision 3)

**Files:**
- Create: `crates/octet-tui/src/commands.rs`
- Modify: `crates/octet-tui/src/lib.rs` (`try_command`, the palette), `view.rs`
  (`COMMANDS`, `palette_entries`, `help`, `sidebar`), `composer.rs` (Tab),
  `crates/octet/src/main.rs` (`HELP`) and `crates/octet/tests/terminal.rs`
  (`help_lists_every_command`)
- Create: `crates/octet/tests/source.rs`, the long-line check

**Interfaces:**
- Produces:
  - `commands::{Cmd, Spec, COMMANDS}`;
  - `Cmd::parse(&str) -> Option<Cmd>`;
  - `pub fn octet_tui::command_names() -> impl Iterator<Item = &'static str>`;
  - `view::help_lines() -> Vec<String>`.

- [ ] **Step 1: Write the failing tests.** In `commands.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_entry_parses_to_its_id_and_ids_are_unique() {
        for spec in COMMANDS {
            assert_eq!(Cmd::parse(spec.name), Some(spec.id), "{}", spec.name);
            for alias in spec.aliases {
                assert_eq!(Cmd::parse(alias), Some(spec.id), "{alias}");
            }
        }
        for (i, a) in COMMANDS.iter().enumerate() {
            assert!(COMMANDS[i + 1..].iter().all(|b| b.id != a.id && b.name != a.name));
        }
        assert_eq!(Cmd::parse("/bogus"), None);
    }
}
```

  In the `lib.rs` tests:

```rust
#[tokio::test]
async fn aliases_dispatch_like_their_command() {
    let mut app = app();
    assert!(matches!(command(&mut app, "/exit").await, Action::Exit(Exit::Quit)));
}
```

  In `terminal.rs`, change `help_lists_every_command` to loop over
  `octet_tui::command_names()` instead of the hard-coded list.
- [ ] **Step 2: Run them.** Command: `scripts/rust-env.sh cargo test -p
  octet-tui every_entry_parses`. Expected: doesn't compile, because the
  `commands` module doesn't exist yet.
- [ ] **Step 3: Write the registry.** Usage lines repeat today's help
  wording.

```rust
//! Every slash command, once. The palette, help, Tab, the sidebar and the
//! CLI help are built from this table; `try_command` dispatches on `Cmd`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cmd {
    Help, Model, Mode, Goal, Session, Export, Copy, New, Reconnect, RemoteControl, Quit,
}
pub struct Spec {
    pub id: Cmd,
    pub name: &'static str,
    pub aliases: &'static [&'static str],
    /// The help screen's line.
    pub usage: &'static str,
    /// The palette's description.
    pub summary: &'static str,
    /// The sidebar's short label, for the commands it lists.
    pub quick: Option<&'static str>,
}
pub const COMMANDS: &[Spec] = &[
    Spec { id: Cmd::Help, name: "/help", aliases: &[], usage: "/help or F1: this screen",
        summary: "Keyboard shortcuts", quick: None },
    Spec { id: Cmd::Model, name: "/model", aliases: &[], usage: "/model [provider] <name> · /model default",
        summary: "Switch model or provider", quick: Some("Switch model") },
    Spec { id: Cmd::Mode, name: "/mode", aliases: &[],
        usage: "/mode [ask|accept-edits|auto|full-access] · Shift+Tab cycles",
        summary: "Permission mode: ask, accept-edits, auto, full-access", quick: Some("Permissions") },
    Spec { id: Cmd::Goal, name: "/goal", aliases: &[],
        usage: "/goal <objective> · status · pause · resume · complete (audit) · clear",
        summary: "Inspect or manage an autonomous goal", quick: None },
    Spec { id: Cmd::Session, name: "/session", aliases: &[], usage: "/session: session ID and journal path",
        summary: "Session ID and journal path", quick: Some("Session info") },
    Spec { id: Cmd::Export, name: "/export", aliases: &[], usage: "/export [path]: copy the journal to a new file",
        summary: "Export journal to a new file", quick: Some("Save journal") },
    Spec { id: Cmd::Copy, name: "/copy", aliases: &[], usage: "/copy or Ctrl+X: copy the last reply to the clipboard",
        summary: "Copy the last reply (also Ctrl+X)", quick: None },
    Spec { id: Cmd::New, name: "/new", aliases: &[], usage: "/new: start a fresh conversation",
        summary: "Start a fresh conversation", quick: Some("Fresh context") },
    Spec { id: Cmd::Reconnect, name: "/reconnect", aliases: &[], usage: "/reconnect: resume the vendor session",
        summary: "Reconnect to the vendor session", quick: Some("Resume vendor") },
    Spec { id: Cmd::RemoteControl, name: "/remote-control", aliases: &[],
        usage: "/remote-control: check phone access setup",
        summary: "Check phone access (tmux, Tailscale, mosh)", quick: None },
    Spec { id: Cmd::Quit, name: "/quit", aliases: &["/exit"], usage: "/quit or Ctrl+C twice: save and exit",
        summary: "Save and exit", quick: None },
];
impl Cmd {
    pub fn parse(name: &str) -> Option<Cmd> {
        COMMANDS
            .iter()
            .find(|spec| spec.name == name || spec.aliases.contains(&name))
            .map(|spec| spec.id)
    }
}
```

  `rustfmt` reflows this. A `Cmd` variant missing from `COMMANDS` is never
  constructed, and `-D warnings` then fails on dead code.
- [ ] **Step 4: Dispatch on `Cmd`.**
  - In `try_command`:

```rust
let Some(cmd) = commands::Cmd::parse(name) else {
    app.notice(format!(
        "Unknown command {name}. Use /help, or edit the draft: only a path such as /usr/lib can start a prompt with a slash."
    ));
    return None;
};
match cmd { Cmd::Quit => …, Cmd::Help => …, /* one arm per variant, bodies unchanged */ }
```

  - For `Cmd::New | Cmd::Reconnect`, the existing `name == "/new"` test
    becomes `cmd == Cmd::New`.
- [ ] **Step 5: Build the other views from the table.**
  - **Palette:** `view::palette_entries()` returns
    `COMMANDS.iter().map(|s| (s.name, s.summary)).chain(PALETTE_KEYS.iter().copied())`,
    yielding `(&'static str, &'static str)`. Update the palette tests and
    `lib.rs` to use `commands::COMMANDS.len()`.
  - **Tab:** the command list in `composer::tab` iterates
    `crate::commands::COMMANDS.iter().map(|s| s.name.to_owned())`.
  - **Help:** `help_lines()` returns the 5 key lines, a blank line, each
    `spec.usage`, the 4 extra lines ("!cmd …", "@ mention …",
    "Ctrl+P commands · Ctrl+G write the prompt in $EDITOR",
    "/approval-demo: offline permission dialog"), a blank line, the approval
    line, the journal line and "Esc or F1 closes help". `help()` sizes its
    modal to `help_lines().len() + 2` rows, as the palette does.
  - **Sidebar:** "QUICK COMMANDS" lists `format!(" {:<11} {}", s.name, quick)`
    for each spec with `quick: Some`.
  - **CLI:** add `pub fn command_names()` to `octet-tui/src/lib.rs`. In
    `main.rs`, replace `const HELP` with
    `fn help() -> String { /* fixed lines, then "Commands: " + names joined ", " and wrapped at 76 columns */ }`,
    and keep the "(check phone access)" note after `/remote-control`.
- [ ] **Step 6: Add the long-line check.** `crates/octet/tests/source.rs`:

```rust
//! Hand-packed code that rustfmt can't reflow shows up as very long lines.
#[test]
fn product_code_has_no_line_over_120_characters_outside_a_lone_literal() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut long = Vec::new();
    for krate in ["octet-proc", "octet-engine", "octet-store", "octet-core", "octet-tui", "octet"] {
        let mut dirs = vec![root.join("crates").join(krate).join("src")];
        while let Some(dir) = dirs.pop() {
            for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    dirs.push(path);
                } else if path.extension().is_some_and(|e| e == "rs") {
                    let text = std::fs::read_to_string(&path).unwrap();
                    for (n, line) in text.lines().enumerate() {
                        let lone = line.trim_start().starts_with('"') || line.trim_start().starts_with("r#\"");
                        if line.chars().count() > 120 && !lone {
                            long.push(format!("{}:{}", path.display(), n + 1));
                        }
                    }
                }
            }
        }
    }
    assert!(long.is_empty(), "lines over 120 characters:\n{}", long.join("\n"));
}
```

  Run it and fix every line it names.
- [ ] **Step 7: Run the gate.** Command: `make rust-check`. Expected: exit 0.
  `help_names_the_command_palette_key` still passes at 100×32.
- [ ] **Step 8: Commit.** Message: "Define every slash command once and build
  help, the palette and Tab from it".

### Task 6: Smaller files (spec decision 6)

**Files:**
- Create: `crates/octet-tui/src/app.rs` and `crates/octet-tui/src/input.rs`
- Modify: `crates/octet-tui/src/lib.rs`, `view.rs` and `commands.rs`

**Interfaces:**
- Consumes: everything from Tasks 4–5.
- Produces:
  - `app::{App, Role}` (moved from `view`);
  - `input::{key_action, paste}`;
  - `commands::{try_command, command}`;
  - `view::draw`, unchanged.

- [ ] **Step 1: `view.rs` → `app.rs`.** Move into `app.rs`:
  - `Role`, `Entry`, `App` and `impl App`;
  - `entry_lines`, `wrap`, `MAX_BYTES` and `BLOCK_BYTES`.

  `view.rs` keeps the colours, `card`, `mode_chip`, `draw` and its helpers,
  `modal`, `help`, `palette`, `approval`, `PALETTE_KEYS` and
  `palette_entries`. Re-export with `pub use crate::app::App;` only where
  callers need it, and update the `use` lines. Move each test to the file
  holding the code it tests. Tests that render through `screen()` stay in
  `view.rs`.
- [ ] **Step 2: Split `draw`.** Break it into `header(frame, area, app)`,
  `conversation(frame, area, app)`, `composer(frame, area, app, draft) -> (Rect, usize)`
  (which returns the input area and scroll offset for the cursor) and
  `overlays(frame, area, app, cursor)`. Keep the code verbatim inside each.
- [ ] **Step 3: `lib.rs` → `input.rs` and `commands.rs`.**
  - Into `input.rs`:
    - `key_action`, split into `help_key`, `approval_key`, `palette_press`,
      `completion_key` and `composer_key`, each returning `Option<Action>`
      where `None` means "fall through";
    - `paste`, `palette_key`, `open_mentions`, `refresh_completion`,
      `accept_completion`, `cycle_mode`, `copy_reply`, `size_label`,
      `cancel_turn`, `TAB_WAIT` and `off_loop`.
  - Into `commands.rs`: `try_command`, `command`, `send_goal_prompt`,
    `goal_send_failed`, `reconnect_notice` and `full_access_notice`. Give
    `/model`, `/mode`, `/goal` and `/export` one function each.
  - `lib.rs` keeps `InputReader`, `TerminalGuard`, `Action`, `Exit`, `run`,
    `run_session`, `session_event`, the shell and editor plumbing, and the
    alert and remote-check helpers.
  - Move the tests with their code.
- [ ] **Step 4: Check the sizes.** Command:
  `wc -l crates/octet-tui/src/{lib,app,view,input,commands}.rs`. Expected:
  each file ≤ ~800 lines.
- [ ] **Step 5: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  same test count as before the task.
- [ ] **Step 6: Commit.** Message: "Split the TUI into app, view, input and
  commands modules".

### Task 7: `App` grouped by concern (spec decision 12)

**Files:**
- Modify: `crates/octet-tui/src/app.rs` and every TUI module that reads
  `App` fields

**Interfaces:**
- Produces: `App { conn: Connection, chat: Transcript, composer: Composer,
  overlay: Overlays, notice, quit_armed, goals, monochrome }`. Sub-struct
  fields are `pub(crate)`.

| Group | Fields |
| --- | --- |
| `Connection` | engine, mode, mode_pending, workspace, model, requested_model, resolved_model, models, session, journal, ready, running, stopped, status, usage, activity |
| `Transcript` | entries, bytes, sanitizer, scroll, catalog_focus, reply, reply_sanitizer, reply_break, reply_stale |
| `Composer` | editor, history, history_index, saved_draft, attachments, shell_running, completion, files, root |
| `Overlays` | help, palette, selection, approvals, approval_scroll |

- [ ] **Step 1: Define the four structs.** Write them in `app.rs` with
  constructors taking the same inputs `App::new` uses today, and make
  `App::new` compose them. Keep every `App` method on `App`. Rename
  `App::transcript()` to `App::visible_lines()` to free the name.
- [ ] **Step 2: Fix compile errors module by module.** Run
  `scripts/rust-env.sh cargo build -p octet-tui 2>&1 | grep -c '^error'` and
  repeat until it reports 0. Every edit is a path change, for example
  `app.running` → `app.conn.running`. Logic doesn't change.
- [ ] **Step 3: Run the gate.** Command: `make rust-check`. Expected: exit 0,
  same test count.
- [ ] **Step 4: Live smoke test.** Run the tmux driver over the demo:
  - prompt;
  - `/approval-demo`, then A;
  - `!echo hi`;
  - `@` popup;
  - `/mode` and Shift+Tab;
  - Ctrl+C twice.

  Expected: the same screens as in the 2026-10-06 run.
- [ ] **Step 5: Commit.** Message: "Group App state into connection,
  transcript, composer and overlays".

### Task 8: Typed errors at the core boundary (spec decision 11)

**Files:**
- Create: `crates/octet-core/src/error.rs`
- Modify: `crates/octet-core/{Cargo.toml,src/lib.rs,src/goal.rs,src/model.rs}`
  and the TUI call sites

**Interfaces:**
- Produces: `octet_core::{GoalError, SelectionError, SessionError,
  ExportError}`. Each `Display` text is identical to today's string.

- [ ] **Step 1: Write the failing test.** In `error.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn messages_keep_their_wording() {
        let io = || std::io::Error::other("disk full");
        let cases: Vec<(String, &str)> = vec![
            (GoalError::InvalidObjective.to_string(), "Goal must be 1–8192 bytes on one line"),
            (GoalError::Read(io()).to_string(), "Cannot read goal: disk full"),
            (GoalError::TooLarge.to_string(), "Goal file exceeds 48 KiB"),
            (GoalError::MissingObjective.to_string(), "Goal objective missing"),
            (GoalError::InvalidStatus.to_string(), "Invalid goal status"),
            (GoalError::InvalidTurns.to_string(), "Invalid goal turn count"),
            (GoalError::InvalidPath.to_string(), "Invalid goal path"),
            (GoalError::Save(io()).to_string(), "Cannot save goal: disk full"),
            (GoalError::Clear(io()).to_string(), "Cannot clear goal: disk full"),
            (GoalError::AlreadyActive.to_string(), "Pause or clear the active goal before replacing it"),
            (GoalError::NoGoal.to_string(), "No goal set"),
            (GoalError::Complete.to_string(), "Goal is already complete; set a new goal to continue."),
            (GoalError::TurnGuard.to_string(), "Goal reached the 200-turn guard. Set a new goal to continue."),
            (GoalError::Persist(Box::new(GoalError::Save(io()))).to_string(),
                "Goal persistence failed: Cannot save goal: disk full"),
            (GoalError::PersistPaused(Box::new(GoalError::Save(io()))).to_string(),
                "Goal persistence failed; paused: Cannot save goal: disk full"),
            (SessionError::Create(io()).to_string(), "Cannot create transcript journal: disk full"),
            (SessionError::Write(io()).to_string(), "Cannot write transcript journal: disk full"),
            (ExportError::Read { path: "/a".into(), source: io() }.to_string(), "Cannot read journal /a: disk full"),
            (ExportError::Create { path: "/b".into(), source: io() }.to_string(), "Cannot create /b: disk full"),
            (ExportError::Write { path: "/b".into(), source: io() }.to_string(), "Cannot write /b: disk full"),
        ];
        for (got, want) in cases {
            assert_eq!(got, want);
        }
    }
}
```

  Add `SelectionError` cases with the five `model.rs` messages, copied
  exactly.
- [ ] **Step 2: Run it.** Command:
  `scripts/rust-env.sh cargo test -p octet-core messages_keep`. Expected:
  doesn't compile.
- [ ] **Step 3: Write the enums.** Use `#[derive(Debug, thiserror::Error)]`
  with the messages above. For example,
  `#[error("Cannot read journal {}: {source}", path.display())] Read { path: PathBuf, #[source] source: std::io::Error }`.
  Then:
  - add `thiserror.workspace = true` to `octet-core`;
  - `GoalStore::save` maps the clock and JSON errors into `Save` with
    `io::Error::other`;
  - an invalid JSON file becomes `GoalError::Parse(serde_json::Error)`, with
    `#[error("Invalid goal file: {0}")]`.
- [ ] **Step 4: Change the signatures.** Change `goal.rs`, `model.rs` and
  `lib.rs` (`Session::open`, `export_journal`) to return the new types. In
  the TUI, a site that passed the `String` to `app.notice(error)` passes
  `error.to_string()`, and `format!("…{error}")` sites stay as they are.
  `main.rs` maps `Session::open` errors with `.to_string()`, as before.
- [ ] **Step 5: Run the gate.** Command: `make rust-check`. Expected: exit 0.
  Every existing test that compares an error message still passes.
- [ ] **Step 6: Commit.** Message: "Return typed errors from octet-core".

### Task 9: Faster `@` ranking (spec decision 4)

**Files:**
- Modify: `crates/octet-tui/src/files.rs`

- [ ] **Step 1: Write the tests.** Keep today's algorithm as a test-only
  reference (`fn reference_rank`, a verbatim copy of the current `rank`,
  `score` and `subsequence`), then:

```rust
#[test]
fn ranking_matches_the_reference_on_generated_paths() {
    let mut seed = 0x2545_f491_4f6c_dd1du64;
    let mut next = || { seed ^= seed << 13; seed ^= seed >> 7; seed ^= seed << 17; seed };
    let parts = ["src", "main", "Lib", "界", "é", "mod", "test", "a_b", "x"];
    let paths: Vec<String> = (0..3000)
        .map(|_| (0..1 + next() % 4).map(|_| parts[(next() % 9) as usize]).collect::<Vec<_>>().join("/") + ".rs")
        .collect();
    let index = Index::from_paths(paths.clone());
    for query in ["", "m", "main", "MAIN", "界", "é/m", "srcmainrs", "zz", "a_b"] {
        assert_eq!(index.rank(query), reference_rank(&paths, query), "{query}");
    }
}
#[test]
#[ignore = "timing; run with cargo test --release -p octet-tui -- --ignored"]
fn ranking_fifty_thousand_paths_takes_under_25_ms() {
    let paths: Vec<String> = (0..50_000)
        .map(|i| format!("crates/module{}/src/sub{}/file_{i}_handler.rs", i % 40, i % 300))
        .collect();
    let index = Index::from_paths(paths);
    for query in ["m", "main", "handler_rs"] {
        let started = std::time::Instant::now();
        let _ = index.rank(query);
        assert!(started.elapsed() < std::time::Duration::from_millis(25), "{query}: {:?}", started.elapsed());
    }
}
```

- [ ] **Step 2: Run the ignored test.** Command: `scripts/rust-env.sh cargo
  test --release -p octet-tui ranking_fifty -- --ignored`. Expected: FAIL
  (71–119 ms measured).
- [ ] **Step 3: Implement it.** Replace `score` and `subsequence` with a
  `Scratch` holding three reusable `Vec<usize>` rows:
  - `Scratch::score(&mut self, lower: &str, query: &[char])` returns `None`
    early when `!query.iter().all(|c| lower.contains(*c))`;
  - `subsequence` uses `clear()`/`resize()` and `std::mem::swap(&mut
    self.ending, &mut self.here)` instead of allocating;
  - `rank` builds `let query: Vec<char> = query.to_lowercase().chars().collect();`
    and one `Scratch`.

  The sort and `take(SHOWN)` don't change. Adapt the existing
  `subsequence` unit tests to call through `Scratch`.
- [ ] **Step 4: Run both tests.** Run the property test with
  `scripts/rust-env.sh cargo test -p octet-tui ranking_matches`; expected:
  PASS. Then run the release-mode test from Step 2; expected: PASS. If 25 ms
  isn't met, profile before changing the algorithm, and record what was
  found.
- [ ] **Step 5: Run the gate.** Command: `make rust-check`. Expected: exit 0.
- [ ] **Step 6: Commit.** Message: "Rank @ suggestions without allocating per
  character".

### Task 10: Small performance fixes (spec decision 14)

**Files:**
- Modify: `crates/octet-tui/src/app.rs` (`visible_lines`, `wrap`,
  `show_models`) and `crates/octet-proc/src/lib.rs` (`read_frames`)
- Test: `crates/octet-proc/tests/transport.rs`

- [ ] **Step 1: Write the failing test.** In `transport.rs`, using the
  existing `config` helper and fake child: send one frame whose JSON payload
  is exactly `max_frame_bytes` bytes, and assert it arrives. Then send one
  that is `max_frame_bytes + 1` bytes and assert
  `ProcessError::FrameTooLarge`. Name it
  `frame_at_the_limit_passes_and_one_more_byte_fails`. It must pass on the
  current code; it pins the limit before the rewrite.
- [ ] **Step 2: Run it.** Command:
  `scripts/rust-env.sh cargo test -p octet-proc frame_at_the_limit`.
  Expected: PASS.
- [ ] **Step 3: Rewrite `read_frames` by slices.**

```rust
Ok(n) => {
    let mut rest = &chunk[..n];
    while let Some(newline) = rest.iter().position(|byte| *byte == b'\n') {
        let (part, tail) = rest.split_at(newline);
        if frame.len() + part.len() > max {
            let _ = tx.send(FrameEvent::Error(ProcessError::FrameTooLarge { limit: max })).await;
            return;
        }
        frame.extend_from_slice(part);
        // …acquire permits and send `frame` exactly as today…
        rest = &tail[1..];
    }
    if frame.len() + rest.len() > max {
        let _ = tx.send(FrameEvent::Error(ProcessError::FrameTooLarge { limit: max })).await;
        return;
    }
    frame.extend_from_slice(rest);
}
```

- [ ] **Step 4: The `app.rs` fixes.**
  - **`wrap`:** `let width_of_word = word.width();` once per word, and the
    same for each grapheme.
  - **`visible_lines`:** first pass, rebuild stale caches over `iter_mut()`
    until the needed row count is covered. Second pass, collect
    `Vec<&Line>` over `iter()`. Clone only the `height` visible rows:
    `rows[scroll..scroll + height]` reversed.
  - **`show_models`:** build the page's notice strings from
    `self.models.iter().skip(…).take(20)` into a `Vec<String>`, then add
    them. There's no `self.models.clone()`.
- [ ] **Step 5: Run the gate.** Command: `make rust-check`. Expected: exit 0.
  The transcript, scroll and catalog tests are unchanged and pass.
- [ ] **Step 6: Commit.** Message: "Read frames by slices and clone only the
  visible transcript rows".

### Task 11: More reliable tests (spec decision 7)

**Files:**
- Modify: `crates/octet-testkit/{Cargo.toml,src/lib.rs}`
- Create: `crates/octet-testkit/src/bin/detached-sleep.rs`
- Modify: `crates/octet-tui/src/shell.rs` (tests),
  `crates/octet-proc/tests/transport.rs:266`, and the test files with
  condition-style sleeps

**Interfaces:**
- Produces:
  - `octet_testkit::wait_until(timeout: Duration, condition: impl FnMut() -> bool) -> bool`;
  - `octet_testkit::detached_sleep() -> PathBuf`;
  - `octet_testkit::QUICK: Duration` (5 s), the shared bound for "finishes
    at once".

- [ ] **Step 1: Add the testkit pieces.**

```rust
/// How long "finishes at once" may take on a machine busy with the suite.
pub const QUICK: Duration = Duration::from_secs(5);
/// Polls `condition` every 10 ms until it holds or `timeout` passes.
pub fn wait_until(timeout: Duration, mut condition: impl FnMut() -> bool) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if condition() {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
```

  Add the `detached-sleep` binary (`libc::setsid()`, then sleep for the
  seconds given in `argv[1]`). Generalize the build in `protocol_child()`
  into `fn testkit_bin(name: &str) -> PathBuf`, and add
  `pub fn detached_sleep() -> PathBuf { testkit_bin("detached-sleep") }`.
- [ ] **Step 2: Remove the perl dependency.** In `shell.rs`,
  `a_process_that_left_the_group_cannot_hold_the_result` runs
  `format!("{} 60 & sleep 0.5; echo started", octet_testkit::detached_sleep().display())`.
  Run `scripts/rust-env.sh cargo test -p octet-tui a_process_that_left`.
  Expected: PASS.
- [ ] **Step 3: Give the ignore its reason.** Set `transport.rs:266` to
  `#[ignore = "timing benchmark; run explicitly"]`.
- [ ] **Step 4: Replace the bounds.** Replace the numeric "finishes at once"
  bounds (`< Duration::from_secs(5)`) with `octet_testkit::QUICK`. Leave
  bounds that test a specific limit (the 30 s bound against a 60 s sleep, the
  probe's 2.7 s) as they are, each with a comment saying what it bounds.
- [ ] **Step 5: Audit the sleeps in tests.** Run `grep -n "sleep("` over the
  test files and `#[cfg(test)]` modules, and classify each sleep:
  - **Waits for a condition:** replace it with `wait_until` (sync) or a
    `tokio::time::timeout` around the awaited event (async).
  - **Part of the scenario:** keep it, for example "cancel after 200 ms" or
    "sleep 0.5 inside a shell command".

  Where a test exercises timer logic with no real child process, use
  `#[tokio::test(start_paused = true)]`. Ledger the count converted and
  kept.
- [ ] **Step 6: Run the gate, twice.** Command: `make rust-check` twice in a
  row. Expected: exit 0 both times.
- [ ] **Step 7: Commit.** Message: "Make the timing-sensitive tests wait for
  conditions".

---

## Finish

- [ ] **Final whole-branch review:** follow executing-plans' Final Review
  section.
- [ ] **Live run:** a full tmux run over the demo and the fake Codex and
  Claude vendors, repeating the 2026-10-06 checklist.
- [ ] **Docs:** update `CHANGELOG.md` only if something user-visible changed
  (the help layout and the time-limit wording).
- [ ] **Hand-off:** use superpowers:finishing-a-development-branch.
