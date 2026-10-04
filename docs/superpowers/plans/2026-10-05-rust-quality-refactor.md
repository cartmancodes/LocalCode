# Rust Quality Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the Rust workspace in line with Rust best practices without changing what users see: typed engine/outcome/error values instead of strings, a decomposed vendor driver, goal orchestration out of the TUI event loop, and one shared test fixture.

**Architecture:** Behaviour-preserving refactor in eight tasks, each verified by the existing suite (`make rust-check`, 119 tests incl. PTY end-to-end). New enums keep their wire/journal spelling through `as_str()`, so journals and vendor traffic are byte-identical. `octet-engine/src/live.rs` becomes a `live/` module with one `Driver` state struct and per-vendor `impl` blocks; goal turn bookkeeping moves to `octet_core::goal::GoalRunner`.

**Tech Stack:** Rust 1.98 workspace (`tokio`, `serde_json`, `thiserror`, `ratatui`, `crossterm`, `libc`), fake vendor `crates/octet-testkit/src/bin/protocol-child.rs`.

**Spec:** Whole-tree quality review of HEAD `70b2ee3` (findings recorded in `docs/reviews/2026-10-05-rust-quality-review.md`, written in Task 8). Prior functional review: `docs/reviews/2026-10-05-rust-tui-review.md`.

## Global Constraints

- No user-visible change unless a task says so. Journal records (`"engine"`, `"finished"`, `"mode"` values), vendor wire JSON, CLI messages and notices stay byte-identical.
- Deliberate behaviour changes (only these): goal persistence failures are always reported as a notice and never silently dropped or fatal to the TUI (Task 7).
- Linux and macOS only (`docs/rust/tui-features.md`): non-Unix code paths are removed, a `compile_error!` states it.
- Run cargo through `scripts/rust-env.sh`. Gate for every task: `scripts/rust-env.sh cargo fmt --all`, then `make rust-check` (fmt check, workspace tests, clippy `-D warnings`) must pass before the commit.
- After Task 6, no line in `crates/*/src` may exceed 120 characters outside string literals (`awk 'length>120' crates/octet-engine/src/live/*.rs` prints only literal-bearing lines). This is what keeps code formattable by rustfmt.
- Work on branch `refactor/rust-quality`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Match the surrounding idiom: `parse(&str) -> Option<Self>` + `as_str()`/`label()` (as `Mode` does), short doc comments that say *why*.

## Out of scope (reviewed and deferred, with reason)

- Protocol-gate binary restructure (`Scenario` enum, `GateStats`, moving fixtures out of `octet-engine/src/lib.rs`): developer tool, separate plan.
- `App` `Phase`/`Overlay` enums and moving `App` to `app.rs`: Task 7 adds the predicates that remove the duplication; the enum rewrite is churn without a bug behind it.
- Typed (`thiserror`) errors in `octet-core`: every one is shown verbatim to the user and no caller branches on them, so they stay `String` and gain context instead (Task 2).
- A separate `Provider` type: `Engine` plus `Selection::parse` rejecting `Demo` is enough (YAGNI).
- `read_frames` byte-at-a-time loop: no measurement shows it matters.

## Review Focus

1. Journal spelling drifts when strings become enums (`engine`, `finished`) → a reader of an old and a new journal sees the same values. Test: Task 4 `journal_keeps_engine_and_outcome_spelling` in `crates/octet-core/tests/session.rs`.
2. Codex reports a turn status Octet does not know (e.g. `"inProgress"`) → it is shown and journaled verbatim, never mapped to `completed`. Test: Task 4 `unknown_vendor_status_is_kept_verbatim`.
3. Vendor stderr gets attached to a driver-local failure (or stops being attached to a vendor failure) once errors are typed → identical text to today. Test: Task 5 `stderr_is_attached_only_to_vendor_failures_and_starts_on_a_line` (rewritten over `DriverError`).
4. The goal file cannot be written during `/reconnect` or a model switch → the TUI stays open with a notice instead of exiting. Test: Task 7 `pause_active_reports_persistence_failure`.
5. `cargo test --release` or a custom `CARGO_TARGET_DIR` runs a stale or missing fake vendor → the fixture is built for the running profile. Verified in Task 1 Step 4.

---

### Task 1: Shared test fixture crate

**Files:**
- Create: `crates/octet-testkit/src/lib.rs`
- Modify: `crates/octet-testkit/Cargo.toml` (drop `libc`, add `[lib]`)
- Modify: `crates/octet-proc/Cargo.toml`, `crates/octet-engine/Cargo.toml`, `crates/octet/Cargo.toml`, `crates/octet-core/Cargo.toml`, `crates/octet-tui/Cargo.toml`, `crates/octet-store/Cargo.toml` (`[dev-dependencies] octet-testkit = { path = "../octet-testkit" }`)
- Modify: `crates/octet-proc/tests/transport.rs:6-29`, `crates/octet-engine/tests/live.rs:4-27`, `crates/octet/tests/terminal.rs:366-389`
- Modify: every test using `std::env::temp_dir().join(...)` for a directory it later removes (`grep -rn 'temp_dir()' crates`)

**Interfaces:**
- Produces: `octet_testkit::protocol_child() -> PathBuf`, `octet_testkit::TempDir` (`TempDir::new(prefix: &str) -> TempDir`, `TempDir::path(&self) -> &Path`, removes the directory on drop).

- [ ] **Step 1: Write the library**

```rust
//! Test support shared by the workspace: the fake vendor binary and
//! self-cleaning temporary directories.
use std::{
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicU64, Ordering},
        OnceLock,
    },
};

/// Builds `protocol-child` for the profile the calling test runs in and
/// returns its path. Release and custom target directories get their own copy.
pub fn protocol_child() -> PathBuf {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY
        .get_or_init(|| {
            // Test executables live in <target>/<profile>/deps/.
            let profile_dir = std::env::current_exe()
                .expect("test executable path")
                .parent()
                .and_then(Path::parent)
                .expect("test executable inside a profile directory")
                .to_path_buf();
            let target_dir = profile_dir.parent().expect("target directory");
            let profile = match profile_dir.file_name().and_then(|n| n.to_str()) {
                Some("debug") | None => "dev",
                Some(other) => other,
            };
            let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
            let workspace = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
            let status = Command::new(cargo)
                .args(["build", "--quiet", "--locked", "-p", "octet-testkit", "--bin", "protocol-child"])
                .args(["--profile", profile])
                .arg("--target-dir")
                .arg(target_dir)
                .current_dir(workspace)
                .status()
                .expect("run cargo to build protocol-child");
            assert!(status.success(), "building protocol-child failed");
            profile_dir.join("protocol-child")
        })
        .clone()
}

/// A unique directory under the system temp dir, removed when dropped, so a
/// failing test does not leave it behind.
pub struct TempDir(PathBuf);
impl TempDir {
    pub fn new(prefix: &str) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("{prefix}-{}-{n}", std::process::id()));
        Self(path)
    }
    pub fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        // Some tests put a file at the path to make writes fail.
        if std::fs::remove_dir_all(&self.0).is_err() {
            let _ = std::fs::remove_file(&self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn temp_dirs_are_unique_and_removed() {
        let first = TempDir::new("octet-testkit");
        let second = TempDir::new("octet-testkit");
        assert_ne!(first.path(), second.path());
        std::fs::create_dir_all(first.path()).unwrap();
        let path = first.path().to_path_buf();
        drop(first);
        assert!(!path.exists());
    }
}
```

`TempDir::new` does not create the directory: the code under test (`Journal::create`, `GoalStore::save`) creates it, as today.

`Cargo.toml` of octet-testkit: remove `libc.workspace = true`; add

```toml
[lib]
path = "src/lib.rs"
```

- [ ] **Step 2: Replace the three copies**

Delete `child_binary()` (transport.rs), the fixture body inside `config()` (tests/live.rs) and `provider_fixture()` (terminal.rs); call `octet_testkit::protocol_child()` instead. In `tests/live.rs` `config()` becomes:

```rust
fn config() -> Config {
    Config {
        engine: "codex".into(),
        binary: octet_testkit::protocol_child(),
        cwd: std::env::temp_dir(),
        model: None,
        resume: None,
        mode: Default::default(),
    }
}
```

(Task 3 changes `engine` to `Engine::Codex`.)

- [ ] **Step 3: Use `TempDir` in tests**

Replace each `let dir = std::env::temp_dir().join(format!("octet-…-{}", std::process::id()));` + trailing `remove_dir_all(dir)` with `let dir = octet_testkit::TempDir::new("octet-…");` and `dir.path()`; delete the explicit `remove_dir_all` (the guard does it). In `octet-tui/src/lib.rs` tests the same applies to `octet-goal-cancel` and `octet-tui-draft`.

- [ ] **Step 4: Verify, including the release profile**

Run: `make rust-check` — Expected: PASS, same test count + 1.
Run: `scripts/rust-env.sh cargo test --release -p octet-proc --test transport` — Expected: PASS and `target/release/protocol-child` exists.

- [ ] **Step 5: Commit** — `git commit -am "Share the fake vendor fixture and temp dirs through octet-testkit"` (add `crates/octet-testkit/src/lib.rs` first).

---

### Task 2: Small hygiene (Unix-only, unsafe, boundaries, helpers)

**Files:**
- Modify: `crates/octet-proc/src/lib.rs:138,164-177,318-339,346`
- Modify: `crates/octet-tui/src/lib.rs:92-100,206-212,284-296`
- Modify: `crates/octet-tui/src/text.rs` (Sanitizer state enum)
- Modify: `crates/octet-engine/src/live.rs:108-162` (visibility), `:181-189,555,610` (identifier check), `:287-290,300-303,764-767` (boundaries)
- Modify: `crates/octet-core/src/model.rs:28`
- Modify: `crates/octet-tui/src/view.rs:255-259,331-334`
- Modify: `crates/octet-store/src/lib.rs` (add `create_private`), `crates/octet-core/src/lib.rs:15-19,117-136`, `crates/octet-core/src/goal.rs:145-185`
- Modify: `crates/octet-tui/src/editor.rs:9`

**Interfaces:**
- Produces: `octet_engine::live::valid_identifier(&str) -> bool`; `octet_store::create_private(&Path) -> io::Result<tokio::fs::File>`.

- [ ] **Step 1: Write failing tests**

In `crates/octet-engine/src/live.rs` `mod model_tests`:

```rust
#[test]
fn identifiers_are_bounded_single_line_and_non_empty() {
    assert!(valid_identifier("gpt-5.5"));
    assert!(valid_identifier(&"x".repeat(256)));
    for bad in ["", "a\u{1b}b", "a\nb"] {
        assert!(!valid_identifier(bad), "{bad:?}");
    }
    assert!(!valid_identifier(&"x".repeat(257)));
}
```

In `crates/octet-store/src/lib.rs` tests:

```rust
#[tokio::test]
async fn private_files_are_new_and_owner_only() {
    use std::os::unix::fs::PermissionsExt;
    let dir = octet_testkit::TempDir::new("octet-store-private");
    fs::create_dir_all(dir.path()).await.unwrap();
    let path = dir.path().join("file");
    drop(create_private(&path).await.unwrap());
    let mode = fs::metadata(&path).await.unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
    assert_eq!(create_private(&path).await.unwrap_err().kind(), io::ErrorKind::AlreadyExists);
}
```

Run: `scripts/rust-env.sh cargo test -p octet-engine -p octet-store` — Expected: FAIL (`valid_identifier`, `create_private` not found).

- [ ] **Step 2: Helpers**

`crates/octet-engine/src/live.rs`:

```rust
/// A vendor model identifier Octet will pass on or display: non-empty,
/// at most 256 bytes, no control characters.
pub fn valid_identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
}
```

Use it in `model_catalog` (both checks), the Claude model check (`actual.filter(|m| valid_identifier(m))`), the Codex `result.model` check, and `Selection::parse` (`if !octet_engine::live::valid_identifier(model)` — keep the existing error text).

`crates/octet-store/src/lib.rs`:

```rust
/// Opens a new owner-only file for writing; fails if `path` exists.
pub async fn create_private(path: &Path) -> io::Result<File> {
    OpenOptions::new().write(true).create_new(true).mode(0o600).open(path).await
}
```

Use it in `Journal::create`, `export_journal` and `GoalStore::save` (each currently builds the same `OpenOptions`).

- [ ] **Step 3: Char boundaries**

Replace every hand-written boundary loop with the std method (stable since 1.91):

| Site | Replacement |
|---|---|
| `emit` (`live.rs:287`) | `let end = remaining.floor_char_boundary(EVENT_BYTES);` |
| `limited` (`live.rs:300`) | `let end = text.floor_char_boundary(EVENT_BYTES);` |
| `codex_tool_detail` (`live.rs:764`) | `let start = output.ceil_char_boundary(output.len() - room);` |
| `App::add` (`view.rs:256`) | `let start = text.ceil_char_boundary(text.len() - BLOCK_BYTES);` |
| `Event::Text` (`view.rs:331`) | `let remove = e.text.ceil_char_boundary(e.text.len() - BLOCK_BYTES);` |
| goal output (`lib.rs:228`) | removed in Task 7; leave it |

`floor_char_boundary(n)` with `n >= len` returns `len`, matching the existing `.min(len)` logic.

- [ ] **Step 4: Unix-only and `unsafe`**

At the top of `crates/octet-proc/src/lib.rs`:

```rust
#[cfg(not(unix))]
compile_error!("Octet supports Linux and macOS only");
```

Delete both `#[cfg(not(unix))]` functions (`signal_group`, `group_alive`) and drop the now-redundant `#[cfg(unix)]` attributes in octet-proc and octet-tui. Store the group as `libc::pid_t`:

```rust
let process_group = libc::pid_t::try_from(child.id().expect("spawned child must have pid"))
    .expect("pid fits pid_t");
```

```rust
fn signal_group(&self, signal: i32) {
    debug_assert!(self.process_group > 1, "never signal init or every process");
    // SAFETY: killpg only sends a signal; the group is our own child's.
    unsafe {
        libc::killpg(self.process_group, signal);
    }
}

fn group_alive(&self) -> bool {
    // SAFETY: signal 0 performs only the permission and existence check.
    (unsafe { libc::killpg(self.process_group, 0) == 0 })
        || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}
```

Add `// SAFETY: pre_exec runs in the forked child before exec; setpgid is async-signal-safe.` above the `pre_exec` block. In `TerminalGuard::suspend`:

```rust
// SAFETY: raise only delivers SIGSTOP to this process.
unsafe {
    libc::raise(libc::SIGSTOP);
}
```

- [ ] **Step 5: Visibility and `#[must_use]`**

Make `claude_permission_args`, `codex_thread_params`, `claude_reported_mode`, `codex_reported`, `codex_turn_overrides` `pub(crate)` (only `mode_tests` use them). Add `#[must_use = "a failed cleanup is only visible in the report"]` to `Process::shutdown` and `#[must_use]` to `Editor::insert`. Fix any new warnings by binding the result (`let _report = …` only where the caller deliberately ignores it, with a comment).

- [ ] **Step 6: Sanitizer state enum**

```rust
#[derive(Clone, Copy, Default)]
enum State {
    #[default]
    Text,
    /// After ESC.
    Escape,
    /// Inside CSI, until a final byte.
    Csi,
    /// Inside OSC/DCS/SOS/PM/APC, until BEL or ST.
    String,
    /// ESC inside a string: `\` completes ST.
    StringEscape,
}
#[derive(Default)]
pub struct Sanitizer {
    state: State,
}
```

Map `0→Text`, `1→Escape`, `2→Csi`, `3→String`, `4 (the `_` arm)→StringEscape`; keep every transition identical. Existing `strips_split_osc_and_csi` covers it.

- [ ] **Step 7: Error context in octet-core**

`export_journal` keeps returning `String` but says which step failed:

```rust
let input = tokio::fs::File::open(source)
    .await
    .map_err(|e| format!("Cannot read journal {}: {e}", source.display()))?;
let size = input
    .metadata()
    .await
    .map_err(|e| format!("Cannot read journal {}: {e}", source.display()))?
    .len();
let mut output = octet_store::create_private(target)
    .await
    .map_err(|e| format!("Cannot create {}: {e}", target.display()))?;
let write = |e: std::io::Error| format!("Cannot write {}: {e}", target.display());
tokio::io::copy(&mut input.take(size), &mut output).await.map_err(write)?;
output.flush().await.map_err(write)?;
output.sync_data().await.map_err(write)
```

`Session::open`: `.map_err(|e| format!("Cannot write transcript journal: {e}"))` on the `append`. `GoalStore::save`: prefix each `map_err` with `"Cannot save goal: "`.

- [ ] **Step 8: Verify** — Run `make rust-check`. Expected: PASS.

- [ ] **Step 9: Commit** — `git commit -am "Tidy unsafe, char boundaries and shared helpers; state Unix-only"`.

---

### Task 3: `Engine` enum

**Files:**
- Modify: `crates/octet-engine/src/live.rs:41-49,83-96,316-345,350-360,403-408,683-711`
- Modify: `crates/octet-core/src/lib.rs:3,19`, `crates/octet-core/src/model.rs`
- Modify: `crates/octet-tui/src/lib.rs:127-189,494-508`, `crates/octet-tui/src/view.rs:40,149,183-189,205,562,604,678,709-736`
- Modify: `crates/octet/src/main.rs:13,33,47-49,74`
- Modify: tests in `crates/octet-engine/tests/live.rs`, `crates/octet-core/tests/session.rs`, `crates/octet-tui/src/{lib,view}.rs`, `crates/octet/tests/terminal.rs`

**Interfaces:**
- Produces: `octet_engine::live::Engine { Claude, Codex, Demo }` with `ALL`, `parse(&str) -> Option<Engine>`, `as_str(self) -> &'static str`, `is_vendor(self) -> bool`, `Display`; `Config::new(engine: Engine, binary: impl Into<PathBuf>, cwd: impl Into<PathBuf>) -> Config`; `Config.engine: Engine`; `Selection.provider: Engine`; `Selection::parse(input: &str, current: Engine)`; `Mode::describe(self, engine: Engine)`. Re-exported from `octet_core` as `octet_core::Engine`.

- [ ] **Step 1: Write failing tests**

`mode_tests` in `live.rs`:

```rust
#[test]
fn engines_parse_their_own_spelling_only() {
    for engine in Engine::ALL {
        assert_eq!(Engine::parse(engine.as_str()), Some(engine));
        assert_eq!(engine.to_string(), engine.as_str());
    }
    assert_eq!(Engine::parse("Claude"), None);
    assert!(!Engine::Demo.is_vendor());
}
```

Change `every_mode_is_described_for_every_engine` to iterate `Engine::ALL`.

`crates/octet/tests/terminal.rs`, next to `unknown_mode_flag_is_a_startup_error` (copy its process-spawning style):

```rust
#[test]
fn unknown_engine_is_a_startup_error() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_octet"))
        .args(["--engine", "gemini"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("Engine must be codex, claude or demo"));
}
```

Run: `scripts/rust-env.sh cargo test -p octet-engine --lib` — Expected: FAIL (`Engine` not found). The CLI test passes before and after (it pins existing behaviour).

- [ ] **Step 2: Add the type**

```rust
/// The backend a session drives. Journals and the CLI use `as_str()`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Engine {
    Claude,
    Codex,
    /// Offline preview; no vendor process.
    Demo,
}
impl Engine {
    pub const ALL: [Engine; 3] = [Engine::Codex, Engine::Claude, Engine::Demo];
    pub fn parse(value: &str) -> Option<Engine> {
        Self::ALL.into_iter().find(|engine| engine.as_str() == value)
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Engine::Claude => "claude",
            Engine::Codex => "codex",
            Engine::Demo => "demo",
        }
    }
    pub fn is_vendor(self) -> bool {
        self != Engine::Demo
    }
}
impl std::fmt::Display for Engine {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}
impl Config {
    /// Ask mode, the vendor's default model, a new vendor session.
    pub fn new(engine: Engine, binary: impl Into<PathBuf>, cwd: impl Into<PathBuf>) -> Self {
        Self {
            engine,
            binary: binary.into(),
            cwd: cwd.into(),
            model: None,
            resume: None,
            mode: Mode::Ask,
        }
    }
}
```

`Mode::describe(self, engine: Engine)`: same arms with `Engine::Claude`/`Engine::Codex`/`Engine::Demo` instead of string patterns; the catch-all `_` arms become `(Engine::Demo, …)`, so the match is exhaustive.

- [ ] **Step 3: Driver and core**

- `spawn_with_limits`: `if config.engine == Engine::Demo`.
- `vendor`: `let claude = config.engine == Engine::Claude;` (Task 6 replaces this).
- `with_stderr(error, engine: Engine, tail)` and `"Cannot start {}"` use `Display`.
- `octet-core/src/lib.rs`: re-export `Engine`; journal `"engine": config.engine.as_str()`.
- `model.rs`:

```rust
pub struct Selection {
    /// Claude or Codex; `parse` never yields `Demo`.
    pub provider: Engine,
    pub model: Option<String>,
}
impl Selection {
    pub fn parse(input: &str, current: Engine) -> Result<Self, String> {
        let vendor = |name: &str| Engine::parse(name).filter(|engine| engine.is_vendor());
        let parts: Vec<_> = input.split_whitespace().collect();
        let (provider, model) = match parts.as_slice() {
            [] => return Err("Use /model <name>, /model codex <name>, /model claude <name>, or /model <provider> default".into()),
            [name] => match vendor(name) {
                Some(engine) => (engine, "default"),
                None => name
                    .split_once('/')
                    .and_then(|(prefix, model)| Some((vendor(prefix)?, model)))
                    .unwrap_or((current, *name)),
            },
            [name, model] if vendor(name).is_some() => (vendor(name).unwrap_or(current), *model),
            _ => return Err("Use /model <name> or /model <codex|claude> <name>".into()),
        };
        if !provider.is_vendor() {
            return Err("Choose a real provider: /model codex or /model claude. Demo has no model.".into());
        }
        if !octet_engine::live::valid_identifier(model) {
            return Err("Model must be a non-empty vendor model name, at most 256 bytes".into());
        }
        Ok(Self {
            provider,
            model: (model != "default").then(|| model.into()),
        })
    }
}
```

`configure`: `binary: … PathBuf::from(self.provider.as_str())`.

- [ ] **Step 4: TUI and binary**

- `App.engine: octet_core::Engine`; string comparisons become `== Engine::Demo`; `format!` sites keep `{}` (Display).
- `run`: `HashMap<Engine, PathBuf>`, keys copied (`config.engine`), `!config.engine.is_vendor()` replaces `== "demo"`.
- `full_access_notice(mode, engine: Engine, session)`.
- `main.rs`: keep parsing order (validation after the loop, so `--engine x --help` still prints help):

```rust
let engine = octet_core::Engine::parse(&engine).ok_or("Engine must be codex, claude or demo")?;
```

and `binary.unwrap_or_else(|| PathBuf::from(engine.as_str()))`.

- [ ] **Step 5: Tests**

Replace `Config { … }` literals in tests with `Config::new(Engine::…, …)` plus field updates (`Config { model: Some("sonnet".into()), ..Config::new(Engine::Claude, "claude", "/tmp") }`). Replace `"codex"`/`"claude"` provider asserts with `Engine::Codex`/`Engine::Claude`. Mechanical; `cargo build --tests` lists every site.

- [ ] **Step 6: Verify** — `make rust-check`, then `grep -rn '"claude"\|"codex"\|"demo"' crates/*/src | grep -v 'tests\|#\[test\]'` should list only `Engine::as_str`, `Mode::describe` text, the protocol gate (out of scope) and message strings. Expected: PASS.

- [ ] **Step 7: Commit** — `git commit -am "Model the engine as an enum instead of a string"`.

---

### Task 4: Turn outcome and goal types

**Files:**
- Modify: `crates/octet-engine/src/live.rs:222` (`Event::Finished`), `:592,629,666-668,932`
- Modify: `crates/octet-core/src/lib.rs:109`, `crates/octet-core/src/goal.rs`
- Modify: `crates/octet-tui/src/view.rs:350-353,774-781`, `crates/octet-tui/src/lib.rs:234-256,650,677`
- Test: `crates/octet-engine/tests/live.rs` (8 assertions), `crates/octet-core/src/goal.rs` tests, `crates/octet-core/tests/session.rs`

**Interfaces:**
- Produces: `octet_engine::live::Outcome { Completed, Interrupted, Failed, Other(String) }` with `from_vendor(&str) -> Outcome`, `as_str(&self) -> &str`, `Display`; `Event::Finished { outcome: Outcome }`; `octet_core::goal::GoalStep { Begin, Continue, Audit }`; `Goal::prompt(&self, step: GoalStep) -> String`; `Goal::finish_turn(&mut self, outcome: &Outcome, assistant: &str) -> bool`; `Status` gains `Copy`, `as_str()`, `parse_stored(&str) -> Option<Status>` and a `Display` printing `Active`/`Paused`/`Complete` (today's `{:?}` text); `GoalStore::save(&self, goal: &Goal)`, `GoalStore::clear(&self)`. Re-export `Outcome` from `octet_core`.

- [ ] **Step 1: Write failing tests**

`mode_tests` in `live.rs`:

```rust
#[test]
fn unknown_vendor_status_is_kept_verbatim() {
    for known in ["completed", "interrupted", "failed"] {
        assert_eq!(Outcome::from_vendor(known).as_str(), known);
    }
    assert_eq!(Outcome::from_vendor("completed"), Outcome::Completed);
    let other = Outcome::from_vendor("inProgress");
    assert_eq!(other, Outcome::Other("inProgress".into()));
    assert_eq!(other.to_string(), "inProgress");
}
```

`goal.rs` tests:

```rust
#[test]
fn prompts_name_their_step() {
    let goal = Goal::new("Ship the app").unwrap();
    assert!(goal.prompt(GoalStep::Begin).contains("Begin the objective."));
    assert!(goal.prompt(GoalStep::Continue).contains("Continue the objective"));
    assert!(goal.prompt(GoalStep::Audit).contains("Audit the entire objective"));
    assert!(goal.summary().contains("Status: Active"));
}
```

`crates/octet-core/tests/session.rs` (reuse the file's existing session setup; it already reads the journal):

```rust
#[tokio::test]
async fn journal_keeps_engine_and_outcome_spelling() {
    let dir = octet_testkit::TempDir::new("octet-journal-spelling");
    let config = Config::new(Engine::Demo, "demo", dir.path());
    let mut session = Session::open(config, dir.path().to_path_buf()).await.unwrap();
    session.handle.send(Command::Prompt("hi".into())).unwrap();
    while let Some(event) = session.events.recv().await {
        if matches!(event, Event::Finished { .. }) {
            break;
        }
    }
    session.shutdown().await;
    let journal = std::fs::read_to_string(&session.journal).unwrap();
    let records: Vec<serde_json::Value> =
        journal.lines().map(|l| serde_json::from_str(l).unwrap()).collect();
    assert_eq!(records[0]["data"]["engine"], "demo");
    assert!(records.iter().any(|r| r["type"] == "finished" && r["data"] == "completed"));
}
```

Add `octet-testkit` and `serde_json` to octet-core `[dev-dependencies]` if missing.

Run: `scripts/rust-env.sh cargo test -p octet-engine --lib -p octet-core` — Expected: FAIL (types not found).

- [ ] **Step 2: `Outcome`**

```rust
/// How a turn ended. Journals and the status line use `as_str()`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Completed,
    Interrupted,
    Failed,
    /// A status Octet does not map, exactly as the vendor sent it.
    Other(String),
}
impl Outcome {
    pub fn from_vendor(status: &str) -> Outcome {
        match status {
            "completed" => Outcome::Completed,
            "interrupted" => Outcome::Interrupted,
            "failed" => Outcome::Failed,
            other => Outcome::Other(other.to_owned()),
        }
    }
    pub fn as_str(&self) -> &str {
        match self {
            Outcome::Completed => "completed",
            Outcome::Interrupted => "interrupted",
            Outcome::Failed => "failed",
            Outcome::Other(status) => status,
        }
    }
}
impl std::fmt::Display for Outcome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}
```

Producers: Claude result → `if interrupt_pending { Outcome::Interrupted } else if v["is_error"] == true { Outcome::Failed } else { Outcome::Completed }`; Codex start error → `Outcome::Failed`; Codex `turn/completed` → `Outcome::from_vendor(status)` (and `status == "failed"` check stays on the raw string); demo → `Interrupted`/`Completed`. Consumers: journal `json!(outcome.as_str())`; `App::event` `self.status = clean(outcome.as_str())`; TUI `finish_turn(&Outcome::Failed, …)`, `finish_turn(&outcome, …)`. Tests: `assert_eq!(outcome, Outcome::Completed)` etc.

- [ ] **Step 3: Goal types**

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status { Active, Paused, Complete }
impl Status {
    /// The goal file's spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Active => "active",
            Status::Paused => "paused",
            Status::Complete => "complete",
        }
    }
}
impl std::fmt::Display for Status {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Status::Active => "Active",
            Status::Paused => "Paused",
            Status::Complete => "Complete",
        })
    }
}

/// Which instruction a goal prompt carries.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GoalStep {
    Begin,
    Continue,
    /// Re-check the whole objective before claiming completion.
    Audit,
}
```

`prompt(&self, step: GoalStep)` matches on `step` with the three existing sentences. `finish_turn(&mut self, outcome: &Outcome, …)` uses `if *outcome != Outcome::Completed`. `summary()` and `view.rs:776` use `{}` instead of `{:?}`. `GoalStore::load`: `Some("active" | "paused") => Status::Paused` (one arm). Split `save`:

```rust
pub async fn save(&self, goal: &Goal) -> Result<(), String> { /* the existing Some branch, status via goal.status.as_str() */ }
/// Removes the stored goal; a missing file is already cleared.
pub async fn clear(&self) -> Result<(), String> { /* the existing None branch */ }
```

Callers: `App::save_goal` becomes `match (&self.goal_store, &self.goal) { (None, _) => Ok(()), (Some(s), Some(g)) => s.save(g).await, (Some(s), None) => s.clear().await }` (Task 7 moves it). Call sites of `prompt(true,false)` → `GoalStep::Begin`, `prompt(false,false)` → `GoalStep::Continue`, `prompt(false, argument == "complete")` → `if argument == "complete" { GoalStep::Audit } else { GoalStep::Continue }`. Remove `.clone()`s on `Status` that `Copy` makes redundant.

- [ ] **Step 4: Verify** — `make rust-check`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "Type turn outcomes and goal steps"`.

---

### Task 5: Typed driver and send errors

**Files:**
- Modify: `crates/octet-engine/Cargo.toml` (already has `thiserror`), `crates/octet-engine/src/live.rs:227-315,350,409,683-711,778-816,881-898`
- Modify: `crates/octet-tui/src/lib.rs` (`.send(...)` error sites: `app.notice = error` → `error.to_string()`)

**Interfaces:**
- Produces: `pub(crate) enum DriverError { Cancelled, ConsumerOverloaded, TurnItemLimit, Vendor(String) }` with `From<String>` and `From<&str>` (→ `Vendor`); `pub enum SendError { PromptTooLong, Busy }` (`thiserror`, `Display` identical to today); `Handle::send(&self, Command) -> Result<(), SendError>`; `emit`, `send`, `confirm_mode`, `switch_reply_mode`, `demo_set_mode`, `demo` return `Result<_, DriverError>`; `with_stderr(error: DriverError, engine: Engine, tail: &[u8]) -> String`.

- [ ] **Step 1: Rewrite the stderr test over the enum (fails to compile first)**

```rust
#[test]
fn stderr_is_attached_only_to_vendor_failures_and_starts_on_a_line() {
    let vendor = || DriverError::from("Vendor disconnected.");
    assert_eq!(
        with_stderr(vendor(), Engine::Claude, b"reason\n"),
        "Vendor disconnected.\nclaude stderr: reason"
    );
    assert_eq!(with_stderr(vendor(), Engine::Claude, b" \n"), "Vendor disconnected.");
    for (local, text) in [
        (DriverError::Cancelled, "Connection cancelled"),
        (DriverError::ConsumerOverloaded, "Output consumer overloaded; session stopped"),
        (DriverError::TurnItemLimit, "Turn item limit reached; session stopped"),
    ] {
        assert_eq!(with_stderr(local, Engine::Codex, b"unrelated log line\n"), text);
    }
    let mut long = vec![b'a'; 1500];
    long.extend_from_slice("\nlast line é\n".as_bytes());
    let text = with_stderr("x".into(), Engine::Codex, &long);
    assert!(text.ends_with("codex stderr: last line é"), "{text}");
}

#[test]
fn send_errors_keep_their_wording() {
    assert_eq!(SendError::PromptTooLong.to_string(), "Prompt exceeds the 64 KiB limit");
    assert_eq!(SendError::Busy.to_string(), "Session is busy or closed; try again");
}
```

Run: `scripts/rust-env.sh cargo test -p octet-engine --lib` — Expected: FAIL (types missing).

- [ ] **Step 2: Types**

```rust
/// Why the driver stopped. Only `Vendor` failures get the vendor's stderr
/// attached; the others are Octet's own and stderr would mislead.
#[derive(Debug, thiserror::Error)]
pub(crate) enum DriverError {
    #[error("Connection cancelled")]
    Cancelled,
    #[error("Output consumer overloaded; session stopped")]
    ConsumerOverloaded,
    #[error("Turn item limit reached; session stopped")]
    TurnItemLimit,
    #[error("{0}")]
    Vendor(String),
}
impl From<String> for DriverError {
    fn from(message: String) -> Self {
        DriverError::Vendor(message)
    }
}
impl From<&str> for DriverError {
    fn from(message: &str) -> Self {
        DriverError::Vendor(message.to_owned())
    }
}

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    #[error("Prompt exceeds the 64 KiB limit")]
    PromptTooLong,
    #[error("Session is busy or closed; try again")]
    Busy,
}
```

`emit` returns `Err(DriverError::ConsumerOverloaded)` at its three `try_send` sites. `return Err("Connection cancelled".into())` → `DriverError::Cancelled`; `"Turn item limit reached; …"` → `DriverError::TurnItemLimit`. Every other `Err("…".into())`/`format!` stays and converts via `From`. `with_stderr` replaces the prefix list with:

```rust
let DriverError::Vendor(error) = error else {
    return error.to_string();
};
```

`spawn_with_limits` maps the error to `String` for `Event::Error` (`error.to_string()` for demo, `vendor` already returns the `with_stderr` string).

- [ ] **Step 3: Prompt length check without per-variant `matches!`**

Add:

```rust
impl Command {
    /// The longest text a prompt command carries; 0 for other commands.
    fn prompt_bytes(&self) -> usize {
        match self {
            Command::Prompt(text) => text.len(),
            Command::PromptWithDisplay { wire, display } => wire.len().max(display.len()),
            Command::Answer { .. } | Command::SetMode(_) => 0,
        }
    }
}
```

`Handle::send`: `if command.prompt_bytes() > PROMPT_LIMIT { return Err(SendError::PromptTooLong); }` and `.map_err(|_| SendError::Busy)`. (`prompt_parts` and its `unreachable!` are removed in Task 6.)

TUI: sites doing `app.notice = error` / `app.notice(e)` / `format!("Goal paused: {e}")` on a `SendError` use `error.to_string()` or `{e}` (Display).

- [ ] **Step 4: Verify** — `make rust-check`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "Type driver failures instead of matching message prefixes"`.

---

### Task 6: Decompose the vendor driver

**Files:**
- Delete: `crates/octet-engine/src/live.rs`
- Create: `crates/octet-engine/src/live/mod.rs` — public types (`Limits`, `Config`, `Engine`, `Event`, `Outcome`, `Command`, `Handle`, `SendError`, `ModelInfo`), `PROMPT_LIMIT`, `EVENT_BYTES`, `DriverError`, `emit`, `limited`, `valid_identifier`, `model_catalog`, `spawn`, `spawn_with_limits`; `mod mode; mod driver; mod claude; mod codex; mod demo; pub use mode::Mode;`; `model_tests`.
- Create: `crates/octet-engine/src/live/mode.rs` — `Mode` and its impl, `claude_mode`, `claude_permission_args`, `codex_thread_params`, `claude_reported_mode`, `codex_reported`, `codex_turn_overrides`, `confirm_mode`; the mode/mapping tests.
- Create: `crates/octet-engine/src/live/driver.rs` — `Protocol`, `Pending`, `ModeRequest`, `Driver`, `vendor()`, `with_stderr`, `seconds`; the stderr/seconds tests.
- Create: `crates/octet-engine/src/live/claude.rs` — `impl Driver<'_>` Claude methods, `claude_stray_reply`, `claude_result_error`, `switch_reply_mode`, Claude `answer`; their tests.
- Create: `crates/octet-engine/src/live/codex.rs` — `impl Driver<'_>` Codex methods, `codex_stray_reply`, `codex_tool_detail`, `error_text`, Codex `answer`; their tests.
- Create: `crates/octet-engine/src/live/demo.rs` — `demo`, `demo_set_mode` (unchanged bodies, formatted).

Pure functions move **verbatim** (only `use` lines and visibility change: `pub(super)`). The only rewritten code is the `vendor` loop.

**Interfaces:**
- Consumes: Tasks 3–5 types.
- Produces (crate-internal):

```rust
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Protocol { Claude, Codex }

pub(super) struct Pending { pub wire: Value, pub deadline: Instant }

/// A Claude mode switch awaiting its control_response.
pub(super) struct ModeRequest { pub id: String, pub target: Mode, pub deadline: Instant }

/// One vendor connection's state. Fields are grouped by who uses them;
/// `claude.rs` and `codex.rs` add the protocol-specific methods.
pub(super) struct Driver<'a> {
    pub protocol: Protocol,
    pub config: Config,
    pub limits: Limits,
    pub process: Process,
    pub tx: &'a mpsc::Sender<Event>,
    // Connection
    pub initialized: bool,
    pub ready: bool,
    pub session: String,
    pub mode: Mode,
    // Turn
    pub running: bool,
    pub interrupt_pending: bool,
    pub deadline: Instant,
    pub request_id: u64,
    pub pending: HashMap<u64, Pending>,
    // Claude
    pub selected_model: String,
    pub streamed: bool,
    pub mode_request: Option<ModeRequest>,
    /// Timed-out switches Claude may still confirm; the header must never show
    /// a stricter mode than the vendor is really in.
    pub late_modes: Vec<(String, Mode)>,
    pub mode_seq: u64,
    // Codex
    pub turn: Option<String>,
    pub start_request: Option<u64>,
    pub interrupt_request: Option<u64>,
    pub text_items: HashSet<String>,
    pub catalog: Vec<ModelInfo>,
    pub catalog_pages: usize,
    pub catalog_cursors: HashSet<String>,
}
```

- [ ] **Step 1: Move pure code, no logic change**

`git mv crates/octet-engine/src/live.rs crates/octet-engine/src/live/mod.rs`, then cut the items listed under **Files** into their new files verbatim, adding `use super::*`-style imports (prefer explicit `use super::{emit, limited, Event, …};`). Tests move with the functions they test. Run `make rust-check` — Expected: PASS. Commit: `git commit -am "Split the live driver into modules"`.

- [ ] **Step 2: `Driver` and the loop**

`driver.rs`:

```rust
pub(super) async fn vendor(
    config: Config,
    limits: Limits,
    mut commands: mpsc::Receiver<Command>,
    mut cancel: watch::Receiver<u64>,
    mut stopping: watch::Receiver<bool>,
    tx: &mpsc::Sender<Event>,
) -> Result<(), String> {
    let protocol = match config.engine {
        Engine::Claude => Protocol::Claude,
        Engine::Codex => Protocol::Codex,
        Engine::Demo => return Err("The demo engine has no vendor process".into()),
    };
    let engine = config.engine;
    let process = Process::spawn(ProcessConfig {
        executable: config.binary.clone(),
        args: match protocol {
            Protocol::Claude => claude::launch_args(&config),
            Protocol::Codex => vec!["app-server".into()],
        },
        cwd: Some(config.cwd.clone()),
        max_frame_bytes: 8 * 1024 * 1024,
        queue_bytes: 16 * 1024 * 1024,
        stderr_bytes: 4096,
        shutdown_grace: Duration::from_millis(150),
        term_grace: Duration::from_millis(250),
    })
    .await
    .map_err(|e| format!("Cannot start {engine}: {e}. Install the CLI and sign in first."))?;
    let mut driver = Driver::new(protocol, config, limits, process, tx);
    let result = driver.run(&mut commands, &mut cancel, &mut stopping).await;
    let report = driver.process.shutdown().await;
    if !report.reaped || !report.descendants_stopped {
        return Err("Could not verify all vendor children stopped".into());
    }
    result.map_err(|error| with_stderr(error, engine, &report.stderr_tail))
}

impl<'a> Driver<'a> {
    async fn run(
        &mut self,
        commands: &mut mpsc::Receiver<Command>,
        cancel: &mut watch::Receiver<u64>,
        stopping: &mut watch::Receiver<bool>,
    ) -> Result<(), DriverError> {
        match self.protocol {
            Protocol::Claude => self.claude_initialize().await?,
            Protocol::Codex => self.codex_initialize().await?,
        }
        loop {
            let wake = self.next_wake();
            tokio::select! {
                biased;
                _ = stopping.changed() => break,
                changed = cancel.changed() => {
                    if changed.is_err() {
                        break;
                    }
                    self.on_cancel().await?;
                }
                _ = tokio::time::sleep_until(wake), if self.timers_armed() => self.on_timer().await?,
                command = commands.recv() => match command {
                    None => break,
                    Some(command) => self.on_command(command).await?,
                },
                frame = self.process.next_frame() => {
                    let frame = frame
                        .map_err(|e| e.to_string())?
                        .ok_or("Vendor disconnected. Check its login and installation.")?;
                    self.on_frame(frame).await?;
                }
            }
        }
        Ok(())
    }
}
```

`claude::launch_args(&Config) -> Vec<OsString>` is the existing Claude arg list (`live.rs:359-391`). `Driver::new` sets the locals' initial values from `live.rs:412-434` (`request_id: 10`, `deadline: Instant::now() + limits.connect`, `mode: config.mode`, the rest empty/false/zero).

Shared methods in `driver.rs` (bodies are the existing arms, with locals as `self.` fields):

| Method | From |
|---|---|
| `fn next_wake(&self) -> Instant` | line 439 (`mode_request.deadline` instead of `.2`) |
| `fn timers_armed(&self) -> bool` | the `if` guard on line 459 |
| `async fn on_cancel(&mut self)` | lines 444-457; `!ready` → `Err(DriverError::Cancelled)`; the interrupt message via `match self.protocol { Claude => self.claude_interrupt().await?, Codex => self.codex_interrupt().await? }`; the drain via `self.deny_all_pending()` |
| `async fn on_timer(&mut self)` | lines 462-473 |
| `async fn on_command(&mut self, Command)` | lines 476-517; prompts call `self.start_turn(wire, display)`; `SetMode` keeps the shared refusals and dispatches the switch to `claude_request_mode(target)` / sets `self.mode` for Codex |
| `async fn start_turn(&mut self, wire: String, display: String)` | lines 479-491, per-protocol send via `claude_send_prompt(&wire)` / `codex_send_prompt(&wire)` |
| `async fn on_frame(&mut self, v: Value)` | lines 521-524 (watchdog), then `match self.protocol { Claude => self.claude_frame(v).await?, Codex => self.codex_frame(v).await? }`, then line 673 (init-order check) |
| `fn resume_watchdog(&mut self)` | the repeated `if pending.is_empty() && running && !interrupt_pending { deadline = now + turn_idle }` (lines 468, 497, 572) |
| `async fn deny_all_pending(&mut self)` | line 456: drain, send `self.answer(&wire, false)`, emit `ApprovalClosed` |
| `fn close_all_pending(&mut self)` | lines 591, 665: drain, emit `ApprovalClosed` |
| `async fn queue_approval(&mut self, wire: Value, detail: String)` | lines 562-565 / 637-640: oversized → deny + notice; else `request_id += 1`, emit `Approval`, insert `Pending` |
| `fn answer(&self, wire: &Value, allow: bool) -> Value` | line 869, dispatching to `claude::answer` / `codex::answer` |
| `async fn send(&self, value: Value)` | line 310 |

`on_command` matches every variant, so `prompt_parts` and its `unreachable!` are deleted:

```rust
match command {
    Command::Prompt(text) => self.start_turn(text.clone(), text).await,
    Command::PromptWithDisplay { wire, display } => self.start_turn(wire, display).await,
    Command::Answer { id, allow } => self.answer_approval(id, allow).await,
    Command::SetMode(target) => self.set_mode(target).await,
}
```

`demo.rs` does the same inline:

```rust
command = commands.recv() => {
    let (text, display) = match command {
        None => break,
        Some(Command::SetMode(target)) => {
            demo_set_mode(tx, &mut mode, target)?;
            continue;
        }
        Some(Command::Answer { .. }) => continue,
        Some(Command::Prompt(text)) => (text.clone(), text),
        Some(Command::PromptWithDisplay { wire, display }) => (wire, display),
    };
    // existing prompt body, formatted
}
```

Keep line 524's watchdog reset as written (it deliberately ignores `pending`). `claude.rs` gets `claude_initialize`, `claude_interrupt`, `claude_send_prompt`, `claude_request_mode`, `claude_frame` (lines 526-593, early `continue` → `return Ok(())`); `codex.rs` gets `codex_initialize`, `codex_interrupt`, `codex_send_prompt`, `codex_frame` (lines 595-671, same `continue` rule). The pending-capacity rules stay in each protocol (Claude denies silently at 8; Codex sends its stray reply).

`continue` inside a frame arm skipped only the init-order check, which cannot fire after a handled frame (`ready` is set only after `initialized`); running it after every frame is equivalent.

- [ ] **Step 3: Verify** — `make rust-check` (the 38 driver tests in `tests/live.rs` and the PTY suite cover both protocols); `awk 'length>120' crates/octet-engine/src/live/*.rs` prints only string-literal lines. Expected: PASS.

- [ ] **Step 4: Commit** — `git commit -am "Give the vendor driver a state struct and per-protocol handlers"`.

---

### Task 7: Goal runner and TUI session flow

**Files:**
- Modify: `crates/octet-core/src/goal.rs` (add `GoalRunner`, `Next`)
- Modify: `crates/octet-tui/src/view.rs:39-74,108-112,132-140,774-781` (`App.goals`, predicates)
- Modify: `crates/octet-tui/src/lib.rs:107-189,219-289,297-479,603-697` and tests

**Interfaces:**
- Consumes: `Goal`, `GoalStep`, `GoalStore::{save, clear}`, `Outcome` (Task 4).
- Produces:

```rust
/// What the UI does after a goal turn ends.
#[derive(Debug, PartialEq, Eq)]
pub enum Next {
    /// Not a goal turn.
    Idle,
    /// Send this continuation.
    Continue { prompt: String, display: String },
    /// The goal stopped working (complete, paused, guard); show this.
    Stopped(String),
}

#[derive(Default)]
pub struct GoalRunner { pub goal: Option<Goal>, store: Option<GoalStore>, running: bool, output: String }
impl GoalRunner {
    pub fn is_attached(&self) -> bool;
    pub async fn attach(&mut self, store: GoalStore) -> Result<(), String>;
    pub fn is_running(&self) -> bool;
    pub fn is_active(&self) -> bool;
    pub async fn save(&self) -> Result<(), String>;
    pub fn observe_text(&mut self, text: &str);
    pub fn user_prompt_sent(&mut self);
    pub fn goal_prompt_sent(&mut self);
    pub fn reset_turn(&mut self);
    pub fn prompt_display(&self) -> String;
    pub async fn turn_failed(&mut self) -> Result<(), String>;
    pub async fn turn_finished(&mut self, outcome: &Outcome) -> Result<Next, String>;
    pub async fn pause_running_turn(&mut self) -> Result<(), String>;
    pub async fn pause_active(&mut self) -> Result<bool, String>;
    pub async fn send_failed(&mut self) -> Result<(), String>;
    pub async fn start(&mut self, objective: &str) -> Result<String, String>;
    pub async fn resume(&mut self, step: GoalStep) -> Result<String, String>;
    pub async fn pause(&mut self) -> Result<&'static str, String>;
    pub async fn clear(&mut self) -> Result<(), String>;
}
```

`App`: replace `goal`, `goal_store`, `goal_running`, `goal_output` with `pub goals: GoalRunner`; add `pub fn is_connecting(&self) -> bool { !self.ready && !self.stopped }` and `pub fn is_busy(&self) -> bool { self.running || self.is_connecting() }`. TUI: `enum Exit { Quit, New, Reconnect, Model(Selection), Mode(Mode) }`; `Action { Continue, Suspend, SetMode(Mode), GoalPrompt(String), Exit(Exit) }`; `run_session` returns `io::Result<Exit>`.

- [ ] **Step 1: Write failing runner tests (`goal.rs`)**

```rust
fn runner_with_goal() -> GoalRunner {
    let mut runner = GoalRunner::default();
    runner.goal = Some(Goal::new("Ship the app").unwrap());
    runner
}

#[tokio::test]
async fn failed_turn_counts_once_and_the_late_finish_is_ignored() {
    let mut runner = runner_with_goal();
    runner.goal_prompt_sent();
    runner.turn_failed().await.unwrap();
    assert_eq!(runner.turn_finished(&Outcome::Failed).await.unwrap(), Next::Idle);
    let goal = runner.goal.as_ref().unwrap();
    assert_eq!((goal.turns, goal.status), (1, Status::Paused));
}

#[tokio::test]
async fn completed_turn_continues_until_the_marker() {
    let mut runner = runner_with_goal();
    runner.goal_prompt_sent();
    runner.observe_text("Still working");
    let Next::Continue { prompt, display } = runner.turn_finished(&Outcome::Completed).await.unwrap() else {
        panic!("expected a continuation");
    };
    assert!(prompt.contains("Continue the objective"));
    assert_eq!(display, "Goal continuation · turn 2");
    runner.goal_prompt_sent();
    runner.observe_text("Verified tests.\n[[OCTET_GOAL_COMPLETE]]");
    assert!(matches!(runner.turn_finished(&Outcome::Completed).await.unwrap(), Next::Stopped(s) if s.contains("Complete")));
}

#[test]
fn goal_output_keeps_the_last_64_kib_on_a_char_boundary() {
    let mut runner = runner_with_goal();
    runner.goal_prompt_sent();
    runner.observe_text(&"界".repeat(30_000));
    assert!(runner.output.len() <= 64 * 1024);
    assert!(runner.output.starts_with('界'));
}

#[tokio::test]
async fn pause_active_reports_persistence_failure() {
    let dir = octet_testkit::TempDir::new("octet-goal-unwritable");
    // A file where the store expects its directory makes every save fail.
    std::fs::write(dir.path(), b"not a directory").unwrap();
    let mut runner = GoalRunner::default();
    runner.attach(GoalStore::new(dir.path(), Path::new("/project"))).await.unwrap_err();
    runner.goal = Some(Goal::new("Ship the app").unwrap());
    assert!(runner.pause_active().await.is_err());
    assert_eq!(runner.goal.as_ref().unwrap().status, Status::Paused);
}

#[tokio::test]
async fn cancelling_pauses_only_a_running_goal_turn() {
    let mut runner = runner_with_goal();
    runner.pause_running_turn().await.unwrap();
    assert!(runner.is_active());
    runner.goal_prompt_sent();
    runner.pause_running_turn().await.unwrap();
    assert!(!runner.is_active());
}
```

(`attach` on an unreadable path returns `Err` but still attaches the store, so the following save is attempted against it.)

Run: `scripts/rust-env.sh cargo test -p octet-core --lib` — Expected: FAIL (`GoalRunner` not found).

- [ ] **Step 2: Implement `GoalRunner`**

```rust
const OUTPUT_LIMIT: usize = 64 * 1024;

/// A goal, where it is stored, and the vendor turn working on it. Every
/// persistence failure is returned so the UI can say so; none is dropped.
#[derive(Default)]
pub struct GoalRunner {
    pub goal: Option<Goal>,
    store: Option<GoalStore>,
    running: bool,
    output: String,
}

impl GoalRunner {
    pub fn is_attached(&self) -> bool {
        self.store.is_some()
    }
    /// Attaches the store and loads its goal; a stored goal is always paused.
    /// On error the store stays attached and no goal is loaded, so the file is
    /// kept until the user clears or replaces it.
    pub async fn attach(&mut self, store: GoalStore) -> Result<(), String> {
        let loaded = store.load().await;
        self.store = Some(store);
        self.goal = loaded?;
        Ok(())
    }
    pub fn is_running(&self) -> bool {
        self.running
    }
    pub fn is_active(&self) -> bool {
        self.goal.as_ref().is_some_and(|goal| goal.status == Status::Active)
    }
    pub async fn save(&self) -> Result<(), String> {
        match (&self.store, &self.goal) {
            (None, _) => Ok(()),
            (Some(store), Some(goal)) => store.save(goal).await,
            (Some(store), None) => store.clear().await,
        }
    }
    async fn save_or_pause(&mut self) -> Result<(), String> {
        let saved = self.save().await;
        if saved.is_err() {
            if let Some(goal) = &mut self.goal {
                goal.status = Status::Paused;
            }
        }
        saved
    }
    /// Streamed assistant text of a goal turn; the completion marker is read
    /// from the end, so only the last 64 KiB is kept.
    pub fn observe_text(&mut self, text: &str) {
        if !self.running {
            return;
        }
        self.output.push_str(text);
        if self.output.len() > OUTPUT_LIMIT {
            let start = self.output.ceil_char_boundary(self.output.len() - OUTPUT_LIMIT);
            self.output.drain(..start);
        }
    }
    /// The user sent an ordinary prompt; it works on the goal only while active.
    pub fn user_prompt_sent(&mut self) {
        self.running = self.is_active();
        self.output.clear();
    }
    pub fn goal_prompt_sent(&mut self) {
        self.running = true;
        self.output.clear();
    }
    /// A new connection: no turn is running.
    pub fn reset_turn(&mut self) {
        self.running = false;
        self.output.clear();
    }
    pub fn prompt_display(&self) -> String {
        self.goal
            .as_ref()
            .map(|goal| format!("Goal: {}", goal.objective))
            .unwrap_or_else(|| "Goal audit".into())
    }
    /// The vendor reported an error. Adapters send it before the failed
    /// terminal event, so the turn is counted here and the later finish ignored.
    pub async fn turn_failed(&mut self) -> Result<(), String> {
        if !self.running {
            return Ok(());
        }
        self.running = false;
        if let Some(goal) = &mut self.goal {
            goal.finish_turn(&Outcome::Failed, &self.output);
        }
        self.save_or_pause().await
    }
    pub async fn turn_finished(&mut self, outcome: &Outcome) -> Result<Next, String> {
        if !self.running {
            return Ok(Next::Idle);
        }
        self.running = false;
        let Some(goal) = &mut self.goal else {
            return Ok(Next::Idle);
        };
        let more = goal.finish_turn(outcome, &self.output);
        self.save_or_pause().await?;
        let Some(goal) = &self.goal else {
            return Ok(Next::Idle);
        };
        Ok(if more {
            Next::Continue {
                prompt: goal.prompt(GoalStep::Continue),
                display: format!("Goal continuation · turn {}", goal.turns + 1),
            }
        } else {
            Next::Stopped(goal.summary())
        })
    }
    /// Esc/Ctrl+C during a goal turn: the goal must not continue by itself.
    pub async fn pause_running_turn(&mut self) -> Result<(), String> {
        if !self.running {
            return Ok(());
        }
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await
    }
    /// A new connection never continues a goal by itself. True if it paused one.
    pub async fn pause_active(&mut self) -> Result<bool, String> {
        if !self.is_active() {
            return Ok(false);
        }
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await.map(|()| true)
    }
    /// The goal prompt could not be sent.
    pub async fn send_failed(&mut self) -> Result<(), String> {
        if let Some(goal) = &mut self.goal {
            goal.status = Status::Paused;
        }
        self.save().await
    }
    /// `/goal <objective>`: the first prompt to send.
    pub async fn start(&mut self, objective: &str) -> Result<String, String> {
        if self.is_active() {
            return Err("Pause or clear the active goal before replacing it".into());
        }
        let goal = Goal::new(objective)?;
        let prompt = goal.prompt(GoalStep::Begin);
        self.goal = Some(goal);
        if let Err(error) = self.save().await {
            self.goal = None;
            return Err(format!("Goal persistence failed: {error}"));
        }
        Ok(prompt)
    }
    /// `/goal resume` (`Continue`) or `/goal complete` (`Audit`).
    pub async fn resume(&mut self, step: GoalStep) -> Result<String, String> {
        let goal = self.goal.as_mut().ok_or("No goal set")?;
        if goal.status == Status::Complete {
            return Err("Goal is already complete; set a new goal to continue.".into());
        }
        if goal.turns >= MAX_GOAL_TURNS {
            return Err("Goal reached the 200-turn guard. Set a new goal to continue.".into());
        }
        goal.status = Status::Active;
        let prompt = goal.prompt(step);
        self.save_or_pause()
            .await
            .map_err(|error| format!("Goal persistence failed; paused: {error}"))?;
        Ok(prompt)
    }
    /// `/goal pause`: the notice to show.
    pub async fn pause(&mut self) -> Result<&'static str, String> {
        match &mut self.goal {
            None => Ok("No goal set"),
            Some(goal) if goal.status == Status::Complete => {
                Ok("Goal is already complete; set a new goal to continue.")
            }
            Some(goal) => {
                goal.status = Status::Paused;
                self.save().await?;
                Ok("Goal paused. Current vendor turn may finish; no next turn will start.")
            }
        }
    }
    pub async fn clear(&mut self) -> Result<(), String> {
        self.goal = None;
        self.running = false;
        self.save().await
    }
}
```

Add `use crate::Outcome;` (re-exported in Task 4) and `octet-testkit` to octet-core dev-dependencies (done in Task 4).

Run: `scripts/rust-env.sh cargo test -p octet-core --lib` — Expected: PASS.

- [ ] **Step 3: Rewire the TUI**

Event loop (replaces `lib.rs:224-256`):

```rust
if let octet_core::Event::Text(text) = &event {
    app.goals.observe_text(text);
}
let outcome = match &event {
    octet_core::Event::Finished { outcome } => Some(outcome.clone()),
    _ => None,
};
let failed = matches!(event, octet_core::Event::Error(_));
app.event(event);
if failed {
    if let Err(error) = app.goals.turn_failed().await {
        app.notice(format!("Goal persistence failed; paused: {error}"));
    }
}
if let Some(outcome) = outcome {
    match app.goals.turn_finished(&outcome).await {
        Ok(Next::Idle) => {}
        Ok(Next::Stopped(summary)) => app.notice(summary),
        Ok(Next::Continue { prompt, display }) => {
            match session.handle.send(Command::PromptWithDisplay { wire: prompt, display }) {
                Ok(()) => {
                    app.goals.goal_prompt_sent();
                    app.running = true;
                    app.status = "continuing goal".into();
                }
                Err(error) => goal_send_failed(app, error).await,
            }
        }
        Err(error) => app.notice(format!("Goal persistence failed; paused: {error}")),
    }
}
```

Helpers:

```rust
/// Pauses the goal whose prompt could not be sent and says why.
async fn goal_send_failed(app: &mut App, error: octet_core::SendError) {
    app.notice(format!("Goal paused: {error}"));
    if let Err(error) = app.goals.send_failed().await {
        app.notice(format!("Goal persistence failed: {error}"));
    }
}

/// Interrupts the turn; a goal working on it is paused first.
async fn cancel_turn(app: &mut App, session: &Session) {
    if let Err(error) = app.goals.pause_running_turn().await {
        app.notice(format!("Goal persistence failed: {error}"));
    }
    session.handle.interrupt();
}

/// A new connection must never continue an autonomous goal by itself.
async fn pause_active_goal(app: &mut App, why: &str) {
    match app.goals.pause_active().await {
        Ok(true) => app.notice(format!("Goal paused for {why}. Use /goal resume to continue.")),
        Ok(false) => {}
        Err(error) => app.notice(format!("Goal paused for {why}, but saving it failed: {error}")),
    }
}
```

- Approval-dialog Ctrl+C → `cancel_turn(app, session).await`. Main Ctrl+C and Esc → `if app.is_busy() { cancel_turn(app, session).await; app.notice = "Cancelling…".into(); } else { … }`.
- Enter → on `Ok(())`: `app.goals.user_prompt_sent()` replaces the `goal_running`/`goal_output` lines.
- `Action::GoalPrompt(prompt)` → send with `display: app.goals.prompt_display()`; `Ok` → `goal_prompt_sent()`, `running`, status `"working on goal"`; `Err` → `goal_send_failed`.
- Startup (`lib.rs:137-153`):

```rust
if !app.goals.is_attached() {
    match app.goals.attach(goal_store.clone()).await {
        Err(error) => app.notice(format!(
            "Stored goal could not be loaded: {error}. Chat is available. Use /goal clear to remove the saved goal, or /goal <objective> to replace it."
        )),
        Ok(()) if app.goals.goal.as_ref().is_some_and(|g| g.status == Status::Paused) => {
            // Rewrites a stored "active" as "paused".
            if let Err(error) = app.goals.save().await {
                app.notice(format!("Goal persistence failed: {error}"));
            }
            app.notice("Stored goal loaded in paused state. Use /goal resume to continue.");
        }
        Ok(()) => {}
    }
}
```
- `/goal` command (`lib.rs:603-697`) becomes a dispatcher; session checks stay in the TUI, goal rules come from the runner:

```rust
"/goal" => {
    use octet_core::goal::{Goal, GoalStep};
    let idle = !app.running && app.ready && !app.stopped;
    match argument {
        "" | "status" => app.notice(
            app.goals.goal.as_ref().map(Goal::summary).unwrap_or_else(|| "No goal set. Use /goal <objective>.".into()),
        ),
        "pause" => match app.goals.pause().await {
            Ok(notice) => app.notice(notice),
            Err(error) => app.notice(format!("Goal persistence failed: {error}")),
        },
        "clear" => match app.goals.clear().await {
            Ok(()) => app.notice("Goal cleared. Current vendor turn may finish."),
            Err(error) => app.notice(format!("Goal persistence failed: {error}")),
        },
        "resume" | "complete" if !idle => app.notice("Wait for a ready, idle session before resuming or auditing a goal"),
        "resume" | "complete" => {
            let step = if argument == "complete" { GoalStep::Audit } else { GoalStep::Continue };
            match app.goals.resume(step).await {
                Ok(prompt) => return Some(Action::GoalPrompt(prompt)),
                Err(error) => app.notice(error),
            }
        }
        _ if !idle => app.notice("Wait for a ready, idle session before starting a goal"),
        objective => match app.goals.start(objective).await {
            Ok(prompt) => return Some(Action::GoalPrompt(prompt)),
            Err(error) => app.notice(error),
        },
    }
}
```

`/goal clear` previously noticed "Goal cleared…" even when the delete failed; it now shows the failure instead (Global Constraints).
- Replace `!app.ready && !app.stopped` with `app.is_connecting()` and `app.running || !app.ready && !app.stopped` with `app.is_busy()`.
- `run` matches `Exit` exhaustively: `Exit::Quit => break`. `pause_active_goal(...)` no longer returns `io::Result` (drop the `?`). `key_action`/`try_command` return `Action::Exit(Exit::…)` where they returned `Quit`/`New`/`Reconnect`/`Model`/`Mode`; Ctrl+Q and `None` input → `Exit::Quit`. `run_session`'s `other => return Ok(other)` becomes `Action::Exit(exit) => return Ok(exit)`.
- `App::connection` calls `self.goals.reset_turn()`; delete `App::save_goal`.
- `view.rs` sidebar reads `app.goals.goal`.

- [ ] **Step 4: Update TUI tests**

`app.goal = …` → `app.goals.goal = …`; `app.goal_running = true` → `app.goals.goal_prompt_sent()`; `Action::Model(..)`/`Action::Mode(..)` → `Action::Exit(Exit::Model(..))`/`Action::Exit(Exit::Mode(..))`; `pause_active_goal(&mut app, "reconnect").await.unwrap()` → `pause_active_goal(&mut app, "reconnect").await`. `goal.finish_turn("completed", …)` → `goal.finish_turn(&Outcome::Completed, …)`.

- [ ] **Step 5: Verify** — `make rust-check` (PTY goal scenarios for both providers × 4 modes, malformed goal file, pause/cancel counting). Then `grep -n 'let _ = app' crates/octet-tui/src/lib.rs` — Expected: no goal saves listed.

- [ ] **Step 6: Commit** — `git commit -am "Move goal turn bookkeeping into GoalRunner; report every save failure"`.

---

### Task 8: Record the review and the result

**Files:**
- Create: `docs/reviews/2026-10-05-rust-quality-review.md`
- Modify: `docs/reviews/2026-10-05-rust-tui-review.md` (last bullet of "Structure and quality assessment": point to the new review)

- [ ] **Step 1:** Write the review record in the style of `docs/reviews/2026-10-05-rust-tui-review.md`: scope, what changed per task with the reason, the deliberate behaviour change (goal save failures are reported), deferred items with reasons (copy "Out of scope" above), and verification evidence (test counts before/after, `make rust-check`).
- [ ] **Step 2:** `make rust-check`. Expected: PASS.
- [ ] **Step 3: Commit** — `git commit -am "Record the Rust quality review"` (add the new file first).
