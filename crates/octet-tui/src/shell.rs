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
        match self.status {
            Status::Exited(code) => format!("exit {code}"),
            Status::Signalled => "killed by a signal".into(),
            Status::TimedOut => "timed out after 10 minutes".into(),
            Status::Cancelled => "cancelled".into(),
        }
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
    let output = if cut {
        format!("{CUT}{text}")
    } else {
        text.into_owned()
    };
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
        assert!(started.elapsed() < Duration::from_secs(5));
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
