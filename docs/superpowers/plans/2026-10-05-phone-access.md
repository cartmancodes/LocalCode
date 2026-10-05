# Phone Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Octet comfortable over tmux + Tailscale SSH + mosh from a phone: a configurable approval window, an approval alert, a prompt box that grows with the draft, and a read-only `/remote-control` setup helper.

**Architecture:** The approval window becomes a `Config` field that flows from the CLI through every reconnect into the engine's `Limits`. The TUI rings a bell and sends an OSC 9 notification when an approval opens, and sizes the composer from the draft's wrapped line count. A new `remote` module in `octet-tui` runs short, timed, read-only checks (tmux, Tailscale, SSH, mosh) and formats a report with the exact phone commands.

**Tech Stack:** Rust 1.98 workspace; `tokio` (process, net, time), `serde_json`, `ratatui`, `crossterm`; PTY tests in `crates/octet/tests/terminal.rs`; fake vendor `octet-testkit`.

**Spec:** `docs/rust/remote-control-ssh.md` (sections "What `/remote-control` could do" and "Phone fixes worth doing next").

## Global Constraints

- `/remote-control` is read-only: it never edits system, tmux or tailnet configuration, starts no listener and stores no secrets.
- Every external check has a 2-second timeout, runs with stdin and stderr closed, and is killed if it overruns.
- Approval timeouts still deny. Default stays 120 seconds; `--approval-timeout` accepts 10–3600 seconds.
- The approval window survives `/model`, `/new`, `/reconnect` and full-access reconnects.
- The composer is 4–7 rows: 2 borders, 1–4 draft rows, 1 hint row.
- The alert is BEL (`\x07`) plus OSC 9 `\x1b]9;Octet: approval needed\x07`, written once per new approval, outside a frame draw; write errors are ignored.
- Copy names things from the user's side ("approval needed", not "OSC 9").
- Run cargo through `scripts/rust-env.sh`. Gate for every task: `scripts/rust-env.sh cargo fmt --all`, then `make rust-check` must pass before the commit.
- Branch `feature/phone-access` from `docs/remote-control-ssh` (the spec lives there). Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A single long line that wraps → the composer grows by wrapped rows, not by newlines (Task 3 test `composer_counts_wrapped_rows`).
2. A pasted 64 KiB draft → the composer stops at 7 rows and the cursor stays visible (Task 3 test `composer_height_is_capped`).
3. A check command that hangs (e.g. `tailscale` waiting on its daemon) → `/remote-control` still answers within about 2 seconds (Task 4 test `run_gives_up_after_the_timeout`).
4. `/model` or `/reconnect` after `--approval-timeout 300` → the new connection keeps 300 s (Task 1 test `configure_carries_approval_timeout`).
5. `--approval-timeout 5` or `abc` → a clear startup error, never a silent default (Task 1 test `invalid_approval_timeout_is_a_startup_error`).

---

### Task 1: Configurable approval window

**Files:**
- Modify: `crates/octet-engine/src/live/mod.rs` (`Config`, `Config::new`, `spawn_with_limits`)
- Modify: `crates/octet-engine/src/live/demo.rs` (use the limit instead of the literal 120 s)
- Modify: `crates/octet-core/src/lib.rs` (`Session::open` passes the limit)
- Modify: `crates/octet-core/src/model.rs` (`Selection::configure` carries it)
- Modify: `crates/octet/src/main.rs` (`--approval-timeout`, help text, `Config` literal)
- Test: `crates/octet-core/tests/session.rs`, `crates/octet-core/src/model.rs` tests, `crates/octet/tests/terminal.rs`

**Interfaces:**
- Produces: `Config.approval_timeout: std::time::Duration` (default `Duration::from_secs(120)` in `Config::new`); `demo(mode, approval: Duration, commands, cancel, stop, tx)`.

- [ ] **Step 1: Write the failing tests**

`crates/octet-core/tests/session.rs`:

```rust
#[tokio::test]
async fn the_session_uses_the_configured_approval_window() {
    let temp = octet_testkit::TempDir::new("octet-approval-window");
    let config = Config {
        approval_timeout: Duration::from_millis(300),
        ..Config::new(Engine::Demo, "demo", temp.path())
    };
    let mut session = Session::open(config, temp.path().to_path_buf()).await.unwrap();
    session.handle.send(Command::Prompt("/approval-demo".into())).unwrap();
    let closed = tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(event) = session.events.recv().await {
            if matches!(event, Event::ApprovalClosed(_)) {
                return true;
            }
        }
        false
    })
    .await
    .unwrap();
    assert!(closed, "the demo approval should expire after 300 ms");
    session.shutdown().await;
}
```

`crates/octet-core/src/model.rs` tests:

```rust
#[test]
fn configure_carries_approval_timeout() {
    let current = Config {
        approval_timeout: std::time::Duration::from_secs(300),
        ..config()
    };
    let next = Selection::parse("claude example", Engine::Codex)
        .unwrap()
        .configure(&current, "thread", None);
    assert_eq!(next.approval_timeout, std::time::Duration::from_secs(300));
}
```

`crates/octet/tests/terminal.rs`:

```rust
#[test]
fn invalid_approval_timeout_is_a_startup_error() {
    for value in ["5", "abc", "3601"] {
        let output = Command::new(env!("CARGO_BIN_EXE_octet"))
            .args(["--engine", "demo", "--approval-timeout", value])
            .output()
            .unwrap();
        assert!(!output.status.success(), "{value}");
        assert!(
            String::from_utf8_lossy(&output.stderr)
                .contains("Approval timeout must be a whole number of seconds from 10 to 3600"),
            "{value}"
        );
    }
}
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-core -p octet --test terminal invalid_approval 2>&1 | grep -E '^error|test result'`
Expected: compile errors (`no field approval_timeout`), and the CLI test fails with "Unknown option --approval-timeout".

- [ ] **Step 3: Implement**

`Config` gains the field and `Config::new` sets `approval_timeout: Duration::from_secs(120)`. In `spawn_with_limits`, pass `limits.approval` to `demo`:

```rust
demo(config.mode, limits.approval, rx, cancel, stopping, &events)
```

In `demo.rs`, thread `approval: Duration` into `DemoTurn` and replace `Duration::from_secs(120)` with `self.approval`. In `Session::open`:

```rust
let limits = octet_engine::live::Limits {
    approval: config.approval_timeout,
    ..Default::default()
};
let (handle, mut engine_events, mut driver) =
    octet_engine::live::spawn_with_limits(config, limits);
```

`Selection::configure` adds `approval_timeout: current.approval_timeout`. In `main.rs`, parse:

```rust
"--approval-timeout" => {
    approval_timeout = value
        .parse::<u64>()
        .ok()
        .filter(|seconds| (10..=3600).contains(seconds))
        .map(Duration::from_secs)
        .ok_or("Approval timeout must be a whole number of seconds from 10 to 3600")?
}
```

Then set it in the `Config` literal (default `Duration::from_secs(120)`). Add `[--approval-timeout SECONDS]` to the usage line of `HELP`.

- [ ] **Step 4: Verify** — `make rust-check`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "Let the approval window be set with --approval-timeout"`.

---

### Task 2: Approval alert

**Files:**
- Modify: `crates/octet-tui/src/lib.rs` (`session_event`, new `ALERT` constant and `alert()`)
- Test: `crates/octet/tests/terminal.rs`

**Interfaces:**
- Produces: `const ALERT: &[u8] = b"\x07\x1b]9;Octet: approval needed\x07";` and `fn alert()`.

- [ ] **Step 1: Write the failing PTY test**

```rust
#[test]
fn a_new_approval_rings_and_notifies() {
    let mut p = Pty::spawn();
    p.wait(|p| p.shows("● ready"));
    p.send(b"/approval-demo\r");
    p.wait(|p| {
        p.output
            .windows(26)
            .any(|w| w == b"\x1b]9;Octet: approval needed")
    });
    p.send(b"d");
    p.quit();
    p.finish();
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `scripts/rust-env.sh cargo test -p octet --test terminal a_new_approval 2>&1 | grep -E 'panicked|test result'`
Expected: FAIL (wait times out).

- [ ] **Step 3: Implement** — in `session_event`, before `app.event(event)`:

```rust
if matches!(event, octet_core::Event::Approval { .. }) {
    alert();
}
```

```rust
/// A bell plus a desktop notification (OSC 9), so tmux, a terminal app or a
/// phone app can flag an approval the user isn't watching. Write errors are
/// ignored: the alert is a courtesy, never a reason to stop.
const ALERT: &[u8] = b"\x07\x1b]9;Octet: approval needed\x07";
fn alert() {
    use std::io::Write;
    let mut stdout = io::stdout();
    let _ = stdout.write_all(ALERT).and_then(|()| stdout.flush());
}
```

- [ ] **Step 4: Verify** — `make rust-check`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "Ring and notify when an approval opens"`.

---

### Task 3: Composer that grows with the draft

**Files:**
- Modify: `crates/octet-tui/src/view.rs` (`draw` layout; new `composer_height`)
- Test: `crates/octet-tui/src/view.rs` tests

**Interfaces:**
- Produces: `fn composer_height(app: &App, terminal_width: u16) -> u16` returning 4–7.

- [ ] **Step 1: Write the failing tests**

```rust
#[test]
fn composer_grows_with_the_draft() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert_eq!(composer_height(&app, 80), 4);
    assert!(app.editor.insert("one\ntwo\nthree"));
    assert_eq!(composer_height(&app, 80), 6);
}
#[test]
fn composer_counts_wrapped_rows() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    // 80 columns leave 74 for text: 100 characters wrap onto a second row.
    assert!(app.editor.insert(&"x".repeat(100)));
    assert_eq!(composer_height(&app, 80), 5);
}
#[test]
fn composer_height_is_capped() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    assert!(app.editor.insert(&"line\n".repeat(40)));
    assert_eq!(composer_height(&app, 80), 7);
}
#[test]
fn an_empty_composer_gives_rows_back_to_the_conversation() {
    let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.event(Event::User("hello".into()));
    let rows = screen(44, 16, &mut app);
    // margin 1 + banner 3, then the conversation card; the composer's top
    // border is 4 rows above the status line.
    assert!(rows[16 - 6].starts_with(" ╭ Prompt"), "{}", rows[16 - 6]);
}
```

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib composer 2>&1 | grep -E '^error|test result'`
Expected: compile error `cannot find function composer_height`.

- [ ] **Step 3: Implement**

```rust
/// The composer's height: borders, the draft's wrapped rows (1–4), and the
/// hint row. An empty prompt takes 4 rows, a long draft at most 7.
fn composer_height(app: &App, terminal_width: u16) -> u16 {
    // Margin 1 + border 1 + padding 1 on each side.
    let text_width = terminal_width.saturating_sub(6) as usize;
    let rows = app.editor.layout(text_width).0.len().clamp(1, 4) as u16;
    2 + rows + 1
}
```

Replace `Constraint::Length(7)` in the vertical layout with `Constraint::Length(composer_height(app, area.width))`.

- [ ] **Step 4: Verify** — `make rust-check`; then update the empty-centre and status tests' row arithmetic if they assumed 7 rows (they slice `rows[4..len-9]`; use `len - 6` for an empty draft). Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "Size the prompt box to the draft"`.

---

### Task 4: `/remote-control` setup helper

**Files:**
- Create: `crates/octet-tui/src/remote.rs`
- Modify: `crates/octet-tui/src/lib.rs` (`mod remote;`, `/remote-control` in `try_command`)
- Modify: `crates/octet-tui/src/view.rs` (`COMMANDS` gains `/remote-control`, help text line)
- Modify: `crates/octet-tui/Cargo.toml` (`serde_json.workspace = true`)
- Test: `crates/octet-tui/src/remote.rs` tests, `crates/octet-tui/src/lib.rs` tests

**Interfaces:**
- Produces:

```rust
pub struct Tailnet { pub name: String, pub address: String }
pub struct Checks {
    pub user: String,
    /// The tmux session Octet runs in; None when not inside tmux.
    pub tmux_session: Option<String>,
    /// Whether tmux passes 24-bit colour; None when unknown.
    pub tmux_rgb: Option<bool>,
    /// None when the tailscale CLI is missing or not connected.
    pub tailnet: Option<Tailnet>,
    /// SSH answered on the tailnet address.
    pub ssh: bool,
    /// mosh-server's version; None when it isn't installed.
    pub mosh: Option<(u32, u32, u32)>,
}
pub async fn probe() -> Checks;
pub fn report(checks: &Checks) -> String;
async fn run(program: &str, args: &[&str]) -> Option<String>;
fn parse_tailnet(status_json: &str) -> Option<Tailnet>;
fn parse_mosh_version(output: &str) -> Option<(u32, u32, u32)>;
```

- [ ] **Step 1: Write the failing tests** (`remote.rs` test module)

```rust
fn ready() -> Checks {
    Checks {
        user: "me".into(),
        tmux_session: Some("octet".into()),
        tmux_rgb: Some(true),
        tailnet: Some(Tailnet { name: "my-mac.tail1234.ts.net".into(), address: "100.101.102.103".into() }),
        ssh: true,
        mosh: Some((1, 4, 0)),
    }
}
#[test]
fn a_ready_host_prints_the_phone_commands() {
    let text = report(&ready());
    assert!(!text.contains("[!!]"), "{text}");
    assert!(text.contains("mosh me@my-mac.tail1234.ts.net -- tmux new -A -s octet"), "{text}");
    assert!(text.contains("Termius"), "{text}");
}
#[test]
fn each_missing_piece_says_how_to_fix_it() {
    let checks = Checks { tmux_session: None, tmux_rgb: None, tailnet: None, ssh: false, mosh: Some((1, 3, 2)), ..ready() };
    let text = report(&checks);
    assert!(text.contains("tmux new -A -s octet"), "{text}");
    assert!(text.contains("tailscale up"), "{text}");
    assert!(text.contains("Remote Login") && text.contains("tailscale set --ssh"), "{text}");
    assert!(text.contains("1.4.0"), "{text}");
    assert!(!text.contains("mosh me@"), "no phone command until the host is reachable: {text}");
}
#[test]
fn tmux_without_rgb_shows_the_config_lines() {
    let text = report(&Checks { tmux_rgb: Some(false), ..ready() });
    assert!(text.contains("terminal-features"), "{text}");
}
#[test]
fn parses_tailscale_status() {
    let json = r#"{"BackendState":"Running","Self":{"DNSName":"my-mac.tail1234.ts.net.","TailscaleIPs":["100.101.102.103","fd7a::1"]}}"#;
    let tailnet = parse_tailnet(json).unwrap();
    assert_eq!(tailnet.name, "my-mac.tail1234.ts.net");
    assert_eq!(tailnet.address, "100.101.102.103");
    assert!(parse_tailnet(r#"{"BackendState":"Stopped","Self":{}}"#).is_none());
    assert!(parse_tailnet("not json").is_none());
}
#[test]
fn parses_mosh_server_version() {
    assert_eq!(parse_mosh_version("mosh-server (mosh 1.4.0) [build mosh-1.4.0]\n"), Some((1, 4, 0)));
    assert_eq!(parse_mosh_version("mosh-server (mosh 1.3.2)"), Some((1, 3, 2)));
    assert_eq!(parse_mosh_version("unexpected"), None);
}
#[tokio::test]
async fn run_gives_up_after_the_timeout() {
    let started = std::time::Instant::now();
    assert_eq!(run("sleep", &["10"]).await, None);
    assert!(started.elapsed() < std::time::Duration::from_secs(4));
    assert_eq!(run("echo", &["hi"]).await.as_deref(), Some("hi\n"));
    assert_eq!(run("octet-no-such-program", &[]).await, None);
}
```

`lib.rs` `model_tests`:

```rust
#[tokio::test]
async fn remote_control_reports_without_changing_the_session() {
    let mut app = app();
    assert!(matches!(command(&mut app, "/remote-control").await, Action::Continue));
    assert!(app.entries_text().contains("Remote control setup"));
    assert_eq!(app.session, "thread-1");
}
```

(`App::entries_text()` is added in Step 3.)

- [ ] **Step 2: Run to verify they fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib remote 2>&1 | grep -E '^error|test result'`
Expected: compile errors (module `remote` missing).

- [ ] **Step 3: Implement `remote.rs`**

```rust
//! `/remote-control`: read-only checks for reaching this session from a
//! phone over tmux, Tailscale SSH and mosh. Nothing here changes system,
//! tmux or tailnet settings. Guide: docs/rust/remote-control-ssh.md.
use std::{process::Stdio, time::Duration};
use tokio::{net::TcpStream, process::Command, time::timeout};

const CHECK_TIMEOUT: Duration = Duration::from_secs(2);
const SESSION: &str = "octet";

/// Runs one read-only check: stdin and stderr closed, killed after the
/// timeout. None when the program is missing, fails or overruns.
async fn run(program: &str, args: &[&str]) -> Option<String> {
    let child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .ok()?;
    let output = timeout(CHECK_TIMEOUT, child.wait_with_output()).await.ok()?.ok()?;
    output.status.success().then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

fn parse_tailnet(status_json: &str) -> Option<Tailnet> {
    let status: serde_json::Value = serde_json::from_str(status_json).ok()?;
    if status["BackendState"] != "Running" {
        return None;
    }
    Some(Tailnet {
        name: status["Self"]["DNSName"].as_str()?.trim_end_matches('.').to_owned(),
        address: status["Self"]["TailscaleIPs"][0].as_str()?.to_owned(),
    })
}

fn parse_mosh_version(output: &str) -> Option<(u32, u32, u32)> {
    let version = output.split("(mosh ").nth(1)?.split(')').next()?;
    let mut parts = version.split('.').map(|part| part.parse().ok());
    Some((parts.next()??, parts.next()??, parts.next().flatten().unwrap_or(0)))
}

pub async fn probe() -> Checks {
    let inside_tmux = std::env::var_os("TMUX").is_some();
    let tmux = async {
        if !inside_tmux {
            return (None, None);
        }
        let session = run("tmux", &["display-message", "-p", "#S"]).await;
        let features = run("tmux", &["display-message", "-p", "#{client_termfeatures}"]).await;
        (
            session.map(|name| name.trim().to_owned()),
            features.map(|list| list.split(',').any(|feature| feature.trim() == "RGB")),
        )
    };
    let tailnet = async {
        // The App Store build keeps its CLI inside the app bundle.
        for program in ["tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"] {
            if let Some(json) = run(program, &["status", "--json"]).await {
                return parse_tailnet(&json);
            }
        }
        None
    };
    let mosh = async { run("mosh-server", &["--version"]).await.as_deref().and_then(parse_mosh_version) };
    let ((tmux_session, tmux_rgb), tailnet, mosh) = tokio::join!(tmux, tailnet, mosh);
    let ssh = match &tailnet {
        Some(tailnet) => matches!(
            timeout(CHECK_TIMEOUT, TcpStream::connect((tailnet.address.as_str(), 22))).await,
            Ok(Ok(_))
        ),
        None => false,
    };
    Checks {
        user: std::env::var("USER").unwrap_or_else(|_| "you".into()),
        tmux_session,
        tmux_rgb,
        tailnet,
        ssh,
        mosh,
    }
}

fn mark(ok: bool, text: &str) -> String {
    format!("{} {text}", if ok { "[ok]" } else { "[!!]" })
}

pub fn report(checks: &Checks) -> String {
    let session = checks.tmux_session.as_deref().unwrap_or(SESSION);
    let mut lines = vec!["Remote control setup (read-only check)".to_owned()];
    lines.push(match &checks.tmux_session {
        Some(name) => mark(true, &format!("Running in tmux session \"{name}\"")),
        None => mark(
            false,
            &format!("Not inside tmux. Quit and restart with: tmux new -A -s {SESSION} \"octet …\""),
        ),
    });
    if checks.tmux_rgb == Some(false) {
        lines.push(mark(
            false,
            "tmux reduces colours. Add to ~/.tmux.conf: set -g default-terminal \"tmux-256color\" and set -as terminal-features \",xterm-256color:RGB\"",
        ));
    }
    lines.push(match &checks.tailnet {
        Some(tailnet) => mark(true, &format!("Tailscale connected: {} ({})", tailnet.name, tailnet.address)),
        None => mark(false, "Tailscale isn't connected. Install it, then run: tailscale up"),
    });
    if checks.tailnet.is_some() {
        lines.push(if checks.ssh {
            mark(true, "SSH answers on the tailnet address")
        } else {
            mark(false, "SSH doesn't answer on the tailnet. Turn on Remote Login (System Settings → General → Sharing) or run: tailscale set --ssh")
        });
    }
    lines.push(match checks.mosh {
        Some((major, minor, patch)) if (major, minor, patch) >= (1, 4, 0) => {
            mark(true, &format!("mosh-server {major}.{minor}.{patch}"))
        }
        Some((major, minor, patch)) => mark(
            false,
            &format!("mosh-server {major}.{minor}.{patch} is too old for 24-bit colour; install 1.4.0 or newer"),
        ),
        None => mark(false, "mosh-server isn't installed: brew install mosh"),
    });
    if let (Some(tailnet), true) = (&checks.tailnet, checks.ssh) {
        lines.push(format!("Phone (Blink):   mosh {}@{} -- tmux new -A -s {session}", checks.user, tailnet.name));
        lines.push(format!(
            "Phone (Termius): host {}, user {}, Mosh on, startup: tmux new -A -s {session}",
            tailnet.name, checks.user
        ));
    }
    lines.push("Guide: docs/rust/remote-control-ssh.md".into());
    lines.join("\n")
}
```

In `view.rs`, add the test helper used above:

```rust
#[cfg(test)]
pub fn entries_text(&self) -> String {
    self.entries.iter().map(|entry| entry.text.as_str()).collect::<Vec<_>>().join("\n")
}
```

In `try_command`:

```rust
"/remote-control" => match argument {
    "" | "status" => app.notice(remote::report(&remote::probe().await)),
    _ => app.notice("Use /remote-control or /remote-control status"),
},
```

Add `("/remote-control", "Check phone access over tmux, Tailscale and mosh")` to `COMMANDS` (array length 10) and a help line `/remote-control: check phone access setup`. Add `serde_json.workspace = true` to `crates/octet-tui/Cargo.toml` `[dependencies]`.

- [ ] **Step 4: Verify** — `make rust-check`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "Add /remote-control: read-only phone access checks"` (add `remote.rs` first).

---

### Task 5: Documentation

**Files:**
- Modify: `docs/tui.md` (commands list, `--approval-timeout`, composer, approval alert)
- Modify: `docs/rust/remote-control-ssh.md` (status: helper and fixes implemented; how to use `/remote-control`; tmux bell settings)
- Modify: `README.md` (commands line)

- [ ] **Step 1:** In `docs/tui.md`, add `/remote-control` to the commands line; add a "Phone access" paragraph linking the guide; document `--approval-timeout SECONDS` (10–3600, default 120); note the prompt box grows from 4 to 7 rows and that a new approval rings the terminal bell and sends a notification.
- [ ] **Step 2:** In `docs/rust/remote-control-ssh.md`, change the status line to "the `/remote-control` helper and the three phone fixes are implemented; device testing pending", replace "could do" with what the command does, and add to tmux setup: `set -g monitor-bell on` and `set -g bell-action any` so a bell in a background window is flagged.
- [ ] **Step 3:** `make rust-check`. Expected: PASS.
- [ ] **Step 4: Commit** — `git commit -am "Document phone access, the approval window and alerts"`.
