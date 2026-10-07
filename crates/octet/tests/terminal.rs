//! Real PTY acceptance tests. No browser or vendor login is needed.
#![cfg(unix)]
// Test code: an unwrap that fails is the test failing.
#![allow(clippy::unwrap_used)]
use std::{
    fs::{self, File},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::process::CommandExt,
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};
struct Pty {
    master: File,
    _slave: File,
    child: Child,
    directory: PathBuf,
    original: libc::tcflag_t,
    output: Vec<u8>,
}
impl Pty {
    fn spawn() -> Self {
        Self::spawn_with(&[])
    }
    fn spawn_with(args: &[&str]) -> Self {
        Self::spawn_with_env(args, &[])
    }
    fn spawn_with_env(args: &[&str], env: &[(&str, &str)]) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "octet-pty-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let mut master = -1;
        let mut slave = -1;
        let mut size = libc::winsize {
            ws_row: 36,
            ws_col: 120,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        assert_eq!(
            // SAFETY: openpty writes two descriptors into the locals above; a null name and termios are allowed, and `size` lives through the call.
            unsafe {
                libc::openpty(
                    &mut master,
                    &mut slave,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::addr_of_mut!(size),
                )
            },
            0
        );
        // SAFETY: openpty succeeded, so `master` is an open descriptor owned by nothing else.
        let master = unsafe { File::from_raw_fd(master) };
        // SAFETY: likewise `slave`.
        let slave = unsafe { File::from_raw_fd(slave) };
        let original = flags(&master);
        // SAFETY: fcntl only changes the flags of the descriptor `master` owns.
        unsafe {
            assert_ne!(
                libc::fcntl(master.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK),
                -1
            );
        }
        let mut command = Command::new(env!("CARGO_BIN_EXE_octet"));
        command
            .args(["--engine", "demo", "--journal-dir"])
            .arg(&directory)
            .args(args)
            .env("TERM", "xterm-256color")
            .env_remove("NO_COLOR")
            .envs(env.iter().copied())
            .stdin(Stdio::from(slave.try_clone().unwrap()))
            .stdout(Stdio::from(slave.try_clone().unwrap()))
            .stderr(Stdio::from(slave.try_clone().unwrap()));
        // SAFETY: the closure runs in the child before exec and calls only async-signal-safe functions (setsid, ioctl).
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = command.spawn().unwrap();
        Self {
            master,
            _slave: slave,
            child,
            directory,
            original,
            output: Vec::new(),
        }
    }
    fn drain(&mut self) {
        let mut bytes = [0; 8192];
        while let Ok(n) = self.master.read(&mut bytes) {
            if n == 0 {
                break;
            }
            self.output.extend_from_slice(&bytes[..n]);
        }
    }
    fn wait(&mut self, condition: impl Fn(&Self) -> bool) {
        self.wait_for(Duration::from_secs(8), condition);
    }
    /// Drains until the screen has been quiet for 100 ms (at most 3 s), so
    /// what follows is not mistaken for a repaint still in flight.
    fn settle(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut quiet_since = Instant::now();
        let mut seen = self.output.len();
        while Instant::now() < deadline {
            self.drain();
            if self.output.len() != seen {
                seen = self.output.len();
                quiet_since = Instant::now();
            } else if quiet_since.elapsed() >= Duration::from_millis(100) {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn wait_for(&mut self, duration: Duration, condition: impl Fn(&Self) -> bool) {
        let deadline = Instant::now() + duration;
        loop {
            self.drain();
            if condition(self) {
                return;
            }
            assert!(
                Instant::now() < deadline,
                "PTY condition timed out: flags={:x} original={:x} {}",
                flags(&self.master),
                self.original,
                String::from_utf8_lossy(&self.output)
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn send(&mut self, text: &[u8]) {
        self.master.write_all(text).unwrap();
    }
    fn records(&self) -> Vec<serde_json::Value> {
        let Ok(files) = fs::read_dir(&self.directory) else {
            return vec![];
        };
        files
            .filter_map(Result::ok)
            .filter_map(|f| fs::read_to_string(f.path()).ok())
            .flat_map(|text| {
                text.lines()
                    .filter_map(|line| serde_json::from_str(line).ok())
                    .collect::<Vec<_>>()
            })
            .collect()
    }
    fn count(&self, kind: &str) -> usize {
        self.records().iter().filter(|v| v["type"] == kind).count()
    }
    fn shows(&self, text: &str) -> bool {
        self.row_shows(2, text)
    }
    fn row_shows(&self, wanted: usize, text: &str) -> bool {
        self.screen()[wanted - 1].contains(text)
    }
    fn screen_shows(&self, text: &str) -> bool {
        self.screen().iter().any(|row| row.contains(text))
    }
    /// The 120 × 36 screen rebuilt from everything written so far. Ratatui
    /// reuses unchanged cells, so text arrives as many cursor-positioned
    /// writes; replaying them gives each row as a person would see it.
    fn screen(&self) -> Vec<String> {
        let output = String::from_utf8_lossy(&self.output);
        let mut chars = output.chars().peekable();
        let (mut row, mut column) = (1usize, 1usize);
        let mut cells = vec![vec![' '; 120]; 36];
        while let Some(ch) = chars.next() {
            if ch == '\u{1b}' && chars.next() == Some('[') {
                let mut parameters = String::new();
                for ch in chars.by_ref() {
                    if ('@'..='~').contains(&ch) {
                        if ch == 'H' || ch == 'f' {
                            let mut values = parameters.split(';');
                            row = values.next().and_then(|v| v.parse().ok()).unwrap_or(1);
                            column = values.next().and_then(|v| v.parse().ok()).unwrap_or(1);
                        } else if ch == 'J' && parameters == "2" {
                            for line in &mut cells {
                                line.fill(' ');
                            }
                        }
                        break;
                    }
                    parameters.push(ch);
                }
            } else if ch == '\r' {
                column = 1;
            } else if ch == '\n' {
                row += 1;
            } else if !ch.is_control() {
                if (1..=cells.len()).contains(&row) && (1..=120).contains(&column) {
                    cells[row - 1][column - 1] = ch;
                }
                column += 1;
            }
        }
        cells.into_iter().map(String::from_iter).collect()
    }
    fn goal(&self) -> Option<serde_json::Value> {
        fs::read_dir(&self.directory)
            .ok()?
            .filter_map(Result::ok)
            .find(|entry| entry.file_name().to_string_lossy().starts_with("goal-"))
            .and_then(|entry| fs::read(entry.path()).ok())
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
    }
    /// Quits the way a person does: Ctrl+C until Octet exits. Early presses
    /// may cancel a turn or clear the draft; two in a row on an idle, empty
    /// prompt quit.
    fn quit(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while self.child.try_wait().unwrap().is_none() {
            assert!(Instant::now() < deadline, "Ctrl+C did not quit Octet");
            // Once Octet restores canonical mode, another Ctrl+C is a
            // terminal signal that can flush its final restoration output.
            // Let shutdown finish without injecting keys into the shell.
            if flags(&self.master) != self.original {
                self.send(b"\x03");
            }
            for _ in 0..15 {
                self.drain();
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
    fn finish(&mut self) {
        self.wait(|p| flags(&p.master) == p.original);
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            self.drain();
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(
                    status.success(),
                    "child status={status}; output={}",
                    String::from_utf8_lossy(&self.output)
                );
                break;
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
        // The child can finish between drain() and try_wait(). Capture the
        // terminal restoration bytes it wrote in that interval as well.
        self.drain();
        assert!(
            self.output.windows(8).any(|w| w == b"\x1b[?1049l"),
            "alternate screen was not restored; output tail={:?}",
            String::from_utf8_lossy(&self.output[self.output.len().saturating_sub(512)..])
        );
    }
}
fn flags(file: &File) -> libc::tcflag_t {
    let mut term = std::mem::MaybeUninit::uninit();
    assert_eq!(
        // SAFETY: tcgetattr fills `term` from an open descriptor; it is read only after a 0 return.
        unsafe { libc::tcgetattr(file.as_raw_fd(), term.as_mut_ptr()) },
        0
    );
    // PENDIN is kernel-owned pending-input state, not a terminal mode. macOS
    // may set it when canonical mode is restored with input in flight.
    // SAFETY: tcgetattr returned 0 above, so `term` is initialised.
    unsafe { term.assume_init() }.c_lflag & !libc::PENDIN
}
impl Drop for Pty {
    fn drop(&mut self) {
        let _ = self.child.kill();
        // A killed child cannot finish exiting until its terminal output is
        // read, so a failed assertion would otherwise hang here.
        let deadline = Instant::now() + Duration::from_secs(5);
        while matches!(self.child.try_wait(), Ok(None)) && Instant::now() < deadline {
            self.drain();
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = fs::remove_dir_all(&self.directory);
    }
}
#[test]
fn real_terminal_handles_paste_approval_resize_suspend_and_quit() {
    let mut p = Pty::spawn();
    p.wait(|p| p.count("ready") == 1);
    p.wait(|p| p.output.windows(5).any(|w| w == b"ready"));
    assert_eq!(flags(&p.master) & libc::ICANON, 0);
    p.send("\x1b[200~Hello 世界\nsecond line\x1b[201~\r".as_bytes());
    p.wait(|p| p.count("finished") == 1);
    p.wait(|p| p.output.windows(9).any(|w| w == b"completed"));
    assert!(p
        .records()
        .iter()
        .any(|v| v["type"] == "user" && v["data"] == "Hello 世界\nsecond line"));
    p.send(b"/approval-demo\r");
    p.wait(|p| p.count("approval") == 1);
    p.wait(|p| p.output.windows(19).any(|w| w == b"Permission required"));
    p.send(b"d");
    p.wait(|p| p.count("finished") == 2);
    let size = libc::winsize {
        ws_row: 18,
        ws_col: 45,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    // SAFETY: TIOCSWINSZ reads `size`, a local winsize that outlives the call.
    unsafe {
        assert_eq!(
            libc::ioctl(p.master.as_raw_fd(), libc::TIOCSWINSZ as _, &size),
            0
        );
    }
    p.send(b"\x1a");
    p.wait(|p| flags(&p.master) == p.original);
    let mut stopped = 0;
    assert_eq!(
        // SAFETY: waitpid writes only into `stopped`, for our own child.
        unsafe { libc::waitpid(p.child.id() as i32, &mut stopped, libc::WUNTRACED) },
        p.child.id() as i32
    );
    assert!(libc::WIFSTOPPED(stopped));
    // SAFETY: kill only signals our own child.
    unsafe {
        libc::kill(p.child.id() as i32, libc::SIGCONT);
    }

    p.wait(|p| flags(&p.master) & libc::ICANON == 0);
    p.settle();
    p.quit();
    p.finish();
}
#[test]
fn sigterm_restores_terminal_during_a_turn() {
    let mut p = Pty::spawn();
    p.wait(|p| p.count("ready") == 1);
    p.wait(|p| p.output.windows(5).any(|w| w == b"ready"));
    p.send(b"hello\r");
    p.wait(|p| p.count("started") == 1);
    // SAFETY: kill only signals our own child.
    unsafe {
        libc::kill(p.child.id() as i32, libc::SIGTERM);
    }
    p.finish();
}

#[test]
#[ignore = "manual release-mode resource diagnostic"]
fn terminal_idle_diagnostic() {
    let started = Instant::now();
    let mut p = Pty::spawn();
    p.wait(|p| p.count("ready") == 1);
    p.wait(|p| p.output.windows(5).any(|w| w == b"ready"));
    let startup = started.elapsed();
    for _ in 0..30 {
        std::thread::sleep(Duration::from_millis(10));
        p.drain();
    }
    let before = p.output.len();
    let resource = |pid: u32| {
        String::from_utf8(
            Command::new("ps")
                .args(["-p", &pid.to_string(), "-o", "rss=", "-o", "time="])
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap()
    };
    let first = resource(p.child.id());
    std::thread::sleep(Duration::from_secs(5));
    p.drain();
    let last = resource(p.child.id());
    assert_eq!(before, p.output.len(), "idle TUI must not repaint");
    eprintln!(
        "demo-ready={startup:?}; idle repaint bytes=0; RSS(KiB)/CPU before={}; after={}",
        first.trim(),
        last.trim()
    );
    p.quit();
    p.finish();
}

#[test]
fn corrupt_goal_does_not_prevent_chat_or_explicit_recovery() {
    let directory = std::env::temp_dir().join(format!("octet-corrupt-goal-{}", std::process::id()));
    let workspace = std::env::current_dir().unwrap().canonicalize().unwrap();
    let store = octet_core::goal::GoalStore::new(&directory, &workspace);
    let goal = octet_core::goal::Goal::new("recover this goal").unwrap();
    tokio::runtime::Runtime::new()
        .unwrap()
        .block_on(store.save(&goal))
        .unwrap();
    let path = fs::read_dir(&directory)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    fs::write(&path, b"{broken goal").unwrap();
    let mut p = Pty::spawn_with(&["--journal-dir", directory.to_str().unwrap()]);
    p.directory = directory;
    p.wait(|p| p.shows("● ready"));
    p.send(b"/goal status\r");
    p.send(b"hello\r");
    p.wait(|p| p.count("finished") == 1 && p.shows("● completed"));
    assert_eq!(fs::read(&path).unwrap(), b"{broken goal");
    p.send(b"/goal clear\r");
    p.wait(|_| !path.exists());
    p.quit();
    p.finish();
}

fn provider_fixture() -> PathBuf {
    octet_testkit::protocol_child()
}

#[test]
fn goals_continue_complete_and_reload_for_both_providers_in_every_mode() {
    let binary = provider_fixture();
    for engine in ["codex", "claude"] {
        for mode in ["ask", "accept-edits", "auto", "full-access"] {
            let args = [
                "--engine",
                engine,
                "--binary",
                binary.to_str().unwrap(),
                "--mode",
                mode,
            ];
            let mut p = Pty::spawn_with(&args);
            p.wait(|p| p.count("ready") == 1);
            p.send(b"/goal fixture-goal\r");
            p.wait(|p| p.goal().is_some_and(|g| g["status"] == "complete"));
            assert_eq!(p.count("finished"), 2, "{engine}/{mode}");
            assert_eq!(p.goal().unwrap()["turns"], 2);
            assert_eq!(
                p.goal().unwrap()["evidence"],
                "Verified both steps and their tests."
            );
            // A new session loads durable completion without starting inference.
            let ready = p.count("ready");
            p.send(b"/new\r");
            p.wait(|p| p.count("ready") > ready);
            assert_eq!(p.count("user"), 2);
            p.send(b"/goal status\r");
            p.send(b"/goal clear\r");
            p.wait(|p| p.goal().is_none());
            p.quit();
            p.finish();
        }
    }
}

#[test]
fn goals_pause_cancel_resume_audit_and_stop_on_failure_for_both_providers() {
    let binary = provider_fixture();
    for engine in ["codex", "claude"] {
        let mut p = Pty::spawn_with(&[
            "--engine",
            engine,
            "--binary",
            binary.to_str().unwrap(),
            "--mode",
            "auto",
        ]);
        p.wait(|p| p.count("ready") == 1);
        p.send(b"/goal fixture-hold\r");
        p.wait(|p| p.count("started") == 1 && p.shows("● working"));
        p.send(b"/goal pause\r");
        p.wait(|p| p.goal().is_some_and(|g| g["status"] == "paused"));
        p.send(b"\x03");
        p.wait(|p| p.count("finished") == 1 && p.shows("● interrupted"));
        p.send(b"/goal resume\r");
        p.wait(|p| p.count("started") == 2 && p.shows("● working"));
        p.send(b"\x03");
        p.wait(|p| p.count("finished") == 2 && p.shows("● interrupted"));
        assert_eq!(p.goal().unwrap()["status"], "paused");
        assert_eq!(
            p.goal().unwrap()["turns"],
            2,
            "paused and cancelled {engine} turns must be counted"
        );
        p.send(b"/goal fixture-fail\r");
        p.wait(|p| p.count("finished") == 3 && p.shows("● failed"));
        assert_eq!(p.goal().unwrap()["status"], "paused");
        assert_eq!(
            p.goal().unwrap()["turns"],
            1,
            "failed {engine} turn must be counted"
        );
        p.send(b"/goal fixture-goal\r");
        p.wait(|p| p.goal().is_some_and(|g| g["status"] == "complete"));
        p.send(b"/goal clear\r");
        p.wait(|p| p.goal().is_none());
        // Seed a paused objective to test the audit path independently of continuation.
        // Canonical, as Octet keys the store, so a symlinked checkout matches.
        let workspace = std::env::current_dir().unwrap().canonicalize().unwrap();
        let store = octet_core::goal::GoalStore::new(&p.directory, &workspace);
        let mut goal = octet_core::goal::Goal::new("fixture-goal").unwrap();
        goal.status = octet_core::goal::Status::Paused;
        tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(store.save(&goal))
            .unwrap();
        let ready = p.count("ready");
        p.send(b"/new\r");
        p.wait(|p| p.count("ready") > ready && p.shows("● ready"));
        let turns = p.count("started");
        assert_eq!(turns, 5);
        p.send(b"/goal complete\r");
        p.wait(|p| p.goal().is_some_and(|g| g["status"] == "complete"));
        assert_eq!(p.count("started"), turns + 1);
        p.quit();
        p.finish();
    }
}

#[test]
#[ignore = "uses installed vendor CLIs and their subscriptions"]
fn installed_providers_complete_a_goal_in_auto_mode() {
    let engines = std::env::var("OCTET_LIVE_ENGINE").ok();
    for engine in ["codex", "claude"]
        .into_iter()
        .filter(|engine| engines.as_deref().is_none_or(|chosen| chosen == *engine))
    {
        let workspace =
            std::env::temp_dir().join(format!("octet-live-goal-{}-{engine}", std::process::id()));
        fs::create_dir_all(&workspace).unwrap();
        let mut args = vec![
            "--engine",
            engine,
            "--binary",
            engine,
            "--mode",
            "auto",
            "--cwd",
            workspace.to_str().unwrap(),
        ];
        let model = std::env::var(format!("OCTET_LIVE_{}_MODEL", engine.to_uppercase())).ok();
        if let Some(model) = &model {
            args.extend(["--model", model.as_str()]);
        }
        let mut p = Pty::spawn_with(&args);
        p.wait_for(Duration::from_secs(45), |p| {
            p.count("ready") == 1 || p.count("error") > 0
        });
        assert_eq!(p.count("error"), 0, "{engine}: {:?}", p.records());
        assert!(
            p.records()
                .iter()
                .any(|v| v["type"] == "mode" && v["data"] == "auto"),
            "{engine}: {:?}",
            p.records()
        );
        p.send(b"/goal In this disposable workspace create smoke.txt containing exactly OCTET_OK followed by a newline. Read it back and verify its contents. Keep the task limited to this file.\r");
        p.wait_for(Duration::from_secs(180), |p| {
            p.goal().is_some_and(|g| g["status"] != "active") || p.count("error") > 0
        });
        let goal = p.goal();
        p.quit();
        p.finish();
        assert_eq!(p.count("error"), 0, "{engine}: {:?}", p.records());
        assert_eq!(
            goal.unwrap()["status"],
            "complete",
            "{engine}: {:?}",
            p.records()
        );
        assert_eq!(
            fs::read_to_string(workspace.join("smoke.txt")).unwrap(),
            "OCTET_OK\n"
        );
        fs::remove_dir_all(workspace).unwrap();
    }
}
#[test]
#[ignore = "uses installed vendor CLIs and their subscriptions"]
fn installed_providers_receive_shell_attachments_and_copy_the_reply() {
    let chosen = std::env::var("OCTET_LIVE_ENGINE").ok();
    for engine in ["codex", "claude"]
        .into_iter()
        .filter(|engine| chosen.as_deref().is_none_or(|chosen| chosen == *engine))
    {
        let workspace = octet_testkit::TempDir::new("octet-live-attachment");
        fs::create_dir_all(workspace.path()).unwrap();
        let cwd = workspace.path().to_str().unwrap();
        let mut args = vec!["--engine", engine, "--binary", engine, "--cwd", cwd];
        let model = std::env::var(format!("OCTET_LIVE_{}_MODEL", engine.to_uppercase())).ok();
        if let Some(model) = &model {
            args.extend(["--model", model.as_str()]);
        }
        let mut p = Pty::spawn_with(&args);
        p.wait_for(Duration::from_secs(45), |p| {
            p.shows("● ready") || p.count("error") > 0
        });
        assert_eq!(p.count("error"), 0, "{engine}: {:?}", p.records());
        p.send(b"!printf OCTET_ATTACHMENT_OK\r");
        p.wait(|p| p.screen_shows("+ printf OCTET_ATTACHMENT_OK (exit 0)"));
        // The marker is absent from the question: only the attachment supplies it.
        p.send(b"Reply with only the exact output of the attached command. Do not use tools.\r");
        p.wait_for(Duration::from_secs(90), |p| {
            p.count("finished") == 1 || p.count("error") > 0
        });
        assert_eq!(p.count("error"), 0, "{engine}: {:?}", p.records());
        let records = p.records();
        let reply: String = records
            .iter()
            .filter(|record| record["type"] == "text")
            .filter_map(|record| record["data"].as_str())
            .collect();
        assert_eq!(reply.trim(), "OCTET_ATTACHMENT_OK", "{engine}");
        assert!(records.iter().any(|record| {
            record["type"] == "user"
                && record["data"]
                    .as_str()
                    .is_some_and(|text| text.ends_with("[+ printf OCTET_ATTACHMENT_OK]"))
        }));
        p.wait(|p| p.shows("● completed"));
        p.send(b"/copy\r");
        // OSC 52 contains base64 of the full marker, as read from the live reply.
        let clipboard = b"\x1b]52;c;T0NURVRfQVRUQUNITUVOVF9PSw==";
        p.wait(|p| p.output.windows(clipboard.len()).any(|w| w == clipboard));
        p.quit();
        p.finish();
    }
}

#[test]
fn auto_mode_skips_the_demo_dialog_and_shift_tab_cycles() {
    let mut p = Pty::spawn_with(&["--mode", "auto"]);
    p.wait(|p| p.count("ready") == 1);
    p.wait(|p| {
        p.records()
            .iter()
            .any(|v| v["type"] == "mode" && v["data"] == "auto")
    });
    p.send(b"/approval-demo\r");
    p.wait(|p| p.count("finished") == 1);
    assert_eq!(p.count("approval"), 0);
    p.send(b"\x1b[Z");
    p.wait(|p| {
        p.records()
            .iter()
            .any(|v| v["type"] == "mode" && v["data"] == "ask")
    });
    p.quit();
    p.finish();
}
#[test]
fn live_mode_change_survives_a_new_session() {
    let mut p = Pty::spawn_with(&["--mode", "auto"]);
    p.wait(|p| {
        p.records()
            .iter()
            .any(|v| v["type"] == "mode" && v["data"] == "auto")
    });
    p.send(b"\x1b[Z");
    p.wait(|p| {
        p.records()
            .iter()
            .any(|v| v["type"] == "mode" && v["data"] == "ask")
    });
    p.send(b"/new\r");
    p.wait(|p| p.count("session") == 2);
    let sessions: Vec<_> = p
        .records()
        .into_iter()
        .filter(|v| v["type"] == "session")
        .map(|v| v["data"]["mode"].as_str().unwrap_or("").to_owned())
        .collect();
    assert!(sessions.contains(&"auto".to_owned()), "{sessions:?}");
    assert!(sessions.contains(&"ask".to_owned()), "{sessions:?}");
    p.quit();
    p.finish();
}
#[test]
fn reconnect_keeps_the_visible_conversation() {
    let mut p = Pty::spawn();
    p.wait(|p| p.count("ready") == 1);
    p.send(b"remember-this-prompt\r");
    p.wait(|p| p.count("finished") == 1);
    // The journal is written before the TUI sees the event; wait for the screen.
    p.wait(|p| p.output.windows(9).any(|w| w == b"completed"));
    p.send(b"/reconnect\r");
    p.wait(|p| p.count("ready") == 2);
    // A resize repaints every cell, so the retained transcript must reappear.
    p.settle();
    let mark = p.output.len();
    let size = libc::winsize {
        ws_row: 30,
        ws_col: 100,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    // SAFETY: TIOCSWINSZ reads `size`, a local winsize that outlives the call.
    unsafe {
        assert_eq!(
            libc::ioctl(p.master.as_raw_fd(), libc::TIOCSWINSZ as _, &size),
            0
        );
    }
    p.wait(|p| {
        p.output[mark..]
            .windows(20)
            .any(|w| w == b"remember-this-prompt")
    });
    p.quit();
    p.finish();
}

#[test]
fn ctrl_c_twice_quits_and_the_first_press_only_warns() {
    let mut p = Pty::spawn();
    p.wait(|p| p.shows("● ready"));
    p.send(b"\x03");
    // The 120 × 36 screen's status line is row 35, above the bottom margin.
    p.wait(|p| p.row_shows(35, "Press Ctrl+C again to quit"));
    assert!(
        p.child.try_wait().unwrap().is_none(),
        "one Ctrl+C must not quit"
    );
    p.send(b"\x03");
    p.finish();
}

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

#[test]
fn remote_control_reports_a_ready_host_with_the_phone_command() {
    // Stand-ins for tmux, tailscale and mosh-server, first on the PATH, so
    // the real binary's checks run end to end without those tools installed.
    let tools = octet_testkit::TempDir::new("octet-remote-tools");
    fs::create_dir_all(tools.path()).unwrap();
    octet_testkit::write_script(
        tools.path(),
        "tmux",
        r#"case "$1 $3" in
  "display-message "*) echo octet ;;
  "show-options terminal-features") echo 'terminal-features[0] ,xterm-256color:RGB' ;;
esac"#,
    );
    octet_testkit::write_script(
        tools.path(),
        "tailscale",
        r#"echo '{"BackendState":"Running","Self":{"DNSName":"test-mac.tail0000.ts.net.","TailscaleIPs":["127.0.0.1"]}}'"#,
    );
    octet_testkit::write_script(
        tools.path(),
        "mosh-server",
        "echo 'mosh-server (mosh 1.4.0) [build mosh-1.4.0]'",
    );
    let path = octet_testkit::path_with(tools.path());
    let mut p = Pty::spawn_with_env(
        &[],
        &[("PATH", path.as_str()), ("TMUX", "/tmp/tmux-test,1,0")],
    );
    p.wait(|p| p.shows("● ready"));
    // The checks share a 2-second deadline; on a machine busy with the
    // parallel suite a stand-in can miss it, so ask again before failing.
    // Each attempt first runs a ! command, which replaces the status line,
    // so "Remote control:" there can only mean the new report.
    for attempt in 1..=3 {
        p.send(format!("!!echo attempt-{attempt}\r").as_bytes());
        p.wait(|p| p.row_shows(35, &format!("echo attempt-{attempt} · exit 0")));
        p.send(b"/remote-control\r");
        p.wait(|p| p.row_shows(35, "Remote control: "));
        if p.screen_shows("[ok] mosh-server 1.4.0") {
            break;
        }
        assert!(attempt < 3, "{}", p.screen().join("\n"));
    }
    assert!(p.screen_shows("[ok] Running in tmux session \"octet\""));
    assert!(p.screen_shows("[ok] Tailscale connected: test-mac.tail0000.ts.net"));
    assert!(!p.screen_shows("tmux reduces colours"));
    // Port 22 on 127.0.0.1 answers only if this machine runs an SSH server,
    // so either outcome is valid; the phone command follows it.
    if p.screen_shows("[ok] SSH answers") {
        assert!(p.screen_shows("tmux new -A -s octet"));
    } else {
        assert!(p.screen_shows("[!!] SSH doesn't answer"));
    }
    p.quit();
    p.finish();
}

#[test]
fn remote_control_keeps_the_screen_live_while_checks_run() {
    // Every check hangs to its deadline. The screen must keep painting and
    // taking keys meanwhile, then show the report when the checks end.
    let tools = octet_testkit::TempDir::new("octet-remote-slow");
    fs::create_dir_all(tools.path()).unwrap();
    for name in ["tmux", "tailscale", "mosh-server"] {
        octet_testkit::write_script(tools.path(), name, "exec sleep 10");
    }
    let path = octet_testkit::path_with(tools.path());
    let mut p = Pty::spawn_with_env(
        &[],
        &[("PATH", path.as_str()), ("TMUX", "/tmp/tmux-test,1,0")],
    );
    p.wait(|p| p.shows("● ready"));
    p.send(b"/remote-control\r");
    // Within 2 s, still inside the 2.5 s the checks take: the screen is live.
    p.wait_for(Duration::from_secs(2), |p| {
        p.screen_shows("Checking phone access")
    });
    p.send(b"typed meanwhile");
    p.wait_for(Duration::from_secs(2), |p| {
        p.screen_shows("typed meanwhile")
    });
    // The status line is painted after the conversation, so once the
    // summary shows, the whole report has.
    p.wait(|p| p.row_shows(35, "Remote control: 3 problems (report above)"));
    assert!(p.screen_shows("tmux didn't answer"));
    p.quit();
    p.finish();
}

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
    // The demo reply starts "This": base64 of "T" begins with "V".
    p.wait(|p| p.output.windows(8).any(|w| w == b"\x1b]52;c;V"));
    p.wait(|p| p.screen_shows("to the clipboard"));
    p.quit();
    p.finish();
}

#[test]
fn bang_runs_a_command_and_attaches_it_to_the_next_prompt() {
    let mut p = Pty::spawn();
    p.wait(|p| p.shows("● ready"));
    p.send(b"!echo hi-from-shell\r");
    p.wait(|p| p.screen_shows("$ echo hi-from-shell"));
    p.wait(|p| p.screen_shows("+ echo hi-from-shell (exit 0)"));
    p.send(b"what does it say\r");
    p.wait(|p| p.screen_shows("[+ echo hi-from-shell]"));
    // The engine received the output itself; the demo shows what it got.
    p.wait(|p| p.screen_shows("Output of `echo hi-from-shell` (exit 0):"));
    p.wait(|p| p.shows("● completed"));
    p.wait(|p| !p.screen_shows("(exit 0) "));
    p.send(b"!!echo local-only\r");
    p.wait(|p| p.screen_shows("$ echo local-only"));
    assert!(!p.screen_shows("+ echo local-only"));
    p.quit();
    p.finish();
}

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

#[test]
fn ctrl_g_edits_the_draft_in_an_external_editor() {
    let tools = octet_testkit::TempDir::new("octet-editor");
    fs::create_dir_all(tools.path()).unwrap();
    octet_testkit::write_script(
        tools.path(),
        "fake-editor",
        r#"printf 'edited by script' > "$1""#,
    );
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
    // The screen repaints in full after the failed start; wait for all of it.
    p.wait(|p| p.screen_shows("Cannot start /no/such/editor") && p.screen_shows("keep me"));
    // Keys reach Octet again after the failed start.
    p.send(b"!");
    p.wait(|p| p.screen_shows("keep me!"));
    p.quit();
    p.finish();
}

#[test]
fn bang_commands_cannot_reach_the_terminal() {
    // Credential prompts (git, ssh, sudo) open /dev/tty; a ! command must
    // fail at once instead of drawing over the screen and stopping.
    let mut p = Pty::spawn();
    p.wait(|p| p.shows("● ready"));
    p.send(b"!printf 'Username: ' > /dev/tty; echo status=$?\r");
    p.wait(|p| p.screen_shows("status="));
    assert!(!p.screen_shows("status=0"), "{}", p.screen().join("\n"));
    // The command line itself shows the text; nothing else may.
    assert!(p
        .screen()
        .iter()
        .filter(|row| row.contains("Username: "))
        .all(|row| row.contains("printf")));
    p.quit();
    p.finish();
}

#[test]
fn an_interrupt_while_editing_does_not_quit() {
    // `code --wait` leaves the terminal cooked, so Ctrl+C there signals the
    // whole group; Octet must keep the session and the edit.
    let tools = octet_testkit::TempDir::new("octet-editor-int");
    fs::create_dir_all(tools.path()).unwrap();
    octet_testkit::write_script(
        tools.path(),
        "fake-editor",
        r#"trap '' INT; kill -INT 0; sleep 0.2; printf 'after interrupt' > "$1""#,
    );
    let editor = tools.path().join("fake-editor");
    let mut p = Pty::spawn_with_env(&[], &[("EDITOR", editor.to_str().unwrap()), ("VISUAL", "")]);
    p.wait(|p| p.shows("● ready"));
    p.send(b"\x07");
    p.wait(|p| p.screen_shows("after interrupt"));
    assert!(p.child.try_wait().unwrap().is_none(), "Octet quit");
    p.quit();
    p.finish();
}

#[test]
fn a_suspend_while_editing_leaves_the_terminal_usable() {
    // vim's Ctrl+Z stops the whole group; on fg the editor restores the
    // terminal it found, and Octet must still get raw keys afterwards.
    let tools = octet_testkit::TempDir::new("octet-editor-tstp");
    fs::create_dir_all(tools.path()).unwrap();
    octet_testkit::write_script(
        tools.path(),
        "fake-editor",
        r#"trap '' TSTP; kill -TSTP 0; sleep 0.5; stty sane; printf 'after stop' > "$1""#,
    );
    let editor = tools.path().join("fake-editor");
    let mut p = Pty::spawn_with_env(&[], &[("EDITOR", editor.to_str().unwrap()), ("VISUAL", "")]);
    p.wait(|p| p.shows("● ready"));
    p.send(b"\x07");
    // Play the shell's part: continue Octet whenever it stops.
    let pid = p.child.id() as libc::pid_t;
    let deadline = Instant::now() + Duration::from_secs(8);
    while !p.screen_shows("after stop") {
        assert!(Instant::now() < deadline, "the edit never came back");
        // SAFETY: kill only signals the child this test started.
        unsafe {
            libc::kill(pid, libc::SIGCONT);
        }
        p.drain();
        std::thread::sleep(Duration::from_millis(50));
    }
    // Octet is back in raw mode: no line editing, no echo. (Typing would not
    // show it: the terminal's own echo looks like Octet drawing the key.)
    p.wait(|p| flags(&p.master) & (libc::ICANON | libc::ECHO) == 0);
    p.quit();
    p.finish();
}

#[test]
fn terminating_octet_while_editing_stops_the_editor() {
    let tools = octet_testkit::TempDir::new("octet-editor-term");
    fs::create_dir_all(tools.path()).unwrap();
    let pid_file = tools.path().join("editor.pid");
    octet_testkit::write_script(
        tools.path(),
        "fake-editor",
        &format!(
            // Ignoring HUP stands in for a real shell, where the kernel's
            // hangup to the old session does not reach the editor.
            r#"echo $$ > '{}'; trap '' HUP; trap 'exit 0' TERM; sleep 30 & wait"#,
            pid_file.display()
        ),
    );
    let editor = tools.path().join("fake-editor");
    let mut p = Pty::spawn_with_env(&[], &[("EDITOR", editor.to_str().unwrap()), ("VISUAL", "")]);
    p.wait(|p| p.shows("● ready"));
    p.send(b"\x07");
    let read_pid = || {
        fs::read_to_string(&pid_file)
            .ok()
            .and_then(|text| text.trim().parse::<libc::pid_t>().ok())
    };
    assert!(
        octet_testkit::wait_until(Duration::from_secs(5), || read_pid().is_some()),
        "the editor never started"
    );
    let editor_pid = read_pid().unwrap();
    // SAFETY: kill only signals the child this test started.
    unsafe {
        libc::kill(p.child.id() as libc::pid_t, libc::SIGTERM);
    }
    p.finish();
    // SAFETY: signal 0 only checks that the process exists.
    let gone = || unsafe { libc::kill(editor_pid, 0) } != 0;
    assert!(
        octet_testkit::wait_until(Duration::from_secs(3), gone),
        "the editor outlived Octet"
    );
}
