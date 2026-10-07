//! The interface state: connection, transcript, composer and dialogs, and how
//! vendor events change it. Drawing is in `view`.
use crate::{
    editor::Editor,
    mascot::State,
    text::{clean, strip, Sanitizer},
    view::{ACCENT, AMBER, FG, MUTED},
};
use octet_core::Event;
use ratatui::prelude::*;
use std::{collections::VecDeque, path::PathBuf};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

const MAX_BYTES: usize = 512 * 1024;
/// Prompts that can wait for the running turn.
pub(crate) const QUEUE_LIMIT: usize = 8;
/// Prompts kept for Up/Down.
const HISTORY_LIMIT: usize = 50;
/// Shown when an edit would push the draft past the prompt limit.
pub const PROMPT_FULL: &str = "Prompt limit reached";
const BLOCK_BYTES: usize = 64 * 1024;
#[derive(Clone, Copy, PartialEq)]
pub enum Role {
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
    pub(crate) text: String,
    pub(crate) width: u16,
    pub(crate) cache: Vec<Line<'static>>,
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
    /// A turn is running.
    Running,
    /// The session ended; only a reconnect leaves this.
    Stopped,
}

impl Connection {
    /// The session is open (idle or in a turn).
    pub(crate) fn is_ready(&self) -> bool {
        matches!(self.phase, ConnPhase::Idle | ConnPhase::Running)
    }
    /// A turn is running.
    pub(crate) fn is_running(&self) -> bool {
        self.phase == ConnPhase::Running
    }
    /// The session has ended.
    pub(crate) fn is_stopped(&self) -> bool {
        self.phase == ConnPhase::Stopped
    }
    /// A turn started or a prompt was sent; a stopped session stays stopped.
    pub(crate) fn start_turn(&mut self) {
        if self.phase != ConnPhase::Stopped {
            self.phase = ConnPhase::Running;
        }
    }
    /// The running turn ended.
    pub(crate) fn end_turn(&mut self) {
        if self.phase == ConnPhase::Running {
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
/// The prompt box: the draft, history, attachments and popups.
pub(crate) struct Composer {
    pub(crate) editor: Editor,
    pub(crate) history: VecDeque<String>,
    pub(crate) history_index: Option<usize>,
    pub(crate) saved_draft: String,
    /// The workspace, where `!` commands run and `@` looks for files.
    pub(crate) root: PathBuf,
    /// `!` outputs waiting to go with the next prompt.
    pub(crate) attachments: Vec<crate::shell::Ran>,
    /// Images waiting to go with the next prompt (`/image`).
    pub(crate) images: Vec<octet_core::ImageAttachment>,
    /// A `!` command is running.
    pub(crate) shell_running: bool,
    /// The suggestion popup, when open.
    pub(crate) completion: Option<crate::composer::Completion>,
    /// The workspace file index for `@`.
    pub(crate) files: crate::files::Files,
    /// Prompts sent while a turn ran, sent in order as turns finish.
    pub(crate) queue: VecDeque<octet_core::Command>,
}
/// Dialogs over the screen: help, the palette and approvals.
pub(crate) struct Overlays {
    pub(crate) approvals: VecDeque<(u64, String)>,
    pub(crate) approval_scroll: u16,
    pub(crate) help: bool,
    /// Lines scrolled past at the top of the help screen.
    pub(crate) help_scroll: u16,
    pub(crate) palette: bool,
    pub(crate) selection: usize,
}
pub struct App {
    pub(crate) conn: Connection,
    pub(crate) chat: Transcript,
    pub(crate) composer: Composer,
    pub(crate) overlay: Overlays,
    pub(crate) monochrome: bool,
    pub(crate) notice: String,
    /// Until when a second Ctrl+C quits; set by the first press on an idle,
    /// empty prompt.
    pub(crate) quit_armed: Option<tokio::time::Instant>,
    pub(crate) goals: octet_core::goal::GoalRunner,
}
impl App {
    pub fn new(config: &octet_core::Config, journal: PathBuf) -> Self {
        Self {
            conn: Connection {
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
                listed: Vec::new(),
                status: "connecting".into(),
                usage: String::new(),
                activity: State::Thinking,
            },
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
                saved_draft: String::new(),
                root: config.cwd.clone(),
                attachments: Vec::new(),
                images: Vec::new(),
                shell_running: false,
                completion: None,
                files: crate::files::Files::Unbuilt,
                queue: VecDeque::new(),
            },
            overlay: Overlays {
                approvals: VecDeque::new(),
                approval_scroll: 0,
                help: false,
                help_scroll: 0,
                palette: false,
                selection: 0,
            },
            monochrome: std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()),
            notice: String::new(),
            quit_armed: None,
            goals: octet_core::goal::GoalRunner::default(),
        }
    }
    pub fn connection(&mut self, config: &octet_core::Config, journal: PathBuf) {
        // Rebuilt per connection, so a build abandoned with the old session
        // never leaves the index stuck at Building.
        self.composer.files = crate::files::Files::Unbuilt;
        self.composer.completion = None;
        // The old session's `!` command was dropped, and killed, with it.
        if std::mem::take(&mut self.composer.shell_running) {
            self.notice("The running command stopped when the session changed");
        }
        self.conn.engine = config.engine;
        self.conn.mode = config.mode;
        self.conn.mode_pending = None;
        self.conn.model = "awaiting model metadata".into();
        self.conn.requested_model = config.model.clone();
        self.conn.resolved_model = None;
        self.conn.models.clear();
        self.conn.session.clear();
        self.chat.catalog_focus = None;
        self.conn.journal = journal;
        self.conn.phase = ConnPhase::Connecting;
        self.conn.effort = config.effort.clone();
        self.conn.status = "connecting".into();
        self.conn.activity = State::Thinking;
        self.conn.usage.clear();
        self.overlay.approvals.clear();
        self.overlay.approval_scroll = 0;
        self.goals.reset_turn();
    }
    /// Neither connected nor stopped: the vendor is still starting.
    pub fn is_connecting(&self) -> bool {
        self.conn.phase == ConnPhase::Connecting
    }
    /// Ready for a prompt: connected, no turn running.
    pub fn is_idle(&self) -> bool {
        self.conn.phase == ConnPhase::Idle
    }
    /// A turn is running or the connection is still being made; Esc cancels.
    pub fn is_busy(&self) -> bool {
        self.conn.is_running() || self.is_connecting()
    }
    fn refresh_model_label(&mut self) {
        self.conn.model = match &self.conn.resolved_model {
            Some(id) => self
                .conn
                .models
                .iter()
                .find(|m| m.id.as_ref() == Some(id))
                .map(|m| format!("{} · {}", clean(&m.name), id))
                .unwrap_or_else(|| id.clone()),
            None if self.conn.engine.offline() => "offline · no model".into(),
            None => self
                .catalog_selection()
                .map(|m| {
                    format!(
                        "{} · {} · unconfirmed",
                        clean(&m.name),
                        m.id.as_deref().unwrap_or(&m.selection)
                    )
                })
                .unwrap_or_else(|| {
                    format!(
                        "{} · unconfirmed",
                        self.conn
                            .requested_model
                            .as_deref()
                            .unwrap_or("provider default")
                    )
                }),
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
    pub fn mode_details(&self) -> String {
        let mut text = format!("Permission mode: {}", self.conn.mode.label());
        if let Some(pending) = self.conn.mode_pending {
            text.push_str(&format!(" (switching to {})", pending.label()));
        }
        text.push_str(&format!("\n\n{} mapping:\n", self.conn.engine));
        for mode in octet_core::Mode::ALL {
            let marker = if mode == self.conn.mode { "●" } else { " " };
            text.push_str(&format!(
                "{marker} {:<13} {}\n",
                mode.label(),
                mode.describe(self.conn.engine)
            ));
        }
        text.push_str(
            "\nShift+Tab cycles ask → accept-edits → auto. /mode full-access reconnects with every check off.",
        );
        text
    }
    pub fn model_details(&self) -> String {
        let selected = self.catalog_selection();
        format!(
            "Provider: {}\nRequested selection: {}\nConfirmed model ID: {}\nCatalog model ID: {}\nModel name: {}\n{}",
            self.conn.engine,
            self.conn.requested_model
                .as_deref()
                .unwrap_or("provider default"),
            self.conn.resolved_model
                .as_deref()
                .unwrap_or(if self.conn.engine.offline() {
                    "none (offline demo)"
                } else {
                    "not yet reported by provider"
                }),
            selected.and_then(|m|m.id.as_deref()).unwrap_or("not reported"),
            selected.map(|m| m.name.as_str()).unwrap_or("not reported"),
            selected.map(|m| m.description.as_str()).unwrap_or("")
        )
    }
    pub fn show_models(&mut self, page: usize) {
        let pages = self.conn.models.len().div_ceil(20).max(1);
        if page == 0 || page > pages {
            self.notice(format!(
                "Choose a catalog page from 1 to {pages}: /model list <page>"
            ));
            return;
        }
        self.notice(self.model_details());
        if self.conn.models.is_empty() {
            self.notice(
                "No catalog reported yet. Explicit model IDs remain supported; /reconnect refreshes discovery.",
            );
        } else {
            let engine = self.conn.engine;
            let count = self.conn.models.len();
            self.notice(format!(
                "{engine} catalog · {count} entries · page {page}/{pages}. \
                 PgUp/PgDn scroll; /model list <page>. Account access may vary."
            ));
            // Page the catalog so large lists do not evict current details from scrollback.
            let entries: Vec<String> = self
                .conn
                .models
                .iter()
                .skip((page - 1) * 20)
                .take(20)
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
                self.notice(entry);
            }
        }
        let vendors = octet_core::model::vendor_names();
        self.notice(format!(
            "{}\n/model default uses the provider default. Switching providers starts fresh context.",
            octet_core::model::catalog_hint(&vendors)
        ));
        if !self.conn.models.is_empty() {
            self.chat.catalog_focus = Some((self.conn.models.len() - (page - 1) * 20).min(20) + 2);
        }
    }
    fn add(&mut self, role: Role, text: &str) {
        let mut text = clean(text);
        if text.len() > BLOCK_BYTES {
            let start = text.ceil_char_boundary(text.len() - BLOCK_BYTES);
            text = format!("[Earlier output is in the journal]\n{}", &text[start..]);
        }
        self.chat.bytes += text.len();
        self.chat.entries.push_back(Entry {
            role,
            text,
            width: 0,
            cache: Vec::new(),
        });
        self.trim();
    }
    fn trim(&mut self) {
        while self.chat.bytes > MAX_BYTES || self.chat.entries.len() > 160 {
            if let Some(old) = self.chat.entries.pop_front() {
                self.chat.bytes -= old.text.len();
            } else {
                break;
            }
        }
    }
    /// Something that failed, as an `ERROR` entry and on the status line.
    pub fn error(&mut self, text: impl Into<String>) {
        let text = text.into();
        self.notice = clean(&text);
        self.add(Role::Error, &text);
    }
    #[cfg(test)]
    pub fn last_role(&self) -> Option<Role> {
        self.chat.entries.back().map(|entry| entry.role)
    }
    pub fn notice(&mut self, text: impl Into<String>) {
        let text = text.into();
        self.notice = clean(&text);
        self.add(Role::Notice, &text);
    }
    pub fn event(&mut self, event: Event) {
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
                self.notice.clear();
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
                e.text.push_str(&text);
                self.chat.bytes += text.len();
                e.width = 0;
                if e.text.len() > BLOCK_BYTES {
                    let remove = e.text.ceil_char_boundary(e.text.len() - BLOCK_BYTES);
                    e.text.drain(..remove);
                    self.chat.bytes -= remove;
                }
                self.trim();
            }
            Event::Tool(text) => {
                self.chat.reply_break = true;
                self.conn.activity = State::tool(&text);
                self.add(Role::Tool, &text);
            }
            Event::Approval { id, detail } => {
                self.overlay.approvals.push_back((id, clean(&detail)));
                self.overlay.approval_scroll = 0;
            }
            Event::ApprovalClosed(id) => {
                self.overlay.approvals.retain(|(key, _)| *key != id);
                self.overlay.approval_scroll = 0;
            }
            Event::Usage(text) => self.conn.usage = clean(&text),
            Event::Finished { outcome } => {
                self.conn.end_turn();
                if self.notice == CANCELLING {
                    self.notice.clear();
                }
                self.conn.status = clean(outcome.as_str());
                self.conn.activity = match outcome {
                    octet_core::Outcome::Completed => State::Success,
                    octet_core::Outcome::Interrupted => State::Sleeping,
                    _ => State::Error,
                };
            }
            Event::Notice(text) => self.notice(text),
            Event::Error(text) => {
                self.add(Role::Error, &text);
                self.conn.status = "error".into();
                self.conn.activity = State::Error;
            }
            Event::Stopped => {
                if self.conn.activity != State::Error {
                    self.conn.activity = State::Sleeping;
                }
                self.conn.mode_pending = None;
                self.conn.phase = ConnPhase::Stopped;
                self.overlay.approvals.clear();
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
    pub fn shell_output(&mut self, ran: &crate::shell::Ran) {
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
    pub fn attach(&mut self, mut ran: crate::shell::Ran) {
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
            self.notice = "Dropped the oldest attachment to stay within 32 KiB".into();
        }
    }
    /// Back to the newest output, cancelling a catalog scroll that the next
    /// frame would otherwise still apply.
    pub fn follow_latest(&mut self) {
        self.chat.scroll = 0;
        self.chat.catalog_focus = None;
    }
    /// Scrolls the conversation by `rows`, up when positive.
    pub fn scroll_by(&mut self, rows: isize) {
        self.chat.catalog_focus = None;
        self.chat.scroll = self.chat.scroll.saturating_add_signed(rows).min(65536);
    }
    /// The most recent reply as the vendor sent it, every segment of the
    /// turn, for `/copy`.
    pub fn last_reply(&self) -> Option<&str> {
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
    pub fn entries_text(&self) -> String {
        self.chat
            .entries
            .iter()
            .map(|entry| entry.text.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    }
    /// A sent draft joins the history unless it repeats the last one.
    pub fn remember(&mut self, draft: String) {
        self.composer.history_index = None;
        if self.composer.history.back() != Some(&draft) {
            self.composer.history.push_back(draft);
            if self.composer.history.len() > HISTORY_LIMIT {
                self.composer.history.pop_front();
            }
        }
    }
    /// Inserts at the cursor, or says the prompt is full.
    pub fn insert_or_warn(&mut self, text: &str) -> bool {
        let fits = self.composer.editor.insert(text);
        if !fits {
            self.notice = PROMPT_FULL.into();
        }
        fits
    }
    /// Replaces the word before the cursor, or says the prompt is full.
    pub fn replace_or_warn(&mut self, start: usize, text: &str) -> bool {
        let fits = self.composer.editor.replace(start, text);
        if !fits {
            self.notice = PROMPT_FULL.into();
        }
        fits
    }
    pub fn recall(&mut self, older: bool) {
        if self.composer.history.is_empty() {
            return;
        }
        if older {
            let index = self
                .composer
                .history_index
                .map(|i| i.saturating_sub(1))
                .unwrap_or_else(|| {
                    self.composer.saved_draft = self.composer.editor.text.clone();
                    self.composer.history.len() - 1
                });
            self.composer.history_index = Some(index);
            self.composer
                .editor
                .set(self.composer.history[index].clone());
        } else if let Some(i) = self.composer.history_index {
            if i + 1 < self.composer.history.len() {
                self.composer.history_index = Some(i + 1);
                self.composer
                    .editor
                    .set(self.composer.history[i + 1].clone());
            } else {
                self.composer.history_index = None;
                self.composer.editor.set(self.composer.saved_draft.clone());
            }
        }
    }
    pub(crate) fn visible_lines(&mut self, width: u16, height: usize) -> Vec<Line<'static>> {
        if let Some(entries) = self.chat.catalog_focus.take() {
            let mut rows = 0usize;
            for entry in self.chat.entries.iter_mut().rev().take(entries) {
                if entry.width != width {
                    entry.cache = entry_lines(entry.role, &entry.text, width);
                    entry.width = width;
                }
                rows += entry.cache.len();
            }
            self.chat.scroll = rows.saturating_sub(height);
        }
        let needed = self.chat.scroll.saturating_add(height);
        // First bring the caches up to date as far back as the view reaches,
        // then borrow rows from them and clone only those on screen.
        let mut available = 0usize;
        for entry in self.chat.entries.iter_mut().rev() {
            if entry.width != width {
                entry.cache = entry_lines(entry.role, &entry.text, width);
                entry.width = width;
            }
            available += entry.cache.len();
            if available >= needed {
                break;
            }
        }
        let lines: Vec<&Line<'static>> = self
            .chat
            .entries
            .iter()
            .rev()
            .flat_map(|entry| entry.cache.iter().rev())
            .take(needed)
            .collect();
        // Clamp the scroll to the actual cached history rather than showing emptiness.
        if self.chat.scroll >= lines.len() {
            self.chat.scroll = lines.len().saturating_sub(height);
        }
        lines
            .into_iter()
            .skip(self.chat.scroll)
            .take(height)
            .rev()
            .cloned()
            .collect()
    }
}
fn entry_lines(role: Role, text: &str, width: u16) -> Vec<Line<'static>> {
    let (label, color) = match role {
        Role::User => ("YOU", ACCENT),
        Role::Assistant => ("OCTET", FG),
        Role::Tool => ("TOOL", MUTED),
        Role::Notice => ("NOTE", MUTED),
        Role::Error => ("ERROR", AMBER),
        Role::Shell => ("SHELL", MUTED),
        Role::ShellFailed => ("SHELL", AMBER),
    };
    let mut result = vec![
        Line::default(),
        Line::from(Span::styled(label, Style::default().fg(color).bold())),
    ];
    let mut code = false;
    for line in text.split('\n') {
        if line.starts_with("```") {
            code = !code;
            result.push(Line::from(Span::styled(
                line.to_owned(),
                Style::default().fg(MUTED),
            )));
            continue;
        }
        let style = if role == Role::Error {
            Style::default().fg(AMBER)
        } else if role == Role::Tool || code {
            Style::default().fg(MUTED)
        } else if line.starts_with('#') {
            Style::default().fg(ACCENT).bold()
        } else {
            Style::default().fg(FG)
        };
        for wrapped in wrap(line, width.max(1) as usize) {
            result.push(Line::from(Span::styled(wrapped, style)));
        }
    }
    result
}
pub fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut rows = vec![String::new()];
    let mut col = 0;
    for word in text.split_word_bounds() {
        let word_width = word.width();
        if word_width <= width {
            if col + word_width > width && col > 0 {
                rows.push(String::new());
                col = 0;
            }
            if col == 0 && rows.len() > 1 && word.trim().is_empty() {
                continue;
            }
            rows.last_mut()
                .expect("rows starts non-empty")
                .push_str(word);
            col += word_width;
        } else {
            for g in word.graphemes(true) {
                let g_width = g.width();
                if col + g_width > width && col > 0 {
                    rows.push(String::new());
                    col = 0;
                }
                rows.last_mut().expect("rows starts non-empty").push_str(g);
                col += g_width;
            }
        }
    }
    rows
}
/// The bottom-line notice while a cancelled turn winds down.
pub(crate) const CANCELLING: &str = "Cancelling…";

#[cfg(test)]
mod tests;
