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
mod image;
mod mode;
mod protocol;
pub use claude::claude_stray_reply;
pub use codex::codex_stray_reply;
pub use image::{
    check_inline, encoded_len, ImageAttachment, ImageError, IMAGES_PER_PROMPT, IMAGE_LIMIT,
};
pub use mode::Mode;

/// The longest prompt, in bytes, Octet sends to a vendor.
pub const PROMPT_LIMIT: usize = 64 * 1024;
const EVENT_CAPACITY: usize = 128;
const EVENT_BYTES: usize = 32 * 1024;

/// How long an approval waits for an answer before it is denied, unless
/// `Config::approval_timeout` says otherwise.
pub const DEFAULT_APPROVAL_TIMEOUT: Duration = Duration::from_secs(120);

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
    /// Unanswered mode switch before it is reported as unconfirmed, for a
    /// protocol whose switches are confirmed by the vendor (Claude's are).
    pub mode_confirm: Duration,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(30),
            turn_idle: Duration::from_secs(600),
            interrupt: Duration::from_secs(10),
            approval: DEFAULT_APPROVAL_TIMEOUT,
            mode_confirm: Duration::from_secs(10),
        }
    }
}

/// One backend Octet can drive. A new vendor is one of these plus its
/// `Protocol` file (see docs/rust/adding-a-provider.md).
pub struct Provider {
    /// The CLI flag, journal and `/model` spelling.
    pub name: &'static str,
    /// The name in notices ("Codex reports …").
    pub title: &'static str,
    /// The command to run when `--binary` is absent.
    pub default_binary: &'static str,
    /// No vendor process and no model (the demo).
    pub offline: bool,
    /// `/mode` descriptions, in `Mode::ALL` order.
    pub modes: [&'static str; 4],
    /// Text can be added to a running turn (`/steer`).
    pub steer: bool,
    /// Images go inside the prompt as base64, so [`check_inline`]'s limits
    /// apply; otherwise the vendor reads the files itself.
    pub inline_images: bool,
    /// Reasoning effort changes take effect from the next turn; otherwise
    /// they need a reconnect (the CLI takes effort at launch).
    pub effort_live: bool,
    /// Runs one session until it stops.
    pub start: StartFn,
}

/// Starts a provider's session; the error is the final message to show.
pub type StartFn = fn(Config, Limits, Channels) -> BoxFuture<Result<(), String>>;

/// A boxed future that can move between threads.
pub type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + Send>>;

/// The channels a session runs on.
pub struct Channels {
    /// Commands from the interface.
    pub commands: mpsc::Receiver<Command>,
    /// Bumped to cancel the running turn.
    pub cancel: watch::Receiver<u64>,
    /// Set to stop the session.
    pub stopping: watch::Receiver<bool>,
    /// Events to the interface.
    pub events: mpsc::Sender<Event>,
}

/// A provider, by its row in the table. Journals and the CLI use
/// `as_str()`; it compares, hashes and prints by name.
#[derive(Clone, Copy)]
pub struct Engine(&'static Provider);
impl Engine {
    /// Codex, over its app-server JSON-RPC protocol.
    pub const CODEX: Engine = Engine(&codex::PROVIDER);
    /// Claude Code, over its stream-json protocol.
    pub const CLAUDE: Engine = Engine(&claude::PROVIDER);
    /// The offline preview; no vendor process.
    pub const DEMO: Engine = Engine(&demo::PROVIDER);
    /// Every provider, in the order help lists them: the provider table.
    pub const ALL: &'static [Engine] = &[Engine::CODEX, Engine::CLAUDE, Engine::DEMO];
    /// The engine named `value` (`codex`, `claude` or `demo`), exactly as
    /// spelled.
    ///
    /// ```
    /// use octet_engine::live::Engine;
    ///
    /// assert_eq!(Engine::parse("claude"), Some(Engine::CLAUDE));
    /// assert_eq!(Engine::parse("Claude"), None);
    /// ```
    pub fn parse(value: &str) -> Option<Engine> {
        Self::ALL
            .iter()
            .copied()
            .find(|engine| engine.as_str() == value)
    }
    /// The provider's row.
    pub fn provider(self) -> &'static Provider {
        self.0
    }
    /// The CLI and journal spelling.
    pub fn as_str(self) -> &'static str {
        self.0.name
    }
    /// The name in notices.
    pub fn title(self) -> &'static str {
        self.0.title
    }
    /// No vendor process and no model.
    pub fn offline(self) -> bool {
        self.0.offline
    }
    /// A real vendor CLI, not the offline demo.
    pub fn is_vendor(self) -> bool {
        !self.0.offline
    }
}
impl PartialEq for Engine {
    fn eq(&self, other: &Self) -> bool {
        self.0.name == other.0.name
    }
}
impl Eq for Engine {}
impl std::hash::Hash for Engine {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        self.0.name.hash(state);
    }
}
impl std::fmt::Debug for Engine {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0.name)
    }
}
impl std::fmt::Display for Engine {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug)]
/// Everything needed to start one session.
pub struct Config {
    /// Which backend.
    pub engine: Engine,
    /// The vendor CLI to run.
    pub binary: PathBuf,
    /// The workspace the vendor works in.
    pub cwd: PathBuf,
    /// A model to request; `None` takes the vendor's default.
    pub model: Option<String>,
    /// A vendor session to resume; `None` starts a new one.
    pub resume: Option<String>,
    /// The permission mode to start in.
    pub mode: Mode,
    /// How long an approval waits for an answer before it is denied.
    pub approval_timeout: Duration,
    /// Reasoning effort to request; `None` takes the vendor's default.
    pub effort: Option<String>,
    /// Open `resume` as a new vendor session that continues it, leaving the
    /// original as it was.
    pub fork: bool,
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
            approval_timeout: DEFAULT_APPROVAL_TIMEOUT,
            effort: None,
            fork: false,
        }
    }
}
/// Provider-owned picker metadata; selection and resolved ID can differ.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelInfo {
    /// What to pass the vendor to select this model.
    pub selection: String,
    /// The full model ID it resolves to, when the vendor says.
    pub id: Option<String>,
    /// The display name.
    pub name: String,
    /// The vendor's description.
    pub description: String,
}

/// A reasoning effort level: one word, at most 64 bytes, no control
/// characters. Vendors decide which levels they accept.
///
/// ```
/// use octet_engine::live::valid_effort;
///
/// assert!(valid_effort("high"));
/// assert!(!valid_effort("two words"));
/// ```
pub fn valid_effort(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && !value.chars().any(|c| c.is_control() || c.is_whitespace())
}

/// A vendor model identifier Octet will pass on or display: non-empty,
/// at most 256 bytes, no control characters.
pub fn valid_identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
}

/// A vendor catalog: `selection_key` names what to pass the vendor,
/// `id_key` the full model ID it resolves to.
fn model_catalog_with(value: &Value, selection_key: &str, id_key: &str) -> Vec<ModelInfo> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .take(256)
        .filter_map(|v| {
            let selection = v[selection_key].as_str()?;
            // Bound untrusted metadata without silently truncating model identifiers.
            if !valid_identifier(selection) {
                return None;
            }
            let id = v[id_key]
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
    /// The turn finished normally.
    Completed,
    /// The user cancelled it.
    Interrupted,
    /// The vendor reported a failure.
    Failed,
    /// A status Octet does not map, exactly as the vendor sent it.
    Other(String),
}
impl Outcome {
    /// Maps a vendor status string, keeping unknown ones verbatim.
    ///
    /// ```
    /// use octet_engine::live::Outcome;
    ///
    /// assert_eq!(Outcome::from_vendor("completed"), Outcome::Completed);
    /// assert_eq!(Outcome::from_vendor("inProgress").as_str(), "inProgress");
    /// ```
    pub fn from_vendor(status: &str) -> Outcome {
        match status {
            "completed" => Outcome::Completed,
            "interrupted" => Outcome::Interrupted,
            "failed" => Outcome::Failed,
            other => Outcome::Other(other.to_owned()),
        }
    }
    /// The journal and status-line spelling.
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
/// What a session reports to the interface, in order.
pub enum Event {
    /// Connected: the vendor session ID (empty until the vendor reports one).
    Ready {
        /// The vendor's session or thread ID.
        session: String,
    },
    /// The vendor's model catalog.
    Models(Vec<ModelInfo>),
    /// The model the vendor confirmed it is using.
    ModelSelected(String),
    /// The permission mode the vendor confirmed.
    ModeChanged(Mode),
    /// The user's prompt, as the transcript shows it.
    User(String),
    /// A turn began.
    Started,
    /// Streamed reply text.
    Text(String),
    /// A tool call or result, as a preview.
    Tool(String),
    /// The vendor asks to run something and waits for the user.
    Approval {
        /// Octet's ID for answering it.
        id: u64,
        /// The full request, as shown to the user.
        detail: String,
    },
    /// An approval was answered, timed out or withdrawn.
    ApprovalClosed(u64),
    /// Token or cost usage, ready to display.
    Usage(String),
    /// The turn ended.
    Finished {
        /// How it ended.
        outcome: Outcome,
    },
    /// Something the user should know that is not an error.
    Notice(String),
    /// Something failed; the text says what.
    Error(String),
    /// The session ended; no more events follow.
    Stopped,
}
#[derive(Debug, PartialEq, Eq)]
/// What the interface asks a session to do.
pub enum Command {
    /// Send a prompt, shown as sent.
    Prompt(String),
    /// A prompt whose sent text differs from what the transcript shows
    /// (attachments, goal prompts), with any images.
    PromptWithDisplay {
        /// What the vendor receives.
        wire: String,
        /// What the transcript and journal show.
        display: String,
        /// Images sent after the text; read when the prompt is sent.
        images: Vec<ImageAttachment>,
    },
    /// The user's answer to an approval.
    Answer {
        /// The approval's ID from `Event::Approval`.
        id: u64,
        /// Allow once, or deny.
        allow: bool,
    },
    /// Switch the permission mode.
    SetMode(Mode),
    /// Add to the running turn, where the provider supports it.
    Steer(String),
    /// Set the reasoning effort for later turns (`None`: vendor default).
    SetEffort(Option<String>),
    /// Ask the vendor to compact its context; it runs as a turn.
    Compact,
}
impl Command {
    /// The longest text a prompt command carries; 0 for other commands.
    fn prompt_bytes(&self) -> usize {
        match self {
            Command::Prompt(text) => text.len(),
            Command::PromptWithDisplay { wire, display, .. } => wire.len().max(display.len()),
            Command::Steer(text) => text.len(),
            Command::Answer { .. }
            | Command::SetMode(_)
            | Command::SetEffort(_)
            | Command::Compact => 0,
        }
    }
}
/// Why a command was not queued.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    /// The prompt is over `PROMPT_LIMIT`.
    #[error("Prompt exceeds the 64 KiB limit")]
    PromptTooLong,
    /// The command queue is full, or the session has stopped.
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
/// Sends commands to a running session; cheap to clone.
pub struct Handle {
    commands: mpsc::Sender<Command>,
    interrupt: watch::Sender<u64>,
    stop: watch::Sender<bool>,
}
impl Handle {
    /// Queues `command` without waiting for it to run.
    ///
    /// # Errors
    ///
    /// `PromptTooLong` for an oversized prompt, or `Busy` if the queue is
    /// full or the session has stopped.
    pub fn send(&self, command: Command) -> Result<(), SendError> {
        if command.prompt_bytes() > PROMPT_LIMIT {
            return Err(SendError::PromptTooLong);
        }
        self.commands.try_send(command).map_err(|_| SendError::Busy)
    }
    /// Cancels the running turn, or the connection while it is being made.
    pub fn interrupt(&self) {
        self.interrupt.send_modify(|n| *n = n.wrapping_add(1));
    }
    /// Stops the session.
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
/// Starts a session for `config` with the default time limits. Returns
/// the command handle, the event stream and the driver task.
pub fn spawn(config: Config) -> (Handle, mpsc::Receiver<Event>, tokio::task::JoinHandle<()>) {
    let limits = Limits {
        approval: config.approval_timeout,
        ..Limits::default()
    };
    spawn_with_limits(config, limits)
}
/// Like `spawn`, with explicit time limits. `limits.approval` is the
/// approval window here; `config.approval_timeout` is not read.
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
        let channels = Channels {
            commands: rx,
            cancel,
            stopping,
            events: events.clone(),
        };
        let result = (config.engine.provider().start)(config, limits, channels).await;
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
        let models = model_catalog_with(
            &json!([
                {"value":"sonnet","displayName":"Sonnet"},
                {"value":"bad\u{1b}id"},
                {"value":"x".repeat(257)},
                {"value":"custom","resolvedModel":"full-id","description":"x".repeat(2049)}
            ]),
            "value",
            "resolvedModel",
        );
        assert_eq!(models.len(), 2);
        assert_eq!(models[0].id, None);
        assert_eq!(models[1].id.as_deref(), Some("full-id"));
        assert!(models[1].description.is_empty());
        assert!(model_catalog_with(&Value::Null, "model", "model").is_empty());
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
    fn engines_compare_and_hash_by_name() {
        use std::collections::HashMap;
        let copy = Engine::parse("codex").unwrap();
        assert_eq!(copy, Engine::CODEX);
        assert_ne!(Engine::CODEX, Engine::CLAUDE);
        let mut binaries = HashMap::new();
        binaries.insert(Engine::CODEX, "a");
        assert_eq!(binaries.get(&copy), Some(&"a"));
        assert_eq!(format!("{:?}", Engine::CLAUDE), "claude");
    }
    #[test]
    fn the_table_is_complete_and_unambiguous() {
        let names: Vec<&str> = Engine::ALL.iter().map(|e| e.as_str()).collect();
        // New rows go after these three; their order is what help shows.
        assert_eq!(names[..3], ["codex", "claude", "demo"]);
        for engine in Engine::ALL {
            assert_eq!(Engine::parse(engine.as_str()), Some(*engine));
            assert!(engine.provider().modes.iter().all(|m| !m.is_empty()));
            assert!(!engine.title().is_empty());
        }
        assert_eq!(Engine::ALL.iter().filter(|e| e.offline()).count(), 1);
        assert!(Engine::DEMO.offline() && !Engine::DEMO.is_vendor());
    }
    #[test]
    fn engines_parse_their_own_spelling_only() {
        for engine in Engine::ALL.iter().copied() {
            assert_eq!(Engine::parse(engine.as_str()), Some(engine));
            assert_eq!(engine.to_string(), engine.as_str());
        }
        assert_eq!(Engine::parse("Claude"), None);
        assert!(!Engine::DEMO.is_vendor());
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
