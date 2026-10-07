//! The vendor process loop every protocol shares. Each vendor's wire format
//! lives in its own file as a `Protocol`; this loop owns the timers,
//! approvals and commands, and calls the protocol for the rest.
use super::{
    protocol::{Core, Phase, Protocol},
    Channels, Command, Config, DriverError, Engine, Event, ImageAttachment, Limits, Mode, Outcome,
    TurnGate, BUSY, FULL_ACCESS_RECONNECTS, NO_TURN,
};
use octet_proc::{Process, ProcessConfig};
use serde_json::Value;
use std::time::Duration;
use tokio::{
    sync::{mpsc, watch},
    time::Instant,
};

/// One vendor connection: the shared core and the vendor's protocol.
struct Driver<P> {
    core: Core,
    protocol: P,
    /// Turn commands the user cancelled before they started.
    gate: TurnGate,
}

/// Runs one session with protocol `P` until it stops.
pub(super) async fn run<P: Protocol>(
    config: Config,
    limits: Limits,
    channels: Channels,
) -> Result<(), String> {
    let Channels {
        mut commands,
        mut cancel,
        mut stopping,
        events,
    } = channels;
    let engine = config.engine;
    let process = Process::spawn(&ProcessConfig {
        executable: config.binary.clone(),
        args: P::launch_args(&config),
        cwd: Some(config.cwd.clone()),
        max_frame_bytes: 8 * 1024 * 1024,
        queue_bytes: 16 * 1024 * 1024,
        stderr_bytes: 4096,
        shutdown_grace: Duration::from_millis(150),
        term_grace: Duration::from_millis(250),
    })
    .map_err(|e| format!("Cannot start {engine}: {e}. Install the CLI and sign in first."))?;
    let mut driver = Driver {
        core: Core::new::<P>(config, limits, process, events),
        protocol: P::default(),
        gate: TurnGate::default(),
    };
    let result = driver.run(&mut commands, &mut cancel, &mut stopping).await;
    let report = driver.core.process.shutdown().await;
    let verified = report.reaped && report.descendants_stopped;
    finish(result, verified, engine, &report.stderr_tail)
}

/// The session's result: the vendor's error with its stderr, and a note when
/// its processes could not be confirmed stopped. Neither hides the other.
fn finish(
    result: Result<(), DriverError>,
    verified: bool,
    engine: Engine,
    stderr: &[u8],
) -> Result<(), String> {
    const UNVERIFIED: &str = "Could not verify all vendor children stopped";
    match (result, verified) {
        (Ok(()), true) => Ok(()),
        (Ok(()), false) => Err(UNVERIFIED.into()),
        (Err(error), true) => Err(with_stderr(error, engine, stderr)),
        (Err(error), false) => Err(format!(
            "{}\n{UNVERIFIED}",
            with_stderr(error, engine, stderr)
        )),
    }
}

impl<P: Protocol> Driver<P> {
    async fn run(
        &mut self,
        commands: &mut mpsc::Receiver<Command>,
        cancel: &mut watch::Receiver<u64>,
        stopping: &mut watch::Receiver<bool>,
    ) -> Result<(), DriverError> {
        self.protocol.initialize(&mut self.core).await?;
        self.core.deadline = Instant::now() + self.core.limits.connect;
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
                    Some(command) => {
                        if self.gate.cancelled(&command, cancel) {
                            self.cancelled_before_start(&command)?;
                        } else {
                            self.on_command(command).await?;
                        }
                    }
                },
                frame = self.core.process.next_frame() => {
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
        let core = &self.core;
        let watchdog =
            (core.phase.watchdog_applies() && core.pending.is_empty()).then_some(core.deadline);
        core.pending
            .values()
            .map(|pending| pending.deadline)
            .chain(self.protocol.deadline())
            .chain(watchdog)
            .min()
            .unwrap_or(core.deadline)
    }

    fn timers_armed(&self) -> bool {
        let core = &self.core;
        core.phase.watchdog_applies()
            || !core.pending.is_empty()
            || self.protocol.deadline().is_some()
    }

    async fn on_cancel(&mut self) -> Result<(), DriverError> {
        if !self.core.phase.is_ready() {
            return Err(DriverError::Cancelled);
        }
        if !self.core.phase.is_running() {
            return Ok(());
        }
        self.core.phase = Phase::Interrupting;
        self.protocol.interrupt(&mut self.core).await?;
        self.core.deadline = Instant::now() + self.core.limits.interrupt;
        self.core.deny_all_pending().await
    }

    async fn on_timer(&mut self) -> Result<(), DriverError> {
        // The connect/turn deadline only applies while connecting or in a turn;
        // when idle it is stale and other timers can wake this branch.
        let core = &mut self.core;
        if core.phase.watchdog_applies()
            && core.pending.is_empty()
            && Instant::now() >= core.deadline
        {
            return Err(self.watchdog_error().into());
        }
        let expired: Vec<u64> = core
            .pending
            .iter()
            .filter(|(_, pending)| pending.deadline <= Instant::now())
            .map(|(id, _)| *id)
            .collect();
        for id in expired {
            let Some(pending) = core.pending.remove(&id) else {
                continue;
            };
            core.send(core.answer(&pending.wire, false)).await?;
            core.emit(Event::ApprovalClosed(id))?;
            core.emit(Event::Notice("Approval timed out and was denied".into()))?;
            core.resume_watchdog();
        }
        self.protocol.on_deadline(&mut self.core).await
    }

    fn watchdog_error(&self) -> String {
        let core = &self.core;
        if !core.phase.is_ready() {
            format!(
                "Vendor did not finish connecting within {}; session stopped.",
                seconds(core.limits.connect)
            )
        } else if core.phase == Phase::Interrupting {
            format!(
                "Vendor did not stop within {} of the interrupt; session stopped. Resume using the session ID.",
                seconds(core.limits.interrupt)
            )
        } else {
            format!(
                "Vendor sent nothing for {}; session stopped. Resume using the session ID.",
                seconds(core.limits.turn_idle)
            )
        }
    }

    /// A turn cancelled before it reached the vendor: recorded as an
    /// interrupted turn, never sent.
    fn cancelled_before_start(&self, command: &Command) -> Result<(), DriverError> {
        let display = command.turn_display().unwrap_or_default().to_owned();
        self.core.emit(Event::User(display))?;
        self.core.emit(Event::Started)?;
        self.core.emit(Event::Finished {
            outcome: Outcome::Interrupted,
        })
    }

    async fn on_command(&mut self, command: Command) -> Result<(), DriverError> {
        match command {
            Command::Prompt(text) => self.start_turn(text.clone(), text, Vec::new()).await,
            Command::PromptWithDisplay {
                wire,
                display,
                images,
            } => self.start_turn(wire, display, images).await,
            Command::Answer { id, allow } => self.answer_approval(id, allow).await,
            Command::SetMode(target) => self.set_mode(target).await,
            Command::Steer(text) => self.steer(text).await,
            Command::SetEffort(effort) => self.set_effort(effort),
            Command::Compact => self.compact().await,
        }
    }

    async fn start_turn(
        &mut self,
        wire: String,
        display: String,
        images: Vec<ImageAttachment>,
    ) -> Result<(), DriverError> {
        if self.begin_turn(display)? {
            self.protocol
                .send_prompt(&mut self.core, &wire, &images)
                .await?;
        }
        Ok(())
    }

    /// Compaction runs as a turn, so Esc cancels it and its events stream.
    async fn compact(&mut self) -> Result<(), DriverError> {
        if self.begin_turn("/compact".into())? {
            self.protocol.compact(&mut self.core).await?;
        }
        Ok(())
    }

    /// Starts a turn shown as `display`; false (with a notice) when the
    /// session is not ready for one.
    fn begin_turn(&mut self, display: String) -> Result<bool, DriverError> {
        if !self.core.phase.is_ready() || self.core.phase.is_running() {
            self.core.emit(Event::Notice(BUSY.into()))?;
            return Ok(false);
        }
        self.core.phase = Phase::InTurn;
        self.protocol.turn_started();
        self.core.deadline = Instant::now() + self.core.limits.turn_idle;
        self.core.emit(Event::User(display))?;
        self.core.emit(Event::Started)?;
        Ok(true)
    }

    /// Live where the provider takes effort per turn; otherwise the
    /// interface reconnects instead of sending this.
    fn set_effort(&mut self, effort: Option<String>) -> Result<(), DriverError> {
        let provider = self.core.config.engine.provider();
        if !provider.effort_live {
            return self.core.emit(Event::Notice(format!(
                "{} takes reasoning effort at launch; it changes on reconnect",
                provider.title
            )));
        }
        let shown = effort.as_deref().unwrap_or("vendor default").to_owned();
        self.core.config.effort = effort;
        let when = if self.core.phase.is_running() {
            "from the next turn"
        } else {
            "from now on"
        };
        self.core
            .emit(Event::Notice(format!("Reasoning effort: {shown} ({when})")))
    }

    async fn steer(&mut self, text: String) -> Result<(), DriverError> {
        match self.core.phase {
            Phase::InTurn => {}
            Phase::Interrupting => {
                return self.core.emit(Event::Notice(format!(
                    "The turn is stopping; this steer was not sent: {text}"
                )));
            }
            _ => {
                return self.core.emit(Event::Notice(NO_TURN.into()));
            }
        }
        if self.protocol.steer(&mut self.core, &text).await? {
            self.core.emit(Event::User(format!("[steer] {text}")))
        } else {
            let title = self.core.config.engine.title();
            self.core.emit(Event::Notice(format!(
                "{title} cannot steer the running turn"
            )))
        }
    }

    async fn answer_approval(&mut self, id: u64, allow: bool) -> Result<(), DriverError> {
        let core = &mut self.core;
        let Some(pending) = core.pending.remove(&id) else {
            return core.emit(Event::Notice("That approval is no longer active".into()));
        };
        core.send(core.answer(&pending.wire, allow)).await?;
        core.emit(Event::ApprovalClosed(id))?;
        // The silence watchdog was paused while the user decided.
        core.resume_watchdog();
        Ok(())
    }

    async fn set_mode(&mut self, target: Mode) -> Result<(), DriverError> {
        // Refusals re-emit the current mode so the TUI clears its pending indicator.
        let core = &self.core;
        if target == Mode::FullAccess || core.mode == Mode::FullAccess {
            core.emit(Event::Notice(FULL_ACCESS_RECONNECTS.into()))?;
            core.emit(Event::ModeChanged(core.mode))
        } else if !core.phase.is_ready() {
            core.emit(Event::Notice(
                "Wait for the connection before changing modes".into(),
            ))?;
            core.emit(Event::ModeChanged(core.mode))
        } else if self.protocol.mode_change_pending() {
            core.emit(Event::Notice("A mode change is already pending".into()))
        } else if target == core.mode {
            core.emit(Event::ModeChanged(core.mode))
        } else {
            self.protocol.set_mode(&mut self.core, target).await
        }
    }

    async fn on_frame(&mut self, frame: Value) -> Result<(), DriverError> {
        // The turn limit is a silence watchdog, not a cap on turn length. Only
        // frames the protocol counts as progress reset it.
        let ours = self.protocol.is_progress(&self.core, &frame);
        if self.core.phase == Phase::InTurn && ours {
            self.core.deadline = Instant::now() + self.core.limits.turn_idle;
        }
        self.protocol.on_frame(&mut self.core, frame).await
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
    #[test]
    fn an_unverified_shutdown_keeps_the_vendor_error() {
        let vendor: Result<(), DriverError> = Err("Vendor disconnected".into());
        let error = finish(vendor, false, Engine::CODEX, b"login expired\n").unwrap_err();
        assert_eq!(
            error,
            "Vendor disconnected\ncodex stderr: login expired\n\
             Could not verify all vendor children stopped"
        );
        assert_eq!(
            finish(Ok(()), false, Engine::CODEX, b"").unwrap_err(),
            "Could not verify all vendor children stopped"
        );
        assert_eq!(finish(Ok(()), true, Engine::CODEX, b""), Ok(()));
    }
    use serde_json::json;
    #[test]
    fn stderr_is_attached_only_to_vendor_failures_and_starts_on_a_line() {
        let vendor = || DriverError::from("Vendor disconnected.");
        assert_eq!(
            with_stderr(vendor(), Engine::CLAUDE, b"reason\n"),
            "Vendor disconnected.\nclaude stderr: reason"
        );
        assert_eq!(
            with_stderr(vendor(), Engine::CLAUDE, b" \n"),
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
                with_stderr(local, Engine::CODEX, b"unrelated log line\n"),
                text
            );
        }
        let mut long = vec![b'a'; 1500];
        long.extend_from_slice("\nlast line é\n".as_bytes());
        let text = with_stderr("x".into(), Engine::CODEX, &long);
        assert!(text.ends_with("codex stderr: last line é"), "{text}");
    }
    #[test]
    fn stray_request_replies_use_production_wording() {
        let request = json!({"subtype": "can_use_tool", "tool_name": "Bash", "input": {}});
        let permission = json!({"type": "control_request", "request_id": "r1", "request": request});
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
