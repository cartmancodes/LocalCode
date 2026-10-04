//! Presentation-independent session boundary. Persist events before publishing;
//! disk and subscriber failures stop execution instead of losing output silently.
pub use lc_engine::live::{Command, Config, Engine, Event, Handle, Mode, Outcome, PROMPT_LIMIT};
use lc_store::Journal;
use serde_json::{json, Value};
use std::{path::PathBuf, time::Duration};
use tokio::{sync::mpsc, time::timeout};
pub struct Session {
    pub handle: Handle,
    pub events: mpsc::Receiver<Event>,
    pub journal: PathBuf,
    task: tokio::task::JoinHandle<()>,
}
impl Session {
    pub async fn open(config: Config, directory: PathBuf) -> Result<Self, String> {
        let mut journal = Journal::create(&directory)
            .await
            .map_err(|e| format!("Cannot create transcript journal: {e}"))?;
        journal.append("session",json!({"engine":config.engine.as_str(),"cwd":config.cwd,"resume":config.resume,"model":config.model,"mode":config.mode.label()}),true).await.map_err(|e|format!("Cannot write transcript journal: {e}"))?;
        let path = journal.path.clone();
        let (handle, mut engine_events, mut driver) = lc_engine::live::spawn(config);
        let control = handle.clone();
        let (tx, events) = mpsc::channel(128);
        let task = tokio::spawn(async move {
            // Refusals re-send the current mode so the UI can clear its pending
            // state; the journal only records actual changes.
            let mut journaled_mode = None;
            while let Some(event) = engine_events.recv().await {
                if let Event::ModeChanged(mode) = event {
                    if journaled_mode == Some(mode) {
                        if timeout(Duration::from_secs(2), tx.send(event))
                            .await
                            .is_err()
                        {
                            control.shutdown();
                            break;
                        }
                        continue;
                    }
                    journaled_mode = Some(mode);
                }
                let (kind, data) = record(&event);
                let durable = matches!(event, Event::Finished { .. } | Event::Stopped);
                let written =
                    timeout(Duration::from_secs(3), journal.append(kind, data, durable)).await;
                if !matches!(written, Ok(Ok(()))) {
                    control.shutdown();
                    let reason = match written {
                        Ok(Err(e)) => e.to_string(),
                        _ => "Journal write timed out".into(),
                    };
                    let _=timeout(Duration::from_secs(1),tx.send(Event::Error(format!("Storage failure: {reason}. Session stopped; journal may have an incomplete tail.")))).await;
                    break;
                }
                if timeout(Duration::from_secs(2), tx.send(event))
                    .await
                    .is_err()
                {
                    control.shutdown();
                    break;
                }
                if tx.is_closed() {
                    control.shutdown();
                    break;
                }
            }
            control.shutdown();
            if timeout(Duration::from_secs(3), &mut driver).await.is_err() {
                driver.abort();
                let _ = driver.await;
            }
        });
        Ok(Self {
            handle,
            events,
            journal: path,
            task,
        })
    }
    pub async fn shutdown(&mut self) {
        self.handle.shutdown();
        // Keep draining so shutdown and durable terminal events cannot wait on UI.
        loop {
            tokio::select! {
                _=&mut self.task=>break,
                event=self.events.recv()=>{if event.is_none(){let _=(&mut self.task).await;break;}}
            }
        }
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        self.handle.shutdown();
    }
}
fn record(event: &Event) -> (&'static str, Value) {
    match event {
        Event::Models(models) => ("models", json!(models.iter().map(|m|json!({"selection":m.selection,"id":m.id,"name":m.name,"description":m.description})).collect::<Vec<_>>())),
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
pub async fn export_journal(
    source: &std::path::Path,
    target: &std::path::Path,
) -> Result<(), String> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let read = |e: std::io::Error| format!("Cannot read journal {}: {e}", source.display());
    let input = tokio::fs::File::open(source).await.map_err(read)?;
    let size = input.metadata().await.map_err(read)?.len();
    let mut output = lc_store::create_private(target)
        .await
        .map_err(|e| format!("Cannot create {}: {e}", target.display()))?;
    let write = |e: std::io::Error| format!("Cannot write {}: {e}", target.display());
    tokio::io::copy(&mut input.take(size), &mut output)
        .await
        .map_err(write)?;
    output.flush().await.map_err(write)?;
    output.sync_data().await.map_err(write)
}

pub mod goal;
pub mod model;

pub use lc_engine::live::ModelInfo;
