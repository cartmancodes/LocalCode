//! Bounded JSON-line transport for owned vendor subprocesses.
#[cfg(not(unix))]
compile_error!("Octet supports Linux and macOS only");

use serde_json::Value;
use std::{
    collections::VecDeque,
    ffi::OsString,
    io,
    path::PathBuf,
    process::Stdio,
    sync::{Arc, Mutex},
    time::Duration,
};
use thiserror::Error;
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::{Child, ChildStdin, Command},
    sync::{mpsc, Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore},
    task::JoinHandle,
    time::timeout,
};

#[derive(Clone, Debug)]
pub struct ProcessConfig {
    pub executable: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: Option<PathBuf>,
    /// Maximum number of bytes in one JSON payload, excluding LF.
    pub max_frame_bytes: usize,
    /// Maximum retained stdout frame bytes in the receive queue.
    pub queue_bytes: usize,
    /// Number of trailing stderr bytes kept for diagnostics.
    pub stderr_bytes: usize,
    /// Grace before process-group TERM.
    pub shutdown_grace: Duration,
    /// Grace between process-group TERM and KILL.
    pub term_grace: Duration,
}

#[derive(Debug, Error)]
pub enum ProcessError {
    #[error("invalid process configuration: {0}")]
    InvalidConfig(&'static str),
    #[error("process I/O failed: {0}")]
    Io(#[from] io::Error),
    #[error("JSON frame exceeds {limit} bytes")]
    FrameTooLarge { limit: usize },
    #[error("invalid JSON frame: {0}")]
    InvalidJson(#[from] serde_json::Error),
    #[error("process output queue closed")]
    QueueClosed,
    #[error("process stdin is closed")]
    StdinClosed,
}

enum FrameEvent {
    Data(Box<[u8]>, OwnedSemaphorePermit),
    Error(ProcessError),
}

#[derive(Clone)]
pub struct ProcessSender {
    stdin: Arc<AsyncMutex<Option<ChildStdin>>>,
    max_frame_bytes: usize,
}

impl ProcessSender {
    pub async fn send(&self, value: &Value) -> Result<(), ProcessError> {
        // Serialize under the lock so concurrent senders cannot retain payload buffers.
        let mut guard = self.stdin.lock().await;
        if guard.is_none() {
            return Err(ProcessError::StdinClosed);
        }
        let mut payload = LimitedPayload {
            bytes: Vec::new(),
            limit: self.max_frame_bytes,
        };
        if serde_json::to_writer(&mut payload, value).is_err() {
            return Err(ProcessError::FrameTooLarge {
                limit: self.max_frame_bytes,
            });
        }
        // Taking ownership makes cancellation close the pipe rather than leaving a
        // partial frame for the next sender to append to.
        let mut stdin = guard.take().ok_or(ProcessError::StdinClosed)?;
        stdin.write_all(&payload.bytes).await?;
        stdin.write_all(b"\n").await?;
        stdin.flush().await?;
        *guard = Some(stdin);
        Ok(())
    }
}

struct LimitedPayload {
    bytes: Vec<u8>,
    limit: usize,
}
impl io::Write for LimitedPayload {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit - self.bytes.len() {
            return Err(io::Error::other("frame limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ShutdownStage {
    AlreadyExited,
    Term,
    Kill,
}

#[derive(Clone, Debug)]
pub struct ShutdownReport {
    pub reaped: bool,
    pub stage: ShutdownStage,
    /// Whether the owned process group was no longer observable after shutdown.
    pub descendants_stopped: bool,
    pub stderr_tail: Vec<u8>,
}

pub struct Process {
    child: Child,
    sender: ProcessSender,
    frame_rx: mpsc::Receiver<FrameEvent>,
    reader_task: Option<JoinHandle<()>>,
    stderr_task: Option<JoinHandle<()>>,
    stderr_tail: Arc<Mutex<VecDeque<u8>>>,
    shutdown_grace: Duration,
    term_grace: Duration,
    shutdown_complete: bool,
    shutdown_report: Option<ShutdownReport>,
    receive_failed: bool,
    process_group: libc::pid_t,
}

impl Process {
    /// Starts the child and its reader tasks. Call it inside a Tokio runtime:
    /// the pipes register with the runtime's reactor.
    pub fn spawn(config: ProcessConfig) -> Result<Self, ProcessError> {
        if config.max_frame_bytes == 0 || config.max_frame_bytes > config.queue_bytes {
            return Err(ProcessError::InvalidConfig(
                "max_frame_bytes must fit inside queue_bytes",
            ));
        }
        if config.queue_bytes > u32::MAX as usize {
            return Err(ProcessError::InvalidConfig(
                "queue_bytes exceeds semaphore capacity",
            ));
        }
        let mut command = Command::new(&config.executable);
        command.args(&config.args);
        if let Some(cwd) = &config.cwd {
            command.current_dir(cwd);
        }
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command.kill_on_drop(true);
        // SAFETY: pre_exec runs in the forked child before exec; setpgid is
        // async-signal-safe and the closure allocates nothing.
        unsafe {
            // The child becomes leader of a new process group before exec.
            command.pre_exec(|| {
                if libc::setpgid(0, 0) == -1 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
        let mut child = command.spawn()?;
        let process_group = libc::pid_t::try_from(child.id().expect("spawned child must have pid"))
            .expect("pid fits pid_t");
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        let (frame_tx, frame_rx) = mpsc::channel(64);
        let permits = Arc::new(Semaphore::new(config.queue_bytes));
        let reader_task = tokio::spawn(read_frames(
            stdout,
            frame_tx,
            permits,
            config.max_frame_bytes,
        ));
        let stderr_tail = Arc::new(Mutex::new(VecDeque::with_capacity(config.stderr_bytes)));
        let stderr_task = tokio::spawn(read_stderr(
            stderr,
            Arc::clone(&stderr_tail),
            config.stderr_bytes,
        ));
        Ok(Self {
            child,
            sender: ProcessSender {
                stdin: Arc::new(AsyncMutex::new(Some(stdin))),
                max_frame_bytes: config.max_frame_bytes,
            },
            frame_rx,
            reader_task: Some(reader_task),
            stderr_task: Some(stderr_task),
            stderr_tail,
            shutdown_grace: config.shutdown_grace,
            term_grace: config.term_grace,
            shutdown_complete: false,
            shutdown_report: None,
            receive_failed: false,
            process_group,
        })
    }

    pub fn sender(&self) -> ProcessSender {
        self.sender.clone()
    }

    pub async fn next_frame(&mut self) -> Result<Option<Value>, ProcessError> {
        if self.receive_failed {
            return Ok(None);
        }
        match self.frame_rx.recv().await {
            Some(FrameEvent::Data(bytes, _permit)) => match serde_json::from_slice(&bytes) {
                Ok(value) => Ok(Some(value)),
                Err(error) => {
                    self.receive_failed = true;
                    if let Some(task) = &self.reader_task {
                        task.abort();
                    }
                    while self.frame_rx.try_recv().is_ok() {}
                    self.frame_rx.close();
                    Err(ProcessError::InvalidJson(error))
                }
            },
            Some(FrameEvent::Error(error)) => {
                self.receive_failed = true;
                Err(error)
            }
            None => Ok(None),
        }
    }

    #[must_use = "a failed cleanup is only visible in the report"]
    pub async fn shutdown(&mut self) -> ShutdownReport {
        if let Some(report) = &self.shutdown_report {
            return report.clone();
        }
        // This path never takes the writer mutex: an unresponsive stdin write cannot delay kill.
        // A cooperative protocol child receives EOF if no write is currently in progress.
        if let Ok(mut guard) = self.sender.stdin.try_lock() {
            guard.take();
        }
        let mut stage = ShutdownStage::AlreadyExited;
        let initial_exit = self.child.try_wait().ok().flatten().is_some();
        if !initial_exit {
            let _ = timeout(self.shutdown_grace, self.child.wait()).await;
        }
        if self.group_alive() {
            stage = ShutdownStage::Term;
            self.signal_group(libc::SIGTERM);
            self.wait_for_group_exit().await;
        }
        if self.group_alive() {
            stage = ShutdownStage::Kill;
            self.signal_group(libc::SIGKILL);
            self.wait_for_group_exit().await;
        }
        let reaped = if self.child.try_wait().ok().flatten().is_some() {
            true
        } else {
            matches!(timeout(self.term_grace, self.child.wait()).await, Ok(Ok(_)))
        };
        if let Some(task) = &self.reader_task {
            task.abort();
        }
        if let Some(task) = self.reader_task.take() {
            let _ = task.await;
        }
        if let Some(task) = &self.stderr_task {
            task.abort();
        }
        if let Some(task) = self.stderr_task.take() {
            let _ = task.await;
        }
        let stderr_tail = self
            .stderr_tail
            .lock()
            .expect("stderr ring lock")
            .iter()
            .copied()
            .collect();
        let descendants_stopped = !self.group_alive();
        self.shutdown_complete = reaped && descendants_stopped;
        let report = ShutdownReport {
            reaped,
            stage,
            descendants_stopped,
            stderr_tail,
        };
        self.shutdown_report = Some(report.clone());
        report
    }

    async fn wait_for_group_exit(&mut self) {
        let deadline = tokio::time::Instant::now() + self.term_grace;
        loop {
            let _ = self.child.try_wait();
            if !self.group_alive() || tokio::time::Instant::now() >= deadline {
                break;
            }
            tokio::time::sleep_until(
                deadline.min(tokio::time::Instant::now() + Duration::from_millis(10)),
            )
            .await;
        }
    }

    fn signal_group(&self, signal: i32) {
        debug_assert!(self.process_group > 1, "never signal init or every process");
        // SAFETY: killpg only sends a signal; the group is our own child's.
        unsafe {
            libc::killpg(self.process_group, signal);
        }
    }

    fn group_alive(&self) -> bool {
        // SAFETY: signal 0 performs only the existence and permission check.
        (unsafe { libc::killpg(self.process_group, 0) == 0 })
            || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        // Last-resort synchronous cleanup when callers forget explicit shutdown.
        if !self.shutdown_complete {
            if self.group_alive() {
                self.signal_group(libc::SIGKILL);
            }
            let _ = self.child.start_kill();
        }
        if let Some(task) = &self.reader_task {
            task.abort();
        }
        if let Some(task) = &self.stderr_task {
            task.abort();
        }
    }
}

async fn read_frames<R: AsyncRead + Unpin>(
    mut stdout: R,
    tx: mpsc::Sender<FrameEvent>,
    permits: Arc<Semaphore>,
    max: usize,
) {
    let mut frame = Vec::with_capacity(max.min(8192));
    let mut chunk = [0u8; 8192];
    loop {
        match stdout.read(&mut chunk).await {
            Ok(0) => {
                if !frame.is_empty() {
                    let _ = tx
                        .send(FrameEvent::Error(ProcessError::Io(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "partial JSON frame at EOF",
                        ))))
                        .await;
                }
                break;
            }
            Ok(n) => {
                for &byte in &chunk[..n] {
                    if byte == b'\n' {
                        let size = frame.len();
                        let permit = match Arc::clone(&permits)
                            .acquire_many_owned(
                                u32::try_from(size.max(1))
                                    .expect("frame size fits u32: queue_bytes is checked at spawn"),
                            )
                            .await
                        {
                            Ok(permit) => permit,
                            Err(_) => return,
                        };
                        let complete = std::mem::take(&mut frame).into_boxed_slice();
                        if tx.send(FrameEvent::Data(complete, permit)).await.is_err() {
                            return;
                        }
                    } else if frame.len() == max {
                        let _ = tx
                            .send(FrameEvent::Error(ProcessError::FrameTooLarge {
                                limit: max,
                            }))
                            .await;
                        return;
                    } else {
                        frame.push(byte);
                    }
                }
            }
            Err(error) => {
                let _ = tx.send(FrameEvent::Error(ProcessError::Io(error))).await;
                break;
            }
        }
    }
}

async fn read_stderr<R: AsyncRead + Unpin>(
    mut stderr: R,
    tail: Arc<Mutex<VecDeque<u8>>>,
    limit: usize,
) {
    let mut chunk = [0u8; 8192];
    loop {
        match stderr.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let mut ring = tail.lock().expect("stderr ring lock");
                for byte in &chunk[..n] {
                    if ring.len() == limit {
                        ring.pop_front();
                    }
                    if limit > 0 {
                        ring.push_back(*byte);
                    }
                }
            }
        }
    }
}
