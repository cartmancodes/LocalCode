//! Offline demo engine: no vendor process.
use super::{
    emit, BoxFuture, Channels, Command, Config, DriverError, Event, Limits, Mode, Outcome, Provider,
};
use std::time::Duration;
use tokio::sync::{mpsc, watch};

/// The offline demo's row in the provider table.
pub(super) const PROVIDER: Provider = Provider {
    name: "demo",
    title: "Demo",
    default_binary: "demo",
    offline: true,
    modes: [
        "Offline demo: /approval-demo shows the dialog",
        "Offline demo: /approval-demo shows the dialog",
        "Offline demo: /approval-demo is allowed without a dialog",
        "Offline demo: /approval-demo is allowed without a dialog",
    ],
    steer: false,
    inline_images: false,
    effort_live: false,
    efforts: &[],
    start,
};

#[expect(
    clippy::needless_pass_by_value,
    reason = "StartFn's shape: the vendor providers take ownership of their Config"
)]
fn start(config: Config, limits: Limits, channels: Channels) -> BoxFuture<Result<(), String>> {
    Box::pin(async move {
        let Channels {
            commands,
            cancel,
            stopping,
            events,
        } = channels;
        demo(
            config.mode,
            limits.approval,
            commands,
            cancel,
            stopping,
            &events,
        )
        .await
        .map_err(|error| error.to_string())
    })
}

pub(super) fn demo_set_mode(
    tx: &mpsc::Sender<Event>,
    mode: &mut Mode,
    target: Mode,
) -> Result<(), DriverError> {
    if target == Mode::FullAccess || *mode == Mode::FullAccess {
        emit(
            tx,
            Event::Notice("Full access is changed by reconnecting; use /mode".into()),
        )?;
    } else {
        *mode = target;
    }
    emit(tx, Event::ModeChanged(*mode))
}
pub(super) async fn demo(
    mut mode: Mode,
    approval: Duration,
    mut commands: mpsc::Receiver<Command>,
    mut cancel: watch::Receiver<u64>,
    mut stop: watch::Receiver<bool>,
    tx: &mpsc::Sender<Event>,
) -> Result<(), DriverError> {
    emit(
        tx,
        Event::Ready {
            session: "demo · offline".into(),
        },
    )?;
    emit(tx, Event::ModeChanged(mode))?;
    // Turn commands received, matched against the count the interface had
    // sent when it last cancelled, as the live driver does.
    let mut turns_taken = 0u64;
    loop {
        tokio::select! {
            _ = stop.changed() => break,
            _ = cancel.changed() => {}
            command = commands.recv() => {
                if let Some(turn) = command.as_ref().filter(|c| c.starts_turn()) {
                    turns_taken += 1;
                    // Marked seen, so the cancel cannot also stop a later turn.
                    if *cancel.borrow_and_update() >= turns_taken {
                        let display = turn.turn_display().unwrap_or_default().to_owned();
                        emit(tx, Event::User(display))?;
                        emit(tx, Event::Started)?;
                        emit(tx, Event::Finished { outcome: Outcome::Interrupted })?;
                        continue;
                    }
                }
                let (text, display) = match command {
                    None => break,
                    Some(Command::SetMode(target)) => {
                        demo_set_mode(tx, &mut mode, target)?;
                        continue;
                    }
                    Some(Command::Answer { .. }) => continue,
                    Some(Command::Steer(_)) => {
                        emit(tx, Event::Notice("No turn is running; send it as a prompt".into()))?;
                        continue;
                    }
                    Some(Command::SetEffort(_)) => {
                        emit(tx, Event::Notice("The offline demo has no reasoning effort".into()))?;
                        continue;
                    }
                    Some(Command::Compact) => {
                        emit(tx, Event::Notice("The offline demo has no context to compact".into()))?;
                        continue;
                    }
                    Some(Command::Prompt(text)) => (text.clone(), text),
                    Some(Command::PromptWithDisplay { wire, display, .. }) => (wire, display),
                };
                let mut turn = DemoTurn {
                    mode: &mut mode,
                    approval,
                    commands: &mut commands,
                    cancel: &mut cancel,
                    stop: &mut stop,
                    tx,
                };
                if turn.run(&text, display).await? == Flow::Stop {
                    return Ok(());
                }
            }
        }
    }
    Ok(())
}

#[derive(PartialEq, Eq)]
enum Flow {
    Continue,
    Stop,
}

/// One demo turn, borrowing the loop's channels.
struct DemoTurn<'a> {
    mode: &'a mut Mode,
    approval: Duration,
    commands: &'a mut mpsc::Receiver<Command>,
    cancel: &'a mut watch::Receiver<u64>,
    stop: &'a mut watch::Receiver<bool>,
    tx: &'a mpsc::Sender<Event>,
}

impl DemoTurn<'_> {
    async fn run(&mut self, text: &str, display: String) -> Result<Flow, DriverError> {
        emit(self.tx, Event::User(display.clone()))?;
        emit(self.tx, Event::Started)?;
        let reply = if text.trim() != "/approval-demo" {
            // Attachments make what a model receives differ from what is
            // shown; the demo shows it so the difference can be checked.
            let sent = if text == display {
                String::new()
            } else {
                format!("\n\nA model would receive:\n\n{text}")
            };
            format!(
                "This is an offline demo. Your prompt was:\n\n{display}{sent}\n\nThe editor, streaming transcript, approval dialog, history, and cancellation are live. Start with --engine codex or --engine claude to work with a model.\n\nTry /approval-demo to preview a permission request."
            )
        } else if !matches!(*self.mode, Mode::Ask | Mode::AcceptEdits) {
            emit(
                self.tx,
                Event::Notice(format!(
                    "Allowed without a dialog by {} mode (demo only)",
                    self.mode.label()
                )),
            )?;
            "Approved automatically. In a live session, the vendor's own reviewer decides."
                .to_owned()
        } else {
            match self.approval().await? {
                None => return Ok(Flow::Stop),
                Some(true) => {
                    "Approved. In a live session, the vendor would now continue.".to_owned()
                }
                Some(false) => "Denied. No action was performed.".to_owned(),
            }
        };
        let mut interrupted = false;
        for word in reply.split_inclusive(' ') {
            tokio::select! {
                _ = self.stop.changed() => return Ok(Flow::Stop),
                _ = self.cancel.changed() => {
                    interrupted = true;
                    break;
                }
                _ = tokio::time::sleep(Duration::from_millis(18)) => {
                    emit(self.tx, Event::Text(word.into()))?;
                }
            }
        }
        let outcome = if interrupted {
            Outcome::Interrupted
        } else {
            Outcome::Completed
        };
        emit(self.tx, Event::Finished { outcome })?;
        Ok(Flow::Continue)
    }

    /// Shows the demo approval. `None` means the driver is stopping.
    async fn approval(&mut self) -> Result<Option<bool>, DriverError> {
        emit(
            self.tx,
            Event::Approval {
                id: 1,
                detail: "Demo only — no command will execute.\n\nWrite a greeting to hello.txt?"
                    .into(),
            },
        )?;
        let expiry = tokio::time::sleep(self.approval);
        tokio::pin!(expiry);
        // A mode switch while the dialog is open applies and keeps waiting.
        let allowed = loop {
            tokio::select! {
                _ = self.stop.changed() => return Ok(None),
                _ = self.cancel.changed() => break false,
                _ = &mut expiry => break false,
                command = self.commands.recv() => match command {
                    Some(Command::SetMode(target)) => demo_set_mode(self.tx, self.mode, target)?,
                    Some(Command::Steer(_)) => emit(
                        self.tx,
                        Event::Notice("The offline demo cannot steer the running turn".into()),
                    )?,
                    Some(Command::SetEffort(_)) => emit(
                        self.tx,
                        Event::Notice("The offline demo has no reasoning effort".into()),
                    )?,
                    Some(Command::Compact) => emit(
                        self.tx,
                        Event::Notice("Wait for the current operation, or cancel it first".into()),
                    )?,
                    command => break matches!(command, Some(Command::Answer { id: 1, allow: true })),
                },
            }
        };
        emit(self.tx, Event::ApprovalClosed(1))?;
        Ok(Some(allowed))
    }
}
