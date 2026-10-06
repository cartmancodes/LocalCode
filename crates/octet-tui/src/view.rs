use crate::{
    editor::Editor,
    mascot::{self, State},
    text::{clean, strip, Sanitizer},
};
use octet_core::Event;
use ratatui::{
    prelude::*,
    widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap},
};
use std::{collections::VecDeque, path::PathBuf};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
// Warm charcoal and burgundy surfaces with readable, muted crimson accents.
// Amber remains reserved for warnings and permission decisions.
pub const BG: Color = Color::Rgb(22, 17, 19);
pub const PANEL: Color = Color::Rgb(31, 23, 26);
pub const FG: Color = Color::Rgb(234, 224, 218);
pub const MUTED: Color = Color::Rgb(174, 151, 154);
pub const EDGE: Color = Color::Rgb(88, 51, 61);
pub const ACCENT: Color = Color::Rgb(216, 124, 130);
pub const AMBER: Color = Color::Rgb(224, 180, 119);
const SELECTED: Color = Color::Rgb(64, 36, 44);
const MAX_BYTES: usize = 512 * 1024;
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
struct Entry {
    role: Role,
    text: String,
    width: u16,
    cache: Vec<Line<'static>>,
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
    activity: State,
    monochrome: bool,
    entries: VecDeque<Entry>,
    bytes: usize,
    sanitizer: Sanitizer,
    pub approvals: VecDeque<(u64, String)>,
    pub approval_scroll: u16,
    pub scroll: usize,
    catalog_focus: Option<usize>,
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
    reply: String,
    reply_sanitizer: Sanitizer,
    /// A tool ran since the last text; the next text starts a new paragraph.
    reply_break: bool,
    /// A new turn started; the last reply stays copyable until its first text.
    reply_stale: bool,
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
        text.push_str("\nShift+Tab cycles ask → accept-edits → auto. /mode full-access reconnects with every check off.");
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
            self.notice("No catalog reported yet. Explicit model IDs remain supported; /reconnect refreshes discovery.");
        } else {
            self.notice(
                format!("{} catalog · {} entries · page {page}/{pages}. PgUp/PgDn scroll; /model list <page>. Account access may vary.", self.engine, self.models.len() ),
            );
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
        self.notice("/model <ID or alias> · /model codex <ID> · /model claude <ID>\n/model default uses the provider default. Switching providers starts fresh context.");
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
    fn mascot_state(&self) -> State {
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
    fn transcript(&mut self, width: u16, height: usize) -> Vec<Line<'static>> {
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
fn card(title: &str) -> Block<'_> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(EDGE))
        .title(Span::styled(title, Style::default().fg(MUTED)))
}
fn mode_chip(app: &App) -> Span<'static> {
    let color = match app.mode {
        octet_core::Mode::Ask => MUTED,
        octet_core::Mode::AcceptEdits | octet_core::Mode::Auto => ACCENT,
        octet_core::Mode::FullAccess => AMBER,
    };
    let text = match app.mode_pending {
        Some(pending) => format!("{}…", pending.label()),
        None => app.mode.label().to_owned(),
    };
    Span::styled(text, Style::default().fg(color).bold())
}
pub fn draw(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    frame.render_widget(Block::default().style(Style::default().bg(BG).fg(FG)), area);
    if area.width < 38 || area.height < 12 {
        frame.render_widget(
            Paragraph::new(
                "Octet\n\nEnlarge the terminal to at least 38 × 12.\nPress Ctrl+C twice to quit.",
            )
            .wrap(Wrap { trim: false }),
            area,
        );
        return;
    }
    // Laid out once per frame: its row count sizes the composer, and its
    // lines and cursor are drawn there.
    let draft = app.editor.layout(draft_width(area.width));
    let regions = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(3),
        Constraint::Length(composer_height(draft.0.len())),
        Constraint::Length(1),
    ])
    .margin(1)
    .split(area);
    let status = format!(
        "● {}",
        if app.approvals.is_empty() {
            app.status.as_str()
        } else {
            "approval needed"
        }
    );
    // Status and usage keep the right edge, sized to their text so a narrow
    // terminal never cuts them; the banner takes the rest.
    let status_width = (status.width().max(app.usage.width()) as u16).clamp(12, 22);
    let header = Layout::horizontal([Constraint::Min(6), Constraint::Length(status_width)])
        .spacing(1)
        .split(regions[0]);
    banner(frame, header[0], app);
    let status_color = if !app.approvals.is_empty() {
        AMBER
    } else if app.stopped {
        MUTED
    } else {
        ACCENT
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(status, Style::default().fg(status_color))),
            Line::from(Span::styled(app.usage.clone(), Style::default().fg(MUTED))),
        ])
        .alignment(Alignment::Right),
        header[1],
    );
    let columns = Layout::horizontal(if area.width >= 112 {
        vec![Constraint::Min(40), Constraint::Length(29)]
    } else {
        vec![Constraint::Min(20)]
    })
    .spacing(2)
    .split(regions[1]);
    let transcript = columns[0];
    // Before the first message the conversation area stays blank, reserved
    // for what the conversation will fill.
    if !app.entries.is_empty() {
        let block = card(if app.scroll > 0 {
            " Conversation · scrollback "
        } else {
            " Conversation "
        });
        let inner = block.inner(transcript);
        frame.render_widget(block, transcript);
        let lines = app.transcript(inner.width.saturating_sub(2), inner.height as usize);
        frame.render_widget(
            Paragraph::new(lines),
            Rect {
                x: inner.x + 1,
                width: inner.width.saturating_sub(2),
                ..inner
            },
        );
    }
    if columns.len() > 1 {
        sidebar(frame, columns[1], app);
    }
    let base = if app.running {
        " Compose next prompt · wait or Esc to cancel "
    } else {
        " Prompt "
    };
    let title = match crate::composer::chip(&app.attachments) {
        Some(chip) => format!("{} · {chip} ", base.trim_end()),
        None => base.to_owned(),
    };
    let composer =
        card(&title).border_style(Style::default().fg(if app.running { EDGE } else { ACCENT }));
    let inner = composer.inner(regions[2]);
    frame.render_widget(composer, regions[2]);
    // The hint line takes the last row only when the draft keeps one; at
    // the minimum height the box has a single row and the draft gets it.
    let hint_rows = u16::from(inner.height >= 2);
    let input = Rect {
        x: inner.x + 1,
        width: inner.width.saturating_sub(2),
        height: inner.height - hint_rows,
        ..inner
    };
    let (lines, (col, row)) = draft;
    let offset = row.saturating_sub(input.height.saturating_sub(1) as usize);
    if app.editor.text.is_empty() {
        frame.render_widget(
            Paragraph::new("Ask about this workspace, describe a change, or type /help…")
                .style(Style::default().fg(MUTED)),
            input,
        );
    } else {
        frame.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .skip(offset)
                    .take(input.height as usize)
                    .map(Line::from)
                    .collect::<Vec<_>>(),
            ),
            input,
        );
    }
    let hints = Rect {
        x: input.x,
        y: inner.bottom().saturating_sub(1),
        width: input.width,
        height: hint_rows,
    };
    frame.render_widget(
        Paragraph::new(if area.width >= 80 {
            "Enter send   Alt+Enter newline   ↑ history   Ctrl+P commands   Esc cancel"
        } else {
            "Enter send · Ctrl+J newline · F1 help"
        })
        .style(Style::default().fg(MUTED)),
        hints,
    );
    completion_popup(frame, regions[2], app);
    frame.render_widget(
        Paragraph::new(if app.notice.is_empty() {
            if app.engine == octet_core::Engine::Demo {
                " Offline demo · no model calls   |   Ctrl+C twice to quit".into()
            } else {
                " Journal saved locally   |   F1 help   |   Ctrl+C twice to quit".into()
            }
        } else {
            app.notice.clone()
        })
        .style(Style::default().fg(MUTED)),
        regions[3],
    );
    if app.help {
        help(frame, area);
    } else if app.palette {
        palette(frame, area, app.selection);
    } else if let Some((id, detail)) = app.approvals.front() {
        approval(
            frame,
            area,
            *id,
            detail,
            app.approval_scroll,
            app.approvals.len(),
        );
    } else {
        frame.set_cursor_position((
            input.x + (col as u16).min(input.width.saturating_sub(1)),
            input.y + ((row - offset) as u16).min(input.height.saturating_sub(1)),
        ));
    }
}
/// The `@`, path or command suggestions, just above the prompt box.
fn completion_popup(frame: &mut Frame, composer: Rect, app: &App) {
    let Some(completion) = &app.completion else {
        return;
    };
    let mut lines: Vec<Line> = completion
        .items
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let chosen = index == completion.selected;
            Line::from(Span::styled(
                format!(" {} {item}", if chosen { "›" } else { " " }),
                Style::default()
                    .fg(if chosen { ACCENT } else { FG })
                    .bg(if chosen { SELECTED } else { PANEL }),
            ))
        })
        .collect();
    if lines.is_empty() {
        let message = match (&completion.kind, &app.files) {
            (crate::composer::Kind::File, crate::files::Files::Ready(_)) => " No matching files",
            (crate::composer::Kind::File, _) => " Indexing files…",
            _ => " No matches",
        };
        lines.push(Line::from(Span::styled(
            message,
            Style::default().fg(MUTED),
        )));
    }
    if let crate::files::Files::Ready(index) = &app.files {
        if index.capped && completion.kind == crate::composer::Kind::File {
            lines.push(Line::from(Span::styled(
                " Indexed the first 50,000 files",
                Style::default().fg(MUTED),
            )));
        }
    }
    let title = match completion.kind {
        crate::composer::Kind::File => " Files · Enter choose · Esc close ",
        crate::composer::Kind::Path => " Paths ",
        crate::composer::Kind::Command => " Commands ",
    };
    // On a short screen the popup is clipped; scroll so the selected row
    // stays in sight.
    let room = composer.y.saturating_sub(2) as usize;
    if completion.items.len() > room && room > 0 {
        let skip = completion.selected.saturating_sub(room - 1);
        lines = lines.into_iter().skip(skip).take(room).collect();
    }
    let height = lines.len() as u16 + 2;
    let area = Rect {
        x: composer.x,
        y: composer.y.saturating_sub(height),
        width: composer.width.min(64),
        height: height.min(composer.y),
    };
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(lines)
            .block(card(title))
            .style(Style::default().bg(PANEL)),
        area,
    );
}
/// The width the draft wraps at: margin, border and padding take three
/// columns on each side.
fn draft_width(terminal_width: u16) -> usize {
    terminal_width.saturating_sub(6) as usize
}
/// The composer's height from the draft's wrapped rows: borders, 1–4 draft
/// rows, and the hint row. An empty prompt takes 4 rows, a long draft 7.
/// The bottom-line notice while a cancelled turn winds down.
pub(crate) const CANCELLING: &str = "Cancelling…";
fn composer_height(draft_rows: usize) -> u16 {
    2 + draft_rows.clamp(1, 4) as u16 + 1
}
/// The top-left banner, after Claude Code's: the mini Octet (or a text mark
/// without colour) beside three lines. The permission mode leads its line so
/// a long model name can never push it off a narrow screen.
fn banner(frame: &mut Frame, area: Rect, app: &App) {
    let lines = vec![
        Line::from(vec![
            Span::styled("Octet", Style::default().fg(ACCENT).bold()),
            Span::styled(
                format!(" v{} · Rust preview", env!("CARGO_PKG_VERSION")),
                Style::default().fg(MUTED),
            ),
        ]),
        Line::from(vec![
            mode_chip(app),
            Span::styled(
                format!("  ·  {}  /  {}", app.engine, app.model),
                Style::default().fg(MUTED),
            ),
        ]),
        Line::from(vec![
            Span::styled(app.mascot_state().label(), Style::default().fg(ACCENT)),
            Span::styled(
                format!("  ·  {}", app.workspace),
                Style::default().fg(MUTED),
            ),
        ]),
    ];
    let (mark_width, mark) = if app.monochrome {
        (
            1,
            vec![Line::from(Span::styled("◇", Style::default().fg(ACCENT)))],
        )
    } else {
        (9, mascot::mini(app.mascot_state(), BG))
    };
    let [mark_area, _, text] = Layout::horizontal([
        Constraint::Length(mark_width),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas(area);
    frame.render_widget(Paragraph::new(mark), mark_area);
    frame.render_widget(Paragraph::new(lines), text);
}
fn sidebar(frame: &mut Frame, area: Rect, app: &App) {
    let block = card(" Workspace ");
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let id = if app.session.is_empty() {
        "Waiting for session…"
    } else {
        app.session.as_str()
    };
    let lines = vec![
        Line::default(),
        Line::from(Span::styled(" ENGINE", Style::default().fg(MUTED))),
        Line::from(format!(" {}", app.engine)),
        Line::default(),
        Line::from(Span::styled(" MODEL", Style::default().fg(MUTED))),
        Line::from(format!(" {}", app.model)),
        Line::from(" /session for full details"),
        Line::default(),
        Line::from(Span::styled(" SESSION", Style::default().fg(MUTED))),
        Line::from(format!(" {id}")),
        Line::default(),
        Line::from(Span::styled(" GOAL", Style::default().fg(MUTED))),
        Line::from(match &app.goals.goal {
            Some(goal) => format!(
                " {} · {}/{} turns",
                goal.status,
                goal.turns,
                octet_core::goal::MAX_GOAL_TURNS
            ),
            None => " No active goal".into(),
        }),
        Line::default(),
        Line::from(Span::styled(" QUICK COMMANDS", Style::default().fg(MUTED))),
        Line::from(" /model      Switch model"),
        Line::from(" /mode       Permissions"),
        Line::from(" /new        Fresh context"),
        Line::from(" /session    Session info"),
        Line::from(" /export     Save journal"),
        Line::from(" /reconnect  Resume vendor"),
        Line::default(),
        Line::from(Span::styled(" CONTROL", Style::default().fg(MUTED))),
        Line::from(" Esc         Cancel turn"),
        Line::from(" PgUp/PgDn   Read history"),
        Line::from(" Ctrl+C ×2   Save and quit"),
        Line::default(),
        Line::from(Span::styled(
            " Preview · core migration",
            Style::default().fg(MUTED),
        )),
        Line::from(Span::styled(
            " remains in progress.",
            Style::default().fg(MUTED),
        )),
    ];
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}
fn modal(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width.saturating_sub(4));
    let height = height.min(area.height.saturating_sub(2));
    Rect {
        x: area.x + (area.width - width) / 2,
        y: area.y + (area.height - height) / 2,
        width,
        height,
    }
}
fn help(frame: &mut Frame, area: Rect) {
    let area = modal(area, 76, 28);
    frame.render_widget(Clear, area);
    let text="Octet terminal preview\n\nEnter send · Alt+Enter / Ctrl+J newline\nArrows, Home, End edit; ↑ ↓ browse prompt history\nCtrl+U clear draft · PgUp/PgDn scroll conversation\nCtrl+End follow · Esc/Ctrl+C cancel turn\nCtrl+C twice quit · Ctrl+Z suspend (return with fg)\n\n/model [provider] <name> · /model default\n/mode [ask|accept-edits|auto|full-access] · Shift+Tab cycles\n/goal <objective> · /goal status|pause|resume\n/goal complete (audit) · /goal clear\n/new · /reconnect · /session · /export [path]\n/copy or Ctrl+X: copy the last reply to the clipboard\n!cmd run and attach output · !!cmd run only · Esc stops\n@ mention a file · Tab completes paths and /commands\nCtrl+G write the prompt in $EDITOR\n/remote-control: check phone access setup\n/approval-demo: offline permission dialog\n\nApproval: A allow once · D/Esc deny\nJournals retain older output beyond the viewport.\nEsc or F1 closes help";
    frame.render_widget(
        Paragraph::new(text)
            .block(card(" Help "))
            .style(Style::default().bg(PANEL).fg(FG))
            .wrap(Wrap { trim: false }),
        area,
    );
}
/// Keys the palette also offers, after the commands.
pub const PALETTE_KEYS: [(&str, &str); 3] = [
    ("Ctrl+G", "Write the prompt in $EDITOR"),
    ("@", "Mention a file"),
    ("!", "Run a shell command"),
];
pub const COMMANDS: [(&str, &str); 11] = [
    ("/help", "Keyboard shortcuts"),
    ("/model", "Switch model or provider"),
    (
        "/mode",
        "Permission mode: ask, accept-edits, auto, full-access",
    ),
    ("/goal", "Inspect or manage an autonomous goal"),
    ("/session", "Session ID and journal path"),
    ("/export", "Export journal to a new file"),
    ("/copy", "Copy the last reply (also Ctrl+X)"),
    ("/new", "Start a fresh conversation"),
    ("/reconnect", "Reconnect to the vendor session"),
    (
        "/remote-control",
        "Check phone access (tmux, Tailscale, mosh)",
    ),
    ("/quit", "Save and exit"),
];
/// The palette's rows: every command, then the keys it also offers.
pub fn palette_entries() -> impl Iterator<Item = &'static (&'static str, &'static str)> {
    COMMANDS.iter().chain(PALETTE_KEYS.iter())
}
fn palette(frame: &mut Frame, area: Rect, selected: usize) {
    let name_width = palette_entries()
        .map(|(name, _)| name.len())
        .max()
        .unwrap_or(0);
    let description_width = palette_entries()
        .map(|(_, description)| description.width())
        .max()
        .unwrap_or(0);
    // Width: borders, the marker and spaces around each column. Height:
    // borders, a blank line above and below the list, and the key line.
    let area = modal(
        area,
        (name_width + description_width + 7) as u16,
        palette_entries().count() as u16 + 5,
    );
    frame.render_widget(Clear, area);
    let mut lines = vec![Line::default()];
    for (index, (command, description)) in palette_entries().enumerate() {
        lines.push(Line::from(Span::styled(
            format!(
                " {} {:<name_width$} {}",
                if index == selected { "›" } else { " " },
                command,
                description
            ),
            Style::default()
                .fg(if index == selected { ACCENT } else { FG })
                .bg(if index == selected { SELECTED } else { PANEL }),
        )));
    }
    lines.push(Line::default());
    lines.push(Line::from(" ↑ ↓ choose · Enter run · Esc close"));
    frame.render_widget(
        Paragraph::new(lines)
            .block(card(" Commands "))
            .style(Style::default().bg(PANEL)),
        area,
    );
}
fn approval(frame: &mut Frame, area: Rect, id: u64, detail: &str, scroll: u16, count: usize) {
    let area = modal(area, 86, 24);
    frame.render_widget(Clear, area);
    let parts = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(2),
        Constraint::Length(3),
    ])
    .split(area);
    frame.render_widget(Paragraph::new(format!("Permission required  #{id}   ·   {count} pending\nReview the requested action before allowing it." )).style(Style::default().fg(AMBER).bg(PANEL)),parts[0]);
    frame.render_widget(
        Paragraph::new(detail)
            .wrap(Wrap { trim: false })
            .scroll((scroll, 0))
            .block(card(" Request details · PgUp/PgDn "))
            .style(Style::default().bg(PANEL).fg(FG)),
        parts[1],
    );
    frame.render_widget(
        Paragraph::new("\n A  Allow once     D / Esc  Deny     Ctrl+C  Cancel turn")
            .style(Style::default().fg(AMBER).bg(PANEL)),
        parts[2],
    );
}
#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};
    fn screen(width: u16, height: u16, app: &mut App) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| draw(frame, app)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
            .collect()
    }
    #[test]
    fn copy_keeps_the_last_reply_until_new_text_arrives() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.event(Event::Started);
        app.event(Event::Text("first".into()));
        app.event(Event::Finished {
            outcome: octet_core::Outcome::Completed,
        });
        app.event(Event::Started);
        assert_eq!(app.last_reply(), Some("first"), "the reply on screen");
        app.event(Event::Text("second".into()));
        assert_eq!(app.last_reply(), Some("second"));
    }
    #[test]
    fn following_the_latest_cancels_a_pending_catalog_scroll() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.models = (0..30)
            .map(|i| octet_core::ModelInfo {
                selection: format!("m{i}"),
                id: Some(format!("m{i}")),
                name: format!("Model {i}"),
                description: String::new(),
            })
            .collect();
        app.show_models(1);
        app.follow_latest();
        screen(120, 36, &mut app);
        assert_eq!(app.scroll, 0, "Ctrl+End before the next frame still wins");
    }
    #[test]
    fn copy_takes_the_whole_reply_with_its_tabs() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.event(Event::Started);
        app.event(Event::Text("one".into()));
        app.event(Event::Tool("Read\nsrc/main.rs".into()));
        app.event(Event::Text("two\tcolumns".into()));
        assert_eq!(app.last_reply(), Some("one\n\ntwo\tcolumns"));
        app.attach(crate::shell::Ran {
            command: "cat Makefile".into(),
            status: crate::shell::Status::Exited(0),
            output: "all:\n\tcargo build\n".into(),
        });
        assert_eq!(app.attachments[0].output, "all:\n\tcargo build\n");
    }
    #[test]
    fn a_new_connection_settles_the_old_sessions_command_and_popup() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.shell_running = true;
        app.completion = Some(crate::composer::Completion {
            kind: crate::composer::Kind::File,
            items: Vec::new(),
            selected: 0,
            start: 0,
        });
        app.connection(&config, "journal-2".into());
        assert!(!app.shell_running, "the old session's command is gone");
        assert!(app.completion.is_none());
        assert!(
            app.entries_text()
                .contains("The running command stopped when the session changed"),
            "{}",
            app.entries_text()
        );
    }
    #[test]
    fn the_popup_keeps_the_selected_row_in_sight_on_a_short_screen() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.completion = Some(crate::composer::Completion {
            kind: crate::composer::Kind::File,
            items: (0..8).map(|i| format!("file{i}.rs")).collect(),
            selected: 7,
            start: 0,
        });
        let rows = screen(80, 14, &mut app);
        assert!(
            rows.iter().any(|row| row.contains(" › file7.rs")),
            "{}",
            rows.join("\n")
        );
    }
    #[test]
    fn a_shell_that_cannot_start_is_an_error_entry() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.error("Cannot run /no/such/shell: No such file or directory");
        assert!(app.last_role() == Some(Role::Error));
        assert!(app.notice.starts_with("Cannot run"));
    }
    #[test]
    fn the_popup_lists_suggestions_above_the_prompt() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.completion = Some(crate::composer::Completion {
            kind: crate::composer::Kind::File,
            items: vec!["src/main.rs".into(), "src/model.rs".into()],
            selected: 1,
            start: 0,
        });
        let rows = screen(100, 30, &mut app);
        assert!(rows.iter().any(|row| row.contains("   src/main.rs")));
        assert!(rows.iter().any(|row| row.contains(" › src/model.rs")));
        app.completion.as_mut().unwrap().items.clear();
        let rows = screen(100, 30, &mut app);
        assert!(rows.iter().any(|row| row.contains("Indexing files…")));
    }
    #[test]
    fn shell_output_is_cleaned_and_attachments_stay_bounded() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        let ran = crate::shell::Ran {
            command: "ls -G".into(),
            status: crate::shell::Status::Exited(1),
            output: "\x1b[31mred\x1b[0m\r\nplain\n".into(),
        };
        app.shell_output(&ran);
        let text = app.entries_text();
        assert!(text.contains("$ ls -G\nred\nplain\nexit 1"), "{text:?}");
        assert!(!text.contains('\x1b'));
        app.attach(ran);
        assert!(!app.attachments[0].output.contains('\x1b'));
        let big = crate::shell::Ran {
            command: "big".into(),
            status: crate::shell::Status::Exited(0),
            output: "x".repeat(crate::shell::OUTPUT_LIMIT),
        };
        app.attach(big);
        assert_eq!(app.attachments.len(), 1, "the oldest attachment is dropped");
        assert_eq!(app.attachments[0].command, "big");
        assert_eq!(
            app.notice,
            "Dropped the oldest attachment to stay within 32 KiB"
        );
    }
    #[test]
    fn sidebar_lines_fit_the_panel_without_wrapping() {
        // Every sidebar line starts with a space; a wrapped remainder would
        // start in the first column, against the border.
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        let rows = screen(120, 48, &mut app);
        let title = rows.iter().find(|row| row.contains("╭ Workspace")).unwrap();
        let border = title.chars().position(|c| c == '╭').unwrap();
        let first = border + 1;
        let inside: Vec<&String> = rows
            .iter()
            .filter(|row| row.chars().nth(border) == Some('│'))
            .collect();
        assert!(inside.len() > 20, "sidebar not drawn");
        for row in inside {
            let start = row.chars().nth(first).unwrap();
            assert_eq!(start, ' ', "wrapped sidebar line: {row}");
        }
    }
    #[test]
    fn composer_grows_with_the_draft() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        assert_eq!(
            composer_height(app.editor.layout(draft_width(80)).0.len()),
            4
        );
        assert!(app.editor.insert("one\ntwo\nthree"));
        assert_eq!(
            composer_height(app.editor.layout(draft_width(80)).0.len()),
            6
        );
    }
    #[test]
    fn composer_counts_wrapped_rows() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        // 80 columns leave 74 for text: 100 characters wrap onto a second row.
        assert!(app.editor.insert(&"x".repeat(100)));
        assert_eq!(
            composer_height(app.editor.layout(draft_width(80)).0.len()),
            5
        );
    }
    #[test]
    fn composer_height_is_capped() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        assert!(app.editor.insert(&"line\n".repeat(40)));
        assert_eq!(
            composer_height(app.editor.layout(draft_width(80)).0.len()),
            7
        );
    }
    #[test]
    fn an_empty_composer_gives_rows_back_to_the_conversation() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.event(Event::User("hello".into()));
        let rows = screen(44, 16, &mut app);
        // The composer's top border sits 4 rows above the status line.
        assert!(rows[16 - 6].starts_with(" ╭ Prompt"), "{}", rows[16 - 6]);
    }
    #[test]
    fn a_finished_turn_clears_the_cancelling_notice() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.event(Event::Started);
        app.notice = CANCELLING.into();
        app.event(Event::Finished {
            outcome: octet_core::Outcome::Interrupted,
        });
        assert_eq!(app.notice, "");
    }
    #[test]
    fn the_draft_shows_at_the_minimum_size() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        assert!(app.editor.insert("VISIBLE"));
        for width in [38, 80] {
            let rows = screen(width, 12, &mut app);
            assert!(
                rows.iter().any(|row| row.contains("VISIBLE")),
                "{width}x12:\n{}",
                rows.join("\n")
            );
        }
    }
    #[test]
    fn the_palette_shows_every_command_and_its_keys() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.palette = true;
        let rows = screen(80, 24, &mut app);
        let mut columns = Vec::new();
        for (name, description) in COMMANDS.iter().chain(PALETTE_KEYS.iter()) {
            // " /mode " must not match the "/model" row.
            let row = rows
                .iter()
                .find(|row| row.contains(&format!(" {name} ")))
                .unwrap_or_else(|| panic!("{name} missing"));
            assert!(row.contains(description), "{name}: description cut: {row}");
            // Columns, not bytes: the selected row's "›" is three bytes wide.
            columns.push(
                row.find(description)
                    .map(|byte| row[..byte].chars().count()),
            );
        }
        assert!(
            columns.windows(2).all(|pair| pair[0] == pair[1]),
            "descriptions start in different columns: {columns:?}"
        );
        assert!(rows.iter().any(|row| row.contains("Esc close")));
    }
    #[test]
    fn empty_conversation_leaves_the_centre_blank() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        for (width, height) in [(80, 24), (60, 20)] {
            let mut app = App::new(&config, "journal".into());
            app.monochrome = false;
            app.event(Event::Ready {
                session: "demo".into(),
            });
            let rows = screen(width, height, &mut app);
            // Rows between the 3-row banner (after the margin) and the empty
            // 4-row composer, the status line and the bottom margin.
            for row in &rows[4..rows.len() - 6] {
                let inside: String = row.chars().skip(1).take(width as usize - 2).collect();
                assert!(inside.trim().is_empty(), "{width}×{height}: {row:?}");
            }
        }
    }
    #[test]
    fn header_banner_shows_mini_version_mode_activity_and_workspace() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        for (width, height) in [(160, 42), (80, 24), (60, 20), (40, 12)] {
            let mut app = App::new(&config, "journal".into());
            app.monochrome = false;
            for event in [
                Event::Ready {
                    session: "demo".into(),
                },
                Event::User("fix the failing test".into()),
                Event::Started,
                Event::Tool("Bash\n{}".into()),
            ] {
                app.event(event);
            }
            let rows = screen(width, height, &mut app);
            // The header starts inside the one-cell margin.
            for row in &rows[1..4] {
                let mini: String = row.chars().skip(1).take(9).collect();
                assert_eq!(mini, "▀".repeat(9), "{width}×{height}: {row}");
            }
            let at = |row: usize| format!("{width}×{height}: {}", rows[row]);
            let version = format!("Octet v{}", env!("CARGO_PKG_VERSION"));
            assert!(rows[1].contains(&version), "{}", at(1));
            assert!(rows[1].contains("working"), "{}", at(1));
            assert!(rows[2].contains("ask"), "{}", at(2));
            assert!(
                rows[3].contains("CODING") && rows[3].contains("/tmp"),
                "{}",
                at(3)
            );
        }
    }
    #[test]
    fn permission_mode_stays_visible_on_narrow_headers() {
        let config = octet_core::Config {
            model: Some("gpt-5.5-codex-max-preview-long-name".into()),
            mode: octet_core::Mode::FullAccess,
            ..octet_core::Config::new(octet_core::Engine::Codex, "codex", "/tmp")
        };
        for width in [80, 60, 40, 38] {
            let mut app = App::new(&config, "journal".into());
            app.monochrome = false;
            let rows = screen(width, 24, &mut app);
            assert!(
                rows[2].contains("full-access"),
                "{width} columns: {}",
                rows[2]
            );
        }
    }
    #[test]
    fn status_shows_in_full_on_narrow_terminals() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        for width in [59, 50, 40] {
            let mut app = App::new(&config, "journal".into());
            app.monochrome = false;
            for event in [
                Event::Ready {
                    session: "demo".into(),
                },
                Event::Started,
                Event::Finished {
                    outcome: octet_core::Outcome::Interrupted,
                },
            ] {
                app.event(event);
            }
            let rows = screen(width, 12, &mut app);
            assert!(rows[1].contains("● interrupted"), "{width}: {}", rows[1]);
        }
    }
    #[test]
    fn no_color_keeps_the_text_header() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.monochrome = true;
        let rows = screen(80, 24, &mut app);
        let mark = format!("◇ Octet v{}", env!("CARGO_PKG_VERSION"));
        assert!(rows[1].contains(&mark), "{}", rows[1]);
        assert!(!rows[1..4].iter().any(|row| row.contains('▀')));
    }
    #[test]
    fn mascot_tracks_activity_and_keeps_errors_visible_after_disconnect() {
        let config = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.monochrome = false;
        for (event, expected) in [
            (
                Event::Ready {
                    session: "demo".into(),
                },
                "IDLE",
            ),
            (Event::Started, "THINKING"),
            (
                Event::Tool("Read\n{\"file_path\":\"src/main.rs\"}".into()),
                "SEARCHING",
            ),
            (
                Event::Tool("Bash\n{\"command\":\"cargo check\"}".into()),
                "CODING",
            ),
            (Event::Tool("dispatch_subagent\n{}".into()), "DELEGATING"),
            (
                Event::Approval {
                    id: 1,
                    detail: "Write src/main.rs".into(),
                },
                "APPROVAL",
            ),
            (Event::ApprovalClosed(1), "DELEGATING"),
            (
                Event::Finished {
                    outcome: octet_core::Outcome::Completed,
                },
                "SUCCESS",
            ),
            (
                Event::Finished {
                    outcome: octet_core::Outcome::Interrupted,
                },
                "SLEEPING",
            ),
            (Event::Error("provider disconnected".into()), "ERROR"),
            (Event::Stopped, "ERROR"),
        ] {
            app.event(event);
            // The banner's third row starts with the activity name; the
            // transcript's own ERROR label must not satisfy this check.
            let rows = screen(160, 42, &mut app);
            let activity = rows[3].chars().skip(11).collect::<String>();
            assert!(
                activity.trim_start().starts_with(expected),
                "mascot should show {expected}: {}",
                rows[3]
            );
        }
    }
    #[test]
    fn renders_narrow_wide_and_approval_without_panics() {
        for (w, h) in [(30, 8), (40, 12), (80, 24), (120, 36)] {
            let c = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp/project");
            let mut a = App::new(&c, "journal".into());
            let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
            t.draw(|f| draw(f, &mut a)).unwrap();
            a.event(Event::Approval {
                id: 7,
                detail: "edit hello.txt".into(),
            });
            t.draw(|f| draw(f, &mut a)).unwrap();
        }
    }
    #[test]
    fn transcript_memory_is_bounded() {
        let c = octet_core::Config::new(octet_core::Engine::Demo, "demo", "/tmp");
        let mut a = App::new(&c, "journal".into());
        for _ in 0..100 {
            a.event(Event::User("x".repeat(64 * 1024)));
            a.event(Event::Text("界".repeat(64 * 1024)));
        }
        assert!(a.bytes <= MAX_BYTES);
        assert!(a.entries.len() <= 160);
        assert!(!a.transcript(60, 20).is_empty());
    }
}

#[cfg(test)]
mod model_detail_tests {
    use super::*;
    fn app() -> App {
        App::new(
            &octet_core::Config {
                model: Some("sonnet".into()),
                ..octet_core::Config::new(octet_core::Engine::Claude, "claude", "/tmp")
            },
            "/tmp/journal".into(),
        )
    }
    #[test]
    fn full_model_details_survive_long_ids_and_reconnection_clears_metadata() {
        let mut a = app();
        let id = format!("provider-{}-version", "long".repeat(40));
        a.event(Event::Models(vec![octet_core::ModelInfo {
            selection: "sonnet".into(),
            id: Some(id.clone()),
            name: "Complete Provider Model Name".into(),
            description: "Provider details".into(),
        }]));
        assert!(a.model_details().contains("not yet reported"));
        a.event(Event::ModelSelected(id.clone()));
        a.show_models(1);
        let details = a
            .entries
            .iter()
            .map(|e| e.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(details.contains(&id));
        assert!(details.contains("Complete Provider Model Name"));
        assert!(details.contains("Requested selection: sonnet"));
        a.connection(
            &octet_core::Config::new(octet_core::Engine::Codex, "codex", "/tmp"),
            "/tmp/new".into(),
        );
        assert!(a.models.is_empty());
        assert!(a.resolved_model.is_none());
        assert!(!a.model_details().contains(&id));
    }
    #[test]
    fn large_catalog_is_paged_without_evicting_current_details() {
        let mut a = app();
        a.event(Event::Models(
            (0..256)
                .map(|n| octet_core::ModelInfo {
                    selection: format!("model-{n}"),
                    id: Some(format!("full-id-{n}")),
                    name: format!("Name {n}"),
                    description: String::new(),
                })
                .collect(),
        ));
        a.show_models(13);
        let visible = a
            .transcript(72, 12)
            .iter()
            .map(|line| line.to_string())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(visible.contains("claude catalog"));
        assert!(visible.contains("256 entries"));
        assert!(visible.contains("Name 240"));
        assert!(a.scroll > 0);
        assert!(a.entries.iter().any(|e| e.text.contains("full-id-255")));
        assert!(a
            .entries
            .iter()
            .any(|e| e.text.contains("Requested selection")));
        assert!(!a.entries.iter().any(|e| e.text.contains("full-id-0\n")));
        let count = a.entries.len();
        a.show_models(usize::MAX);
        assert_eq!(a.entries.len(), count + 1);
    }
    #[test]
    fn stopped_session_clears_pending_mode() {
        let c = octet_core::Config::new(octet_core::Engine::Claude, "claude", "/tmp");
        let mut a = App::new(&c, "journal".into());
        a.mode_pending = Some(octet_core::Mode::Auto);
        a.event(Event::ModeChanged(octet_core::Mode::Auto));
        assert_eq!((a.mode, a.mode_pending), (octet_core::Mode::Auto, None));
        a.mode_pending = Some(octet_core::Mode::Ask);
        a.event(Event::Stopped);
        assert_eq!((a.mode, a.mode_pending), (octet_core::Mode::Auto, None));
    }
    #[test]
    fn header_shows_confirmed_and_pending_mode() {
        let c = octet_core::Config {
            mode: octet_core::Mode::Auto,
            ..octet_core::Config::new(octet_core::Engine::Codex, "codex", "/tmp/project")
        };
        let mut a = App::new(&c, "journal".into());
        let mut t = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        let screen = |t: &ratatui::Terminal<ratatui::backend::TestBackend>| {
            t.backend()
                .buffer()
                .content()
                .iter()
                .map(|c| c.symbol())
                .collect::<String>()
        };
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("auto"));
        assert_eq!(mode_chip(&a).style.fg, Some(ACCENT));
        a.mode_pending = Some(octet_core::Mode::Ask);
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("ask…"));
        a.event(Event::ModeChanged(octet_core::Mode::FullAccess));
        assert_eq!(mode_chip(&a).style.fg, Some(AMBER));
        assert!(COMMANDS.iter().any(|(name, _)| *name == "/mode"));
    }
}
