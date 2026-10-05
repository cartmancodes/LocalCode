# Composer Power-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/copy` and Ctrl+X (OSC 52), `!cmd`/`!!cmd` with attachments, `@` file mentions, Tab completion and Ctrl+G external editing to Octet's prompt box.

**Architecture:** Four small I/O modules in `crates/octet-tui/src` (`clipboard.rs`, `shell.rs`, `files.rs`, `external.rs`) plus one pure-logic module (`composer.rs`) for completion and attachment text. `lib.rs` routes keys and owns the background tasks (shell command, file index, editor child), whose results arrive through `run_session`'s `select!`, as `/remote-control` does. `view.rs` draws the completion popup, the attachment chip and the new `SHELL` entries.

**Tech Stack:** Rust 1.98.1, tokio (full), ratatui 0.30, crossterm 0.29, libc. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-composer-power-ups-design.md`

## Global Constraints

- No new crate dependencies; base64 is written locally.
- Prompt limit: `octet_core::PROMPT_LIMIT` (64 KiB) for every draft, edited draft and final wire text.
- Shell output: at most 32 KiB kept, from the end, marked `[earlier output cut]`; 10-minute limit; stdin closed; own process group.
- File index: at most 50,000 paths; popup shows at most 8.
- Clipboard: at most 100 KB of text per copy.
- Nothing blocks the event loop; vendor events keep draining during every new feature.
- Approvals are unchanged; `!` needs none.
- Every step that changes behaviour starts with a failing test. Run tests with `scripts/rust-env.sh cargo test …`; the gate is `make rust-check` (fmt check, all tests, clippy `-D warnings`).
- Commit messages: imperative subject, body says why, end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Match the surrounding style: short `///` doc comments that say why, no new comment density beyond the files' existing level.

## Review Focus

- A `!` command printing colour codes or carriage returns (`ls -G`, progress bars) must not put escape sequences on the screen or into the vendor prompt — Task 3 cleans output and tests it.
- A `!` command that reads stdin (`cat`, `read x`) must finish at once instead of hanging — Task 2 tests `cat`.
- An `@` inside a word (`me@example.com`) must not open the file popup — Task 5 tests it.
- A multi-byte draft before an `@` or Tab word (`héllo @ma`) must not panic on byte offsets — Task 5 tests it.
- `EDITOR` naming a missing program must leave the draft and the screen intact — Task 6 tests it end to end.

---

### Task 1: Copy the last reply (`/copy`, Ctrl+X)

**Files:**
- Create: `crates/octet-tui/src/clipboard.rs`
- Modify: `crates/octet-tui/src/lib.rs` (module list; `alert()` → `write_terminal`; `copy_reply`; Ctrl+X; `/copy`)
- Modify: `crates/octet-tui/src/view.rs` (`App::last_reply`; `COMMANDS`; help text)
- Test: unit tests in `clipboard.rs` and `lib.rs` `model_tests`; `crates/octet/tests/terminal.rs`

**Interfaces:**
- Produces: `clipboard::LIMIT: usize`, `clipboard::osc52(text: &str) -> Option<(Vec<u8>, bool)>`, `App::last_reply(&self) -> Option<&str>`, `lib::write_terminal(bytes: &[u8])`, `lib::size_label(bytes: usize) -> String`.

- [ ] **Step 1: Write the failing clipboard tests**

Create `crates/octet-tui/src/clipboard.rs` with only the tests and empty stubs:

```rust
//! `/copy`: text to the clipboard through OSC 52, which terminals, tmux
//! (`set -g set-clipboard on`) and mosh 1.4+ pass on, even to a phone.

/// The most text sent in one copy.
pub const LIMIT: usize = 100 * 1024;

/// The escape that sets the clipboard, and whether `text` was cut to fit.
/// None for empty text.
pub fn osc52(_text: &str) -> Option<(Vec<u8>, bool)> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn encodes_known_strings() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(osc52("hi"), Some((b"\x1b]52;c;aGk=\x07".to_vec(), false)));
        assert_eq!(osc52(""), None);
    }
    #[test]
    fn cuts_long_text_on_a_character_boundary() {
        let text = "é".repeat(LIMIT);
        let (bytes, cut) = osc52(&text).unwrap();
        assert!(cut);
        let encoded = &bytes[7..bytes.len() - 1];
        assert!(encoded.len() <= LIMIT.div_ceil(3) * 4);
        assert_eq!(encoded.len() % 4, 0);
    }
}
```

Add `mod clipboard;` to the module list at the top of `crates/octet-tui/src/lib.rs` (after `mod editor;`), and add `fn base64(_data: &[u8]) -> String { String::new() }` below `osc52` so the tests compile.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib clipboard`
Expected: FAIL — `encodes_known_strings` (left `""` vs right `"Zg=="`) and `cuts_long_text_on_a_character_boundary` (`unwrap` on `None`).

- [ ] **Step 3: Implement**

Replace the two stubs:

```rust
pub fn osc52(text: &str) -> Option<(Vec<u8>, bool)> {
    if text.is_empty() {
        return None;
    }
    let end = text.floor_char_boundary(LIMIT.min(text.len()));
    let mut bytes = b"\x1b]52;c;".to_vec();
    bytes.extend_from_slice(base64(&text.as_bytes()[..end]).as_bytes());
    bytes.push(0x07);
    Some((bytes, end < text.len()))
}

fn base64(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let padded = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = u32::from(padded[0]) << 16 | u32::from(padded[1]) << 8 | u32::from(padded[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(TABLE[(n >> (18 - 6 * i)) as usize & 63] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}
```

- [ ] **Step 4: Run the clipboard tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib clipboard`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the failing `/copy` tests**

In `crates/octet-tui/src/lib.rs`, inside `mod model_tests`, add:

```rust
    #[tokio::test]
    async fn copy_reports_the_size_of_the_last_reply() {
        let mut app = app();
        assert!(matches!(command(&mut app, "/copy").await, Action::Continue));
        assert_eq!(app.notice, "Nothing to copy yet");
        app.event(octet_core::Event::Started);
        app.event(octet_core::Event::Text("hello".into()));
        app.event(octet_core::Event::Finished {
            outcome: octet_core::Outcome::Completed,
        });
        assert_eq!(app.last_reply(), Some("hello"));
        assert!(matches!(command(&mut app, "/copy").await, Action::Continue));
        assert_eq!(app.notice, "Copied 5 B to the clipboard");
    }
    #[test]
    fn sizes_read_naturally() {
        assert_eq!(size_label(5), "5 B");
        assert_eq!(size_label(1229), "1.2 KB");
        assert_eq!(size_label(100 * 1024), "100.0 KB");
    }
```

- [ ] **Step 6: Run them and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib copy_reports sizes_read`
Expected: compile errors for `last_reply` and `size_label`. Add stubs so they compile and fail on behaviour: in `view.rs` `impl App` add `pub fn last_reply(&self) -> Option<&str> { None }`; in `lib.rs` add `fn size_label(_bytes: usize) -> String { String::new() }`. Re-run: FAIL on the assertions.

- [ ] **Step 7: Implement `/copy` and Ctrl+X**

In `view.rs`, replace the `last_reply` stub:

```rust
    /// The most recent reply, as shown, for `/copy`.
    pub fn last_reply(&self) -> Option<&str> {
        self.entries
            .iter()
            .rev()
            .find(|entry| entry.role == Role::Assistant)
            .map(|entry| entry.text.as_str())
    }
```

In `lib.rs`, replace `alert()`'s body so it shares a writer, and add `copy_reply` and `size_label` next to it:

```rust
fn alert() {
    write_terminal(ALERT);
}
/// Writes control bytes straight to the terminal, outside a frame. Errors are
/// ignored: these are courtesies, never a reason to stop.
fn write_terminal(bytes: &[u8]) {
    use std::io::Write;
    let mut stdout = io::stdout();
    let _ = stdout.write_all(bytes).and_then(|()| stdout.flush());
}
/// `/copy` and Ctrl+X: the last reply to the clipboard.
fn copy_reply(app: &mut App) {
    let Some((bytes, cut, size)) = app.last_reply().and_then(|text| {
        clipboard::osc52(text).map(|(bytes, cut)| (bytes, cut, text.len().min(clipboard::LIMIT)))
    }) else {
        app.notice = "Nothing to copy yet".into();
        return;
    };
    write_terminal(&bytes);
    app.notice = if cut {
        "Copied the first 100 KB to the clipboard".into()
    } else {
        format!("Copied {} to the clipboard", size_label(size))
    };
}
fn size_label(bytes: usize) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    }
}
```

In `key_action`'s main `match key.code`, next to the Ctrl+P arm, add:

```rust
        KeyCode::Char('x') if ctrl => copy_reply(app),
```

In `try_command`, next to `"/help"`, add:

```rust
        "/copy" => copy_reply(app),
```

In `view.rs`, change `COMMANDS` to 11 entries, adding after `/export`:

```rust
    ("/copy", "Copy the last reply (also Ctrl+X)"),
```

In `help()`'s text, after the line `/new · /reconnect · /session · /export [path]\n`, insert:

```text
/copy or Ctrl+X: copy the last reply to the clipboard\n
```

Tasks 3, 5 and 6 add three more help lines, which would overflow the box, so change `help()`'s `modal(area, 76, 24)` to `modal(area, 76, 28)` now.

- [ ] **Step 8: Run the TUI tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib`
Expected: PASS, including the existing palette test that checks every command fits.

- [ ] **Step 9: Write the end-to-end test**

Append to `crates/octet/tests/terminal.rs`:

```rust
#[test]
fn copy_sends_the_last_reply_to_the_clipboard() {
    let mut p = Pty::spawn();
    p.wait(|p| p.shows("● ready"));
    p.send(b"/copy\r");
    p.wait(|p| p.screen_shows("Nothing to copy yet"));
    p.send(b"hello\r");
    p.wait(|p| p.screen_shows("Try /approval-demo"));
    p.wait(|p| p.shows("● completed"));
    p.send(b"\x18");
    p.wait(|p| {
        p.output
            .windows(8)
            .any(|w| w == b"\x1b]52;c;V")
    });
    assert!(p.screen_shows("to the clipboard"));
    p.quit();
    p.finish();
}
```

(`V` is the first base64 character of the demo reply, which begins "This"; `b"T"` = 0x54 → `VG…`.)

Add `"/copy"` to the list in the existing `help_lists_every_command` test, and add `/copy` to `HELP` in `crates/octet/src/main.rs` on the commands line: `Commands: /model, /mode, /goal, /session, /new, /reconnect, /export,\n          /copy, /remote-control (check phone access)\n`.

- [ ] **Step 10: Run the terminal tests**

Run: `scripts/rust-env.sh cargo test -p octet --test terminal -- copy_sends help_lists`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add crates/octet-tui/src/clipboard.rs crates/octet-tui/src/lib.rs crates/octet-tui/src/view.rs crates/octet/src/main.rs crates/octet/tests/terminal.rs
git commit -F - <<'EOF'
Copy the last reply with /copy or Ctrl+X

OSC 52 asks the terminal to set its clipboard, which tmux (with
set-clipboard on) and mosh 1.4+ pass on, so a reply can reach a phone's
clipboard. Text is capped at 100 KB.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: Shell runner (`shell.rs`)

**Files:**
- Create: `crates/octet-tui/src/shell.rs`
- Modify: `crates/octet-tui/src/lib.rs` (module list only)
- Test: unit tests in `shell.rs`

**Interfaces:**
- Produces:
  - `shell::OUTPUT_LIMIT: usize` (32 KiB), `shell::CUT: &str` (`"[earlier output cut]\n"`)
  - `enum shell::Status { Exited(i32), Signalled, TimedOut, Cancelled }` (`Debug, Clone, PartialEq`)
  - `struct shell::Ran { pub command: String, pub status: Status, pub output: String }` (`Debug, Clone`), with `fn ok(&self) -> bool` and `fn summary(&self) -> String`
  - `async fn shell::run(command: &str, cwd: &Path, cancel: tokio::sync::oneshot::Receiver<()>) -> Result<Ran, String>`

- [ ] **Step 1: Write the failing tests**

Create `crates/octet-tui/src/shell.rs`:

```rust
//! `!` commands: run one command the user typed, in the workspace, bounded in
//! output and time. It is the user's own action, so no approval applies.
use std::{io::Read, path::Path, process::Stdio, time::Duration};
use tokio::{process::Command, sync::oneshot};

/// Output kept per command, from the end.
pub const OUTPUT_LIMIT: usize = 32 * 1024;
/// How long a command may run.
const TIME_LIMIT: Duration = Duration::from_secs(600);
/// Starts output that lost its beginning to the limit.
pub const CUT: &str = "[earlier output cut]\n";

#[derive(Debug, Clone, PartialEq)]
pub enum Status {
    Exited(i32),
    Signalled,
    TimedOut,
    Cancelled,
}

#[derive(Debug, Clone)]
pub struct Ran {
    pub command: String,
    pub status: Status,
    pub output: String,
}

impl Ran {
    pub fn ok(&self) -> bool {
        self.status == Status::Exited(0)
    }
    /// How it ended, for the transcript and the attachment header.
    pub fn summary(&self) -> String {
        String::new()
    }
}

/// Runs `command` with the user's shell (`$SHELL`, else `sh`).
pub async fn run(command: &str, cwd: &Path, cancel: oneshot::Receiver<()>) -> Result<Ran, String> {
    let shell = std::env::var("SHELL")
        .ok()
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(|| "sh".into());
    run_with(&shell, command, cwd, cancel, TIME_LIMIT).await
}

async fn run_with(
    _shell: &str,
    _command: &str,
    _cwd: &Path,
    _cancel: oneshot::Receiver<()>,
    _limit: Duration,
) -> Result<Ran, String> {
    Err("not implemented".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    async fn sh(command: &str) -> Ran {
        let (_keep, cancel) = oneshot::channel();
        run_with("sh", command, Path::new("."), cancel, TIME_LIMIT)
            .await
            .unwrap()
    }
    #[tokio::test]
    async fn reports_exit_code_and_merged_output() {
        let ran = sh("echo out; echo err >&2; exit 3").await;
        assert_eq!(ran.status, Status::Exited(3));
        assert!(ran.output.contains("out\n") && ran.output.contains("err\n"), "{}", ran.output);
        assert!(!ran.ok());
        assert_eq!(ran.summary(), "exit 3");
    }
    #[tokio::test]
    async fn keeps_the_end_of_long_output() {
        let ran = sh("i=0; while [ $i -lt 6000 ]; do echo line$i; i=$((i+1)); done").await;
        assert!(ran.output.starts_with(CUT), "{}", &ran.output[..40]);
        assert!(ran.output.len() <= OUTPUT_LIMIT + CUT.len());
        assert!(ran.output.ends_with("line5999\n"));
    }
    #[tokio::test]
    async fn commands_reading_stdin_finish_at_once() {
        let started = std::time::Instant::now();
        assert_eq!(sh("cat").await.status, Status::Exited(0));
        assert!(started.elapsed() < Duration::from_secs(5));
    }
    #[tokio::test]
    async fn background_children_do_not_hold_it_open() {
        let started = std::time::Instant::now();
        let ran = sh("sleep 30 & echo done").await;
        assert_eq!(ran.output, "done\n");
        assert!(started.elapsed() < Duration::from_secs(5));
    }
    #[tokio::test]
    async fn cancel_stops_the_whole_pipeline() {
        let (cancel, cancelled) = oneshot::channel();
        let started = std::time::Instant::now();
        let task = tokio::spawn(async move {
            run_with("sh", "sleep 30 | cat", Path::new("."), cancelled, TIME_LIMIT).await
        });
        tokio::time::sleep(Duration::from_millis(200)).await;
        cancel.send(()).unwrap();
        let ran = task.await.unwrap().unwrap();
        assert_eq!(ran.status, Status::Cancelled);
        assert_eq!(ran.summary(), "cancelled");
        assert!(started.elapsed() < Duration::from_secs(5));
    }
    #[tokio::test]
    async fn times_out() {
        let (_keep, cancel) = oneshot::channel();
        let ran = run_with("sh", "sleep 30", Path::new("."), cancel, Duration::from_millis(300))
            .await
            .unwrap();
        assert_eq!(ran.status, Status::TimedOut);
        assert_eq!(ran.summary(), "timed out after 10 minutes");
    }
    #[tokio::test]
    async fn a_missing_shell_is_an_error() {
        let (_keep, cancel) = oneshot::channel();
        let error = run_with("/no/such/shell", "true", Path::new("."), cancel, TIME_LIMIT)
            .await
            .unwrap_err();
        assert!(error.contains("/no/such/shell"), "{error}");
    }
}
```

Add `mod shell;` to `lib.rs`'s module list.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib shell`
Expected: FAIL — every test panics on `unwrap()` of `Err("not implemented")`, and `a_missing_shell_is_an_error` fails because the message lacks the path.

- [ ] **Step 3: Implement**

Replace `summary` and `run_with`, and add the helpers:

```rust
    pub fn summary(&self) -> String {
        match self.status {
            Status::Exited(code) => format!("exit {code}"),
            Status::Signalled => "killed by a signal".into(),
            Status::TimedOut => "timed out after 10 minutes".into(),
            Status::Cancelled => "cancelled".into(),
        }
    }
```

```rust
async fn run_with(
    shell: &str,
    command: &str,
    cwd: &Path,
    mut cancel: oneshot::Receiver<()>,
    limit: Duration,
) -> Result<Ran, String> {
    let fail = |e: std::io::Error| format!("Cannot run {shell}: {e}");
    // One pipe for stdout and stderr keeps their order.
    let (reader, writer) = std::io::pipe().map_err(fail)?;
    let mut child = {
        let mut cmd = Command::new(shell);
        cmd.arg("-c")
            .arg(command)
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(writer.try_clone().map_err(fail)?)
            .stderr(writer)
            .process_group(0)
            .kill_on_drop(true);
        cmd.spawn().map_err(fail)?
    };
    let group = child.id();
    let output = tokio::task::spawn_blocking(move || read_tail(reader, OUTPUT_LIMIT));
    let status = tokio::select! {
        status = child.wait() => match status.map_err(fail)?.code() {
            Some(code) => Status::Exited(code),
            None => Status::Signalled,
        },
        _ = tokio::time::sleep(limit) => Status::TimedOut,
        _ = &mut cancel => Status::Cancelled,
    };
    // Children left in the group would hold the pipe open.
    kill_group(group);
    let _ = child.wait().await;
    let (bytes, cut) = output.await.map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&bytes);
    let output = if cut { format!("{CUT}{text}") } else { text.into_owned() };
    Ok(Ran {
        command: command.to_owned(),
        status,
        output,
    })
}

fn kill_group(group: Option<u32>) {
    if let Some(pid) = group.and_then(|pid| libc::pid_t::try_from(pid).ok()) {
        // SAFETY: signals only the process group this command was started in.
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
    }
}

/// Reads to the end, keeping the last `limit` bytes; true when some were cut.
fn read_tail(mut reader: impl Read, limit: usize) -> (Vec<u8>, bool) {
    let mut kept = Vec::new();
    let mut cut = false;
    let mut chunk = [0; 8192];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                kept.extend_from_slice(&chunk[..n]);
                if kept.len() > limit * 2 {
                    kept.drain(..kept.len() - limit);
                    cut = true;
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(_) => break,
        }
    }
    if kept.len() > limit {
        kept.drain(..kept.len() - limit);
        cut = true;
    }
    (kept, cut)
}
```

Note: `Command::process_group` is on tokio's `Command` (unix). The `Command` value is dropped at the end of the block, which closes the parent's copies of the pipe's write end, so the reader sees end-of-file once the group is gone.

- [ ] **Step 4: Run the tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib shell`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add crates/octet-tui/src/shell.rs crates/octet-tui/src/lib.rs
git commit -F - <<'EOF'
Add a bounded runner for ! commands

Runs one command with the user's shell in its own process group, stdin
closed and stderr merged, keeping the last 32 KiB of output and stopping
after 10 minutes or on cancel. Killing the group also frees background
children that would hold the output pipe open.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

(`#[allow(dead_code)]` is not needed: clippy runs on `--all-targets`, and the tests use every item. If clippy reports `run` unused in the non-test build, add `#[cfg_attr(not(test), allow(dead_code))]` to it until Task 3 uses it, and remove that attribute in Task 3.)

---

### Task 3: `!` and `!!` with attachments

**Files:**
- Create: `crates/octet-tui/src/composer.rs`
- Modify: `crates/octet-tui/src/view.rs` (`Role::Shell`, `Role::ShellFailed`, `App::attachments`, `App::shell_running`, `App::shell_output`, `App::attach`, composer title chip, help text)
- Modify: `crates/octet-tui/src/lib.rs` (`Action::RunShell`, `Action::CancelShell`, `ShellTask`, Enter/Esc/Ctrl+C routing, sending with attachments, `select!` branch)
- Test: unit tests in `composer.rs`, `view.rs`, `lib.rs`; `crates/octet/tests/terminal.rs`

**Interfaces:**
- Consumes: `shell::{run, Ran, Status, OUTPUT_LIMIT, CUT}` from Task 2.
- Produces:
  - `composer::chip(attachments: &[shell::Ran]) -> Option<String>`
  - `composer::with_attachments(draft: &str, attachments: &[shell::Ran], limit: usize) -> (String, String)` — `(wire, display)`
  - `App::attachments: Vec<shell::Ran>`, `App::shell_running: bool`, `App::shell_output(&mut self, ran: &shell::Ran)`, `App::attach(&mut self, ran: shell::Ran)`
  - `Action::RunShell { command: String, attach: bool }`, `Action::CancelShell`

- [ ] **Step 1: Write the failing composer tests**

Create `crates/octet-tui/src/composer.rs`:

```rust
//! The prompt box's attachment and completion logic, free of terminal I/O.
use crate::shell::{Ran, CUT};

/// The prompt box title's note of waiting attachments.
pub fn chip(_attachments: &[Ran]) -> Option<String> {
    None
}

/// The text sent to the vendor and the text shown, for a draft with `!`
/// outputs attached. Outputs lose their beginnings until the wire fits.
pub fn with_attachments(draft: &str, _attachments: &[Ran], _limit: usize) -> (String, String) {
    (draft.to_owned(), draft.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shell::Status;
    fn ran(command: &str, output: &str, code: i32) -> Ran {
        Ran {
            command: command.into(),
            status: Status::Exited(code),
            output: output.into(),
        }
    }
    #[test]
    fn the_chip_names_one_attachment_or_counts_several() {
        assert_eq!(chip(&[]), None);
        assert_eq!(chip(&[ran("git status", "", 0)]).as_deref(), Some("+ git status (exit 0)"));
        assert_eq!(chip(&[ran("a", "", 0), ran("b", "", 1)]).as_deref(), Some("+2 attached"));
    }
    #[test]
    fn the_vendor_gets_the_output_and_the_transcript_a_marker() {
        let (wire, display) = with_attachments("explain", &[ran("git status", "clean\n", 0)], 1 << 16);
        assert_eq!(
            wire,
            "explain\n\nOutput of `git status` (exit 0):\n```\nclean\n```"
        );
        assert_eq!(display, "explain\n\n[+ git status]");
    }
    #[test]
    fn the_fence_outgrows_backticks_in_the_output() {
        let (wire, _) = with_attachments("x", &[ran("cat a.md", "```rust\nfn main() {}\n```\n", 0)], 1 << 16);
        assert!(wire.contains("\n````\n```rust"), "{wire}");
        assert!(wire.ends_with("```\n````"), "{wire}");
    }
    #[test]
    fn outputs_are_cut_from_the_start_to_fit_the_limit() {
        let output = "x".repeat(5000) + "END\n";
        let (wire, _) = with_attachments("q", &[ran("big", &output, 0)], 1000);
        assert!(wire.len() <= 1000, "{}", wire.len());
        assert!(wire.contains(CUT.trim_end()));
        assert!(wire.contains("END"));
    }
}
```

Add `mod composer;` to `lib.rs`'s module list.

- [ ] **Step 2: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib composer`
Expected: FAIL on all four assertions.

- [ ] **Step 3: Implement the composer functions**

```rust
pub fn chip(attachments: &[Ran]) -> Option<String> {
    match attachments {
        [] => None,
        [one] => Some(format!("+ {} ({})", one.command, one.summary())),
        many => Some(format!("+{} attached", many.len())),
    }
}

pub fn with_attachments(draft: &str, attachments: &[Ran], limit: usize) -> (String, String) {
    let mut outputs: Vec<String> = attachments.iter().map(|a| a.output.clone()).collect();
    let build = |outputs: &[String]| {
        let mut wire = draft.to_owned();
        for (attachment, output) in attachments.iter().zip(outputs) {
            let fence = fence_for(output);
            let body = output.strip_suffix('\n').unwrap_or(output);
            wire.push_str(&format!(
                "\n\nOutput of `{}` ({}):\n{fence}\n{body}\n{fence}",
                attachment.command,
                attachment.summary()
            ));
        }
        wire
    };
    let mut wire = build(&outputs);
    while wire.len() > limit {
        let over = wire.len() - limit;
        let Some(output) = outputs.iter_mut().find(|o| o.len() > CUT.len()) else {
            break;
        };
        let body = output.strip_prefix(CUT).unwrap_or(output);
        let drop = body.ceil_char_boundary((over + CUT.len()).min(body.len()));
        *output = format!("{CUT}{}", &body[drop..]);
        wire = build(&outputs);
    }
    let display = attachments.iter().fold(draft.to_owned(), |text, a| {
        format!("{text}\n\n[+ {}]", a.command)
    });
    (wire, display)
}

/// Three backticks, or one more than the longest run in `text`.
fn fence_for(text: &str) -> String {
    let longest = text
        .split(|c| c != '`')
        .map(str::len)
        .max()
        .unwrap_or(0);
    "`".repeat(longest.max(2) + 1)
}
```

The display joins markers with blank lines: for two attachments it is `draft\n\n[+ a]\n\n[+ b]`.

- [ ] **Step 4: Run the composer tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib composer`
Expected: PASS (4 tests).

- [ ] **Step 5: Write the failing App tests**

In `view.rs`'s `mod tests`, add:

```rust
    #[test]
    fn shell_output_is_cleaned_and_attachments_stay_bounded() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        let ran = crate::shell::Ran {
            command: "ls -G".into(),
            status: crate::shell::Status::Exited(1),
            output: "\x1b[31mred\x1b[0m\r\nplain\n".into(),
        };
        app.shell_output(&ran);
        let text = app.entries_text();
        assert!(text.contains("$ ls -G\nred\nplain\nexit 1"), "{text:?}");
        assert!(!text.contains('\x1b'));
        app.attach(ran);
        assert!(!app.attachments[0].output.contains('\x1b'));
        let big = crate::shell::Ran {
            command: "big".into(),
            status: crate::shell::Status::Exited(0),
            output: "x".repeat(crate::shell::OUTPUT_LIMIT),
        };
        app.attach(big);
        assert_eq!(app.attachments.len(), 1, "the oldest attachment is dropped");
        assert_eq!(app.attachments[0].command, "big");
        assert_eq!(app.notice, "Dropped the oldest attachment to stay within 32 KiB");
    }
```

- [ ] **Step 6: Run and watch it fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib shell_output_is_cleaned`
Expected: compile errors (`shell_output`, `attach`, `attachments`). Add the fields and empty methods (Step 7 shapes, bodies `{}`) to compile, then confirm the assertions fail.

- [ ] **Step 7: Implement roles, fields and methods in `view.rs`**

Extend `Role`:

```rust
pub enum Role {
    User,
    Assistant,
    Tool,
    Notice,
    Error,
    Shell,
    ShellFailed,
}
```

In `entry_lines`, add arms:

```rust
        Role::Shell => ("SHELL", MUTED),
        Role::ShellFailed => ("SHELL", AMBER),
```

Add fields to `App` (after `goals`) and initialise them in `App::new`:

```rust
    /// The workspace, where `!` commands run and `@` looks for files.
    pub root: PathBuf,
    /// `!` outputs waiting to go with the next prompt.
    pub attachments: Vec<crate::shell::Ran>,
    /// A `!` command is running.
    pub shell_running: bool,
```

```rust
            root: config.cwd.clone(),
            attachments: Vec::new(),
            shell_running: false,
```

Add methods in `impl App`:

```rust
    /// A finished `!` command in the transcript.
    pub fn shell_output(&mut self, ran: &crate::shell::Ran) {
        let output = clean(&ran.output);
        let newline = if output.is_empty() || output.ends_with('\n') { "" } else { "\n" };
        let role = if ran.ok() { Role::Shell } else { Role::ShellFailed };
        self.add(role, format!("$ {}\n{output}{newline}{}", ran.command, ran.summary()));
    }
    /// Keeps `ran` for the next prompt, dropping the oldest attachments
    /// beyond 32 KiB of output.
    pub fn attach(&mut self, mut ran: crate::shell::Ran) {
        ran.output = clean(&ran.output);
        self.attachments.push(ran);
        let total = |all: &[crate::shell::Ran]| all.iter().map(|a| a.output.len()).sum::<usize>();
        let mut dropped = false;
        while self.attachments.len() > 1 && total(&self.attachments) > crate::shell::OUTPUT_LIMIT {
            self.attachments.remove(0);
            dropped = true;
        }
        if dropped {
            self.notice = "Dropped the oldest attachment to stay within 32 KiB".into();
        }
    }
```

`clean` (from `text.rs`) already strips escape sequences and carriage returns (its test covers `\r`), so `"\x1b[31mred\x1b[0m\r\nplain\n"` becomes `"red\nplain\n"`.

Composer title chip — in `draw`, replace the `let composer = card(if app.running { … } else { … })` statement with:

```rust
    let base = if app.running {
        " Compose next prompt · wait or Esc to cancel "
    } else {
        " Prompt "
    };
    let title = match crate::composer::chip(&app.attachments) {
        Some(chip) => format!("{} · {chip} ", base.trim_end()),
        None => base.to_owned(),
    };
    let composer = card(&title)
        .border_style(Style::default().fg(if app.running { EDGE } else { ACCENT }));
```

Help text: after the `/copy` line from Task 1, insert `!cmd run and attach output · !!cmd run only · Esc stops\n`.

- [ ] **Step 8: Run the view tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib view`
Expected: PASS.

- [ ] **Step 9: Write the failing routing tests**

In `lib.rs` `mod model_tests`, add:

```rust
    #[tokio::test]
    async fn bang_lines_run_locally_and_double_bang_does_not_attach() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-bang").await;
        app.editor.set("!echo hi".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::RunShell { ref command, attach: true } if command == "echo hi"
        ));
        assert!(app.editor.text.is_empty());
        assert_eq!(app.history.back().map(String::as_str), Some("!echo hi"));
        app.editor.set("!!  pwd ".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::RunShell { ref command, attach: false } if command == "pwd"
        ));
        app.editor.set("!".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::Continue
        ));
        assert_eq!(app.notice, "Type a command after !");
        app.shell_running = true;
        app.editor.set("!ls".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::Continue
        ));
        assert_eq!(app.notice, "A command is already running");
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Esc)).await,
            Action::CancelShell
        ));
    }
    #[tokio::test]
    async fn attachments_go_with_the_next_prompt_then_clear() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-attach").await;
        app.attach(crate::shell::Ran {
            command: "echo hi".into(),
            status: crate::shell::Status::Exited(0),
            output: "hi\n".into(),
        });
        app.editor.set("explain".into());
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert!(app.attachments.is_empty());
        let shown = loop {
            match session.events.recv().await.unwrap() {
                octet_core::Event::User(text) => break text,
                _ => continue,
            }
        };
        assert_eq!(shown, "explain\n\n[+ echo hi]");
    }
    #[tokio::test]
    async fn esc_on_an_empty_idle_draft_removes_attachments() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-detach").await;
        app.attach(crate::shell::Ran {
            command: "ls".into(),
            status: crate::shell::Status::Exited(0),
            output: String::new(),
        });
        key_action(&mut app, &mut session, key(KeyCode::Esc)).await;
        assert!(app.attachments.is_empty());
        assert_eq!(app.notice, "Attachments removed");
    }
```

    #[tokio::test]
    async fn goal_prompts_never_carry_attachments() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-goal-attach").await;
        app.attach(crate::shell::Ran {
            command: "ls".into(),
            status: crate::shell::Status::Exited(0),
            output: "a\n".into(),
        });
        send_goal_prompt(&mut app, &session, "continue the goal".into()).await;
        assert_eq!(app.attachments.len(), 1, "kept for the user's next prompt");
        let shown = loop {
            match session.events.recv().await.unwrap() {
                octet_core::Event::User(text) => break text,
                _ => continue,
            }
        };
        assert!(!shown.contains("[+ ls]"), "{shown}");
    }
```

`model_tests` has a `ctrl(c)` helper but no plain-key one; add this beside it:

```rust
    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
```

(`app()` sets `ready = true`, and the demo driver queues a prompt sent before its own `Ready`, so the tests need no extra waiting.)

- [ ] **Step 10: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib bang_lines attachments_go esc_on_an_empty goal_prompts_never`
Expected: compile errors for `Action::RunShell`/`CancelShell`; after adding the variants (Step 11), assertion failures.

- [ ] **Step 11: Implement routing in `lib.rs`**

Add to `enum Action`:

```rust
    /// Run a `!` line; `attach` keeps its output for the next prompt.
    RunShell { command: String, attach: bool },
    /// Stop the running `!` command.
    CancelShell,
```

In `key_action`'s `KeyCode::Enter` arm, directly after `if draft.is_empty() { return Action::Continue; }`, insert:

```rust
            if let Some(rest) = draft.strip_prefix('!') {
                let (attach, command) = match rest.strip_prefix('!') {
                    Some(command) => (false, command.trim()),
                    None => (true, rest.trim()),
                };
                if command.is_empty() {
                    app.notice = "Type a command after !".into();
                    return Action::Continue;
                }
                if app.shell_running {
                    app.notice = "A command is already running".into();
                    return Action::Continue;
                }
                let command = command.to_owned();
                app.editor.take();
                app.history_index = None;
                if app.history.back() != Some(&draft) {
                    app.history.push_back(draft);
                    if app.history.len() > 50 {
                        app.history.pop_front();
                    }
                }
                return Action::RunShell { command, attach };
            }
```

Replace the prompt send (`match session.handle.send(Command::Prompt(draft.clone()))`) with:

```rust
            let command = if app.attachments.is_empty() {
                Command::Prompt(draft.clone())
            } else {
                let (wire, display) =
                    composer::with_attachments(&draft, &app.attachments, octet_core::PROMPT_LIMIT);
                Command::PromptWithDisplay { wire, display }
            };
            match session.handle.send(command) {
                Ok(()) => {
                    app.attachments.clear();
```

(keep the rest of the existing `Ok(())` body unchanged).

In the Ctrl+C arm, make the first branch `if app.shell_running { return Action::CancelShell; }` before `if app.is_busy()`. Replace the `KeyCode::Esc` arm with:

```rust
        KeyCode::Esc => {
            if app.shell_running {
                return Action::CancelShell;
            } else if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = "Cancelling…".into();
            } else if app.editor.text.is_empty() && !app.attachments.is_empty() {
                app.attachments.clear();
                app.notice = "Attachments removed".into();
            } else {
                app.scroll = 0;
            }
        }
```

In `run_session`, add beside `remote_check`:

```rust
    // The running `!` command, if any.
    let mut shell_task: Option<ShellTask> = None;
```

and define above `run_session`:

```rust
/// A `!` command running in the background.
struct ShellTask {
    task: tokio::task::JoinHandle<Result<shell::Ran, String>>,
    cancel: Option<tokio::sync::oneshot::Sender<()>>,
    attach: bool,
}
```

Add the `select!` branch after the `remote_check` branch:

```rust
            result = async {
                match shell_task.as_mut() {
                    Some(running) => (&mut running.task).await,
                    None => std::future::pending().await,
                }
            } => {
                let attach = shell_task.take().is_some_and(|running| running.attach);
                app.shell_running = false;
                match result {
                    Ok(Ok(ran)) => {
                        app.shell_output(&ran);
                        if attach {
                            app.attach(ran);
                        }
                    }
                    Ok(Err(error)) => app.notice(error),
                    Err(error) => app.notice(format!("The command failed: {error}")),
                }
                dirty = true;
            }
```

Handle the new actions in the action `match`:

```rust
                    Action::RunShell { command, attach } => {
                        let (cancel, cancelled) = tokio::sync::oneshot::channel();
                        let root = app.root.clone();
                        app.shell_running = true;
                        app.notice = format!("Running {command} · Esc to stop");
                        shell_task = Some(ShellTask {
                            task: tokio::spawn(async move { shell::run(&command, &root, cancelled).await }),
                            cancel: Some(cancel),
                            attach,
                        });
                    }
                    Action::CancelShell => {
                        if let Some(cancel) = shell_task.as_mut().and_then(|running| running.cancel.take()) {
                            let _ = cancel.send(());
                        }
                    }
```

`App::root` (added in Step 7) is the real workspace path; `App::workspace` is only its cleaned display form. Task 5 relies on `App::root`.

Remove any temporary `allow(dead_code)` from Task 2.

- [ ] **Step 12: Run the TUI tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib`
Expected: PASS.

- [ ] **Step 13: Write the end-to-end test**

Append to `crates/octet/tests/terminal.rs`:

```rust
#[test]
fn bang_runs_a_command_and_attaches_it_to_the_next_prompt() {
    let mut p = Pty::spawn();
    p.wait(|p| p.shows("● ready"));
    p.send(b"!echo hi-from-shell\r");
    p.wait(|p| p.screen_shows("$ echo hi-from-shell"));
    p.wait(|p| p.screen_shows("+ echo hi-from-shell (exit 0)"));
    p.send(b"what does it say\r");
    p.wait(|p| p.screen_shows("[+ echo hi-from-shell]"));
    p.wait(|p| p.shows("● completed"));
    assert!(!p.screen_shows("(exit 0) "), "the chip clears after sending");
    p.send(b"!!echo local-only\r");
    p.wait(|p| p.screen_shows("$ echo local-only"));
    assert!(!p.screen_shows("+ echo local-only"));
    p.quit();
    p.finish();
}
```

- [ ] **Step 14: Run it**

Run: `scripts/rust-env.sh cargo test -p octet --test terminal -- bang_runs`
Expected: PASS. If the chip assertion races the repaint, wait on `row_shows` for the prompt box title row instead of asserting immediately.

- [ ] **Step 15: Commit**

```bash
git add crates/octet-tui/src crates/octet/tests/terminal.rs
git commit -F - <<'EOF'
Run ! commands and attach their output to the next prompt

!cmd runs in the background, shows a SHELL entry and attaches its
cleaned output to the next prompt: the vendor gets a fenced block, the
transcript a [+ cmd] marker. !!cmd only shows it. Esc stops a running
command first, and on an empty idle draft removes attachments.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 4: File index (`files.rs`)

**Files:**
- Create: `crates/octet-tui/src/files.rs`
- Modify: `crates/octet-tui/src/lib.rs` (module list)
- Test: unit tests in `files.rs`

**Interfaces:**
- Produces:
  - `files::SHOWN: usize` (8)
  - `struct files::Index { pub capped: bool, .. }` with `fn build(root: &Path) -> Index` (blocking; run it with `spawn_blocking`), `fn rank(&self, query: &str) -> Vec<&str>`, and `#[cfg(test)] fn from_paths(paths: Vec<String>) -> Index`
  - `enum files::Files { Unbuilt, Wanted, Building, Ready(Index) }`

- [ ] **Step 1: Write the failing tests**

Create `crates/octet-tui/src/files.rs`:

```rust
//! `@` mentions: the workspace's files, listed once in the background and
//! ranked for what the user typed.
use std::path::{Path, PathBuf};

/// Paths kept per workspace.
const LIMIT: usize = 50_000;
/// Suggestions shown at once.
pub const SHOWN: usize = 8;

pub struct Index {
    paths: Vec<String>,
    /// The workspace had more than `LIMIT` files.
    pub capped: bool,
}

/// Where the session's index is.
pub enum Files {
    Unbuilt,
    /// Asked for; the session loop starts the build.
    Wanted,
    Building,
    Ready(Index),
}

impl Index {
    /// Git's view of the workspace when it is a work tree, else a walk.
    pub fn build(root: &Path) -> Index {
        git(root, LIMIT).unwrap_or_else(|| walk(root, LIMIT))
    }
    #[cfg(test)]
    pub fn from_paths(paths: Vec<String>) -> Index {
        Index { paths, capped: false }
    }
    pub fn rank(&self, _query: &str) -> Vec<&str> {
        Vec::new()
    }
}

fn git(_root: &Path, _limit: usize) -> Option<Index> {
    None
}

fn walk(_root: &Path, _limit: usize) -> Index {
    Index { paths: Vec::new(), capped: false }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn index(paths: &[&str]) -> Index {
        Index::from_paths(paths.iter().map(|p| p.to_string()).collect())
    }
    #[test]
    fn file_name_matches_come_first_then_fewer_gaps_then_shorter_paths() {
        let all = index(&["lib/x.rs", "a/lib.rs", "xmainx_long_name.rs", "m_a_i_n.rs", "src/main.rs"]);
        assert_eq!(all.rank("lib"), ["a/lib.rs", "lib/x.rs"]);
        assert_eq!(all.rank("main"), ["src/main.rs", "xmainx_long_name.rs", "m_a_i_n.rs"]);
        assert_eq!(all.rank("MAIN")[0], "src/main.rs", "case is ignored");
        assert!(all.rank("zzz").is_empty());
        assert_eq!(all.rank("").len(), 5);
    }
    #[test]
    fn shows_at_most_eight() {
        let many: Vec<String> = (0..20).map(|i| format!("f{i}.rs")).collect();
        assert_eq!(Index::from_paths(many).rank("f").len(), SHOWN);
    }
    #[test]
    fn walks_a_plain_folder_skipping_hidden_and_build_output() {
        let dir = octet_testkit::TempDir::new("octet-files-walk");
        for path in ["a.rs", "sub/b.rs", ".hidden/c.rs", "target/d.rs", "node_modules/e.js"] {
            let path = dir.path().join(path);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, "").unwrap();
        }
        let index = walk(dir.path(), LIMIT);
        assert_eq!(index.paths, ["a.rs", "sub/b.rs"]);
        assert!(!index.capped);
        let capped = walk(dir.path(), 1);
        assert_eq!(capped.paths.len(), 1);
        assert!(capped.capped);
    }
    #[test]
    fn lists_tracked_and_untracked_files_but_not_ignored_ones() {
        let dir = octet_testkit::TempDir::new("octet-files-git");
        std::fs::create_dir_all(dir.path()).unwrap();
        let git = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(dir.path())
                .output()
                .unwrap()
                .status
                .success());
        };
        git(&["init", "-q"]);
        for (name, text) in [("tracked.rs", ""), ("untracked.rs", ""), ("ignored.log", ""), (".gitignore", "*.log\n")] {
            std::fs::write(dir.path().join(name), text).unwrap();
        }
        git(&["add", "tracked.rs"]);
        let index = git_index(dir.path());
        assert!(index.paths.contains(&"tracked.rs".to_string()));
        assert!(index.paths.contains(&"untracked.rs".to_string()));
        assert!(!index.paths.iter().any(|p| p == "ignored.log"));
        assert!(git_in_plain_folder_is_none());
    }
    fn git_index(root: &Path) -> Index {
        git(root, LIMIT).expect("a work tree lists through git")
    }
    fn git_in_plain_folder_is_none() -> bool {
        let dir = octet_testkit::TempDir::new("octet-files-plain");
        std::fs::create_dir_all(dir.path()).unwrap();
        git(dir.path(), LIMIT).is_none()
    }
}
```

Add `mod files;` to `lib.rs`'s module list.

- [ ] **Step 2: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib files`
Expected: FAIL in all four tests.

- [ ] **Step 3: Implement**

```rust
impl Index {
    pub fn rank(&self, query: &str) -> Vec<&str> {
        let query = query.to_lowercase();
        let mut scored: Vec<((bool, usize), &str)> = self
            .paths
            .iter()
            .filter_map(|path| score(path, &query).map(|score| (score, path.as_str())))
            .collect();
        scored.sort_by(|a, b| {
            a.0.cmp(&b.0)
                .then_with(|| a.1.len().cmp(&b.1.len()))
                .then_with(|| a.1.cmp(b.1))
        });
        scored.into_iter().take(SHOWN).map(|(_, path)| path).collect()
    }
}

/// Lower is better: (outside the file name, gaps between matched characters).
fn score(path: &str, query: &str) -> Option<(bool, usize)> {
    if query.is_empty() {
        return Some((false, 0));
    }
    let lower = path.to_lowercase();
    let name = &lower[lower.rfind('/').map_or(0, |slash| slash + 1)..];
    subsequence(name, query)
        .map(|gaps| (false, gaps))
        .or_else(|| subsequence(&lower, query).map(|gaps| (true, gaps)))
}

/// Gaps between `query`'s characters found in order in `text`, or None.
fn subsequence(text: &str, query: &str) -> Option<usize> {
    let mut chars = text.chars().enumerate();
    let mut gaps = 0;
    let mut last: Option<usize> = None;
    for wanted in query.chars() {
        let (at, _) = chars.by_ref().find(|(_, c)| *c == wanted)?;
        if last.is_some_and(|last| at != last + 1) {
            gaps += 1;
        }
        last = Some(at);
    }
    Some(gaps)
}

fn git(root: &Path, limit: usize) -> Option<Index> {
    let output = std::process::Command::new("git")
        .args(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        .current_dir(root)
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let mut paths = Vec::new();
    let mut capped = false;
    for path in output.stdout.split(|byte| *byte == 0).filter(|p| !p.is_empty()) {
        if paths.len() == limit {
            capped = true;
            break;
        }
        paths.push(String::from_utf8_lossy(path).into_owned());
    }
    paths.sort();
    paths.dedup();
    Some(Index { paths, capped })
}

fn walk(root: &Path, limit: usize) -> Index {
    let mut paths = Vec::new();
    let mut capped = false;
    let mut pending = vec![PathBuf::new()];
    'walk: while let Some(dir) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(root.join(&dir)) else {
            continue;
        };
        let mut entries: Vec<_> = entries.filter_map(Result::ok).collect();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') || name == "target" || name == "node_modules" {
                continue;
            }
            let path = dir.join(&name);
            // Symlinks are skipped, so a link loop cannot trap the walk.
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => pending.push(path),
                Ok(kind) if kind.is_file() => {
                    if paths.len() == limit {
                        capped = true;
                        break 'walk;
                    }
                    paths.push(path.to_string_lossy().into_owned());
                }
                _ => {}
            }
        }
    }
    paths.sort();
    Index { paths, capped }
}
```

- [ ] **Step 4: Run the tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib files`
Expected: PASS (4 tests). If clippy later flags `Files` or `build` unused outside tests, Task 5 uses them; add `#[cfg_attr(not(test), allow(dead_code))]` temporarily and remove it in Task 5.

- [ ] **Step 5: Commit**

```bash
git add crates/octet-tui/src/files.rs crates/octet-tui/src/lib.rs
git commit -F - <<'EOF'
Index workspace files for @ mentions

Lists files through git (tracked plus untracked, not ignored) or, outside
a work tree, a walk that skips hidden folders, target and node_modules,
up to 50,000 paths. Ranking prefers file-name matches, then fewer gaps,
then shorter paths.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 5: `@` mentions and Tab completion

**Files:**
- Modify: `crates/octet-tui/src/composer.rs` (token detection, mention text, Tab logic)
- Modify: `crates/octet-tui/src/editor.rs` (`Editor::replace`)
- Modify: `crates/octet-tui/src/view.rs` (`App::completion`, `App::files`, `App::connection` resets the index, popup drawing, help text)
- Modify: `crates/octet-tui/src/lib.rs` (key routing, `refresh_completion`, `accept_completion`, index task branch)
- Test: unit tests in `composer.rs`, `editor.rs`, `lib.rs`; `crates/octet/tests/terminal.rs`

**Interfaces:**
- Consumes: `files::{Index, Files, SHOWN}` (Task 4); `App::root` (Task 3); `view::COMMANDS` (Task 1).
- Produces:
  - `composer::Kind { File, Path, Command }` (`Clone, Copy, PartialEq, Debug`)
  - `composer::Completion { pub kind: Kind, pub items: Vec<String>, pub selected: usize, pub start: usize }`
  - `composer::mention_at(text: &str, cursor: usize) -> Option<(usize, &str)>`
  - `composer::mention(path: &str) -> String`
  - `composer::Tab { Replace { start: usize, text: String }, Popup(Completion), Mention(usize), Nothing }`
  - `composer::tab(text: &str, cursor: usize, root: &Path, home: Option<&Path>) -> Tab`
  - `Editor::replace(&mut self, start: usize, text: &str) -> bool` (replaces `start..cursor`)

- [ ] **Step 1: Write the failing composer tests**

Append to `composer.rs`'s `mod tests`:

```rust
    #[test]
    fn finds_the_mention_being_typed() {
        assert_eq!(mention_at("@ma", 3), Some((0, "ma")));
        assert_eq!(mention_at("see @src/ma", 11), Some((4, "src/ma")));
        assert_eq!(mention_at("héllo @ma", "héllo @ma".len()), Some((7, "ma")));
        assert_eq!(mention_at("me@example.com", 14), None, "mid-word @ is not a mention");
        assert_eq!(mention_at("@ma rest", 8), None, "the cursor left the token");
    }
    #[test]
    fn mentions_quote_paths_with_spaces() {
        assert_eq!(mention("src/main.rs"), "@src/main.rs ");
        assert_eq!(mention("my notes.md"), "@\"my notes.md\" ");
    }
    #[test]
    fn tab_completes_commands_paths_and_mentions() {
        let dir = octet_testkit::TempDir::new("octet-tab");
        std::fs::create_dir_all(dir.path().join("src/bin")).unwrap();
        std::fs::write(dir.path().join("src/main.rs"), "").unwrap();
        std::fs::write(dir.path().join("src/model.rs"), "").unwrap();
        let root = dir.path();
        let tab = |text: &str| super::tab(text, text.len(), root, None);
        assert!(matches!(tab("/rem"), Tab::Replace { start: 0, ref text } if text == "/remote-control "));
        assert!(matches!(tab("/re"), Tab::Popup(Completion { kind: Kind::Command, .. })));
        assert!(matches!(tab("look at src/ma"), Tab::Replace { start: 8, ref text } if text == "src/main.rs"));
        assert!(matches!(tab("src/b"), Tab::Replace { start: 0, ref text } if text == "src/bin/"));
        assert!(matches!(tab("src/m"), Tab::Popup(Completion { kind: Kind::Path, ref items, .. }) if items.len() == 2));
        assert!(matches!(tab("héllo @ma"), Tab::Mention(7)));
        assert!(matches!(tab("plain words"), Tab::Nothing));
        assert!(matches!(tab("nowhere/zz"), Tab::Nothing), "missing folders complete to nothing");
        assert!(
            matches!(tab("/usr/li"), Tab::Replace { ref text, .. } if text.starts_with("/usr/lib")),
            "a second / makes it a path, not a command"
        );
    }
```

Add the declarations (bodies returning `None`/`String::new()`/`Tab::Nothing`) and the types so the tests compile:

```rust
use std::path::Path;

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Kind {
    File,
    Path,
    Command,
}

/// The suggestion popup above the prompt box.
#[derive(Debug)]
pub struct Completion {
    pub kind: Kind,
    pub items: Vec<String>,
    pub selected: usize,
    /// Where the word being completed starts in the draft.
    pub start: usize,
}

/// What Tab does to the draft.
#[derive(Debug)]
pub enum Tab {
    Replace { start: usize, text: String },
    Popup(Completion),
    /// Open the `@` popup for the mention starting here.
    Mention(usize),
    Nothing,
}
```

- [ ] **Step 2: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib composer`
Expected: FAIL in the three new tests.

- [ ] **Step 3: Implement the composer logic**

```rust
/// The start of the word ending at `cursor`.
fn word_start(text: &str, cursor: usize) -> usize {
    text[..cursor]
        .char_indices()
        .rev()
        .find(|(_, c)| c.is_whitespace())
        .map_or(0, |(at, c)| at + c.len_utf8())
}

pub fn mention_at(text: &str, cursor: usize) -> Option<(usize, &str)> {
    let start = word_start(text, cursor);
    let token = &text[start..cursor];
    let query = token.strip_prefix('@')?;
    // The word must end at the cursor, or the user moved past it.
    if text[cursor..].chars().next().is_some_and(|c| !c.is_whitespace()) {
        return None;
    }
    (!query.contains('"')).then_some((start, query))
}

pub fn mention(path: &str) -> String {
    if path.contains(char::is_whitespace) {
        format!("@\"{path}\" ")
    } else {
        format!("@{path} ")
    }
}

pub fn tab(text: &str, cursor: usize, root: &Path, home: Option<&Path>) -> Tab {
    let start = word_start(text, cursor);
    let word = &text[start..cursor];
    if word.starts_with('@') {
        return Tab::Mention(start);
    }
    let first_word = text[..start].trim().is_empty();
    if first_word && word.starts_with('/') && !word[1..].contains('/') {
        let names: Vec<String> = crate::view::COMMANDS
            .iter()
            .map(|(name, _)| name.to_string())
            .filter(|name| name.starts_with(word))
            .collect();
        return choose(start, word, names, Kind::Command, " ");
    }
    if word.contains('/') || word.starts_with('~') {
        return choose(start, word, path_candidates(root, home, word), Kind::Path, "");
    }
    Tab::Nothing
}

/// One candidate fills in; several fill their common prefix, or open the
/// popup when that adds nothing.
fn choose(start: usize, word: &str, items: Vec<String>, kind: Kind, suffix: &str) -> Tab {
    match items.len() {
        0 => Tab::Nothing,
        1 => Tab::Replace {
            start,
            text: format!("{}{}", items[0], if items[0].ends_with('/') { "" } else { suffix }),
        },
        _ => {
            let prefix = common_prefix(&items);
            if prefix.len() > word.len() {
                Tab::Replace { start, text: prefix }
            } else {
                Tab::Popup(Completion {
                    kind,
                    items: items.into_iter().take(crate::files::SHOWN).collect(),
                    selected: 0,
                    start,
                })
            }
        }
    }
}

fn common_prefix(items: &[String]) -> String {
    let first = &items[0];
    let mut end = first.len();
    for item in &items[1..] {
        end = first
            .char_indices()
            .zip(item.chars())
            .find(|((_, a), b)| a != b)
            .map_or(end.min(item.len()), |((at, _), _)| at.min(end));
    }
    first[..end].to_owned()
}

/// Entries of the word's folder starting with its last part, as whole words
/// (folders end in `/`). Hidden entries only when the part starts with `.`.
fn path_candidates(root: &Path, home: Option<&Path>, word: &str) -> Vec<String> {
    let (folder, part) = match word.rfind('/') {
        Some(slash) => (&word[..=slash], &word[slash + 1..]),
        None => ("", word),
    };
    let base = if let Some(rest) = folder.strip_prefix("~/") {
        match home {
            Some(home) => home.join(rest),
            None => return Vec::new(),
        }
    } else if folder.starts_with('/') {
        std::path::PathBuf::from(folder)
    } else {
        root.join(folder)
    };
    let Ok(entries) = std::fs::read_dir(base) else {
        return Vec::new();
    };
    let mut found: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            let visible = !name.starts_with('.') || part.starts_with('.');
            (visible && name.starts_with(part)).then(|| {
                let folder_mark = if entry.path().is_dir() { "/" } else { "" };
                format!("{folder}{name}{folder_mark}")
            })
        })
        .take(200)
        .collect();
    found.sort();
    found
}
```

- [ ] **Step 4: Run the composer tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib composer`
Expected: PASS.

- [ ] **Step 5: Write and pass `Editor::replace`**

Test in `editor.rs` (add a `#[cfg(test)] mod tests` if none exists):

```rust
    #[test]
    fn replace_swaps_the_text_before_the_cursor() {
        let mut editor = Editor::default();
        assert!(editor.insert("see @ma now"));
        editor.cursor = 7;
        assert!(editor.replace(4, "@src/main.rs "));
        assert_eq!(editor.text, "see @src/main.rs  now");
        assert_eq!(editor.cursor, 17);
        assert!(!editor.replace(0, &"x".repeat(octet_core::PROMPT_LIMIT + 1)));
    }
```

Run `scripts/rust-env.sh cargo test -p octet-tui --lib replace_swaps` (FAIL: no method), then implement:

```rust
    /// Replaces `start..cursor` with `text`; false when it would not fit the
    /// prompt limit.
    #[must_use = "false means the text did not fit the prompt limit"]
    pub fn replace(&mut self, start: usize, text: &str) -> bool {
        if self.text.len() - (self.cursor - start) + text.len() > octet_core::PROMPT_LIMIT {
            return false;
        }
        self.text.replace_range(start..self.cursor, text);
        self.cursor = start + text.len();
        true
    }
```

Re-run: PASS.

- [ ] **Step 6: Write the failing routing tests**

In `lib.rs` `mod model_tests`:

```rust
    #[tokio::test]
    async fn at_opens_the_file_popup_and_enter_accepts() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at").await;
        app.files = crate::files::Files::Ready(crate::files::Index::from_paths(vec![
            "README.md".into(),
            "src/main.rs".into(),
        ]));
        for c in "see @mai".chars() {
            key_action(&mut app, &mut session, key(KeyCode::Char(c))).await;
        }
        let completion = app.completion.as_ref().expect("popup open");
        assert_eq!(completion.items, ["src/main.rs"]);
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert_eq!(app.editor.text, "see @src/main.rs ");
        assert!(app.completion.is_none());
        assert!(!app.running, "Enter accepted instead of sending");
    }
    #[tokio::test]
    async fn the_first_at_asks_for_the_index_and_esc_closes_the_popup() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at-index").await;
        key_action(&mut app, &mut session, key(KeyCode::Char('@'))).await;
        assert!(matches!(app.files, crate::files::Files::Wanted));
        assert!(app.completion.is_some());
        key_action(&mut app, &mut session, key(KeyCode::Esc)).await;
        assert!(app.completion.is_none());
        assert_eq!(app.editor.text, "@");
    }
    #[tokio::test]
    async fn an_at_inside_a_word_opens_nothing() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at-word").await;
        for c in "me@host".chars() {
            key_action(&mut app, &mut session, key(KeyCode::Char(c))).await;
        }
        assert!(app.completion.is_none());
    }
```

- [ ] **Step 7: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib at_opens the_first_at an_at_inside`
Expected: compile errors for `app.files`/`app.completion`; after adding fields (Step 8), assertion failures.

- [ ] **Step 8: Implement App state, routing and the index task**

`view.rs` — add to `App` and `App::new`:

```rust
    /// The suggestion popup, when open.
    pub completion: Option<crate::composer::Completion>,
    /// The workspace file index for `@`.
    pub files: crate::files::Files,
```

```rust
            completion: None,
            files: crate::files::Files::Unbuilt,
```

In `App::connection` (called for every new session), add `self.files = crate::files::Files::Unbuilt;` so the index is rebuilt after `/reconnect`, `/model` and `/mode`, and a build abandoned with the old session never leaves it stuck at `Building`.

`lib.rs` — add helpers:

```rust
/// Opens the `@` popup for the mention starting at `start`, asking for the
/// index on first use.
fn open_mentions(app: &mut App, start: usize) {
    if matches!(app.files, files::Files::Unbuilt) {
        app.files = files::Files::Wanted;
    }
    app.completion = Some(composer::Completion {
        kind: composer::Kind::File,
        items: Vec::new(),
        selected: 0,
        start,
    });
    refresh_completion(app);
}
/// Re-ranks the `@` popup for the current draft, closing it when the cursor
/// has left the mention.
fn refresh_completion(app: &mut App) {
    let Some(completion) = app.completion.as_mut() else {
        return;
    };
    if completion.kind != composer::Kind::File {
        return;
    }
    match composer::mention_at(&app.editor.text, app.editor.cursor) {
        Some((start, query)) if start == completion.start => {
            completion.items = match &app.files {
                files::Files::Ready(index) => index.rank(query).into_iter().map(str::to_owned).collect(),
                _ => Vec::new(),
            };
            completion.selected = completion.selected.min(completion.items.len().saturating_sub(1));
        }
        _ => app.completion = None,
    }
}
/// Puts the selected suggestion into the draft and closes the popup.
fn accept_completion(app: &mut App) {
    let Some(completion) = app.completion.take() else {
        return;
    };
    let Some(item) = completion.items.get(completion.selected) else {
        return;
    };
    let text = match completion.kind {
        composer::Kind::File => composer::mention(item),
        composer::Kind::Path => item.clone(),
        composer::Kind::Command => format!("{item} "),
    };
    if !app.editor.replace(completion.start, &text) {
        app.notice = "Prompt limit reached".into();
    }
}
```

In `key_action`, directly after the `if app.palette { … }` block, insert the popup's keys:

```rust
    if let Some(completion) = app.completion.as_mut() {
        match key.code {
            KeyCode::Up => {
                completion.selected = completion.selected.saturating_sub(1);
                return Action::Continue;
            }
            KeyCode::Down => {
                completion.selected = (completion.selected + 1).min(completion.items.len().saturating_sub(1));
                return Action::Continue;
            }
            KeyCode::Tab | KeyCode::Enter => {
                accept_completion(app);
                return Action::Continue;
            }
            KeyCode::Esc => {
                app.completion = None;
                return Action::Continue;
            }
            // Path and command popups close on any other key; the file popup
            // follows the edit below.
            _ if completion.kind != composer::Kind::File => app.completion = None,
            _ => {}
        }
    }
```

In the main `match key.code`, add a Tab arm (before the `KeyCode::Char(c)` arm):

```rust
        KeyCode::Tab => {
            let home = std::env::var_os("HOME").map(PathBuf::from);
            match composer::tab(&app.editor.text, app.editor.cursor, &app.root, home.as_deref()) {
                composer::Tab::Replace { start, text } => {
                    if !app.editor.replace(start, &text) {
                        app.notice = "Prompt limit reached".into();
                    }
                }
                composer::Tab::Popup(completion) => app.completion = Some(completion),
                composer::Tab::Mention(start) => open_mentions(app, start),
                composer::Tab::Nothing => {}
            }
        }
```

At the end of `key_action`, just before the final `Action::Continue`, add:

```rust
    if let KeyCode::Char('@') = key.code {
        if app.completion.is_none() {
            if let Some((start, "")) = composer::mention_at(&app.editor.text, app.editor.cursor) {
                open_mentions(app, start);
            }
        }
    }
    refresh_completion(app);
```

`run_session` — beside `shell_task`:

```rust
    // The workspace file index being built for `@`.
    let mut index_task: Option<tokio::task::JoinHandle<files::Index>> = None;
```

Add the branch:

```rust
            index = async {
                match index_task.as_mut() {
                    Some(task) => task.await,
                    None => std::future::pending().await,
                }
            } => {
                index_task = None;
                app.files = match index {
                    Ok(index) => files::Files::Ready(index),
                    Err(_) => files::Files::Unbuilt,
                };
                refresh_completion(app);
                dirty = true;
            }
```

At the end of the input branch's handler (after `match action { … }` and before `dirty = true;`), start a wanted build:

```rust
                if matches!(app.files, files::Files::Wanted) {
                    let root = app.root.clone();
                    index_task = Some(tokio::task::spawn_blocking(move || files::Index::build(&root)));
                    app.files = files::Files::Building;
                }
```

Remove any temporary `allow(dead_code)` from Task 4.

- [ ] **Step 9: Draw the popup**

In `view.rs`, add:

```rust
/// The `@`, path or command suggestions, just above the prompt box.
fn completion_popup(frame: &mut Frame, composer: Rect, app: &App) {
    let Some(completion) = &app.completion else {
        return;
    };
    let mut lines: Vec<Line> = completion
        .items
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let chosen = index == completion.selected;
            Line::from(Span::styled(
                format!(" {} {item}", if chosen { "›" } else { " " }),
                Style::default()
                    .fg(if chosen { ACCENT } else { FG })
                    .bg(if chosen { SELECTED } else { PANEL }),
            ))
        })
        .collect();
    if lines.is_empty() {
        let message = match (&completion.kind, &app.files) {
            (crate::composer::Kind::File, crate::files::Files::Ready(_)) => " No matching files",
            (crate::composer::Kind::File, _) => " Indexing files…",
            _ => " No matches",
        };
        lines.push(Line::from(Span::styled(message, Style::default().fg(MUTED))));
    }
    if let crate::files::Files::Ready(index) = &app.files {
        if index.capped && completion.kind == crate::composer::Kind::File {
            lines.push(Line::from(Span::styled(
                " Indexed the first 50,000 files",
                Style::default().fg(MUTED),
            )));
        }
    }
    let title = match completion.kind {
        crate::composer::Kind::File => " Files · Enter choose · Esc close ",
        crate::composer::Kind::Path => " Paths ",
        crate::composer::Kind::Command => " Commands ",
    };
    let height = lines.len() as u16 + 2;
    let width = composer.width.min(64);
    let area = Rect {
        x: composer.x,
        y: composer.y.saturating_sub(height),
        width,
        height: height.min(composer.y),
    };
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(lines).block(card(title)).style(Style::default().bg(PANEL)),
        area,
    );
}
```

Call it in `draw` right after the composer's hint row is drawn: `completion_popup(frame, regions[2], app);`.

Add a view test:

```rust
    #[test]
    fn the_popup_lists_suggestions_above_the_prompt() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.completion = Some(crate::composer::Completion {
            kind: crate::composer::Kind::File,
            items: vec!["src/main.rs".into(), "src/model.rs".into()],
            selected: 1,
            start: 0,
        });
        let rows = screen(100, 30, &mut app);
        assert!(rows.iter().any(|row| row.contains("   src/main.rs")));
        assert!(rows.iter().any(|row| row.contains(" › src/model.rs")));
        app.completion.as_mut().unwrap().items.clear();
        let rows = screen(100, 30, &mut app);
        assert!(rows.iter().any(|row| row.contains("Indexing files…")));
    }
```

Help text: after the `!cmd` line, insert `@ mention a file · Tab completes paths and /commands\n`.

- [ ] **Step 10: Run the TUI tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib`
Expected: PASS.

- [ ] **Step 11: Write the end-to-end test**

```rust
#[test]
fn at_mentions_a_workspace_file() {
    let workspace = octet_testkit::TempDir::new("octet-at-workspace");
    fs::create_dir_all(workspace.path().join("src")).unwrap();
    fs::write(workspace.path().join("src/main.rs"), "fn main() {}\n").unwrap();
    let cwd = workspace.path().to_str().unwrap().to_owned();
    let mut p = Pty::spawn_with(&["--cwd", &cwd]);
    p.wait(|p| p.shows("● ready"));
    p.send(b"explain @mai");
    p.wait(|p| p.screen_shows("› src/main.rs"));
    p.send(b"\r");
    p.wait(|p| p.screen_shows("explain @src/main.rs"));
    p.send(b"\x15");
    p.send(b"/rem\t");
    p.wait(|p| p.screen_shows("/remote-control"));
    p.quit();
    p.finish();
}
```

- [ ] **Step 12: Run it**

Run: `scripts/rust-env.sh cargo test -p octet --test terminal -- at_mentions`
Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add crates/octet-tui/src crates/octet/tests/terminal.rs
git commit -F - <<'EOF'
Mention files with @ and complete with Tab

@ opens a popup of the workspace's files, ranked as you type; Enter or
Tab inserts the path, which the vendor reads with its own tools. Tab
completes paths from the filesystem and slash commands at the start of
the draft. The index builds in the background on first use.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 6: Ctrl+G external editor

**Files:**
- Create: `crates/octet-tui/src/external.rs`
- Modify: `crates/octet-tui/src/lib.rs` (`TerminalGuard::resume`, `Action::ExternalEditor`, `input: Option<InputReader>`, editing branch, no drawing while editing)
- Modify: `crates/octet-tui/src/view.rs` (help text)
- Test: unit tests in `external.rs`; `crates/octet/tests/terminal.rs`

**Interfaces:**
- Produces:
  - `struct external::Edit { pub path: PathBuf, pub program: String, pub args: Vec<String> }` — removes its file on drop
  - `external::editor_command() -> Vec<String>`
  - `external::prepare(draft: &str, command: Vec<String>) -> Result<Edit, String>`
  - `Edit::finish(self, success: bool) -> Result<String, String>`
  - `Action::ExternalEditor`

- [ ] **Step 1: Write the failing tests**

Create `crates/octet-tui/src/external.rs`:

```rust
//! Ctrl+G: write the draft in the user's editor. The temp file is private
//! and removed whatever happens.
use std::{
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

/// The draft on disk, and the editor to open it with.
pub struct Edit {
    pub path: PathBuf,
    pub program: String,
    pub args: Vec<String>,
}

impl Drop for Edit {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// `$VISUAL`, then `$EDITOR`, then `vi`, split on whitespace so
/// `code --wait` works.
pub fn editor_command() -> Vec<String> {
    ["VISUAL", "EDITOR"]
        .iter()
        .filter_map(|name| std::env::var(name).ok())
        .find(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "vi".into())
        .split_whitespace()
        .map(str::to_owned)
        .collect()
}

pub fn prepare(_draft: &str, _command: Vec<String>) -> Result<Edit, String> {
    Err("not implemented".into())
}

impl Edit {
    /// The edited draft; the original stays when the editor failed or the
    /// result is over the prompt limit.
    pub fn finish(self, _success: bool) -> Result<String, String> {
        Err("not implemented".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn edit(draft: &str) -> Edit {
        prepare(draft, vec!["true".into()]).unwrap()
    }
    #[test]
    fn round_trips_an_edited_draft() {
        let edit = edit("first");
        assert_eq!(std::fs::read_to_string(&edit.path).unwrap(), "first");
        let mode = std::fs::metadata(&edit.path).unwrap();
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(mode.permissions().mode() & 0o777, 0o600);
        std::fs::write(&edit.path, "second line\n").unwrap();
        let path = edit.path.clone();
        assert_eq!(edit.finish(true).unwrap(), "second line");
        assert!(!path.exists(), "the temp file is removed");
    }
    #[test]
    fn a_failed_or_oversized_edit_keeps_the_draft() {
        let failed = edit("keep");
        let path = failed.path.clone();
        assert!(failed.finish(false).unwrap_err().contains("draft is unchanged"));
        assert!(!path.exists());
        let big = edit("keep");
        std::fs::write(&big.path, "x".repeat(octet_core::PROMPT_LIMIT + 1)).unwrap();
        assert!(big.finish(true).unwrap_err().contains("64 KiB"));
    }
    #[test]
    fn an_empty_command_is_refused() {
        assert!(prepare("x", Vec::new()).is_err());
    }
}
```

Add `mod external;` to `lib.rs`'s module list.

- [ ] **Step 2: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib external`
Expected: FAIL (`unwrap` on `Err("not implemented")`).

- [ ] **Step 3: Implement**

```rust
pub fn prepare(draft: &str, mut command: Vec<String>) -> Result<Edit, String> {
    if command.is_empty() {
        return Err("No editor is set; set $EDITOR".into());
    }
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let path = std::env::temp_dir().join(format!(
        "octet-prompt-{}-{}.md",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(|e| format!("Cannot create {}: {e}", path.display()))?;
    let program = command.remove(0);
    let edit = Edit { path, program, args: command };
    file.write_all(draft.as_bytes())
        .map_err(|e| format!("Cannot write {}: {e}", edit.path.display()))?;
    Ok(edit)
}

impl Edit {
    pub fn finish(self, success: bool) -> Result<String, String> {
        if !success {
            return Err("The editor exited with an error; the draft is unchanged".into());
        }
        let text = std::fs::read_to_string(&self.path)
            .map_err(|e| format!("Cannot read the edited draft: {e}; the draft is unchanged"))?;
        let text = text
            .strip_suffix("\r\n")
            .or_else(|| text.strip_suffix('\n'))
            .unwrap_or(&text)
            .to_owned();
        if text.len() > octet_core::PROMPT_LIMIT {
            return Err("The edited prompt is over the 64 KiB limit; the draft is unchanged".into());
        }
        Ok(text)
    }
}
```

(`finish` consumes `self`, so `Drop` removes the file after the read, on every path.)

- [ ] **Step 4: Run the unit tests**

Run: `scripts/rust-env.sh cargo test -p octet-tui --lib external`
Expected: PASS (3 tests).

- [ ] **Step 5: Write the failing end-to-end tests**

```rust
#[test]
fn ctrl_g_edits_the_draft_in_an_external_editor() {
    let tools = octet_testkit::TempDir::new("octet-editor");
    fs::create_dir_all(tools.path()).unwrap();
    stand_in(tools.path(), "fake-editor", r#"printf 'edited by script' > "$1""#);
    let editor = tools.path().join("fake-editor");
    let mut p = Pty::spawn_with_env(&[], &[("EDITOR", editor.to_str().unwrap()), ("VISUAL", "")]);
    p.wait(|p| p.shows("● ready"));
    p.send(b"draft text");
    p.wait(|p| p.screen_shows("draft text"));
    p.send(b"\x07");
    p.wait(|p| p.screen_shows("edited by script"));
    assert!(!p.screen_shows("draft text"));
    p.quit();
    p.finish();
}

#[test]
fn a_missing_editor_keeps_the_draft_and_the_screen() {
    let mut p = Pty::spawn_with_env(&[], &[("EDITOR", "/no/such/editor"), ("VISUAL", "")]);
    p.wait(|p| p.shows("● ready"));
    p.send(b"keep me");
    p.wait(|p| p.screen_shows("keep me"));
    p.send(b"\x07");
    p.wait(|p| p.screen_shows("Cannot start /no/such/editor"));
    assert!(p.screen_shows("keep me"));
    p.send(b"!");
    p.wait(|p| p.screen_shows("keep me!"), );
    p.quit();
    p.finish();
}
```

The last two lines prove keys reach Octet again after the failed start.

- [ ] **Step 6: Run and watch them fail**

Run: `scripts/rust-env.sh cargo test -p octet --test terminal -- ctrl_g_edits a_missing_editor`
Expected: FAIL — Ctrl+G does nothing yet, so the waits time out.

- [ ] **Step 7: Implement the editor round trip in `lib.rs`**

Split `TerminalGuard::suspend` so the re-entry is reusable:

```rust
    fn resume(&self) -> io::Result<()> {
        enable_raw_mode()?;
        execute!(io::stdout(), EnterAlternateScreen, EnableBracketedPaste)
    }
    fn suspend(&self) -> io::Result<()> {
        Self::restore();
        // SAFETY: raise only delivers SIGSTOP to this process.
        unsafe {
            libc::raise(libc::SIGSTOP);
        }
        self.resume()
    }
```

Add to `enum Action`:

```rust
    /// Hand the terminal to the user's editor for the draft.
    ExternalEditor,
```

In `key_action`'s main match, next to Ctrl+X:

```rust
        KeyCode::Char('g') if ctrl => return Action::ExternalEditor,
```

In `run_session`:

1. Change `let mut input = InputReader::new();` to `let mut input = Some(InputReader::new());`, and the input branch's future from `input.events.recv()` to:

```rust
            event = async {
                match input.as_mut() {
                    Some(reader) => reader.events.recv().await,
                    None => std::future::pending().await,
                }
            } => {
```

2. Add beside `index_task`:

```rust
    // The external editor, while it has the terminal.
    let mut editing: Option<(tokio::process::Child, external::Edit)> = None;
```

3. Skip painting while editing: change the paint condition to `if dirty && editing.is_none() && last_paint.elapsed() >= frame_time {`, and the frame-timer branch's guard to `if dirty && editing.is_none()`.

4. Handle the action:

```rust
                    Action::ExternalEditor => match external::prepare(&app.editor.text, external::editor_command()) {
                        Err(error) => app.notice(error),
                        Ok(edit) => {
                            // Stop reading keys so the editor gets them all.
                            input = None;
                            TerminalGuard::restore();
                            match tokio::process::Command::new(&edit.program).args(&edit.args).arg(&edit.path).spawn() {
                                Ok(child) => editing = Some((child, edit)),
                                Err(error) => {
                                    guard.resume()?;
                                    terminal.resize(terminal.size()?.into())?;
                                    input = Some(InputReader::new());
                                    app.notice(format!("Cannot start {}: {error}", edit.program));
                                }
                            }
                        }
                    },
```

5. Add the branch that ends editing:

```rust
            status = async {
                match editing.as_mut() {
                    Some((child, _)) => child.wait().await,
                    None => std::future::pending().await,
                }
            } => {
                if let Some((_, edit)) = editing.take() {
                    guard.resume()?;
                    terminal.resize(terminal.size()?.into())?;
                    input = Some(InputReader::new());
                    match edit.finish(status.is_ok_and(|status| status.success())) {
                        Ok(text) => app.editor.set(text),
                        Err(error) => app.notice(error),
                    }
                }
                dirty = true;
            }
```

Vendor events keep arriving through the existing `session.events` branch while `editing` is `Some`; they update `app` and set `dirty`, and the screen repaints once the editor exits.

Help text: after the `@` line, insert `Ctrl+G write the prompt in $EDITOR\n`.

- [ ] **Step 8: Run the tests**

Run: `scripts/rust-env.sh cargo test -p octet --test terminal -- ctrl_g_edits a_missing_editor` then `scripts/rust-env.sh cargo test -p octet-tui --lib`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add crates/octet-tui/src crates/octet/tests/terminal.rs
git commit -F - <<'EOF'
Write the prompt in an external editor with Ctrl+G

The draft goes to a private temp file and $VISUAL, $EDITOR or vi gets
the terminal. Octet stops reading keys and drawing meanwhile but keeps
handling vendor events, so a long edit cannot stall the session. A
failed or oversized edit keeps the original draft.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 7: Documentation and final verification

**Files:**
- Modify: `docs/tui.md`, `README.md`, `docs/rust/tui-features.md`, `docs/remote-control.md`, `CHANGELOG.md`

- [ ] **Step 1: Update `docs/tui.md`**

In the key table, add rows:

```markdown
| `@` | Mention a workspace file; a popup suggests paths as you type (Enter or Tab inserts, Esc closes) |
| Tab | Complete a path, or a `/command` at the start of the prompt |
| Ctrl+G | Write the prompt in `$VISUAL` or `$EDITOR` (default `vi`) |
| Ctrl+X | Copy the last reply to the clipboard (same as `/copy`) |
```

Add `/copy` to the commands line, and a new paragraph after the commands paragraph:

```markdown
A prompt starting with `!` runs the rest as a shell command in the workspace:
`!git status` shows the output in the conversation as a `SHELL` entry and
attaches it to your next prompt (the prompt box title shows
`+ git status (exit 0)`); `!!git status` shows it without attaching. The
vendor receives the output as a fenced block after your text; the
conversation and the journal show `[+ git status]`. Commands run with your
shell and your permissions, without an approval, with stdin closed. Each
keeps its last 32 KiB of output and stops after 10 minutes; Esc stops it
sooner. Attachments wait up to 32 KiB in total; Esc on an empty prompt
removes them. `cd` and `export` do not carry over between commands.

`@` inserts the path only; the vendor reads the file with its own tools.
The file list comes from `git ls-files` (tracked and untracked, not
ignored) or, outside git, a walk that skips hidden folders, `target` and
`node_modules`, up to 50,000 files. While Ctrl+G's editor is open, Octet
keeps receiving the vendor's output and repaints when you return; an
approval that arrives meanwhile rings the bell and its timer keeps running.

`/copy` uses the OSC 52 escape. Inside tmux it needs
`set -g set-clipboard on`; mosh 1.4 and later pass it on; terminal and
phone apps vary in whether they accept it.
```

Update the "Preview scope" implemented list to add: `@` file mentions, Tab completion, `!` shell commands with attachments, an external editor, `/copy`.

- [ ] **Step 2: Update the README**

In **In the terminal**, replace the editor bullet with:

```markdown
- A multiline editor with Unicode-aware editing, bracketed paste and history.
  `@` suggests workspace files, Tab completes paths and commands, Ctrl+G
  opens the prompt in your own editor, and the prompt box grows with the
  draft.
- `!git status` runs a command and attaches its output to your next prompt;
  `!!` runs it without attaching. `/copy` (Ctrl+X) puts the last reply on
  the clipboard, even over SSH to a phone.
```

Add to the **Keys** table: `| @ | Mention a file |`, `| Tab | Complete a path or command |`, `| Ctrl+G | Edit the prompt in $EDITOR |`, `| Ctrl+X | Copy the last reply |`. Add to the **Commands** table: ``| `/copy` | Copy the last reply to the clipboard |`` and ``| `!cmd`, `!!cmd` | Run a shell command; `!` attaches its output to the next prompt |``.

- [ ] **Step 3: Update `docs/rust/tui-features.md`, the phone guide and the CHANGELOG**

- `tui-features.md`, Prompt editor row: append "`@` file mentions with fuzzy search, Tab completion for paths and commands, Ctrl+G to edit in `$EDITOR`, and `!`/`!!` shell commands whose output can be attached to the next prompt." Add a row: `| Copy | /copy or Ctrl+X copies the last reply through OSC 52, including over tmux and mosh. |`
- `docs/remote-control.md`, in the tmux setup step after the colour lines, add: ``Add `set -g set-clipboard on` so `/copy` reaches your phone's clipboard.``
- `CHANGELOG.md`, under `### Added`: "- `@` file mentions, Tab completion for paths and commands, `!`/`!!` shell commands with output attached to the next prompt, Ctrl+G to write the prompt in an external editor, and `/copy` (Ctrl+X) through OSC 52."

- [ ] **Step 4: Check links and run the full gate**

Run:

```bash
python3 - <<'EOF'
import re, os
for f in ['README.md', 'docs/tui.md', 'docs/rust/tui-features.md', 'docs/remote-control.md', 'CHANGELOG.md']:
    for link in re.findall(r'\]\(([^)\s]+)\)', open(f).read()):
        if not link.startswith(('http', '#')) and not os.path.exists(os.path.normpath(os.path.join(os.path.dirname(f), link.split('#')[0]))):
            print('BROKEN', f, link)
EOF
make rust-check
```

Expected: no `BROKEN` lines; `make rust-check` exits 0 with every test passing (the three pre-existing ignored tests stay ignored).

- [ ] **Step 5: Commit**

```bash
git add README.md docs CHANGELOG.md
git commit -F - <<'EOF'
Document @ mentions, Tab, ! commands, Ctrl+G and /copy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```
