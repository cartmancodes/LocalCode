//! Interactive vendor driver. A single owner correlates wire events while
//! cancellation and shutdown use independent watch channels.
use serde_json::Value;
use std::{path::PathBuf, time::Duration};
use tokio::{
    sync::{mpsc, watch},
    time::timeout,
};

mod claude;
mod codex;
mod demo;
mod driver;
mod mode;
use demo::demo;
use driver::vendor;
pub use mode::Mode;

pub const PROMPT_LIMIT: usize = 64 * 1024;
const EVENT_CAPACITY: usize = 128;
const EVENT_BYTES: usize = 32 * 1024;

/// Driver time limits. The defaults suit real vendors; tests shorten them.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Spawn to a usable session.
    pub connect: Duration,
    /// Longest silence from the vendor inside a turn. Not a cap on turn length.
    pub turn_idle: Duration,
    /// Interrupt request to the turn's terminal event.
    pub interrupt: Duration,
    /// Unanswered approval before it is denied.
    pub approval: Duration,
    /// Unanswered Claude mode switch before it is reported as unconfirmed.
    pub mode_confirm: Duration,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(30),
            turn_idle: Duration::from_secs(600),
            interrupt: Duration::from_secs(10),
            approval: Duration::from_secs(120),
            mode_confirm: Duration::from_secs(10),
        }
    }
}

/// The backend a session drives. Journals and the CLI use `as_str()`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Engine {
    Claude,
    Codex,
    /// Offline preview; no vendor process.
    Demo,
}
impl Engine {
    pub const ALL: [Engine; 3] = [Engine::Codex, Engine::Claude, Engine::Demo];
    pub fn parse(value: &str) -> Option<Engine> {
        Self::ALL
            .into_iter()
            .find(|engine| engine.as_str() == value)
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Engine::Claude => "claude",
            Engine::Codex => "codex",
            Engine::Demo => "demo",
        }
    }
    pub fn is_vendor(self) -> bool {
        self != Engine::Demo
    }
}
impl std::fmt::Display for Engine {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug)]
pub struct Config {
    pub engine: Engine,
    pub binary: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub resume: Option<String>,
    pub mode: Mode,
    /// How long an approval waits for an answer before it is denied.
    pub approval_timeout: Duration,
}
impl Config {
    /// Ask mode, the vendor's default model, a new vendor session.
    pub fn new(engine: Engine, binary: impl Into<PathBuf>, cwd: impl Into<PathBuf>) -> Self {
        Self {
            engine,
            binary: binary.into(),
            cwd: cwd.into(),
            model: None,
            resume: None,
            mode: Mode::Ask,
            approval_timeout: Duration::from_secs(120),
        }
    }
}
/// Provider-owned picker metadata; selection and resolved ID can differ.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelInfo {
    pub selection: String,
    pub id: Option<String>,
    pub name: String,
    pub description: String,
}

/// A vendor model identifier Octet will pass on or display: non-empty,
/// at most 256 bytes, no control characters.
pub fn valid_identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
}

fn model_catalog(value: &Value, claude: bool) -> Vec<ModelInfo> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .take(256)
        .filter_map(|v| {
            let selection = v[if claude { "value" } else { "model" }].as_str()?;
            // Bound untrusted metadata without silently truncating model identifiers.
            if !valid_identifier(selection) {
                return None;
            }
            let id = v[if claude { "resolvedModel" } else { "model" }]
                .as_str()
                .filter(|id| valid_identifier(id))
                .map(str::to_owned);
            Some(ModelInfo {
                selection: selection.into(),
                id,
                name: v["displayName"]
                    .as_str()
                    .filter(|s| s.len() <= 512)
                    .unwrap_or(selection)
                    .into(),
                description: v["description"]
                    .as_str()
                    .filter(|s| s.len() <= 2048)
                    .unwrap_or("")
                    .into(),
            })
        })
        .collect()
}

/// How a turn ended. Journals and the status line use `as_str()`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Completed,
    Interrupted,
    Failed,
    /// A status Octet does not map, exactly as the vendor sent it.
    Other(String),
}
impl Outcome {
    pub fn from_vendor(status: &str) -> Outcome {
        match status {
            "completed" => Outcome::Completed,
            "interrupted" => Outcome::Interrupted,
            "failed" => Outcome::Failed,
            other => Outcome::Other(other.to_owned()),
        }
    }
    pub fn as_str(&self) -> &str {
        match self {
            Outcome::Completed => "completed",
            Outcome::Interrupted => "interrupted",
            Outcome::Failed => "failed",
            Outcome::Other(status) => status,
        }
    }
}
impl std::fmt::Display for Outcome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug)]
pub enum Event {
    Ready { session: String },
    Models(Vec<ModelInfo>),
    ModelSelected(String),
    ModeChanged(Mode),
    User(String),
    Started,
    Text(String),
    Tool(String),
    Approval { id: u64, detail: String },
    ApprovalClosed(u64),
    Usage(String),
    Finished { outcome: Outcome },
    Notice(String),
    Error(String),
    Stopped,
}
#[derive(Debug)]
pub enum Command {
    Prompt(String),
    PromptWithDisplay { wire: String, display: String },
    Answer { id: u64, allow: bool },
    SetMode(Mode),
}
impl Command {
    /// The longest text a prompt command carries; 0 for other commands.
    fn prompt_bytes(&self) -> usize {
        match self {
            Command::Prompt(text) => text.len(),
            Command::PromptWithDisplay { wire, display } => wire.len().max(display.len()),
            Command::Answer { .. } | Command::SetMode(_) => 0,
        }
    }
}
/// Why a command was not queued.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    #[error("Prompt exceeds the 64 KiB limit")]
    PromptTooLong,
    #[error("Session is busy or closed; try again")]
    Busy,
}
/// Why the driver stopped. Only `Vendor` failures get the vendor's stderr
/// attached; the others are Octet's own and stderr would mislead.
#[derive(Debug, thiserror::Error)]
pub(crate) enum DriverError {
    #[error("Connection cancelled")]
    Cancelled,
    #[error("Output consumer overloaded; session stopped")]
    ConsumerOverloaded,
    #[error("Turn item limit reached; session stopped")]
    TurnItemLimit,
    #[error("{0}")]
    Vendor(String),
}
impl From<String> for DriverError {
    fn from(message: String) -> Self {
        DriverError::Vendor(message)
    }
}
impl From<&str> for DriverError {
    fn from(message: &str) -> Self {
        DriverError::Vendor(message.to_owned())
    }
}
#[derive(Clone)]
pub struct Handle {
    commands: mpsc::Sender<Command>,
    interrupt: watch::Sender<u64>,
    stop: watch::Sender<bool>,
}
impl Handle {
    pub fn send(&self, command: Command) -> Result<(), SendError> {
        if command.prompt_bytes() > PROMPT_LIMIT {
            return Err(SendError::PromptTooLong);
        }
        self.commands.try_send(command).map_err(|_| SendError::Busy)
    }
    pub fn interrupt(&self) {
        self.interrupt.send_modify(|n| *n = n.wrapping_add(1));
    }
    pub fn shutdown(&self) {
        let _ = self.stop.send(true);
    }
}
// Output is split before enqueueing. A stalled consumer fails the session rather
// than blocking the control path or silently dropping semantic output.
fn emit(tx: &mpsc::Sender<Event>, event: Event) -> Result<(), DriverError> {
    let text = match event {
        Event::Text(text) => text,
        // Tool detail is a preview: megabytes of command output must not flood
        // the queue, so it is cut to one event.
        Event::Tool(text) => {
            return tx
                .try_send(Event::Tool(limited(&text)))
                .map_err(|_| DriverError::ConsumerOverloaded)
        }
        event => {
            return tx
                .try_send(event)
                .map_err(|_| DriverError::ConsumerOverloaded)
        }
    };
    let mut remaining = text.as_str();
    while !remaining.is_empty() {
        let end = remaining.floor_char_boundary(EVENT_BYTES);
        let chunk = remaining[..end].to_owned();
        tx.try_send(Event::Text(chunk))
            .map_err(|_| DriverError::ConsumerOverloaded)?;
        remaining = &remaining[end..];
    }
    Ok(())
}

fn limited(text: &str) -> String {
    let end = text.floor_char_boundary(EVENT_BYTES);
    if end == text.len() {
        text.to_owned()
    } else {
        format!("{}\n[detail exceeds preview limit]", &text[..end])
    }
}
pub fn spawn(config: Config) -> (Handle, mpsc::Receiver<Event>, tokio::task::JoinHandle<()>) {
    spawn_with_limits(config, Limits::default())
}
pub fn spawn_with_limits(
    config: Config,
    limits: Limits,
) -> (Handle, mpsc::Receiver<Event>, tokio::task::JoinHandle<()>) {
    let (commands, rx) = mpsc::channel(16);
    let (interrupt, cancel) = watch::channel(0);
    let (stop, stopping) = watch::channel(false);
    let (events, output) = mpsc::channel(EVENT_CAPACITY);
    let handle = Handle {
        commands,
        interrupt,
        stop,
    };
    let task = tokio::spawn(async move {
        let result = if config.engine == Engine::Demo {
            demo(config.mode, limits.approval, rx, cancel, stopping, &events)
                .await
                .map_err(|error| error.to_string())
        } else {
            vendor(config, limits, rx, cancel, stopping, &events).await
        };
        if let Err(error) = result {
            // After the driver stops, bounded waiting can deliver the final error.
            let _ = timeout(Duration::from_secs(2), events.send(Event::Error(error))).await;
        }
        let _ = timeout(Duration::from_secs(2), events.send(Event::Stopped)).await;
    });
    (handle, output, task)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn identifiers_are_bounded_single_line_and_non_empty() {
        assert!(valid_identifier("gpt-5.5"));
        assert!(valid_identifier(&"x".repeat(256)));
        for bad in ["", "a\u{1b}b", "a\nb"] {
            assert!(!valid_identifier(bad), "{bad:?}");
        }
        assert!(!valid_identifier(&"x".repeat(257)));
    }
    #[test]
    fn catalog_rejects_invalid_identifiers_and_bounds_metadata() {
        let models = model_catalog(
            &json!([
                {"value":"sonnet","displayName":"Sonnet"},
                {"value":"bad\u{1b}id"},
                {"value":"x".repeat(257)},
                {"value":"custom","resolvedModel":"full-id","description":"x".repeat(2049)}
            ]),
            true,
        );
        assert_eq!(models.len(), 2);
        assert_eq!(models[0].id, None);
        assert_eq!(models[1].id.as_deref(), Some("full-id"));
        assert!(models[1].description.is_empty());
        assert!(model_catalog(&Value::Null, false).is_empty());
    }
    #[test]
    fn unknown_vendor_status_is_kept_verbatim() {
        for known in ["completed", "interrupted", "failed"] {
            assert_eq!(Outcome::from_vendor(known).as_str(), known);
        }
        assert_eq!(Outcome::from_vendor("completed"), Outcome::Completed);
        let other = Outcome::from_vendor("inProgress");
        assert_eq!(other, Outcome::Other("inProgress".into()));
        assert_eq!(other.to_string(), "inProgress");
    }
    #[test]
    fn engines_parse_their_own_spelling_only() {
        for engine in Engine::ALL {
            assert_eq!(Engine::parse(engine.as_str()), Some(engine));
            assert_eq!(engine.to_string(), engine.as_str());
        }
        assert_eq!(Engine::parse("Claude"), None);
        assert!(!Engine::Demo.is_vendor());
    }
    #[test]
    fn send_errors_keep_their_wording() {
        assert_eq!(
            SendError::PromptTooLong.to_string(),
            "Prompt exceeds the 64 KiB limit"
        );
        assert_eq!(
            SendError::Busy.to_string(),
            "Session is busy or closed; try again"
        );
    }
}
