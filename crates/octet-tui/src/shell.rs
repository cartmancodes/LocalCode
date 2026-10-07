//! `!` commands: run one command the user typed, in the workspace, bounded in
//! output and time. It is the user's own action, so no approval applies.
//! Commands run in their own session, away from Octet's terminal.
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
    /// Stopped at the time limit it carries.
    TimedOut(Duration),
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
        match self.status {
            Status::Exited(code) => format!("exit {code}"),
            Status::Signalled => "killed by a signal".into(),
            Status::TimedOut(limit) => format!("timed out after {}", duration_text(limit)),
            Status::Cancelled => "cancelled".into(),
        }
    }
}

/// A limit as people say it: whole minutes, else whole seconds, else
/// milliseconds.
pub fn duration_text(limit: Duration) -> String {
    if limit.subsec_nanos() != 0 {
        return format!("{} ms", limit.as_millis());
    }
    match limit.as_secs() {
        0 => format!("{} ms", limit.as_millis()),
        60 => "1 minute".into(),
        s if s % 60 == 0 => format!("{} minutes", s / 60),
        1 => "1 second".into(),
        s => format!("{s} seconds"),
    }
}

/// Why a `!` command could not run to completion.
#[derive(Debug, thiserror::Error)]
pub enum ShellError {
    #[error("Cannot run {shell}: {source}")]
    Run {
        shell: String,
        source: std::io::Error,
    },
    /// The task running the command panicked or was cancelled.
    #[error("The command failed: {0}")]
    Task(#[from] tokio::task::JoinError),
}

/// Runs `command` with the user's shell (`$SHELL`, else `sh`).
pub async fn run(
    command: &str,
    cwd: &Path,
    cancel: oneshot::Receiver<()>,
) -> Result<Ran, ShellError> {
    let shell = std::env::var("SHELL")
        .ok()
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(|| "sh".into());
    run_with(&shell, command, cwd, cancel, TIME_LIMIT).await
}

async fn run_with(
    shell: &str,
    command: &str,
    cwd: &Path,
    mut cancel: oneshot::Receiver<()>,
    limit: Duration,
) -> Result<Ran, ShellError> {
    let fail = |source| ShellError::Run {
        shell: shell.to_owned(),
        source,
    };
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
            // Prompts for credentials would otherwise wait on a terminal
            // nobody is reading.
            .env("GIT_TERMINAL_PROMPT", "0")
            .kill_on_drop(true);
        // SAFETY: setsid is async-signal-safe. A new session has no
        // controlling terminal, so opening /dev/tty fails at once instead of
        // drawing over Octet; its group id is its pid, for kill_group.
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        cmd.spawn().map_err(fail)?
    };
    let group = child.id();
    // Declared after `child`, so on an early drop (a reconnect or quit drops
    // this future) it runs first, while the shell is unreaped and its pid,
    // the group's id, still reserved.
    let guard = Group(group);
    let tail = std::sync::Arc::new(std::sync::Mutex::new(Tail::default()));
    let reader_tail = std::sync::Arc::clone(&tail);
    // A plain thread, not spawn_blocking: a reader stuck on a pipe held by a
    // process that left the group must not hold up the runtime's shutdown.
    let (done, read) = oneshot::channel();
    std::thread::spawn(move || {
        read_tail(reader, OUTPUT_LIMIT, &reader_tail);
        let _ = done.send(());
    });
    // Wait for the shell to exit without reaping it, so its pid, which is
    // also the group id, stays reserved until the group is killed.
    let exited = tokio::task::spawn_blocking(move || wait_exit(group));
    let mut status = tokio::select! {
        _ = exited => None,
        _ = tokio::time::sleep(limit) => Some(Status::TimedOut(limit)),
        _ = &mut cancel => Some(Status::Cancelled),
    };
    // Children left in the group would hold the pipe open.
    drop(guard);
    let code = child.wait().await.map_err(fail)?.code();
    if status.is_none() {
        status = Some(code.map_or(Status::Signalled, Status::Exited));
    }
    // A process that left the group may keep the pipe open; don't wait on it.
    let _ = tokio::time::timeout(Duration::from_secs(1), read).await;
    let (bytes, cut) = {
        let tail = tail
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        (tail.kept.iter().copied().collect::<Vec<u8>>(), tail.cut)
    };
    let text = overstrike(&String::from_utf8_lossy(at_line(&bytes, cut)));
    let output = if cut { format!("{CUT}{text}") } else { text };
    Ok(Ran {
        command: command.to_owned(),
        status: status.unwrap_or(Status::Signalled),
        output,
    })
}

/// A `!` command running in the background, and whether its output goes
/// with the next prompt. Dropping it (a session that ends) aborts the run,
/// whose guard kills the command's whole process group.
pub(crate) struct Running {
    task: tokio::task::JoinHandle<Result<Ran, ShellError>>,
    cancel: Option<oneshot::Sender<()>>,
    pub(crate) attach: bool,
}
impl Running {
    /// Starts `command` in `root`.
    pub(crate) fn spawn(command: String, root: std::path::PathBuf, attach: bool) -> Self {
        let (cancel, cancelled) = oneshot::channel();
        Self {
            task: tokio::spawn(async move { run(&command, &root, cancelled).await }),
            cancel: Some(cancel),
            attach,
        }
    }
    /// Asks the command to stop (Esc); its result still arrives.
    pub(crate) fn cancel(&mut self) {
        if let Some(cancel) = self.cancel.take() {
            let _ = cancel.send(());
        }
    }
    /// Waits for the command to end.
    pub(crate) async fn wait(&mut self) -> Result<Ran, ShellError> {
        (&mut self.task)
            .await
            .unwrap_or_else(|error| Err(error.into()))
    }
}
impl Drop for Running {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Blocks until `pid` exits, leaving it unreaped.
fn wait_exit(pid: Option<u32>) {
    let Some(pid) = pid.and_then(|pid| libc::id_t::try_from(pid).ok()) else {
        return;
    };
    loop {
        // SAFETY: siginfo_t is plain C data, for which all zeroes is valid.
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        // SAFETY: waitid writes only into `info`; WNOWAIT leaves the child
        // for tokio to reap.
        let result =
            unsafe { libc::waitid(libc::P_PID, pid, &mut info, libc::WEXITED | libc::WNOWAIT) };
        if result == 0 || std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
        {
            return;
        }
    }
}

/// Kills a command's process group when dropped, so no path leaves the
/// pipeline running: `kill_on_drop` alone stops only the shell.
struct Group(Option<u32>);
impl Drop for Group {
    fn drop(&mut self) {
        kill_group(self.0.take());
    }
}

fn kill_group(group: Option<u32>) {
    if let Some(pid) = group.and_then(|pid| libc::pid_t::try_from(pid).ok()) {
        // SAFETY: signals only the process group this command was started in.
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
    }
}

/// The end of the output read so far.
#[derive(Default)]
struct Tail {
    /// A deque, so dropping old output from the front moves nothing.
    kept: std::collections::VecDeque<u8>,
    cut: bool,
}

/// Reads to the end into `tail`, keeping the last `limit` bytes.
fn read_tail(mut reader: impl Read, limit: usize, tail: &std::sync::Mutex<Tail>) {
    let mut chunk = [0; 8192];
    loop {
        let n = match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        };
        let mut tail = tail
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        tail.kept.extend(&chunk[..n]);
        if tail.kept.len() > limit {
            let excess = tail.kept.len() - limit;
            tail.kept.drain(..excess);
            tail.cut = true;
        }
    }
}

/// A cut tail starts after its first newline, so it never begins inside a
/// line, an escape sequence or a character.
fn at_line(bytes: &[u8], cut: bool) -> &[u8] {
    match bytes.iter().position(|byte| *byte == b'\n') {
        Some(newline) if cut => &bytes[newline + 1..],
        _ => bytes,
    }
}

/// Each line as a terminal leaves it: a carriage return starts the line
/// over, so progress counters keep only their last state.
fn overstrike(text: &str) -> String {
    text.split('\n')
        .map(|line| {
            let line = line.strip_suffix('\r').unwrap_or(line);
            line.rsplit('\r').next().unwrap_or(line)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    // Multi-threaded: the run makes progress while this test polls for it.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dropping_a_shell_run_kills_its_group() {
        let dir = octet_testkit::TempDir::new("octet-shell-drop");
        std::fs::create_dir_all(dir.path()).unwrap();
        let pidfile = dir.path().join("group");
        // The shell's pid is its group's id (setsid); the pipeline lives on in it.
        let command = format!("echo $$ > {}; sleep 30 | cat", pidfile.display());
        let run = tokio::spawn(async move {
            let (_keep, cancel) = oneshot::channel();
            run_with("sh", &command, Path::new("."), cancel, TIME_LIMIT).await
        });
        assert!(octet_testkit::wait_until(octet_testkit::QUICK, || {
            std::fs::read_to_string(&pidfile).is_ok_and(|text| text.ends_with('\n'))
        }));
        let group: libc::pid_t = std::fs::read_to_string(&pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // A reconnect or quit drops the run mid-way.
        run.abort();
        let _ = run.await;
        // SAFETY: signal 0 to the group only checks that a member exists.
        let gone = octet_testkit::wait_until(octet_testkit::QUICK, || unsafe {
            libc::kill(-group, 0) != 0
        });
        assert!(gone, "the command's processes outlived it");
    }
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
        assert!(
            ran.output.contains("out\n") && ran.output.contains("err\n"),
            "{}",
            ran.output
        );
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
    async fn carriage_returns_keep_what_a_terminal_would_show() {
        let ran = sh("printf '10%%\\r20%%\\r30%%\\nok\\r\\n'").await;
        assert_eq!(ran.output, "30%\nok\n");
    }
    #[tokio::test]
    async fn a_cut_tail_starts_at_a_line() {
        // 6,000 nine-byte lines: the last 32 KiB would start one byte into a
        // line, which could also be inside an escape sequence.
        let ran = sh("i=1000; while [ $i -lt 7000 ]; do echo line$i; i=$((i+1)); done").await;
        let body = ran.output.strip_prefix(CUT).expect("cut");
        assert!(body.starts_with("line"), "{}", &body[..20]);
        assert!(body.ends_with("line6999\n"));
    }
    #[tokio::test]
    async fn a_process_that_left_the_group_cannot_hold_the_result() {
        let started = std::time::Instant::now();
        // Without the fix this waits the full 60 s; the bound leaves room
        // for a machine busy with the rest of the suite.
        let escaped = octet_testkit::detached_sleep();
        let ran = sh(&format!(
            "'{}' 60 & sleep 0.5; echo started",
            escaped.display()
        ))
        .await;
        assert!(ran.output.contains("started"), "{}", ran.output);
        assert!(
            started.elapsed() < Duration::from_secs(30),
            "{:?}",
            started.elapsed()
        );
    }
    #[tokio::test]
    async fn commands_reading_stdin_finish_at_once() {
        let started = std::time::Instant::now();
        assert_eq!(sh("cat").await.status, Status::Exited(0));
        assert!(started.elapsed() < octet_testkit::QUICK);
    }
    #[tokio::test]
    async fn background_children_do_not_hold_it_open() {
        let started = std::time::Instant::now();
        let ran = sh("sleep 30 & echo done").await;
        assert_eq!(ran.output, "done\n");
        assert!(started.elapsed() < octet_testkit::QUICK);
    }
    #[tokio::test]
    async fn cancel_stops_the_whole_pipeline() {
        let (cancel, cancelled) = oneshot::channel();
        let started = std::time::Instant::now();
        let task = tokio::spawn(async move {
            run_with(
                "sh",
                "sleep 30 | cat",
                Path::new("."),
                cancelled,
                TIME_LIMIT,
            )
            .await
        });
        tokio::time::sleep(Duration::from_millis(200)).await;
        cancel.send(()).unwrap();
        let ran = task.await.unwrap().unwrap();
        assert_eq!(ran.status, Status::Cancelled);
        assert_eq!(ran.summary(), "cancelled");
        assert!(started.elapsed() < octet_testkit::QUICK);
    }
    #[tokio::test]
    async fn times_out() {
        let (_keep, cancel) = oneshot::channel();
        let ran = run_with(
            "sh",
            "sleep 30",
            Path::new("."),
            cancel,
            Duration::from_millis(300),
        )
        .await
        .unwrap();
        assert_eq!(ran.status, Status::TimedOut(Duration::from_millis(300)));
        assert_eq!(ran.summary(), "timed out after 300 ms");
    }
    #[test]
    fn time_limit_wording_follows_the_limit() {
        let summary = |limit| {
            Ran {
                command: "x".into(),
                status: Status::TimedOut(limit),
                output: String::new(),
            }
            .summary()
        };
        assert_eq!(
            summary(Duration::from_secs(600)),
            "timed out after 10 minutes"
        );
        assert_eq!(summary(Duration::from_secs(60)), "timed out after 1 minute");
        assert_eq!(
            summary(Duration::from_secs(90)),
            "timed out after 90 seconds"
        );
        assert_eq!(
            summary(Duration::from_millis(300)),
            "timed out after 300 ms"
        );
        assert_eq!(
            summary(Duration::from_millis(1500)),
            "timed out after 1500 ms"
        );
        assert_eq!(summary(Duration::ZERO), "timed out after 0 ms");
    }
    #[tokio::test]
    async fn a_missing_shell_is_an_error() {
        let (_keep, cancel) = oneshot::channel();
        let error = run_with("/no/such/shell", "true", Path::new("."), cancel, TIME_LIMIT)
            .await
            .unwrap_err();
        assert!(matches!(error, ShellError::Run { .. }));
        assert!(
            error.to_string().starts_with("Cannot run /no/such/shell: "),
            "{error}"
        );
    }
}
