//! The interface state: connection, transcript, composer and dialogs, and how
//! vendor events change it. Drawing is in `view`.
use crate::{
    editor::Editor,
    mascot::State,
    text::{Sanitizer, clean, strip},
};
use octet_core::Event;
use ratatui::prelude::*;
use std::fmt::Write as _;
use std::{collections::VecDeque, path::PathBuf};

const MAX_BYTES: usize = 512 * 1024;
/// Prompts that can wait for the running turn.
pub(crate) const QUEUE_LIMIT: usize = 8;
/// Prompts kept for Up/Down.
const HISTORY_LIMIT: usize = 50;
/// Shown when an edit would push the draft past the prompt limit.
pub(crate) const PROMPT_FULL: &str = "Prompt limit reached";
const BLOCK_BYTES: usize = 64 * 1024;
/// How far a streaming reply may outgrow `BLOCK_BYTES` before its start is
/// dropped: each drop re-wraps the whole reply, so it happens in steps.
const BLOCK_SLACK: usize = 16 * 1024;
/// Transcript entries kept on screen; older ones are in the journal.
const MAX_ENTRIES: usize = 160;
/// Models listed per `/model` page.
pub(crate) const CATALOG_PAGE: usize = 20;
/// The farthest the conversation scrolls back, in rows.
const MAX_SCROLL: usize = 65_536;
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Role {
    User,
    Assistant,
    Tool,
    Notice,
    Error,
    Shell,
    ShellFailed,
}
pub(crate) struct Entry {
    pub(crate) role: Role,
    /// The provider connected when it was added, so a handoff can say who
    /// wrote each reply.
    pub(crate) engine: octet_core::Engine,
    pub(crate) text: String,
    /// The width `cache` was wrapped at; `None` until it is.
    pub(crate) width: Option<u16>,
    pub(crate) cache: Vec<Line<'static>>,
    /// Where appended text starts re-wrapping.
    pub(crate) tail: crate::view::transcript::Tail,
}
impl Entry {
    pub(crate) fn new(role: Role, engine: octet_core::Engine, text: String) -> Self {
        Self {
            role,
            engine,
            text,
            width: None,
            cache: Vec::new(),
            tail: crate::view::transcript::Tail::default(),
        }
    }
    /// Its text changed other than at the end: wrap it whole next time.
    pub(crate) fn invalidate(&mut self) {
        self.width = None;
    }
    /// Adds streamed text to the end.
    pub(crate) fn append(&mut self, text: &str) {
        self.text.push_str(text);
        self.tail.grown = true;
    }
}
/// The vendor connection and what it reported.
pub(crate) struct Connection {
    pub(crate) engine: octet_core::Engine,
    pub(crate) mode: octet_core::Mode,
    pub(crate) mode_pending: Option<octet_core::Mode>,
    pub(crate) workspace: String,
    pub(crate) model: String,
    pub(crate) requested_model: Option<String>,
    pub(crate) resolved_model: Option<String>,
    pub(crate) models: Vec<octet_core::ModelInfo>,
    pub(crate) session: String,
    pub(crate) journal: PathBuf,
    pub(crate) phase: ConnPhase,
    /// The reasoning effort requested; `None` is the vendor default.
    pub(crate) effort: Option<String>,
    /// The session a `/fork` started from, until the vendor names the fork.
    pub(crate) forking_from: Option<String>,
    /// What `/sessions` last listed, for `/resume N`.
    pub(crate) listed: Vec<octet_core::RecentSession>,
    pub(crate) status: String,
    pub(crate) usage: String,
    pub(crate) activity: State,
}
/// Where the vendor connection is, as the interface sees it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ConnPhase {
    /// Starting or reconnecting; not ready for prompts.
    Connecting,
    /// Ready, with no turn running.
    Idle,
    /// A turn is running; `cancelling` once the user cancelled it and it
    /// has not ended yet.
    Running { cancelling: bool },
    /// The session ended; only a reconnect leaves this.
    Stopped,
}

impl Connection {
    /// A connection being opened for `config`, journaling to `journal`.
    pub(crate) fn new(config: &octet_core::Config, journal: PathBuf) -> Self {
        Self {
            engine: config.engine,
            mode: config.mode,
            mode_pending: None,
            workspace: clean(&config.cwd.display().to_string()),
            model: "awaiting model metadata".into(),
            requested_model: config.model.clone(),
            resolved_model: None,
            models: Vec::new(),
            session: String::new(),
            journal,
            phase: ConnPhase::Connecting,
            effort: config.effort.clone(),
            // Announced when the vendor names the fork, which for Claude is
            // after its first turn, possibly several reconnects later.
            forking_from: config.resume.clone().filter(|_| config.fork),
            listed: Vec::new(),
            status: "connecting".into(),
            usage: String::new(),
            activity: State::Thinking,
        }
    }
    /// The session is open (idle or in a turn).
    pub(crate) fn is_ready(&self) -> bool {
        matches!(self.phase, ConnPhase::Idle | ConnPhase::Running { .. })
    }
    /// A turn is running.
    pub(crate) fn is_running(&self) -> bool {
        matches!(self.phase, ConnPhase::Running { .. })
    }
    /// The user cancelled the running turn, which has not ended yet.
    pub(crate) fn is_cancelling(&self) -> bool {
        self.phase == ConnPhase::Running { cancelling: true }
    }
    /// The user cancelled the running turn.
    pub(crate) fn cancel(&mut self) {
        if self.is_running() {
            self.phase = ConnPhase::Running { cancelling: true };
        }
    }
    /// The session has ended.
    pub(crate) fn is_stopped(&self) -> bool {
        self.phase == ConnPhase::Stopped
    }
    /// A turn started or a prompt was sent; a stopped session stays stopped.
    pub(crate) fn start_turn(&mut self) {
        if self.phase != ConnPhase::Stopped {
            self.phase = ConnPhase::Running { cancelling: false };
        }
    }
    /// The running turn ended.
    pub(crate) fn end_turn(&mut self) {
        if self.is_running() {
            self.phase = ConnPhase::Idle;
        }
    }
}

/// The conversation: entries, scroll and the copyable reply.
pub(crate) struct Transcript {
    pub(crate) entries: VecDeque<Entry>,
    pub(crate) bytes: usize,
    pub(crate) sanitizer: Sanitizer,
    pub(crate) scroll: usize,
    pub(crate) catalog_focus: Option<usize>,
    /// The current turn's reply as sent, tabs and all, for `/copy`.
    pub(crate) reply: String,
    pub(crate) reply_sanitizer: Sanitizer,
    /// A tool ran since the last text; the next text starts a new paragraph.
    pub(crate) reply_break: bool,
    /// A new turn started; the last reply stays copyable until its first text.
    pub(crate) reply_stale: bool,
}
/// A prompt as history keeps it: its text and the images sent with it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Sent {
    pub(crate) text: String,
    pub(crate) images: Vec<octet_core::ImageAttachment>,
}
/// The prompt box: the draft, history, attachments and popups.
pub(crate) struct Composer {
    pub(crate) editor: Editor,
    pub(crate) history: VecDeque<Sent>,
    pub(crate) history_index: Option<usize>,
    /// The draft being written when history browsing began.
    pub(crate) saved_draft: Sent,
    /// Browsing brings back each prompt's images: the draft had none of its
    /// own when browsing began, so none can be lost.
    pub(crate) recall_images: bool,
    /// The workspace, where `!` commands run and `@` looks for files.
    pub(crate) root: PathBuf,
    /// `!` outputs waiting to go with the next prompt.
    pub(crate) attachments: Vec<crate::shell::Ran>,
    /// Images waiting to go with the next prompt (`/image`).
    pub(crate) images: Vec<octet_core::ImageAttachment>,
    /// The suggestion popup, when open.
    pub(crate) completion: Option<crate::composer::Completion>,
    /// The workspace file index for `@`.
    pub(crate) files: crate::files::Files,
    /// A Tab folder listing is still running (perhaps stuck on a slow
    /// mount); the next Tab waits for it instead of starting another.
    pub(crate) listing: std::sync::Arc<std::sync::atomic::AtomicBool>,
    /// Prompts sent while a turn ran, sent in order as turns finish.
    pub(crate) queue: VecDeque<crate::vendor::Prompt>,
}
/// Dialogs over the screen: help, the palette and approvals.
pub(crate) struct Overlays {
    pub(crate) approvals: VecDeque<(u64, String)>,
    pub(crate) approval_scroll: u16,
    /// When the front approval was last drawn after being hidden (it
    /// arrived, or help, the palette or the editor covered it); `None` while
    /// unseen. Answer keys wait `APPROVAL_ARM` after that, so a key typed as
    /// the dialog appears cannot answer it.
    pub(crate) approval_shown: Option<std::time::Instant>,
    pub(crate) help: bool,
    /// Lines scrolled past at the top of the help screen.
    pub(crate) help_scroll: u16,
    pub(crate) palette: bool,
    pub(crate) selection: usize,
}
/// How long answer keys wait after an approval is shown.
pub(crate) const APPROVAL_ARM: std::time::Duration = std::time::Duration::from_millis(400);
impl Overlays {
    /// A different approval is now in front: it starts unscrolled and
    /// unseen.
    pub(crate) fn front_changed(&mut self) {
        self.approval_scroll = 0;
        self.approval_shown = None;
    }
    /// Something covers the dialog (help, the palette, the editor): it must
    /// be seen again before a key answers it.
    pub(crate) fn approval_hidden(&mut self) {
        self.approval_shown = None;
    }
    /// The dialog was drawn: its answer keys wait from now, unless it was
    /// already on screen.
    pub(crate) fn approval_drawn(&mut self) {
        self.approval_shown
            .get_or_insert_with(std::time::Instant::now);
    }
    /// The front approval has been on screen for `APPROVAL_ARM`.
    pub(crate) fn approval_armed(&self) -> bool {
        self.approval_shown
            .is_some_and(|shown| shown.elapsed() >= APPROVAL_ARM)
    }
}
pub(crate) struct App {
    pub(crate) conn: Connection,
    pub(crate) chat: Transcript,
    pub(crate) composer: Composer,
    pub(crate) overlay: Overlays,
    pub(crate) monochrome: bool,
    /// The status bar's message: the latest note, hint or error.
    pub(crate) status_line: String,
    /// What the status line holds, so a passing state (the quit hint,
    /// "Cancelling…") is cleared by kind, never by comparing text.
    pub(crate) status_kind: StatusKind,
    /// Until when a second Ctrl+C quits; set by the first press on an idle,
    /// empty prompt.
    pub(crate) quit_armed: Option<tokio::time::Instant>,
    pub(crate) goals: octet_core::goal::GoalRunner,
    /// The one job running off the loop.
    pub(crate) job: Option<crate::jobs::Running>,
    /// The running `!` command, if any.
    pub(crate) shell: Option<crate::shell::Running>,
    /// The conversation so far, waiting to go with the next prompt to a
    /// provider that took over from another.
    pub(crate) pending_handoff: Option<crate::handoff::Handoff>,
}
impl App {
    pub(crate) fn new(config: &octet_core::Config, journal: PathBuf) -> Self {
        Self {
            conn: Connection::new(config, journal),
            chat: Transcript {
                entries: VecDeque::new(),
                bytes: 0,
                sanitizer: Sanitizer::default(),
                scroll: 0,
                catalog_focus: None,
                reply: String::new(),
                reply_sanitizer: Sanitizer::keeping_tabs(),
                reply_break: false,
                reply_stale: false,
            },
            composer: Composer {
                editor: Editor::default(),
                history: VecDeque::new(),
                history_index: None,
                saved_draft: Sent::default(),
                recall_images: true,
                root: config.cwd.clone(),
                attachments: Vec::new(),
                images: Vec::new(),
                listing: std::sync::Arc::default(),
                completion: None,
                files: crate::files::Files::Unbuilt,
                queue: VecDeque::new(),
            },
            overlay: Overlays {
                approvals: VecDeque::new(),
                approval_scroll: 0,
                approval_shown: None,
                help: false,
                help_scroll: 0,
                palette: false,
                selection: 0,
            },
            monochrome: std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()),
            status_line: String::new(),
            status_kind: StatusKind::Plain,
            quit_armed: None,
            goals: octet_core::goal::GoalRunner::default(),
            job: None,
            shell: None,
            pending_handoff: None,
        }
    }
    pub(crate) fn connection(&mut self, config: &octet_core::Config, journal: PathBuf) {
        self.composer.completion = None;
        // A `!` command belongs to its session: dropping it kills its group.
        if self.shell.take().is_some() {
            self.note("The running command stopped when the session changed");
        }
        // Only the last /sessions listing outlives a connection.
        let listed = std::mem::take(&mut self.conn.listed);
        self.conn = Connection {
            listed,
            ..Connection::new(config, journal)
        };
        self.chat.catalog_focus = None;
        self.overlay.approvals.clear();
        self.overlay.approval_scroll = 0;
        self.goals.reset_turn();
    }
    /// Another provider takes over: renders the conversation so far to go
    /// with the next prompt. Whether there was anything to carry.
    pub(crate) fn carry_conversation(&mut self) -> bool {
        self.pending_handoff =
            crate::handoff::render(&self.chat.entries, crate::handoff::HANDOFF_BUDGET);
        self.pending_handoff.is_some()
    }
    /// A `!` command is running.
    pub(crate) fn shell_running(&self) -> bool {
        self.shell.is_some()
    }
    /// Neither connected nor stopped: the vendor is still starting.
    pub(crate) fn is_connecting(&self) -> bool {
        self.conn.phase == ConnPhase::Connecting
    }
    /// Ready for a prompt: connected, no turn running.
    pub(crate) fn is_idle(&self) -> bool {
        self.conn.phase == ConnPhase::Idle
    }
    /// A turn is running or the connection is still being made; Esc cancels.
    pub(crate) fn is_busy(&self) -> bool {
        self.conn.is_running() || self.is_connecting()
    }
    fn refresh_model_label(&mut self) {
        self.conn.model = match &self.conn.resolved_model {
            Some(id) => self
                .conn
                .models
                .iter()
                .find(|m| m.id.as_ref() == Some(id))
                .map_or_else(|| id.clone(), |m| format!("{} · {}", clean(&m.name), id)),
            None if self.conn.engine.offline() => "offline · no model".into(),
            None => self.catalog_selection().map_or_else(
                || {
                    format!(
                        "{} · unconfirmed",
                        self.conn
                            .requested_model
                            .as_deref()
                            .unwrap_or("provider default")
                    )
                },
                |m| {
                    format!(
                        "{} · {} · unconfirmed",
                        clean(&m.name),
                        m.id.as_deref().unwrap_or(&m.selection)
                    )
                },
            ),
        };
    }
    fn catalog_selection(&self) -> Option<&octet_core::ModelInfo> {
        if let Some(id) = &self.conn.resolved_model {
            self.conn.models.iter().find(|m| m.id.as_ref() == Some(id))
        } else {
            self.conn
                .models
                .iter()
                .find(|m| m.selection == self.conn.requested_model.as_deref().unwrap_or("default"))
        }
    }
    pub(crate) fn mode_details(&self) -> String {
        let mut text = format!("Permission mode: {}", self.conn.mode.label());
        if let Some(pending) = self.conn.mode_pending {
            let _ = write!(text, " (switching to {})", pending.label());
        }
        let _ = write!(text, "\n\n{} mapping:\n", self.conn.engine);
        for mode in octet_core::Mode::ALL {
            let marker = if mode == self.conn.mode { "●" } else { " " };
            let _ = writeln!(
                text,
                "{marker} {:<13} {}",
                mode.label(),
                mode.describe(self.conn.engine)
            );
        }
        text.push_str(
            "\nShift+Tab cycles ask → accept-edits → auto. /mode full-access reconnects with every check off.",
        );
        text
    }
    pub(crate) fn model_details(&self) -> String {
        let selected = self.catalog_selection();
        format!(
            "Provider: {}\nRequested selection: {}\nConfirmed model ID: {}\nCatalog model ID: {}\nModel name: {}\n{}",
            self.conn.engine,
            self.conn
                .requested_model
                .as_deref()
                .unwrap_or("provider default"),
            self.conn
                .resolved_model
                .as_deref()
                .unwrap_or(if self.conn.engine.offline() {
                    "none (offline demo)"
                } else {
                    "not yet reported by provider"
                }),
            selected
                .and_then(|m| m.id.as_deref())
                .unwrap_or("not reported"),
            selected.map_or("not reported", |m| m.name.as_str()),
            selected.map_or("", |m| m.description.as_str())
        )
    }
    pub(crate) fn show_models(&mut self, page: usize) {
        let pages = self.conn.models.len().div_ceil(CATALOG_PAGE).max(1);
        if page == 0 || page > pages {
            self.note(format!(
                "Choose a catalog page from 1 to {pages}: /model list <page>"
            ));
            return;
        }
        self.note(self.model_details());
        if self.conn.models.is_empty() {
            self.note(
                "No catalog reported yet. Explicit model IDs remain supported; /reconnect refreshes discovery.",
            );
        } else {
            let engine = self.conn.engine;
            let count = self.conn.models.len();
            self.note(format!(
                "{engine} catalog · {count} entries · page {page}/{pages}. \
                 PgUp/PgDn scroll; /model list <page>. Account access may vary."
            ));
            // Page the catalog so large lists do not evict current details from scrollback.
            let entries: Vec<String> = self
                .conn
                .models
                .iter()
                .skip((page - 1) * CATALOG_PAGE)
                .take(CATALOG_PAGE)
                .map(|model| {
                    format!(
                        "{}\nModel ID: {}\n{}\nSelect: /model {} {}",
                        model.name,
                        model.id.as_deref().unwrap_or("unresolved alias"),
                        model.description,
                        self.conn.engine,
                        model.selection
                    )
                })
                .collect();
            for entry in entries {
                self.note(entry);
            }
        }
        let vendors = octet_core::model::vendor_names();
        self.note(format!(
            "{}\n/model default uses the provider default. Switching providers starts fresh context.",
            catalog_hint(&vendors)
        ));
        if !self.conn.models.is_empty() {
            self.chat.catalog_focus =
                Some((self.conn.models.len() - (page - 1) * CATALOG_PAGE).min(CATALOG_PAGE) + 2);
        }
    }
    fn add(&mut self, role: Role, text: &str) {
        let mut text = clean(text);
        if text.len() > BLOCK_BYTES {
            let start = text.ceil_char_boundary(text.len() - BLOCK_BYTES);
            text = format!("[Earlier output is in the journal]\n{}", &text[start..]);
        }
        self.chat.bytes += text.len();
        self.chat
            .entries
            .push_back(Entry::new(role, self.conn.engine, text));
        self.trim();
    }
    fn trim(&mut self) {
        while self.chat.bytes > MAX_BYTES || self.chat.entries.len() > MAX_ENTRIES {
            if let Some(old) = self.chat.entries.pop_front() {
                self.chat.bytes -= old.text.len();
            } else {
                break;
            }
        }
    }
    /// Something that failed, as an `ERROR` entry and on the status line.
    pub(crate) fn error(&mut self, text: impl Into<String>) {
        let text = text.into();
        self.status(StatusKind::Plain, &text);
        self.add(Role::Error, &text);
    }
    #[cfg(test)]
    pub(crate) fn last_role(&self) -> Option<Role> {
        self.chat.entries.back().map(|entry| entry.role)
    }
    /// Information, as a transcript note and on the status line.
    pub(crate) fn note(&mut self, text: impl Into<String>) {
        let text = text.into();
        self.status(StatusKind::Plain, &text);
        self.add(Role::Notice, &text);
    }
    /// The goal file could not be saved; `paused` when the goal was paused
    /// because of it.
    pub(crate) fn goal_save_failed(&mut self, error: impl std::fmt::Display, paused: bool) {
        let paused = if paused { "; paused" } else { "" };
        self.error(format!("Goal persistence failed{paused}: {error}"));
    }
    /// A turn is running or an approval waits: the session is busy.
    pub(crate) fn turn_open(&self) -> bool {
        self.conn.is_running() || !self.overlay.approvals.is_empty()
    }
    /// A refusal or a passing hint, on the status line only.
    pub(crate) fn hint(&mut self, text: impl Into<String>) {
        self.status(StatusKind::Plain, &text.into());
    }
    /// Shows `text` on the status line as a `kind` of message.
    pub(crate) fn status(&mut self, kind: StatusKind, text: &str) {
        self.status_line = clean(text);
        self.status_kind = kind;
    }
    /// Clears the status line if it still shows a `kind` message.
    pub(crate) fn clear_status(&mut self, kind: StatusKind) {
        if self.status_kind == kind {
            self.status_line.clear();
            self.status_kind = StatusKind::Plain;
        }
    }
    #[expect(
        clippy::too_many_lines,
        reason = "one short arm per event; a fuller state machine is out of scope (spec)"
    )]
    pub(crate) fn event(&mut self, event: Event) {
        match event {
            Event::Models(models) => {
                self.conn.models = models;
                self.refresh_model_label();
            }
            Event::ModelSelected(id) => {
                self.conn.resolved_model = Some(id);
                self.refresh_model_label();
            }
            Event::ModeChanged(mode) => {
                self.conn.mode = mode;
                self.conn.mode_pending = None;
            }
            Event::Ready { session } => {
                self.refresh_model_label();
                // Claude can report its session ID mid-turn: a running or
                // stopped connection keeps its phase.
                if self.conn.phase == ConnPhase::Connecting {
                    self.conn.phase = ConnPhase::Idle;
                }
                self.conn.session = clean(&session);
                if !session.is_empty()
                    && self
                        .conn
                        .forking_from
                        .as_ref()
                        .is_some_and(|from| *from != session)
                    && let Some(from) = self.conn.forking_from.take()
                {
                    self.note(format!("Forked from {from} into {session}"));
                }
                if !self.conn.is_running() {
                    self.conn.status = "ready".into();
                    self.conn.activity = State::Idle;
                }
            }
            Event::User(text) => {
                self.add(Role::User, &text);
                self.chat.scroll = 0;
            }
            Event::Started => {
                self.conn.start_turn();
                self.conn.activity = State::Thinking;
                self.conn.status = "working".into();
                self.status(StatusKind::Plain, "");
                self.chat.sanitizer = Sanitizer::default();
                self.chat.reply_stale = true;
            }
            Event::Text(text) => {
                self.conn.activity = State::Thinking;
                self.keep_reply(&text);
                let text = self.chat.sanitizer.push(&text);
                if self
                    .chat
                    .entries
                    .back()
                    .is_none_or(|e| e.role != Role::Assistant)
                {
                    self.add(Role::Assistant, "");
                }
                let e = self
                    .chat
                    .entries
                    .back_mut()
                    .expect("an assistant entry was just ensured");
                e.append(&text);
                self.chat.bytes += text.len();
                if e.text.len() > BLOCK_BYTES + BLOCK_SLACK {
                    let remove = e.text.ceil_char_boundary(e.text.len() - BLOCK_BYTES);
                    e.text.drain(..remove);
                    self.chat.bytes -= remove;
                    // Its start moved: the next paint wraps it whole.
                    e.invalidate();
                }
                self.trim();
            }
            Event::Tool(text) => {
                self.chat.reply_break = true;
                self.conn.activity = State::tool(&text);
                self.add(Role::Tool, &text);
            }
            Event::Approval { id, detail } => {
                let first = self.overlay.approvals.is_empty();
                self.overlay.approvals.push_back((id, clean(&detail)));
                if first {
                    self.overlay.front_changed();
                }
            }
            Event::ApprovalClosed(id) => {
                let front = self.overlay.approvals.front().map(|(key, _)| *key);
                self.overlay.approvals.retain(|(key, _)| *key != id);
                if front == Some(id) {
                    self.overlay.front_changed();
                }
            }
            Event::Usage(text) => self.conn.usage = clean(&text),
            Event::Finished { outcome } => {
                self.conn.end_turn();
                self.clear_status(StatusKind::Cancelling);
                self.conn.status = clean(outcome.as_str());
                self.conn.activity = match outcome {
                    octet_core::Outcome::Completed => State::Success,
                    octet_core::Outcome::Interrupted => State::Sleeping,
                    _ => State::Error,
                };
            }
            Event::Notice(text) => self.note(text),
            Event::Error(text) => {
                self.add(Role::Error, &text);
                self.conn.status = "error".into();
                self.conn.activity = State::Error;
            }
            Event::Stopped => {
                // A cancelled connection stops without finishing a turn.
                self.clear_status(StatusKind::Cancelling);
                if self.conn.activity != State::Error {
                    self.conn.activity = State::Sleeping;
                }
                self.conn.mode_pending = None;
                self.conn.phase = ConnPhase::Stopped;
                if let Some(from) = self.conn.forking_from.take() {
                    self.note(format!(
                        "The fork from {from} did not open; /reconnect tries again"
                    ));
                }
                self.overlay.approvals.clear();
                // Nothing would send them, and after a reconnect they would
                // follow newer prompts.
                let dropped = std::mem::take(&mut self.composer.queue).len();
                if dropped > 0 {
                    self.note(format!(
                        "Dropped {}: the session stopped",
                        queued_prompts(dropped)
                    ));
                }
                self.conn.status = "disconnected".into();
            }
        }
    }
    pub(crate) fn mascot_state(&self) -> State {
        if self.overlay.approvals.is_empty() {
            self.conn.activity
        } else {
            State::Approval
        }
    }
    /// A finished `!` command in the transcript.
    pub(crate) fn shell_output(&mut self, ran: &crate::shell::Ran) {
        let output = clean(&ran.output);
        let newline = if output.is_empty() || output.ends_with('\n') {
            ""
        } else {
            "\n"
        };
        let role = if ran.ok() {
            Role::Shell
        } else {
            Role::ShellFailed
        };
        self.add(
            role,
            &format!("$ {}\n{output}{newline}{}", ran.command, ran.summary()),
        );
    }
    /// Keeps `ran` for the next prompt, dropping the oldest attachments
    /// beyond 32 KiB of output.
    pub(crate) fn attach(&mut self, mut ran: crate::shell::Ran) {
        ran.output = strip(&ran.output);
        self.composer.attachments.push(ran);
        let total = |all: &[crate::shell::Ran]| all.iter().map(|a| a.output.len()).sum::<usize>();
        let mut dropped = false;
        while self.composer.attachments.len() > 1
            && total(&self.composer.attachments) > crate::shell::OUTPUT_LIMIT
        {
            self.composer.attachments.remove(0);
            dropped = true;
        }
        if dropped {
            self.hint(format!(
                "Dropped the oldest attachment to stay within {} KiB",
                crate::shell::OUTPUT_LIMIT / 1024
            ));
        }
    }
    /// Back to the newest output, cancelling a catalog scroll that the next
    /// frame would otherwise still apply.
    pub(crate) fn follow_latest(&mut self) {
        self.chat.scroll = 0;
        self.chat.catalog_focus = None;
    }
    /// Scrolls the conversation by `rows`, up when positive.
    pub(crate) fn scroll_by(&mut self, rows: isize) {
        self.chat.catalog_focus = None;
        self.chat.scroll = self.chat.scroll.saturating_add_signed(rows).min(MAX_SCROLL);
    }
    /// The most recent reply as the vendor sent it, every segment of the
    /// turn, for `/copy`.
    pub(crate) fn last_reply(&self) -> Option<&str> {
        (!self.chat.reply.is_empty()).then_some(self.chat.reply.as_str())
    }
    /// Adds streamed text to the reply `/copy` takes, up to just over the
    /// clipboard limit so a cut can still be reported.
    fn keep_reply(&mut self, text: &str) {
        if std::mem::take(&mut self.chat.reply_stale) {
            self.chat.reply.clear();
            self.chat.reply_sanitizer = Sanitizer::keeping_tabs();
            self.chat.reply_break = false;
        }
        let text = self.chat.reply_sanitizer.push(text);
        if text.is_empty() || self.chat.reply.len() > crate::clipboard::LIMIT {
            return;
        }
        if std::mem::take(&mut self.chat.reply_break) && !self.chat.reply.is_empty() {
            self.chat.reply.push_str("\n\n");
        }
        self.chat.reply.push_str(&text);
        if self.chat.reply.len() > crate::clipboard::LIMIT + 1 {
            let end = self
                .chat
                .reply
                .floor_char_boundary(crate::clipboard::LIMIT + 1);
            self.chat.reply.truncate(end);
        }
    }
    #[cfg(test)]
    pub(crate) fn entries_text(&self) -> String {
        self.chat
            .entries
            .iter()
            .map(|entry| entry.text.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    }
    /// A sent draft joins the history unless it repeats the last one.
    pub(crate) fn remember(&mut self, draft: String) {
        self.remember_with(draft, Vec::new());
    }
    /// A sent draft and the images sent with it, recalled together.
    pub(crate) fn remember_with(&mut self, text: String, images: Vec<octet_core::ImageAttachment>) {
        self.composer.history_index = None;
        let sent = Sent { text, images };
        if self.composer.history.back() != Some(&sent) {
            self.composer.history.push_back(sent);
            if self.composer.history.len() > HISTORY_LIMIT {
                self.composer.history.pop_front();
            }
        }
    }
    /// Inserts at the cursor, or says the prompt is full.
    pub(crate) fn insert_or_warn(&mut self, text: &str) -> bool {
        let fits = self.composer.editor.insert(text);
        if !fits {
            self.hint(PROMPT_FULL);
        }
        fits
    }
    /// Replaces the word before the cursor, or says the prompt is full.
    pub(crate) fn replace_or_warn(&mut self, start: usize, text: &str) -> bool {
        let fits = self.composer.editor.replace(start, text);
        if !fits {
            self.hint(PROMPT_FULL);
        }
        fits
    }
    pub(crate) fn recall(&mut self, older: bool) {
        if self.composer.history.is_empty() {
            return;
        }
        if older {
            let index = self.composer.history_index.map_or_else(
                || {
                    self.composer.recall_images = self.composer.images.is_empty();
                    self.composer.saved_draft = Sent {
                        text: self.composer.editor.text().to_owned(),
                        images: Vec::new(),
                    };
                    self.composer.history.len() - 1
                },
                |i| i.saturating_sub(1),
            );
            self.composer.history_index = Some(index);
            self.show(self.composer.history[index].clone());
        } else if let Some(i) = self.composer.history_index {
            if i + 1 < self.composer.history.len() {
                self.composer.history_index = Some(i + 1);
                self.show(self.composer.history[i + 1].clone());
            } else {
                self.composer.history_index = None;
                let saved = std::mem::take(&mut self.composer.saved_draft);
                self.show(saved);
            }
        }
    }
    /// Puts a sent (or saved) draft back in the prompt box, with its images
    /// unless the draft being written has images of its own.
    fn show(&mut self, sent: Sent) {
        self.composer.editor.set(sent.text);
        if self.composer.recall_images {
            self.composer.images = sent.images;
        }
    }
}
/// The `/model` catalog footer's first line.
fn catalog_hint(vendors: &[&str]) -> String {
    std::iter::once("/model <ID or alias>".to_owned())
        .chain(vendors.iter().map(|v| format!("/model {v} <ID>")))
        .collect::<Vec<_>>()
        .join(" · ")
}
/// "1 queued prompt", "3 queued prompts".
pub(crate) fn queued_prompts(count: usize) -> String {
    format!("{count} queued prompt{}", if count == 1 { "" } else { "s" })
}
/// The bottom-line notice while a cancelled turn winds down.
pub(crate) const CANCELLING: &str = "Cancelling…";

/// What the status line holds.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum StatusKind {
    /// A note, hint or error.
    #[default]
    Plain,
    /// "Press Ctrl+C again to quit", until it expires.
    QuitHint,
    /// "Cancelling…", until the turn (or connection) ends.
    Cancelling,
}

#[cfg(test)]
mod tests;
