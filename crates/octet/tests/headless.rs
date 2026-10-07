//! Print, JSON and RPC modes, run as processes against the fake vendor. No
//! terminal is attached: these modes must not need one.
// Test code: an unwrap that fails is the test failing.
#![allow(clippy::unwrap_used)]
use serde_json::{Value, json};
use std::{
    io::Write,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    time::Duration,
};

/// How long any one process or line may take.
const LIMIT: Duration = Duration::from_secs(20);

/// `octet` against the fake Codex, journaling into its own directory.
fn octet(args: &[&str]) -> (Child, octet_testkit::TempDir) {
    let temp = octet_testkit::TempDir::new("octet-headless");
    std::fs::create_dir_all(temp.path()).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_octet"))
        .arg("--binary")
        .arg(octet_testkit::protocol_child())
        .arg("--journal-dir")
        .arg(temp.path())
        .arg("--cwd")
        .arg(temp.path())
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    (child, temp)
}

/// Runs `octet args` with `input` on stdin; its exit code, stdout and stderr.
fn run(args: &[&str], input: &str) -> (i32, String, String) {
    let (mut child, _temp) = octet(args);
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    let output = octet_testkit::wait_child(child, LIMIT);
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8(output.stdout).unwrap(),
        String::from_utf8(output.stderr).unwrap(),
    )
}

#[test]
fn print_writes_the_reply_and_exits_zero() {
    let (code, stdout, stderr) = run(&["--print", "hello"], "");
    assert_eq!(code, 0, "{stderr}");
    assert_eq!(
        stdout,
        format!("{}\n", octet_testkit::scenario::CODEX_REPLY)
    );
}

#[test]
fn print_reads_the_prompt_from_stdin() {
    let (code, stdout, stderr) = run(&["-p", "-"], "params\n");
    assert_eq!(code, 0, "{stderr}");
    // The fake echoes the turn it received: the prompt came from stdin.
    assert!(stdout.contains(r#""text":"params""#), "{stdout}");
}

#[test]
fn print_json_writes_event_lines() {
    let (code, stdout, stderr) = run(&["--print", "hello", "--output", "json"], "");
    assert_eq!(code, 0, "{stderr}");
    let events: Vec<Value> = stdout
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert!(events.contains(&json!({"type":"user","data":"hello"})));
    assert!(events.contains(&json!({"type":"text","data":octet_testkit::scenario::CODEX_REPLY})));
    assert_eq!(
        events.last(),
        Some(&json!({"type":"finished","data":"completed"}))
    );
}

#[test]
fn print_denies_approvals_and_says_why() {
    let (code, stdout, stderr) = run(&["--print", "approval"], "");
    assert_eq!(code, 0, "{stderr}");
    assert!(stdout.contains("decline"), "{stdout}");
    assert!(stderr.contains("--mode auto"), "{stderr}");
}

#[test]
fn print_failed_turn_exits_one() {
    let (code, _, stderr) = run(&["--print", "fail"], "");
    assert_eq!(code, 1, "{stderr}");
    assert!(stderr.contains("You've hit your usage limit."), "{stderr}");
}

#[test]
fn print_and_rpc_cannot_be_combined() {
    let (code, _, stderr) = run(&["--print", "hello", "--rpc"], "");
    assert_eq!(code, 2, "a usage error");
    assert!(stderr.contains("--print and --rpc"), "{stderr}");
}

/// An RPC client: writes commands, reads event lines.
struct Rpc {
    child: Child,
    stdin: Option<ChildStdin>,
    lines: mpsc::Receiver<String>,
    _temp: octet_testkit::TempDir,
}
impl Rpc {
    fn start() -> Self {
        let (mut child, temp) = octet(&["--rpc"]);
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().unwrap();
        let lines = octet_testkit::line_reader(stdout);
        Self {
            child,
            stdin,
            lines,
            _temp: temp,
        }
    }
    fn send(&mut self, command: &Value) {
        let stdin = self.stdin.as_mut().unwrap();
        writeln!(stdin, "{command}").unwrap();
        stdin.flush().unwrap();
    }
    /// Reads event lines until one matches `wanted`.
    fn until(&self, wanted: impl Fn(&Value) -> bool) -> Value {
        loop {
            let line = self.lines.recv_timeout(LIMIT).expect("no matching line");
            let event: Value = serde_json::from_str(&line).unwrap();
            if wanted(&event) {
                return event;
            }
        }
    }
    fn exit_code(mut self) -> i32 {
        drop(self.stdin.take());
        octet_testkit::wait_child(self.child, LIMIT)
            .status
            .code()
            .unwrap_or(-1)
    }
}

#[test]
fn rpc_runs_prompts_and_answers_approvals() {
    let mut rpc = Rpc::start();
    rpc.until(|e| e["type"] == "ready");
    rpc.send(&json!({"type":"prompt","text":"hello"}));
    rpc.until(|e| e == &json!({"type":"text","data":octet_testkit::scenario::CODEX_REPLY}));
    rpc.until(|e| e["type"] == "finished");
    rpc.send(&json!({"type":"prompt","text":"approval"}));
    let approval = rpc.until(|e| e["type"] == "approval");
    let id = approval["data"]["id"].as_u64().unwrap();
    rpc.send(&json!({"type":"answer","id":id,"allow":true}));
    rpc.until(|e| e == &json!({"type":"text","data":"accept"}));
    rpc.until(|e| e == &json!({"type":"finished","data":"completed"}));
    rpc.send(&json!({"type":"nonsense"}));
    let error = rpc.until(|e| e["type"] == "error");
    assert!(
        error["data"].as_str().unwrap().contains("nonsense"),
        "{error}"
    );
    rpc.send(&json!({"type":"quit"}));
    assert_eq!(rpc.exit_code(), 0);
}

#[test]
fn rpc_quits_on_eof() {
    let rpc = Rpc::start();
    rpc.until(|e| e["type"] == "ready");
    assert_eq!(rpc.exit_code(), 0);
}

#[test]
fn print_sigint_interrupts_the_turn_and_exits_130() {
    let (mut child, _temp) = octet(&["--print", "hold", "--output", "json"]);
    drop(child.stdin.take());
    let stdout = child.stdout.take().unwrap();
    let lines = octet_testkit::line_reader(stdout);
    loop {
        let line = lines.recv_timeout(LIMIT).expect("the turn never started");
        if line.contains(r#""type":"started""#) {
            break;
        }
    }
    let pid = libc::pid_t::try_from(child.id()).unwrap();
    // SAFETY: kill only sends a signal to our own child process.
    assert_eq!(unsafe { libc::kill(pid, libc::SIGINT) }, 0);
    let last = std::iter::from_fn(|| lines.recv_timeout(LIMIT).ok()).last();
    assert_eq!(
        last.as_deref(),
        Some(r#"{"data":"interrupted","type":"finished"}"#)
    );
    assert_eq!(
        octet_testkit::wait_child(child, LIMIT).status.code(),
        Some(130)
    );
}

#[test]
fn rpc_runs_a_piped_script_to_the_end() {
    let input = concat!(
        r#"{"type":"prompt","text":"hello"}"#,
        "\n",
        r#"{"type":"prompt","text":"hello"}"#,
        "\n"
    );
    let (code, stdout, stderr) = run(&["--rpc"], input);
    assert_eq!(code, 0, "{stderr}");
    let finished = stdout
        .lines()
        .filter(|line| line.contains(r#""type":"finished""#))
        .count();
    assert_eq!(finished, 2, "{stdout}");
}

#[test]
fn rpc_exits_one_when_a_piped_turn_fails() {
    let (code, stdout, _) = run(&["--rpc"], "{\"type\":\"prompt\",\"text\":\"fail\"}\n");
    assert_eq!(code, 1, "{stdout}");
    assert!(
        stdout.contains(r#"{"data":"failed","type":"finished"}"#),
        "{stdout}"
    );
}

#[test]
fn rpc_denies_approvals_once_stdin_closes() {
    let (code, stdout, _) = run(&["--rpc"], "{\"type\":\"prompt\",\"text\":\"approval\"}\n");
    assert_eq!(code, 0, "{stdout}");
    assert!(
        stdout.contains(r#"{"data":"decline","type":"text"}"#),
        "{stdout}"
    );
}

/// Starts `octet args` with its JSON event lines on a channel.
fn with_lines(args: &[&str]) -> (Child, mpsc::Receiver<String>, octet_testkit::TempDir) {
    let (mut child, temp) = octet(args);
    let stdout = child.stdout.take().unwrap();
    let lines = octet_testkit::line_reader(stdout);
    (child, lines, temp)
}

/// Waits for a line containing `wanted`.
fn wait_line(lines: &mpsc::Receiver<String>, wanted: &str) {
    loop {
        let line = lines.recv_timeout(LIMIT).expect("the line never came");
        if line.contains(wanted) {
            return;
        }
    }
}

/// Sends `signal` to `child` and returns its exit code.
fn signal_and_wait(child: Child, signal: libc::c_int) -> Option<i32> {
    let pid = libc::pid_t::try_from(child.id()).unwrap();
    // SAFETY: kill only sends a signal to our own child process.
    assert_eq!(unsafe { libc::kill(pid, signal) }, 0);
    octet_testkit::wait_child(child, LIMIT).status.code()
}

#[test]
fn print_exits_130_when_the_vendor_stops_after_ctrl_c() {
    let (mut child, lines, _temp) =
        with_lines(&["--print", "die-on-interrupt", "--output", "json"]);
    drop(child.stdin.take());
    wait_line(&lines, r#""type":"started""#);
    assert_eq!(signal_and_wait(child, libc::SIGINT), Some(130));
}

#[test]
fn print_reports_a_prompt_over_the_limit_from_stdin() {
    // Two-byte characters, so the cut at the limit falls inside one.
    let long = "é".repeat(40_000);
    let (code, _, stderr) = run(&["-p", "-"], &long);
    assert_eq!(code, 2, "a usage error");
    assert!(stderr.contains("The prompt is over 64 KiB"), "{stderr}");
}

#[test]
fn print_stops_cleanly_on_sigterm() {
    let (mut child, lines, temp) = with_lines(&["--print", "hold", "--output", "json"]);
    drop(child.stdin.take());
    wait_line(&lines, r#""type":"started""#);
    assert_eq!(signal_and_wait(child, libc::SIGTERM), Some(143));
    assert!(last_journal_record(temp.path()).contains(r#""type":"stopped""#));
}

#[test]
fn rpc_stops_cleanly_on_sigint_and_sigterm() {
    for (signal, code) in [(libc::SIGINT, 130), (libc::SIGTERM, 143)] {
        let (mut child, lines, temp) = with_lines(&["--rpc"]);
        // Held open: `wait` would close stdin, and the end of input also
        // ends RPC.
        let _stdin = child.stdin.take();
        wait_line(&lines, r#""type":"ready""#);
        assert_eq!(signal_and_wait(child, signal), Some(code), "{signal}");
        assert!(last_journal_record(temp.path()).contains(r#""type":"stopped""#));
    }
}

/// The last line of the only journal in `directory`.
fn last_journal_record(directory: &std::path::Path) -> String {
    let journal = std::fs::read_dir(directory)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().is_some_and(|e| e == "jsonl"))
        .unwrap();
    let text = std::fs::read_to_string(journal).unwrap();
    text.lines().last().unwrap_or_default().to_owned()
}

#[test]
fn print_shuts_down_when_stdout_closes() {
    let (mut child, temp) = octet(&["--print", "hello", "--output", "json"]);
    drop(child.stdin.take());
    // Nobody reads the replies: the first write fails.
    drop(child.stdout.take());
    assert_eq!(
        octet_testkit::wait_child(child, LIMIT).status.code(),
        Some(1)
    );
    assert!(last_journal_record(temp.path()).contains(r#""type":"stopped""#));
}

#[test]
fn an_argument_prompt_over_the_limit_is_refused_before_opening() {
    let long = "x".repeat(70 * 1024);
    let (mut child, temp) = octet(&["--print", &long]);
    drop(child.stdin.take());
    let output = octet_testkit::wait_child(child, LIMIT);
    assert_eq!(output.status.code(), Some(2), "a usage error");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("The prompt is over 64 KiB"), "{stderr}");
    let journals = std::fs::read_dir(temp.path()).unwrap().count();
    assert_eq!(journals, 0, "no session should have opened");
}

#[test]
fn print_survives_a_closed_stderr() {
    let (mut child, temp) = octet(&["--print", "approval"]);
    drop(child.stdin.take());
    // Nobody reads the notices: writing the denial to stderr fails.
    drop(child.stderr.take());
    let output = octet_testkit::wait_child(child, LIMIT);
    assert_eq!(output.status.code(), Some(0), "not a panic");
    assert!(last_journal_record(temp.path()).contains(r#""type":"stopped""#));
}

#[test]
fn a_prompt_that_cannot_be_read_is_a_run_failure() {
    let temp = octet_testkit::TempDir::new("octet-headless");
    std::fs::create_dir_all(temp.path()).unwrap();
    // A directory opens, but reading it fails.
    let directory = std::fs::File::open(temp.path()).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_octet"))
        .args(["--binary"])
        .arg(octet_testkit::protocol_child())
        .arg("--journal-dir")
        .arg(temp.path())
        .args(["-p", "-"])
        .stdin(directory)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let output = octet_testkit::wait_child(child, LIMIT);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("Cannot read the prompt"), "{stderr}");
    assert_eq!(output.status.code(), Some(1), "not a usage error");
}

#[test]
fn rpc_answers_go_ahead_of_queued_prompts() {
    let mut rpc = Rpc::start();
    rpc.until(|e| e["type"] == "ready");
    rpc.send(&json!({"type":"prompt","text":"approval"}));
    let approval = rpc.until(|e| e["type"] == "approval");
    let id = approval["data"]["id"].as_u64().unwrap();
    // A pipelining client queues its next prompt before answering.
    rpc.send(&json!({"type":"prompt","text":"hello"}));
    let asked = std::time::Instant::now();
    rpc.send(&json!({"type":"answer","id":id,"allow":true}));
    rpc.until(|e| e == &json!({"type":"text","data":"accept"}));
    assert!(
        asked.elapsed() < Duration::from_secs(5),
        "the answer waited"
    );
    rpc.until(|e| e == &json!({"type":"text","data":octet_testkit::scenario::CODEX_REPLY}));
    rpc.send(&json!({"type":"quit"}));
    assert_eq!(rpc.exit_code(), 0);
}

#[test]
fn rpc_quits_while_stdin_stays_open() {
    let mut rpc = Rpc::start();
    rpc.until(|e| e["type"] == "ready");
    rpc.send(&json!({"type":"quit"}));
    // The client keeps its end of stdin open.
    let Rpc { child, stdin, .. } = rpc;
    let output = octet_testkit::wait_child(child, Duration::from_secs(5));
    drop(stdin);
    assert_eq!(output.status.code(), Some(0));
}

#[test]
fn print_says_when_the_session_ends_unfinished() {
    let (mut child, _temp) = octet(&[
        "--engine",
        "claude",
        "--print",
        octet_testkit::scenario::DELTA_FLOOD,
        "--output",
        "json",
    ]);
    drop(child.stdin.take());
    // Nobody reads the replies for a while: the session gives up on us.
    let stdout = child.stdout.take().unwrap();
    std::thread::sleep(Duration::from_secs(4));
    let drain = std::thread::spawn(move || std::io::read_to_string(stdout).unwrap_or_default());
    let output = octet_testkit::wait_child(child, LIMIT);
    let _ = drain.join();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(1), "{stderr}");
    assert!(stderr.contains("ended without finishing"), "{stderr}");
}

#[test]
fn a_relative_home_is_ignored() {
    let temp = octet_testkit::TempDir::new("octet-relative-home");
    std::fs::create_dir_all(temp.path()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_octet"))
        .arg("--binary")
        .arg(octet_testkit::protocol_child())
        .args(["--print", "hello"])
        .current_dir(temp.path())
        .env("HOME", "relative-home")
        .env_remove("XDG_DATA_HOME")
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2), "a usage error");
    assert!(!temp.path().join("relative-home").exists());
}
