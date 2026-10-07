//! Presentation-independent session boundary. Persist events before publishing;
//! disk and subscriber failures stop execution instead of losing output silently.
#![forbid(unsafe_code)]
pub use octet_engine::live::{
    APPROVAL_DEMO, Command, Config, DEFAULT_APPROVAL_TIMEOUT, Engine, Event, Handle, IMAGE_LIMIT,
    IMAGES_PER_PROMPT, ImageAttachment, ImageError, Mode, Outcome, PROMPT_LIMIT, SendError,
    check_inline, encoded_len, valid_effort,
};
use octet_store::Journal;
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::{sync::mpsc, time::timeout};
/// A running session: commands go in through `handle`, journaled events
/// come out of `events`.
pub struct Session {
    /// Sends prompts, answers and mode changes.
    pub handle: Handle,
    /// Events, each already in the journal.
    pub events: mpsc::Receiver<Event>,
    journal: PathBuf,
    task: tokio::task::JoinHandle<()>,
}
impl std::fmt::Debug for Session {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Session")
            .field("journal", &self.journal)
            .finish_non_exhaustive()
    }
}
impl Session {
    /// Starts a session for `config`, journaling to a new file in `directory`.
    /// # Errors
    ///
    /// Fails if the journal cannot be created or its first record written.
    pub async fn open(config: Config, directory: PathBuf) -> Result<Self, SessionError> {
        let mut journal = Journal::create(&directory)
            .await
            .map_err(SessionError::Create)?;
        let header = json!({
            "engine": config.engine.as_str(),
            "cwd": config.cwd,
            "resume": config.resume,
            "model": config.model,
            "mode": config.mode.label(),
        });
        journal
            .append("session", header, true)
            .await
            .map_err(SessionError::Write)?;
        let path = journal.path().to_path_buf();
        let (handle, engine_events, driver) = octet_engine::live::spawn(config);
        let (tx, events) = mpsc::channel(128);
        let task = tokio::spawn(pump(engine_events, journal, tx, handle.clone(), driver));
        Ok(Self {
            handle,
            events,
            journal: path,
            task,
        })
    }
    /// This session's journal file.
    pub fn journal(&self) -> &Path {
        &self.journal
    }
    /// Stops the vendor and waits for the last events to be journaled. It
    /// takes the session: there is nothing to do with one after it.
    pub async fn shutdown(mut self) {
        self.handle.shutdown();
        // Keep draining so shutdown and durable terminal events cannot wait on UI.
        loop {
            tokio::select! {
                _ = &mut self.task => break,
                event = self.events.recv() => {
                    if event.is_none() {
                        let _ = (&mut self.task).await;
                        break;
                    }
                }
            }
        }
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        self.handle.shutdown();
    }
}
/// An event as one JSON object, in the journal's record shape:
/// `{"type": kind, "data": data}`.
pub fn event_json(event: &Event) -> Value {
    let (kind, data) = record(event);
    json!({"type": kind, "data": data})
}
/// How long the interface may take to accept one event.
const DELIVERY: Duration = Duration::from_secs(2);
/// How long one journal write may take.
const JOURNAL_WRITE: Duration = Duration::from_secs(3);
/// How long the storage-failure message may wait for the interface.
const LAST_WORDS: Duration = Duration::from_secs(1);
/// How long the driver may take to stop before it is aborted.
const DRIVER_STOP: Duration = Duration::from_secs(3);

/// Hands `event` to the interface; false when it is gone or too slow.
async fn deliver(tx: &mpsc::Sender<Event>, event: Event) -> bool {
    matches!(timeout(DELIVERY, tx.send(event)).await, Ok(Ok(())))
}

/// Journals each engine event, then hands it to the interface. A storage
/// failure, or an interface that is gone or too slow, stops the session.
async fn pump(
    mut engine_events: mpsc::Receiver<Event>,
    mut journal: Journal,
    tx: mpsc::Sender<Event>,
    control: Handle,
    mut driver: tokio::task::JoinHandle<()>,
) {
    // Refusals re-send the current mode so the UI can clear its pending
    // state; the journal only records actual changes.
    let mut journaled_mode = None;
    while let Some(event) = engine_events.recv().await {
        if let Event::ModeChanged(mode) = event {
            if journaled_mode == Some(mode) {
                if !deliver(&tx, event).await {
                    break;
                }
                continue;
            }
            journaled_mode = Some(mode);
        }
        let (kind, data) = record(&event);
        let durable = matches!(event, Event::Finished { .. } | Event::Stopped);
        let written = timeout(JOURNAL_WRITE, journal.append(kind, data, durable)).await;
        if !matches!(written, Ok(Ok(()))) {
            let reason = match written {
                Ok(Err(e)) => e.to_string(),
                _ => "Journal write timed out".into(),
            };
            let message = format!(
                "Storage failure: {reason}. Session stopped; journal may have an incomplete tail."
            );
            let _ = timeout(LAST_WORDS, tx.send(Event::Error(message))).await;
            break;
        }
        if !deliver(&tx, event).await {
            // Stopping on a stalled interface is by design; doing it silently
            // is not. The journal says why the session ended.
            let reason = "The interface did not keep up with the vendor; session stopped.";
            let _ = timeout(JOURNAL_WRITE, journal.append("error", json!(reason), true)).await;
            break;
        }
    }
    control.shutdown();
    // The driver's last events have nowhere to go: dropping the receiver lets
    // its final sends fail at once instead of waiting out their timeouts.
    drop(engine_events);
    if timeout(DRIVER_STOP, &mut driver).await.is_err() {
        driver.abort();
        let _ = driver.await;
    }
}

fn record(event: &Event) -> (&'static str, Value) {
    match event {
        Event::Models(models) => {
            let models: Vec<Value> = models
                .iter()
                .map(|m| {
                    json!({
                        "selection": m.selection,
                        "id": m.id,
                        "name": m.name,
                        "description": m.description,
                    })
                })
                .collect();
            ("models", json!(models))
        }
        Event::ModelSelected(id) => ("model_selected", json!(id)),
        Event::ModeChanged(mode) => ("mode", json!(mode.label())),
        Event::Ready { session } => ("ready", json!(session)),
        Event::User(text) => ("user", json!(text)),
        Event::Started => ("started", Value::Null),
        Event::Text(text) => ("text", json!(text)),
        Event::Tool(text) => ("tool", json!(text)),
        Event::Approval { id, detail } => ("approval", json!({"id":id,"detail":detail})),
        Event::ApprovalClosed(id) => ("approval_closed", json!(id)),
        Event::Usage(text) => ("usage", json!(text)),
        Event::Finished { outcome } => ("finished", json!(outcome.as_str())),
        Event::Notice(text) => ("notice", json!(text)),
        Event::Error(text) => ("error", json!(text)),
        Event::Stopped => ("stopped", Value::Null),
    }
}

/// Export a bounded journal snapshot without overwriting any existing file.
///
/// # Errors
///
/// Fails if the journal cannot be read, or `target` exists or cannot be
/// written.
pub async fn export_journal(
    source: &std::path::Path,
    target: &std::path::Path,
) -> Result<(), ExportError> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let read = |error| ExportError::Read {
        path: source.to_owned(),
        error,
    };
    let input = tokio::fs::File::open(source).await.map_err(read)?;
    let size = input.metadata().await.map_err(read)?.len();
    let mut output =
        octet_store::create_private(target)
            .await
            .map_err(|error| ExportError::Create {
                path: target.to_owned(),
                error,
            })?;
    let copied = async {
        tokio::io::copy(&mut input.take(size), &mut output).await?;
        output.flush().await?;
        output.sync_data().await
    }
    .await;
    if let Err(error) = copied {
        // A partial copy would look like a real export, and a retry to the
        // same path would then fail.
        drop(output);
        let _ = tokio::fs::remove_file(target).await;
        return Err(ExportError::Write {
            path: target.to_owned(),
            error,
        });
    }
    Ok(())
}

mod error;
pub use error::{ExportError, GoalError, SelectionError, SessionError};
pub mod goal;
pub mod model;
pub mod sessions;
pub use octet_store::JournalSummary;
pub use sessions::{RecentSession, recent_sessions};

pub use octet_engine::live::ModelInfo;

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_failed_export_leaves_no_file() {
        let temp = octet_testkit::TempDir::new("octet-core-export");
        std::fs::create_dir_all(temp.path()).unwrap();
        // A directory opens, but reading it fails mid-copy.
        let source = temp.path().join("not-a-journal");
        std::fs::create_dir_all(&source).unwrap();
        let target = temp.path().join("export.jsonl");
        assert!(export_journal(&source, &target).await.is_err());
        assert!(!target.exists(), "a partial export was left behind");
    }
    #[tokio::test]
    async fn a_consumer_stall_is_journaled() {
        let temp = octet_testkit::TempDir::new("octet-core-stall");
        let journal = Journal::create(temp.path()).await.unwrap();
        let path = journal.path().to_path_buf();
        let config = Config::new(Engine::DEMO, "demo", temp.path());
        let (handle, _driver_events, driver) = octet_engine::live::spawn(config);
        let (engine_tx, engine_rx) = mpsc::channel(8);
        // The interface takes one event and then never reads again.
        let (tx, _never_read) = mpsc::channel(1);
        let pump = tokio::spawn(pump(engine_rx, journal, tx, handle, driver));
        for text in ["a", "b", "c"] {
            let _ = engine_tx.send(Event::Text(text.into())).await;
        }
        tokio::time::timeout(Duration::from_secs(10), pump)
            .await
            .expect("the pump stopped")
            .unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        let last: Value = serde_json::from_str(text.lines().last().unwrap()).unwrap();
        assert_eq!(last["type"], "error", "{text}");
        assert!(
            last["data"].as_str().unwrap().contains("did not keep up"),
            "{last}"
        );
    }
    #[tokio::test]
    async fn a_closed_receiver_is_not_a_delivery() {
        let (tx, rx) = mpsc::channel(1);
        drop(rx);
        assert!(!deliver(&tx, Event::Started).await);
        let (tx, mut rx) = mpsc::channel(1);
        assert!(deliver(&tx, Event::Started).await);
        assert!(matches!(rx.recv().await, Some(Event::Started)));
    }
}
