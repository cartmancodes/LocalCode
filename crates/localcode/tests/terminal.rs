//! Real PTY acceptance tests. No Python, browser, or vendor login is needed.
#![cfg(unix)]
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
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "lc-pty-{}-{}",
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
        let master = unsafe { File::from_raw_fd(master) };
        let slave = unsafe { File::from_raw_fd(slave) };
        let original = flags(&master);
        unsafe {
            assert_ne!(
                libc::fcntl(master.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK),
                -1
            );
        }
        let mut command = Command::new(env!("CARGO_BIN_EXE_localcode"));
        command
            .args(["--engine", "demo", "--journal-dir"])
            .arg(&directory)
            .args(args)
            .env("TERM", "xterm-256color")
            .env_remove("NO_COLOR")
            .stdin(Stdio::from(slave.try_clone().unwrap()))
            .stdout(Stdio::from(slave.try_clone().unwrap()))
            .stderr(Stdio::from(slave.try_clone().unwrap()));
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
        // Ratatui reuses unchanged cells, so a status can be split across many
        // cursor-positioned writes. Reconstruct the ASCII/status header row.
        let output = String::from_utf8_lossy(&self.output);
        let mut chars = output.chars().peekable();
        let (mut row, mut column) = (1usize, 1usize);
        let mut header = vec![' '; 120];
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
                            header.fill(' ');
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
                if row == 2 && column > 0 && column <= header.len() {
                    header[column - 1] = ch;
                }
                column += 1;
            }
        }
        header.into_iter().collect::<String>().contains(text)
    }
    fn goal(&self) -> Option<serde_json::Value> {
        fs::read_dir(&self.directory)
            .ok()?
            .filter_map(Result::ok)
            .find(|entry| entry.file_name().to_string_lossy().starts_with("goal-"))
            .and_then(|entry| fs::read(entry.path()).ok())
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
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
        assert!(self.output.windows(8).any(|w| w == b"\x1b[?1049l"));
    }
}
fn flags(file: &File) -> libc::tcflag_t {
    let mut term = std::mem::MaybeUninit::uninit();
    assert_eq!(
        unsafe { libc::tcgetattr(file.as_raw_fd(), term.as_mut_ptr()) },
        0
    );
    // PENDIN is kernel-owned pending-input state, not a terminal mode. macOS
    // may set it when canonical mode is restored with input in flight.
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
        unsafe { libc::waitpid(p.child.id() as i32, &mut stopped, libc::WUNTRACED) },
        p.child.id() as i32
    );
    assert!(libc::WIFSTOPPED(stopped));
    unsafe {
        libc::kill(p.child.id() as i32, libc::SIGCONT);
    }

    p.wait(|p| flags(&p.master) & libc::ICANON == 0);
    for _ in 0..20 {
        p.drain();
        std::thread::sleep(Duration::from_millis(10));
    }
    p.send(b"\x11");
    p.finish();
}
#[test]
fn sigterm_restores_terminal_during_a_turn() {
    let mut p = Pty::spawn();
    p.wait(|p| p.count("ready") == 1);
    p.wait(|p| p.output.windows(5).any(|w| w == b"ready"));
    p.send(b"hello\r");
    p.wait(|p| p.count("started") == 1);
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
    p.send(b"\x11");
    p.finish();
}

#[test]
fn unknown_mode_flag_is_a_startup_error() {
    let output = Command::new(env!("CARGO_BIN_EXE_localcode"))
        .args(["--engine", "demo", "--mode", "bogus"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("Unknown mode bogus"));
}

#[test]
fn unknown_engine_is_a_startup_error() {
    let output = Command::new(env!("CARGO_BIN_EXE_localcode"))
        .args(["--engine", "gemini"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("Engine must be codex, claude or demo")
    );
}

#[test]
fn corrupt_goal_does_not_prevent_chat_or_explicit_recovery() {
    let directory = std::env::temp_dir().join(format!("lc-corrupt-goal-{}", std::process::id()));
    let workspace = std::env::current_dir().unwrap().canonicalize().unwrap();
    let store = lc_core::goal::GoalStore::new(&directory, &workspace);
    let goal = lc_core::goal::Goal::new("recover this goal").unwrap();
    tokio::runtime::Runtime::new()
        .unwrap()
        .block_on(store.save(Some(&goal)))
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
    p.send(b"\x11");
    p.finish();
}

fn provider_fixture() -> PathBuf {
    lc_testkit::protocol_child()
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
            p.send(b"\x11");
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
        let workspace = std::env::current_dir().unwrap();
        let store = lc_core::goal::GoalStore::new(&p.directory, &workspace);
        let mut goal = lc_core::goal::Goal::new("fixture-goal").unwrap();
        goal.status = lc_core::goal::Status::Paused;
        tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(store.save(Some(&goal)))
            .unwrap();
        let ready = p.count("ready");
        p.send(b"/new\r");
        p.wait(|p| p.count("ready") > ready && p.shows("● ready"));
        let turns = p.count("started");
        assert_eq!(turns, 5);
        p.send(b"/goal complete\r");
        p.wait(|p| p.goal().is_some_and(|g| g["status"] == "complete"));
        assert_eq!(p.count("started"), turns + 1);
        p.send(b"\x11");
        p.finish();
    }
}

#[test]
#[ignore = "uses installed vendor CLIs and their subscriptions"]
fn installed_providers_complete_a_goal_in_auto_mode() {
    let engines = std::env::var("LOCALCODE_LIVE_ENGINE").ok();
    for engine in ["codex", "claude"]
        .into_iter()
        .filter(|engine| engines.as_deref().is_none_or(|chosen| chosen == *engine))
    {
        let workspace =
            std::env::temp_dir().join(format!("lc-live-goal-{}-{engine}", std::process::id()));
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
        let model = std::env::var(format!("LOCALCODE_LIVE_{}_MODEL", engine.to_uppercase())).ok();
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
        p.send(b"/goal In this disposable workspace create smoke.txt containing exactly LOCALCODE_OK followed by a newline. Read it back and verify its contents. Keep the task limited to this file.\r");
        p.wait_for(Duration::from_secs(180), |p| {
            p.goal().is_some_and(|g| g["status"] != "active") || p.count("error") > 0
        });
        let goal = p.goal();
        p.send(b"\x11");
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
            "LOCALCODE_OK\n"
        );
        fs::remove_dir_all(workspace).unwrap();
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
    p.send(b"\x11");
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
    p.send(b"\x11");
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
    for _ in 0..20 {
        p.drain();
        std::thread::sleep(Duration::from_millis(10));
    }
    let mark = p.output.len();
    let size = libc::winsize {
        ws_row: 30,
        ws_col: 100,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
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
    p.send(b"\x11");
    p.finish();
}
