//! The vendor process loop shared by Claude and Codex. Protocol details live
//! in `claude.rs` and `codex.rs` as further `impl Driver` blocks.
use super::{
    claude, codex, emit, Command, Config, DriverError, Engine, Event, Limits, Mode, ModelInfo,
    EVENT_BYTES,
};
use lc_proc::{Process, ProcessConfig};
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    time::Duration,
};
use tokio::{
    sync::{mpsc, watch},
    time::{timeout, Instant},
};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Protocol {
    Claude,
    Codex,
}

/// A vendor approval shown to the user, denied when its deadline passes.
pub(super) struct Pending {
    pub(super) wire: Value,
    pub(super) deadline: Instant,
}

/// A Claude mode switch awaiting its control_response.
pub(super) struct ModeRequest {
    pub(super) id: String,
    pub(super) target: Mode,
    pub(super) deadline: Instant,
}

/// One vendor connection's state. Fields are grouped by who uses them;
/// `claude.rs` and `codex.rs` add the protocol-specific methods.
pub(super) struct Driver<'a> {
    pub(super) protocol: Protocol,
    pub(super) config: Config,
    pub(super) limits: Limits,
    pub(super) process: Process,
    pub(super) tx: &'a mpsc::Sender<Event>,
    // Connection
    pub(super) initialized: bool,
    pub(super) ready: bool,
    pub(super) session: String,
    pub(super) mode: Mode,
    // Turn
    pub(super) running: bool,
    pub(super) interrupt_pending: bool,
    /// Connect deadline, then the turn's silence watchdog.
    pub(super) deadline: Instant,
    pub(super) request_id: u64,
    pub(super) pending: HashMap<u64, Pending>,
    // Claude
    pub(super) selected_model: String,
    pub(super) streamed: bool,
    pub(super) mode_request: Option<ModeRequest>,
    /// Timed-out switches Claude may still confirm; the header must never show
    /// a stricter mode than the vendor is really in.
    pub(super) late_modes: Vec<(String, Mode)>,
    pub(super) mode_seq: u64,
    // Codex
    pub(super) turn: Option<String>,
    pub(super) start_request: Option<u64>,
    pub(super) interrupt_request: Option<u64>,
    pub(super) text_items: HashSet<String>,
    pub(super) catalog: Vec<ModelInfo>,
    pub(super) catalog_pages: usize,
    pub(super) catalog_cursors: HashSet<String>,
}

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
    fn new(
        protocol: Protocol,
        config: Config,
        limits: Limits,
        process: Process,
        tx: &'a mpsc::Sender<Event>,
    ) -> Self {
        Self {
            protocol,
            mode: config.mode,
            config,
            limits,
            process,
            tx,
            initialized: false,
            ready: false,
            session: String::new(),
            running: false,
            interrupt_pending: false,
            deadline: Instant::now() + limits.connect,
            request_id: 10,
            pending: HashMap::new(),
            selected_model: String::new(),
            streamed: false,
            mode_request: None,
            late_modes: Vec::new(),
            mode_seq: 0,
            turn: None,
            start_request: None,
            interrupt_request: None,
            text_items: HashSet::new(),
            catalog: Vec::new(),
            catalog_pages: 0,
            catalog_cursors: HashSet::new(),
        }
    }

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
        self.deadline = Instant::now() + self.limits.connect;
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

    /// The earliest timer. The connect/turn deadline is stale when idle;
    /// counting it then would make every idle timer fire immediately and spin.
    /// It is also paused while an approval waits: the vendor is then silent
    /// because of us.
    fn next_wake(&self) -> Instant {
        let watchdog =
            ((!self.ready || self.running) && self.pending.is_empty()).then_some(self.deadline);
        self.pending
            .values()
            .map(|pending| pending.deadline)
            .chain(self.mode_request.as_ref().map(|request| request.deadline))
            .chain(watchdog)
            .min()
            .unwrap_or(self.deadline)
    }

    fn timers_armed(&self) -> bool {
        !self.ready || self.running || !self.pending.is_empty() || self.mode_request.is_some()
    }

    async fn on_cancel(&mut self) -> Result<(), DriverError> {
        if !self.ready {
            return Err(DriverError::Cancelled);
        }
        if !self.running {
            return Ok(());
        }
        self.interrupt_pending = true;
        match self.protocol {
            Protocol::Claude => self.claude_interrupt().await?,
            Protocol::Codex => self.codex_interrupt().await?,
        }
        self.deadline = Instant::now() + self.limits.interrupt;
        self.deny_all_pending().await
    }

    async fn on_timer(&mut self) -> Result<(), DriverError> {
        // The connect/turn deadline only applies while connecting or in a turn;
        // when idle it is stale and other timers can wake this branch.
        if (!self.ready || self.running)
            && self.pending.is_empty()
            && Instant::now() >= self.deadline
        {
            return Err(self.watchdog_error().into());
        }
        let expired: Vec<u64> = self
            .pending
            .iter()
            .filter(|(_, pending)| pending.deadline <= Instant::now())
            .map(|(id, _)| *id)
            .collect();
        for id in expired {
            let Some(pending) = self.pending.remove(&id) else {
                continue;
            };
            self.send(self.answer(&pending.wire, false)).await?;
            self.emit(Event::ApprovalClosed(id))?;
            self.emit(Event::Notice("Approval timed out and was denied".into()))?;
            self.resume_watchdog();
        }
        if let Some(request) = self
            .mode_request
            .take_if(|request| request.deadline <= Instant::now())
        {
            let notice = format!(
                "Claude has not confirmed the switch to {}; the header shows {} until it does",
                request.target.label(),
                self.mode.label()
            );
            self.late_modes.push((request.id, request.target));
            self.emit(Event::Notice(notice))?;
            self.emit(Event::ModeChanged(self.mode))?;
        }
        Ok(())
    }

    fn watchdog_error(&self) -> String {
        if !self.ready {
            format!(
                "Vendor did not finish connecting within {}; session stopped.",
                seconds(self.limits.connect)
            )
        } else if self.interrupt_pending {
            format!(
                "Vendor did not stop within {} of the interrupt; session stopped. Resume using the session ID.",
                seconds(self.limits.interrupt)
            )
        } else {
            format!(
                "Vendor sent nothing for {}; session stopped. Resume using the session ID.",
                seconds(self.limits.turn_idle)
            )
        }
    }

    async fn on_command(&mut self, command: Command) -> Result<(), DriverError> {
        match command {
            Command::Prompt(text) => self.start_turn(text.clone(), text).await,
            Command::PromptWithDisplay { wire, display } => self.start_turn(wire, display).await,
            Command::Answer { id, allow } => self.answer_approval(id, allow).await,
            Command::SetMode(target) => self.set_mode(target).await,
        }
    }

    async fn start_turn(&mut self, wire: String, display: String) -> Result<(), DriverError> {
        if !self.ready || self.running {
            return self.emit(Event::Notice(
                "Wait for the current operation, or cancel it first".into(),
            ));
        }
        self.running = true;
        self.turn = None;
        self.streamed = false;
        self.text_items.clear();
        self.interrupt_pending = false;
        self.deadline = Instant::now() + self.limits.turn_idle;
        self.emit(Event::User(display))?;
        self.emit(Event::Started)?;
        match self.protocol {
            Protocol::Claude => self.claude_send_prompt(&wire).await,
            Protocol::Codex => self.codex_send_prompt(&wire).await,
        }
    }

    async fn answer_approval(&mut self, id: u64, allow: bool) -> Result<(), DriverError> {
        let Some(pending) = self.pending.remove(&id) else {
            return self.emit(Event::Notice("That approval is no longer active".into()));
        };
        self.send(self.answer(&pending.wire, allow)).await?;
        self.emit(Event::ApprovalClosed(id))?;
        // The silence watchdog was paused while the user decided.
        self.resume_watchdog();
        Ok(())
    }

    async fn set_mode(&mut self, target: Mode) -> Result<(), DriverError> {
        // Refusals re-emit the current mode so the TUI clears its pending indicator.
        if target == Mode::FullAccess || self.mode == Mode::FullAccess {
            self.emit(Event::Notice(
                "Full access is changed by reconnecting; use /mode".into(),
            ))?;
            self.emit(Event::ModeChanged(self.mode))
        } else if !self.ready {
            self.emit(Event::Notice(
                "Wait for the connection before changing modes".into(),
            ))?;
            self.emit(Event::ModeChanged(self.mode))
        } else if self.mode_request.is_some() {
            self.emit(Event::Notice("A mode change is already pending".into()))
        } else if target == self.mode {
            self.emit(Event::ModeChanged(self.mode))
        } else {
            match self.protocol {
                Protocol::Claude => self.claude_request_mode(target).await,
                Protocol::Codex => self.codex_set_mode(target),
            }
        }
    }

    async fn on_frame(&mut self, frame: Value) -> Result<(), DriverError> {
        // The turn limit is a silence watchdog, not a cap on turn length. Only
        // frames for this session count: another thread's chatter is not progress.
        let ours = self.protocol == Protocol::Claude
            || frame
                .pointer("/params/threadId")
                .and_then(Value::as_str)
                .is_none_or(|id| id == self.session);
        if self.running && !self.interrupt_pending && ours {
            self.deadline = Instant::now() + self.limits.turn_idle;
        }
        match self.protocol {
            Protocol::Claude => self.claude_frame(frame).await?,
            Protocol::Codex => self.codex_frame(frame).await?,
        }
        if !self.initialized && self.ready {
            return Err("Unexpected protocol initialization order".into());
        }
        Ok(())
    }

    pub(super) fn emit(&self, event: Event) -> Result<(), DriverError> {
        emit(self.tx, event)
    }

    pub(super) async fn send(&self, value: Value) -> Result<(), DriverError> {
        timeout(Duration::from_secs(3), self.process.sender().send(&value))
            .await
            .map_err(|_| DriverError::from("Vendor stdin is unresponsive"))?
            .map_err(|e| DriverError::from(e.to_string()))
    }

    /// The wire reply allowing or denying a vendor approval.
    pub(super) fn answer(&self, wire: &Value, allow: bool) -> Value {
        match self.protocol {
            Protocol::Claude => claude::answer(wire, allow),
            Protocol::Codex => codex::answer(wire, allow),
        }
    }

    /// Restarts the silence watchdog once no approval is waiting on the user.
    pub(super) fn resume_watchdog(&mut self) {
        if self.pending.is_empty() && self.running && !self.interrupt_pending {
            self.deadline = Instant::now() + self.limits.turn_idle;
        }
    }

    /// Shows a vendor approval to the user, or denies it when it is too large
    /// to show completely.
    pub(super) async fn queue_approval(
        &mut self,
        wire: Value,
        detail: String,
    ) -> Result<(), DriverError> {
        if detail.len() > EVENT_BYTES {
            self.send(self.answer(&wire, false)).await?;
            return self.emit(Event::Notice(
                "Oversized approval denied: cannot show the complete request".into(),
            ));
        }
        self.request_id += 1;
        self.emit(Event::Approval {
            id: self.request_id,
            detail,
        })?;
        let deadline = Instant::now() + self.limits.approval;
        self.pending
            .insert(self.request_id, Pending { wire, deadline });
        Ok(())
    }

    async fn deny_all_pending(&mut self) -> Result<(), DriverError> {
        for (id, pending) in std::mem::take(&mut self.pending) {
            self.send(self.answer(&pending.wire, false)).await?;
            self.emit(Event::ApprovalClosed(id))?;
        }
        Ok(())
    }

    /// The turn ended: its approvals no longer apply.
    pub(super) fn close_all_pending(&mut self) -> Result<(), DriverError> {
        for id in std::mem::take(&mut self.pending).into_keys() {
            self.emit(Event::ApprovalClosed(id))?;
        }
        Ok(())
    }
}

/// A failure plus what the vendor itself said on stderr, which usually names
/// the real cause (an unknown session ID, an expired login).
pub(super) fn with_stderr(error: DriverError, engine: Engine, tail: &[u8]) -> String {
    // Failures the driver itself caused say nothing about the vendor.
    let DriverError::Vendor(error) = error else {
        return error.to_string();
    };
    let mut end = &tail[tail.len().saturating_sub(1024)..];
    if end.len() < tail.len() {
        // Cut mid-stream: start at the next whole line.
        if let Some(newline) = end.iter().position(|byte| *byte == b'\n') {
            end = &end[newline + 1..];
        }
    }
    let text = String::from_utf8_lossy(end);
    let text = text.trim();
    if text.is_empty() {
        error
    } else {
        format!("{error}\n{engine} stderr: {text}")
    }
}
pub(super) fn seconds(limit: Duration) -> String {
    match limit.as_secs() {
        0 => format!("{} ms", limit.as_millis()),
        1 => "1 second".into(),
        n => format!("{n} seconds"),
    }
}
#[cfg(test)]
mod tests {
    use super::super::{
        claude::{claude_result_error, claude_stray_reply},
        codex::{codex_stray_reply, error_text},
    };
    use super::*;
    use serde_json::json;
    #[test]
    fn stderr_is_attached_only_to_vendor_failures_and_starts_on_a_line() {
        let vendor = || DriverError::from("Vendor disconnected.");
        assert_eq!(
            with_stderr(vendor(), Engine::Claude, b"reason\n"),
            "Vendor disconnected.\nclaude stderr: reason"
        );
        assert_eq!(
            with_stderr(vendor(), Engine::Claude, b" \n"),
            "Vendor disconnected."
        );
        for (local, text) in [
            (DriverError::Cancelled, "Connection cancelled"),
            (
                DriverError::ConsumerOverloaded,
                "Output consumer overloaded; session stopped",
            ),
            (
                DriverError::TurnItemLimit,
                "Turn item limit reached; session stopped",
            ),
        ] {
            assert_eq!(
                with_stderr(local, Engine::Codex, b"unrelated log line\n"),
                text
            );
        }
        let mut long = vec![b'a'; 1500];
        long.extend_from_slice("\nlast line é\n".as_bytes());
        let text = with_stderr("x".into(), Engine::Codex, &long);
        assert!(text.ends_with("codex stderr: last line é"), "{text}");
    }
    #[test]
    fn stray_request_replies_use_production_wording() {
        let permission = json!({"type":"control_request","request_id":"r1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{}}});
        let unknown = json!({"type":"control_request","request_id":"r2","request":{"subtype":"hook_callback"}});
        let codex = json!({"id":7,"method":"item/tool/requestUserInput","params":{}});
        for reply in [
            claude_stray_reply(&permission).unwrap(),
            claude_stray_reply(&unknown).unwrap(),
            codex_stray_reply(&codex).unwrap(),
        ] {
            assert!(!reply.to_string().contains("fixture"), "{reply}");
        }
        assert_eq!(
            claude_stray_reply(&permission).unwrap()["response"]["response"]["behavior"],
            "deny"
        );
        assert_eq!(
            claude_stray_reply(&unknown).unwrap()["response"]["subtype"],
            "error"
        );
        assert_eq!(codex_stray_reply(&codex).unwrap()["error"]["code"], -32601);
        let approval = json!({"id":8,"method":"item/commandExecution/requestApproval","params":{}});
        assert_eq!(
            codex_stray_reply(&approval).unwrap()["result"]["decision"],
            "decline"
        );
    }
    #[test]
    fn error_text_keeps_structured_kinds_and_names_missing_errors() {
        assert_eq!(
            error_text(&json!({"message":"bad","codexErrorInfo":{"httpStatus":429}})),
            "bad ({\"httpStatus\":429})"
        );
        assert_eq!(
            error_text(&Value::Null),
            "The vendor reported an error without details"
        );
        assert_eq!(
            claude_result_error(&json!({"is_error":true,"subtype":"error_max_turns"})),
            "Claude returned an error (error_max_turns)"
        );
        assert_eq!(seconds(Duration::from_millis(400)), "400 ms");
        assert_eq!(seconds(Duration::from_secs(600)), "600 seconds");
    }
}
