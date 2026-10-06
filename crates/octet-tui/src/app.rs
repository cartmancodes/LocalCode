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
pub struct App {
    pub engine: octet_core::Engine,
    pub mode: octet_core::Mode,
    pub mode_pending: Option<octet_core::Mode>,
    pub workspace: String,
    pub model: String,
    pub requested_model: Option<String>,
    pub resolved_model: Option<String>,
    pub models: Vec<octet_core::ModelInfo>,
    pub session: String,
    pub journal: PathBuf,
    pub editor: Editor,
    pub ready: bool,
    pub running: bool,
    pub stopped: bool,
    pub status: String,
    pub usage: String,
    pub(crate) activity: State,
    pub(crate) monochrome: bool,
    pub(crate) entries: VecDeque<Entry>,
    pub(crate) bytes: usize,
    pub(crate) sanitizer: Sanitizer,
    pub approvals: VecDeque<(u64, String)>,
    pub approval_scroll: u16,
    pub scroll: usize,
    pub(crate) catalog_focus: Option<usize>,
    pub help: bool,
    pub palette: bool,
    pub selection: usize,
    pub history: VecDeque<String>,
    pub history_index: Option<usize>,
    pub saved_draft: String,
    pub notice: String,
    /// Until when a second Ctrl+C quits; set by the first press on an idle,
    /// empty prompt.
    pub quit_armed: Option<tokio::time::Instant>,
    pub goals: octet_core::goal::GoalRunner,
    /// The workspace, where `!` commands run and `@` looks for files.
    pub root: PathBuf,
    /// `!` outputs waiting to go with the next prompt.
    pub attachments: Vec<crate::shell::Ran>,
    /// A `!` command is running.
    pub shell_running: bool,
    /// The suggestion popup, when open.
    pub completion: Option<crate::composer::Completion>,
    /// The workspace file index for `@`.
    pub files: crate::files::Files,
    /// The current turn's reply as sent, tabs and all, for `/copy`.
    pub(crate) reply: String,
    pub(crate) reply_sanitizer: Sanitizer,
    /// A tool ran since the last text; the next text starts a new paragraph.
    pub(crate) reply_break: bool,
    /// A new turn started; the last reply stays copyable until its first text.
    pub(crate) reply_stale: bool,
}
impl App {
    pub fn new(config: &octet_core::Config, journal: PathBuf) -> Self {
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
            editor: Editor::default(),
            ready: false,
            running: false,
            stopped: false,
            status: "connecting".into(),
            usage: String::new(),
            activity: State::Thinking,
            monochrome: std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()),
            entries: VecDeque::new(),
            bytes: 0,
            sanitizer: Sanitizer::default(),
            approvals: VecDeque::new(),
            approval_scroll: 0,
            scroll: 0,
            catalog_focus: None,
            help: false,
            palette: false,
            selection: 0,
            history: VecDeque::new(),
            history_index: None,
            saved_draft: String::new(),
            notice: String::new(),
            quit_armed: None,
            goals: octet_core::goal::GoalRunner::default(),
            root: config.cwd.clone(),
            attachments: Vec::new(),
            shell_running: false,
            completion: None,
            files: crate::files::Files::Unbuilt,
            reply: String::new(),
            reply_sanitizer: Sanitizer::keeping_tabs(),
            reply_break: false,
            reply_stale: false,
        }
    }
    pub fn connection(&mut self, config: &octet_core::Config, journal: PathBuf) {
        // Rebuilt per connection, so a build abandoned with the old session
        // never leaves the index stuck at Building.
        self.files = crate::files::Files::Unbuilt;
        self.completion = None;
        // The old session's `!` command was dropped, and killed, with it.
        if std::mem::take(&mut self.shell_running) {
            self.notice("The running command stopped when the session changed");
        }
        self.engine = config.engine;
        self.mode = config.mode;
        self.mode_pending = None;
        self.model = "awaiting model metadata".into();
        self.requested_model = config.model.clone();
        self.resolved_model = None;
        self.models.clear();
        self.session.clear();
        self.catalog_focus = None;
        self.journal = journal;
        self.ready = false;
        self.running = false;
        self.stopped = false;
        self.status = "connecting".into();
        self.activity = State::Thinking;
        self.usage.clear();
        self.approvals.clear();
        self.approval_scroll = 0;
        self.goals.reset_turn();
    }
    /// Neither connected nor stopped: the vendor is still starting.
    pub fn is_connecting(&self) -> bool {
        !self.ready && !self.stopped
    }
    /// A turn is running or the connection is still being made; Esc cancels.
    pub fn is_busy(&self) -> bool {
        self.running || self.is_connecting()
    }
    fn refresh_model_label(&mut self) {
        self.model = match &self.resolved_model {
            Some(id) => self
                .models
                .iter()
                .find(|m| m.id.as_ref() == Some(id))
                .map(|m| format!("{} · {}", clean(&m.name), id))
                .unwrap_or_else(|| id.clone()),
            None if self.engine == octet_core::Engine::Demo => "offline · no model".into(),
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
                        self.requested_model
                            .as_deref()
                            .unwrap_or("provider default")
                    )
                }),
        };
    }
    fn catalog_selection(&self) -> Option<&octet_core::ModelInfo> {
        if let Some(id) = &self.resolved_model {
            self.models.iter().find(|m| m.id.as_ref() == Some(id))
        } else {
            self.models
                .iter()
                .find(|m| m.selection == self.requested_model.as_deref().unwrap_or("default"))
        }
    }
    pub fn mode_details(&self) -> String {
        let mut text = format!("Permission mode: {}", self.mode.label());
        if let Some(pending) = self.mode_pending {
            text.push_str(&format!(" (switching to {})", pending.label()));
        }
        text.push_str(&format!("\n\n{} mapping:\n", self.engine));
        for mode in octet_core::Mode::ALL {
            let marker = if mode == self.mode { "●" } else { " " };
            text.push_str(&format!(
                "{marker} {:<13} {}\n",
                mode.label(),
                mode.describe(self.engine)
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
            self.engine,
            self.requested_model
                .as_deref()
                .unwrap_or("provider default"),
            self.resolved_model
                .as_deref()
                .unwrap_or(if self.engine == octet_core::Engine::Demo {
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
        let pages = self.models.len().div_ceil(20).max(1);
        if page == 0 || page > pages {
            self.notice(format!(
                "Choose a catalog page from 1 to {pages}: /model list <page>"
            ));
            return;
        }
        self.notice(self.model_details());
        if self.models.is_empty() {
            self.notice(
                "No catalog reported yet. Explicit model IDs remain supported; /reconnect refreshes discovery.",
            );
        } else {
            let engine = self.engine;
            let count = self.models.len();
            self.notice(format!(
                "{engine} catalog · {count} entries · page {page}/{pages}. \
                 PgUp/PgDn scroll; /model list <page>. Account access may vary."
            ));
            // Page the catalog so large lists do not evict current details from scrollback.
            for model in self
                .models
                .clone()
                .into_iter()
                .skip((page - 1) * 20)
                .take(20)
            {
                self.notice(format!(
                    "{}\nModel ID: {}\n{}\nSelect: /model {} {}",
                    model.name,
                    model.id.as_deref().unwrap_or("unresolved alias"),
                    model.description,
                    self.engine,
                    model.selection
                ));
            }
        }
        self.notice(
            "/model <ID or alias> · /model codex <ID> · /model claude <ID>\n\
             /model default uses the provider default. Switching providers starts fresh context.",
        );
        if !self.models.is_empty() {
            self.catalog_focus = Some((self.models.len() - (page - 1) * 20).min(20) + 2);
        }
    }
    fn add(&mut self, role: Role, text: String) {
        let mut text = clean(&text);
        if text.len() > BLOCK_BYTES {
            let start = text.ceil_char_boundary(text.len() - BLOCK_BYTES);
            text = format!("[Earlier output is in the journal]\n{}", &text[start..]);
        }
        self.bytes += text.len();
        self.entries.push_back(Entry {
            role,
            text,
            width: 0,
            cache: Vec::new(),
        });
        self.trim();
    }
    fn trim(&mut self) {
        while self.bytes > MAX_BYTES || self.entries.len() > 160 {
            if let Some(old) = self.entries.pop_front() {
                self.bytes -= old.text.len();
            } else {
                break;
            }
        }
    }
    /// Something that failed, as an `ERROR` entry and on the status line.
    pub fn error(&mut self, text: impl Into<String>) {
        let text = text.into();
        self.notice = clean(&text);
        self.add(Role::Error, text);
    }
    #[cfg(test)]
    pub fn last_role(&self) -> Option<Role> {
        self.entries.back().map(|entry| entry.role)
    }
    pub fn notice(&mut self, text: impl Into<String>) {
        let text = text.into();
        self.notice = clean(&text);
        self.add(Role::Notice, text);
    }
    pub fn event(&mut self, event: Event) {
        match event {
            Event::Models(models) => {
                self.models = models;
                self.refresh_model_label();
            }
            Event::ModelSelected(id) => {
                self.resolved_model = Some(id);
                self.refresh_model_label();
            }
            Event::ModeChanged(mode) => {
                self.mode = mode;
                self.mode_pending = None;
            }
            Event::Ready { session } => {
                self.refresh_model_label();
                self.ready = true;
                self.session = clean(&session);
                if !self.running {
                    self.status = "ready".into();
                    self.activity = State::Idle;
                }
            }
            Event::User(text) => {
                self.add(Role::User, text);
                self.scroll = 0;
            }
            Event::Started => {
                self.running = true;
                self.activity = State::Thinking;
                self.status = "working".into();
                self.notice.clear();
                self.sanitizer = Sanitizer::default();
                self.reply_stale = true;
            }
            Event::Text(text) => {
                self.activity = State::Thinking;
                self.keep_reply(&text);
                let text = self.sanitizer.push(&text);
                if self
                    .entries
                    .back()
                    .is_none_or(|e| e.role != Role::Assistant)
                {
                    self.add(Role::Assistant, String::new());
                }
                let e = self.entries.back_mut().unwrap();
                e.text.push_str(&text);
                self.bytes += text.len();
                e.width = 0;
                if e.text.len() > BLOCK_BYTES {
                    let remove = e.text.ceil_char_boundary(e.text.len() - BLOCK_BYTES);
                    e.text.drain(..remove);
                    self.bytes -= remove;
                }
                self.trim();
            }
            Event::Tool(text) => {
                self.reply_break = true;
                self.activity = State::tool(&text);
                self.add(Role::Tool, text);
            }
            Event::Approval { id, detail } => {
                self.approvals.push_back((id, clean(&detail)));
                self.approval_scroll = 0;
            }
            Event::ApprovalClosed(id) => {
                self.approvals.retain(|(key, _)| *key != id);
                self.approval_scroll = 0;
            }
            Event::Usage(text) => self.usage = clean(&text),
            Event::Finished { outcome } => {
                self.running = false;
                if self.notice == CANCELLING {
                    self.notice.clear();
                }
                self.status = clean(outcome.as_str());
                self.activity = match outcome {
                    octet_core::Outcome::Completed => State::Success,
                    octet_core::Outcome::Interrupted => State::Sleeping,
                    _ => State::Error,
                };
            }
            Event::Notice(text) => self.notice(text),
            Event::Error(text) => {
                self.add(Role::Error, text);
                self.status = "error".into();
                self.activity = State::Error;
            }
            Event::Stopped => {
                if self.activity != State::Error {
                    self.activity = State::Sleeping;
                }
                self.mode_pending = None;
                self.stopped = true;
                self.running = false;
                self.ready = false;
                self.approvals.clear();
                self.status = "disconnected".into();
            }
        }
    }
    pub(crate) fn mascot_state(&self) -> State {
        if self.approvals.is_empty() {
            self.activity
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
            format!("$ {}\n{output}{newline}{}", ran.command, ran.summary()),
        );
    }
    /// Keeps `ran` for the next prompt, dropping the oldest attachments
    /// beyond 32 KiB of output.
    pub fn attach(&mut self, mut ran: crate::shell::Ran) {
        ran.output = strip(&ran.output);
        self.attachments.push(ran);
        let total = |all: &[crate::shell::Ran]| all.iter().map(|a| a.output.len()).sum::<usize>();
        let mut dropped = false;
        while self.attachments.len() > 1 && total(&self.attachments) > crate::shell::OUTPUT_LIMIT {
            self.attachments.remove(0);
            dropped = true;
        }
        if dropped {
            self.notice = "Dropped the oldest attachment to stay within 32 KiB".into();
        }
    }
    /// Back to the newest output, cancelling a catalog scroll that the next
    /// frame would otherwise still apply.
    pub fn follow_latest(&mut self) {
        self.scroll = 0;
        self.catalog_focus = None;
    }
    /// Scrolls the conversation by `rows`, up when positive.
    pub fn scroll_by(&mut self, rows: isize) {
        self.catalog_focus = None;
        self.scroll = self.scroll.saturating_add_signed(rows).min(65536);
    }
    /// The most recent reply as the vendor sent it, every segment of the
    /// turn, for `/copy`.
    pub fn last_reply(&self) -> Option<&str> {
        (!self.reply.is_empty()).then_some(self.reply.as_str())
    }
    /// Adds streamed text to the reply `/copy` takes, up to just over the
    /// clipboard limit so a cut can still be reported.
    fn keep_reply(&mut self, text: &str) {
        if std::mem::take(&mut self.reply_stale) {
            self.reply.clear();
            self.reply_sanitizer = Sanitizer::keeping_tabs();
            self.reply_break = false;
        }
        let text = self.reply_sanitizer.push(text);
        if text.is_empty() || self.reply.len() > crate::clipboard::LIMIT {
            return;
        }
        if std::mem::take(&mut self.reply_break) && !self.reply.is_empty() {
            self.reply.push_str("\n\n");
        }
        self.reply.push_str(&text);
        if self.reply.len() > crate::clipboard::LIMIT + 1 {
            let end = self.reply.floor_char_boundary(crate::clipboard::LIMIT + 1);
            self.reply.truncate(end);
        }
    }
    #[cfg(test)]
    pub fn entries_text(&self) -> String {
        self.entries
            .iter()
            .map(|entry| entry.text.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    }
    /// A sent draft joins the history unless it repeats the last one.
    pub fn remember(&mut self, draft: String) {
        self.history_index = None;
        if self.history.back() != Some(&draft) {
            self.history.push_back(draft);
            if self.history.len() > HISTORY_LIMIT {
                self.history.pop_front();
            }
        }
    }
    /// Inserts at the cursor, or says the prompt is full.
    pub fn insert_or_warn(&mut self, text: &str) -> bool {
        let fits = self.editor.insert(text);
        if !fits {
            self.notice = PROMPT_FULL.into();
        }
        fits
    }
    /// Replaces the word before the cursor, or says the prompt is full.
    pub fn replace_or_warn(&mut self, start: usize, text: &str) -> bool {
        let fits = self.editor.replace(start, text);
        if !fits {
            self.notice = PROMPT_FULL.into();
        }
        fits
    }
    pub fn recall(&mut self, older: bool) {
        if self.history.is_empty() {
            return;
        }
        if older {
            let index = self
                .history_index
                .map(|i| i.saturating_sub(1))
                .unwrap_or_else(|| {
                    self.saved_draft = self.editor.text.clone();
                    self.history.len() - 1
                });
            self.history_index = Some(index);
            self.editor.set(self.history[index].clone());
        } else if let Some(i) = self.history_index {
            if i + 1 < self.history.len() {
                self.history_index = Some(i + 1);
                self.editor.set(self.history[i + 1].clone());
            } else {
                self.history_index = None;
                self.editor.set(self.saved_draft.clone());
            }
        }
    }
    pub(crate) fn transcript(&mut self, width: u16, height: usize) -> Vec<Line<'static>> {
        if let Some(entries) = self.catalog_focus.take() {
            let mut rows = 0usize;
            for entry in self.entries.iter_mut().rev().take(entries) {
                if entry.width != width {
                    entry.cache = entry_lines(entry.role, &entry.text, width);
                    entry.width = width;
                }
                rows += entry.cache.len();
            }
            self.scroll = rows.saturating_sub(height);
        }
        let mut lines = Vec::new();
        let needed = self.scroll.saturating_add(height);
        for entry in self.entries.iter_mut().rev() {
            if entry.width != width {
                entry.cache = entry_lines(entry.role, &entry.text, width);
                entry.width = width;
            }
            for line in entry.cache.iter().rev() {
                lines.push(line.clone());
                if lines.len() >= needed {
                    break;
                }
            }
            if lines.len() >= needed {
                break;
            }
        }
        // Clamp the scroll to the actual cached history rather than showing emptiness.
        if self.scroll >= lines.len() {
            self.scroll = lines.len().saturating_sub(height);
        }
        lines
            .into_iter()
            .skip(self.scroll)
            .take(height)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
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
        if word.width() <= width {
            if col + word.width() > width && col > 0 {
                rows.push(String::new());
                col = 0;
            }
            if col == 0 && rows.len() > 1 && word.trim().is_empty() {
                continue;
            }
            rows.last_mut().unwrap().push_str(word);
            col += word.width();
        } else {
            for g in word.graphemes(true) {
                if col + g.width() > width && col > 0 {
                    rows.push(String::new());
                    col = 0;
                }
                rows.last_mut().unwrap().push_str(g);
                col += g.width();
            }
        }
    }
    rows
}
/// The bottom-line notice while a cancelled turn winds down.
pub(crate) const CANCELLING: &str = "Cancelling…";

#[cfg(test)]
mod tests;
