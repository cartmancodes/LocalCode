//! Interactive vendor driver. A single owner correlates wire events while
//! cancellation and shutdown use independent watch channels.
use octet_proc::ProcessConfig;
use serde_json::Value;
use std::{collections::VecDeque, ffi::OsString, path::PathBuf, time::Duration};
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
    IMAGE_LIMIT, IMAGES_PER_PROMPT, ImageAttachment, ImageError, check_inline, encoded_len,
};
pub use mode::Mode;

/// The longest prompt, in bytes, a user may type (or a command may send as
/// its displayed text).
pub const PROMPT_LIMIT: usize = 64 * 1024;
/// The most a prompt sends the vendor, wire text and all: room for the typed
/// text, its `!` attachments and a provider handoff's transcript, while still
/// bounding memory.
pub const WIRE_LIMIT: usize = 4 * PROMPT_LIMIT;
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
    /// Unanswered mode switch before it is reported as unconfirmed, for a
    /// protocol whose switches are confirmed by the vendor (Claude's are).
    pub mode_confirm: Duration,
    /// Reading an attached image when its prompt is sent, for a vendor that
    /// takes the bytes inline: a file swapped for a FIFO, or on a stalled
    /// mount, must not hold the session.
    pub image_read: Duration,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(30),
            turn_idle: Duration::from_secs(600),
            interrupt: Duration::from_secs(10),
            mode_confirm: Duration::from_secs(10),
            image_read: Duration::from_secs(10),
        }
    }
}

/// `d` from now. A duration too large for the clock (a library caller's
/// `Duration::MAX`) means "about never" instead of a panic.
pub(crate) fn deadline_after(d: Duration) -> tokio::time::Instant {
    /// "About never": thirty years.
    const ABOUT_NEVER: Duration = Duration::from_hours(30 * 365 * 24);
    let now = tokio::time::Instant::now();
    now.checked_add(d).unwrap_or_else(|| now + ABOUT_NEVER)
}

/// `text` cut to 256 bytes, for vendor strings that should be short (a
/// status, a method name) but come from an untrusted pipe.
pub(crate) fn short(text: &str) -> String {
    const SHORT: usize = 256;
    if text.len() <= SHORT {
        text.to_owned()
    } else {
        format!("{}…", &text[..text.floor_char_boundary(SHORT)])
    }
}

/// One backend Octet can drive. A new vendor is one of these plus its
/// `Protocol` file (see docs/rust/adding-a-provider.md).
#[derive(Debug)]
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
    /// The reasoning effort levels the vendor takes; empty passes any word
    /// on (Codex's levels depend on the model).
    pub efforts: &'static [&'static str],
    /// How long the CLI may take to exit once its input closes, before it
    /// is sent SIGTERM.
    pub shutdown_grace: Duration,
    /// The CLI can list its models without opening a session (`probe`).
    pub lists_models: bool,
    /// The vendor CLI's arguments for `config` (none for the demo).
    pub(crate) launch_args: fn(&Config) -> Vec<OsString>,
    /// Runs one session until it stops.
    pub(crate) start: StartFn,
}

/// Starts a provider's session; the error is the final message to show.
pub(crate) type StartFn = fn(Config, Limits, Channels) -> BoxFuture<Result<(), String>>;

/// A boxed future that can move between threads.
pub(crate) type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + Send>>;

/// The channels a session runs on.
pub(crate) struct Channels {
    /// Commands from the interface.
    pub(crate) commands: mpsc::Receiver<Command>,
    /// Bumped to cancel the running turn.
    pub(crate) cancel: watch::Receiver<u64>,
    /// Set to stop the session.
    pub(crate) stopping: watch::Receiver<bool>,
    /// Events to the interface.
    pub(crate) events: mpsc::Sender<Event>,
}

/// The arguments Octet launches `config`'s vendor CLI with: the one
/// contract the driver and the protocol gate share.
pub fn launch_args(config: &Config) -> Vec<OsString> {
    (config.engine.provider().launch_args)(config)
}

/// How Octet runs `engine`'s CLI: its limits on frames, queued output and
/// stderr, and its shutdown graces. The protocol gate uses the same.
pub fn vendor_process(
    engine: Engine,
    executable: PathBuf,
    args: Vec<OsString>,
    cwd: PathBuf,
) -> ProcessConfig {
    ProcessConfig {
        executable,
        args,
        cwd: Some(cwd),
        max_frame_bytes: 8 * 1024 * 1024,
        queue_bytes: 16 * 1024 * 1024,
        stderr_bytes: 4096,
        shutdown_grace: engine.provider().shutdown_grace,
        term_grace: Duration::from_millis(250),
    }
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
    /// Whether this provider takes `level` as its reasoning effort.
    ///
    /// # Errors
    ///
    /// Names the levels the provider takes, when it lists them and `level`
    /// is not one.
    pub fn check_effort(self, level: &str) -> Result<(), String> {
        let levels = self.0.efforts;
        match levels.split_last() {
            Some((last, rest)) if !levels.contains(&level) => Err(format!(
                "{} takes effort {} or {last}",
                self.0.title,
                rest.join(", ")
            )),
            _ => Ok(()),
        }
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
    /// Open the CLI only far enough to list its models: no vendor session,
    /// no prompt. Used by `probe`.
    pub catalog_only: bool,
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
            catalog_only: false,
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
    /// Whether this command starts a turn: a prompt or a compaction.
    pub fn starts_turn(&self) -> bool {
        matches!(
            self,
            Command::Prompt(_) | Command::PromptWithDisplay { .. } | Command::Compact
        )
    }
    /// What the transcript shows for a turn command.
    fn turn_display(&self) -> Option<&str> {
        match self {
            Command::Prompt(text) => Some(text),
            Command::PromptWithDisplay { display, .. } => Some(display),
            Command::Compact => Some("/compact"),
            _ => None,
        }
    }
    /// The typed text a prompt command carries, bounded by
    /// [`PROMPT_LIMIT`]; 0 for other commands.
    fn prompt_bytes(&self) -> usize {
        match self {
            Command::Prompt(text) | Command::Steer(text) => text.len(),
            Command::PromptWithDisplay { display, .. } => display.len(),
            Command::Answer { .. }
            | Command::SetMode(_)
            | Command::SetEffort(_)
            | Command::Compact => 0,
        }
    }
    /// What a prompt command sends the vendor, bounded by [`WIRE_LIMIT`]:
    /// the wire text when it differs from the display.
    fn wire_bytes(&self) -> usize {
        match self {
            Command::PromptWithDisplay { wire, .. } => wire.len(),
            other => other.prompt_bytes(),
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
#[derive(Clone, Debug)]
/// Sends commands to a running session; cheap to clone.
pub struct Handle {
    commands: mpsc::Sender<Command>,
    /// Carries how many turn commands had been sent when the user
    /// cancelled, so a turn queued before the cancel never starts.
    interrupt: watch::Sender<u64>,
    stop: watch::Sender<bool>,
    /// Turn commands sent so far.
    turns: std::sync::Arc<std::sync::atomic::AtomicU64>,
}
impl Handle {
    /// Queues `command` without waiting for it to run.
    ///
    /// # Errors
    ///
    /// `PromptTooLong` for an oversized prompt, or `Busy` if the queue is
    /// full or the session has stopped.
    pub fn send(&self, command: Command) -> Result<(), SendError> {
        use std::sync::atomic::Ordering;
        if command.prompt_bytes() > PROMPT_LIMIT || command.wire_bytes() > WIRE_LIMIT {
            return Err(SendError::PromptTooLong);
        }
        // Reserve the slot first: a turn is counted only once nothing can
        // fail, so a concurrent `interrupt` never covers a turn that was
        // refused (and whose number the next prompt would reuse).
        let permit = self.commands.try_reserve().map_err(|_| SendError::Busy)?;
        if command.starts_turn() {
            self.turns.fetch_add(1, Ordering::SeqCst);
        }
        permit.send(command);
        Ok(())
    }
    /// Cancels the running turn, any turn already sent but not yet started,
    /// or the connection while it is being made.
    pub fn interrupt(&self) {
        let sent = self.turns.load(std::sync::atomic::Ordering::SeqCst);
        self.interrupt.send_modify(|n| *n = sent);
    }
    /// Stops the session.
    pub fn shutdown(&self) {
        let _ = self.stop.send(true);
    }
}
// Output is split before enqueueing. While the consumer is behind, the driver
// stops reading the vendor (backpressure) rather than block the control path or
// drop output; a consumer stalled past octet-core's 2 s delivery limit still
// ends the session there, with a journaled reason.
/// Free event-queue slots below which the driver stops reading the vendor's
/// output until the interface catches up. One frame expands to at most a
/// few events, so this leaves room for it and for the turn's end.
pub(crate) const HEADROOM: usize = 32;
/// The most text one reply adds to the transcript: half the event queue.
const TEXT_LIMIT: usize = 2 * 1024 * 1024;
/// Queue slots text leaves free, for the events that end a turn. Text is
/// also cut to the room left, so a frame of several large blocks sent
/// before the interface reads any cannot overflow the queue and stop the
/// session.
const TEXT_RESERVE: usize = 8;

fn emit(tx: &mpsc::Sender<Event>, event: Event) -> Result<(), DriverError> {
    let mut text = match event {
        Event::Text(text) => text,
        // Tool detail is a preview: megabytes of command output must not flood
        // the queue, so it is cut to one event.
        Event::Tool(text) => {
            return tx
                .try_send(Event::Tool(limited(&text)))
                .map_err(|_| DriverError::ConsumerOverloaded);
        }
        event => {
            return tx
                .try_send(event)
                .map_err(|_| DriverError::ConsumerOverloaded);
        }
    };
    let room = tx.capacity().saturating_sub(TEXT_RESERVE) * EVENT_BYTES;
    if text.len() > TEXT_LIMIT.min(room) {
        let marker = if text.len() > TEXT_LIMIT && TEXT_LIMIT <= room {
            format!(
                "\n[Octet shows at most {} MiB of one reply; the rest is cut]",
                TEXT_LIMIT / (1024 * 1024)
            )
        } else {
            "\n[Octet is behind on showing replies; the rest is cut]".to_owned()
        };
        // The marker takes room too.
        let keep = TEXT_LIMIT.min(room).saturating_sub(marker.len());
        text.truncate(text.floor_char_boundary(keep));
        text.push_str(&marker);
    }
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

/// The demo prompt that opens its sample approval dialog.
pub const APPROVAL_DEMO: &str = "/approval-demo";
/// Refusal: a turn is already running or the session is not ready.
const BUSY: &str = "Wait for the current operation, or cancel it first";
/// Refusal: a steer with no turn to add to.
const NO_TURN: &str = "No turn is running; send it as a prompt";
/// Refusal: full access needs a new launch.
const FULL_ACCESS_RECONNECTS: &str = "Full access is changed by reconnecting; use /mode";

/// Recognises a turn command the user cancelled before it started. The
/// interface counts the turn commands it sends, and a cancel carries that
/// count; a turn numbered at or below it must not run.
#[derive(Default)]
struct TurnGate {
    taken: u64,
}
impl TurnGate {
    /// Counts `command` if it starts a turn; true if it was cancelled. The
    /// cancel is not marked seen, so the caller still handles it.
    fn cancelled(&mut self, command: &Command, cancel: &watch::Receiver<u64>) -> bool {
        if !command.starts_turn() {
            return false;
        }
        self.taken += 1;
        *cancel.borrow() >= self.taken
    }
}

/// Appends `item`, dropping and returning the oldest entry once `list` holds
/// `max`, so per-connection lists stay bounded.
fn push_bounded<T>(list: &mut VecDeque<T>, item: T, max: usize) -> Option<T> {
    let dropped = if list.len() >= max {
        list.pop_front()
    } else {
        None
    };
    list.push_back(item);
    dropped
}

fn limited(text: &str) -> String {
    let end = text.floor_char_boundary(EVENT_BYTES);
    if end == text.len() {
        text.to_owned()
    } else {
        format!("{}\n[detail exceeds preview limit]", &text[..end])
    }
}
/// How long a probe may take before it gives up.
pub const PROBE_LIMIT: Duration = Duration::from_secs(20);

/// The models `engine`'s CLI lists, read without opening a vendor session
/// or sending a prompt: the CLI starts catalog-only and is stopped once it
/// has listed them (Codex sends `Ready` after its last page; Claude before
/// its list).
///
/// # Errors
///
/// The provider cannot list its models, or its CLI could not start,
/// failed, or did not list them within `limit`.
pub async fn probe(
    engine: Engine,
    binary: PathBuf,
    cwd: PathBuf,
    limit: Duration,
) -> Result<Vec<ModelInfo>, String> {
    let title = engine.title();
    if !engine.provider().lists_models {
        return Err(format!("{title} cannot list its models"));
    }
    let mut config = Config::new(engine, binary, cwd);
    config.catalog_only = true;
    let (handle, mut events, task) = spawn(config);
    let listed = timeout(limit, async {
        let (mut ready, mut models) = (false, None);
        while let Some(event) = events.recv().await {
            match event {
                Event::Ready { .. } => ready = true,
                Event::Models(list) => models = Some(list),
                Event::Error(error) => return Err(error),
                _ => {}
            }
            if ready && let Some(list) = models.take() {
                return Ok(list);
            }
        }
        Err(format!("{title} stopped before listing its models"))
    })
    .await
    .unwrap_or_else(|_| {
        Err(format!(
            "{title} did not list its models within {} s",
            limit.as_secs()
        ))
    });
    handle.shutdown();
    // Let the session stop its CLI; bounded, like any shutdown.
    let _ = timeout(Duration::from_secs(5), async {
        while events.recv().await.is_some() {}
        let _ = task.await;
    })
    .await;
    listed
}

/// Starts a session for `config` with the default time limits. Returns
/// the command handle, the event stream and the driver task.
pub fn spawn(config: Config) -> (Handle, mpsc::Receiver<Event>, tokio::task::JoinHandle<()>) {
    spawn_with_limits(config, Limits::default())
}
/// Like `spawn`, with explicit time limits. The approval window is
/// `config.approval_timeout` in both.
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
        turns: std::sync::Arc::default(),
    };
    let task = tokio::spawn(async move {
        let channels = Channels {
            commands: rx,
            cancel,
            stopping,
            events: events.clone(),
        };
        // The driver runs as its own task, so a bug that panics in it still
        // ends the session with an error and `Stopped`.
        let driver = tokio::spawn((config.engine.provider().start)(config, limits, channels));
        let result = driver
            .await
            .unwrap_or_else(|failure| Err(format!("The session's driver failed: {failure}")));
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
    #[test]
    fn claude_has_time_to_exit_on_its_own() {
        let process = |engine| vendor_process(engine, "cli".into(), Vec::new(), ".".into());
        // Claude Code takes about 0.9 s to exit once its input closes.
        assert_eq!(
            process(Engine::CLAUDE).shutdown_grace,
            Duration::from_millis(1500)
        );
        assert_eq!(
            process(Engine::CODEX).shutdown_grace,
            Duration::from_millis(150)
        );
    }
    #[test]
    fn the_turn_gate_stops_only_turns_sent_before_a_cancel() {
        let (cancel_tx, cancel) = watch::channel(0u64);
        let mut gate = TurnGate::default();
        let prompt = Command::Prompt("a".into());
        // Only turn commands count.
        assert!(!gate.cancelled(&Command::SetMode(Mode::Auto), &cancel));
        assert!(!gate.cancelled(&prompt, &cancel));
        // The interface had sent two turns when the user cancelled.
        cancel_tx.send_modify(|sent| *sent = 2);
        assert!(gate.cancelled(&Command::Compact, &cancel));
        assert!(!gate.cancelled(&prompt, &cancel));
    }
    #[test]
    fn one_huge_reply_is_cut_instead_of_stopping_the_session() {
        let (tx, mut rx) = mpsc::channel(EVENT_CAPACITY);
        let huge = "x".repeat(5 * 1024 * 1024);
        assert!(emit(&tx, Event::Text(huge)).is_ok(), "the queue overflowed");
        let mut text = String::new();
        while let Ok(Event::Text(chunk)) = rx.try_recv() {
            text.push_str(&chunk);
        }
        assert!(text.len() < TEXT_LIMIT + 200, "{}", text.len());
        assert!(text.ends_with("[Octet shows at most 2 MiB of one reply; the rest is cut]"));
    }
    fn test_handle(capacity: usize) -> (Handle, mpsc::Receiver<Command>) {
        let (commands, rx) = mpsc::channel(capacity);
        let (interrupt, _cancel) = watch::channel(0);
        let (stop, _stopping) = watch::channel(false);
        let handle = Handle {
            commands,
            interrupt,
            stop,
            turns: std::sync::Arc::default(),
        };
        (handle, rx)
    }
    #[test]
    fn a_long_wire_with_a_short_display_is_accepted() {
        let (handle, _rx) = test_handle(4);
        let sent = handle.send(Command::PromptWithDisplay {
            wire: "x".repeat(100 * 1024),
            display: "short".into(),
            images: Vec::new(),
        });
        assert_eq!(sent, Ok(()));
    }
    #[test]
    fn a_long_display_or_wire_is_refused() {
        let (handle, _rx) = test_handle(4);
        let with = |wire: usize, display: usize| Command::PromptWithDisplay {
            wire: "x".repeat(wire),
            display: "y".repeat(display),
            images: Vec::new(),
        };
        assert_eq!(
            handle.send(with(10, PROMPT_LIMIT + 1)),
            Err(SendError::PromptTooLong)
        );
        assert_eq!(
            handle.send(with(WIRE_LIMIT + 1, 10)),
            Err(SendError::PromptTooLong)
        );
        assert_eq!(
            handle.send(Command::Prompt("z".repeat(PROMPT_LIMIT + 1))),
            Err(SendError::PromptTooLong)
        );
    }
    #[test]
    fn send_counts_a_turn_only_when_queued() {
        let (commands, _rx) = mpsc::channel(1);
        let (interrupt, _cancel) = watch::channel(0);
        let (stop, _stopping) = watch::channel(false);
        let handle = Handle {
            commands,
            interrupt,
            stop,
            turns: std::sync::Arc::default(),
        };
        handle.send(Command::Compact).unwrap();
        // The queue is full: the prompt is refused and never counted.
        assert_eq!(
            handle.send(Command::Prompt("a".into())),
            Err(SendError::Busy)
        );
        assert_eq!(handle.turns.load(std::sync::atomic::Ordering::SeqCst), 1);
    }
    #[test]
    fn deadlines_never_overflow() {
        let far = deadline_after(Duration::MAX);
        let now = tokio::time::Instant::now();
        assert!(far > now + Duration::from_hours(8760));
        let near = deadline_after(Duration::from_secs(1));
        assert!(near <= tokio::time::Instant::now() + Duration::from_secs(1));
    }
    #[test]
    fn many_large_blocks_in_one_frame_leave_room_to_finish() {
        // One Claude frame may carry several text blocks, each near the cap,
        // before the interface reads any of them.
        let (tx, mut rx) = mpsc::channel(EVENT_CAPACITY);
        for _ in 0..4 {
            let block = "x".repeat(TEXT_LIMIT);
            assert!(
                emit(&tx, Event::Text(block)).is_ok(),
                "the queue overflowed"
            );
        }
        assert!(
            emit(
                &tx,
                Event::Finished {
                    outcome: Outcome::Completed
                }
            )
            .is_ok()
        );
        let mut text = String::new();
        while let Ok(Event::Text(chunk)) = rx.try_recv() {
            text.push_str(&chunk);
        }
        assert!(text.contains("the rest is cut"), "{}", text.len());
    }
    #[test]
    fn bounded_lists_drop_their_oldest_entry() {
        let mut list = VecDeque::from([1, 2, 3]);
        assert_eq!(push_bounded(&mut list, 4, 3), Some(1));
        assert_eq!(list, [2, 3, 4]);
        assert_eq!(push_bounded(&mut list, 5, 8), None);
        assert_eq!(list, [2, 3, 4, 5]);
    }
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
    fn effort_levels_are_checked_where_the_provider_lists_them() {
        for level in ["low", "medium", "high", "xhigh", "max"] {
            assert_eq!(Engine::CLAUDE.check_effort(level), Ok(()), "{level}");
        }
        assert_eq!(
            Engine::CLAUDE.check_effort("bogus"),
            Err("Claude takes effort low, medium, high, xhigh or max".to_owned())
        );
        // Codex's levels depend on the model, so any word is passed on.
        assert_eq!(Engine::CODEX.check_effort("minimal"), Ok(()));
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
