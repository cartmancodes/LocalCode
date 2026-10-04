use crate::{
    editor::Editor,
    text::{clean, Sanitizer},
};
use lc_core::Event;
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
}
struct Entry {
    role: Role,
    text: String,
    width: u16,
    cache: Vec<Line<'static>>,
}
pub struct App {
    pub engine: String,
    pub mode: lc_core::Mode,
    pub mode_pending: Option<lc_core::Mode>,
    pub workspace: String,
    pub model: String,
    pub requested_model: Option<String>,
    pub resolved_model: Option<String>,
    pub models: Vec<lc_core::ModelInfo>,
    pub session: String,
    pub journal: PathBuf,
    pub editor: Editor,
    pub ready: bool,
    pub running: bool,
    pub stopped: bool,
    pub status: String,
    pub usage: String,
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
    pub goal: Option<lc_core::goal::Goal>,
    pub goal_store: Option<lc_core::goal::GoalStore>,
    pub goal_running: bool,
    pub goal_output: String,
}
impl App {
    pub fn new(config: &lc_core::Config, journal: PathBuf) -> Self {
        Self {
            engine: config.engine.clone(),
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
            goal: None,
            goal_store: None,
            goal_running: false,
            goal_output: String::new(),
        }
    }
    pub fn connection(&mut self, config: &lc_core::Config, journal: PathBuf) {
        self.engine = config.engine.clone();
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
        self.usage.clear();
        self.approvals.clear();
        self.approval_scroll = 0;
        self.goal_running = false;
        self.goal_output.clear();
    }
    pub async fn save_goal(&self) -> Result<(), String> {
        match &self.goal_store {
            Some(store) => store.save(self.goal.as_ref()).await,
            None => Ok(()),
        }
    }
    fn refresh_model_label(&mut self) {
        self.model = match &self.resolved_model {
            Some(id) => self
                .models
                .iter()
                .find(|m| m.id.as_ref() == Some(id))
                .map(|m| format!("{} · {}", clean(&m.name), id))
                .unwrap_or_else(|| id.clone()),
            None if self.engine == "demo" => "offline · no model".into(),
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
    fn catalog_selection(&self) -> Option<&lc_core::ModelInfo> {
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
        for mode in lc_core::Mode::ALL {
            let marker = if mode == self.mode { "●" } else { " " };
            text.push_str(&format!(
                "{marker} {:<13} {}\n",
                mode.label(),
                mode.describe(&self.engine)
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
                .unwrap_or(if self.engine == "demo" {
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
            let mut start = text.len() - BLOCK_BYTES;
            while !text.is_char_boundary(start) {
                start += 1;
            }
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
                }
            }
            Event::User(text) => {
                self.add(Role::User, text);
                self.scroll = 0;
            }
            Event::Started => {
                self.running = true;
                self.status = "working".into();
                self.notice.clear();
                self.sanitizer = Sanitizer::default();
            }
            Event::Text(text) => {
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
                    let mut remove = e.text.len() - BLOCK_BYTES;
                    while !e.text.is_char_boundary(remove) {
                        remove += 1;
                    }
                    e.text.drain(..remove);
                    self.bytes -= remove;
                }
                self.trim();
            }
            Event::Tool(text) => self.add(Role::Tool, text),
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
                self.status = clean(&outcome);
            }
            Event::Notice(text) => self.notice(text),
            Event::Error(text) => {
                self.add(Role::Error, text);
                self.status = "error".into();
            }
            Event::Stopped => {
                self.mode_pending = None;
                self.stopped = true;
                self.running = false;
                self.ready = false;
                self.approvals.clear();
                self.status = "disconnected".into();
            }
        }
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
        Role::Assistant => ("LOCALCODE", FG),
        Role::Tool => ("TOOL", MUTED),
        Role::Notice => ("NOTE", MUTED),
        Role::Error => ("ERROR", AMBER),
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
        lc_core::Mode::Ask => MUTED,
        lc_core::Mode::AcceptEdits | lc_core::Mode::Auto => ACCENT,
        lc_core::Mode::FullAccess => AMBER,
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
                "LocalCode\n\nEnlarge the terminal to at least 38 × 12.\nCtrl+Q quits safely.",
            )
            .wrap(Wrap { trim: false }),
            area,
        );
        return;
    }
    let regions = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(3),
        Constraint::Length(7),
        Constraint::Length(1),
    ])
    .margin(1)
    .split(area);
    let header = Layout::horizontal([
        Constraint::Length(16),
        Constraint::Min(6),
        Constraint::Length(22),
    ])
    .split(regions[0]);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(
                " ◇ LOCALCODE",
                Style::default().fg(ACCENT).bold(),
            )),
            Line::from(Span::styled("   RUST PREVIEW", Style::default().fg(MUTED))),
        ]),
        header[0],
    );
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(app.workspace.clone()),
            Line::from(vec![
                Span::styled(
                    format!("{}  /  {}  ·  ", app.engine, app.model),
                    Style::default().fg(MUTED),
                ),
                mode_chip(app),
            ]),
        ]),
        header[1],
    );
    let status_color = if !app.approvals.is_empty() {
        AMBER
    } else if app.stopped {
        MUTED
    } else {
        ACCENT
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(
                format!(
                    "● {}",
                    if app.approvals.is_empty() {
                        app.status.as_str()
                    } else {
                        "approval needed"
                    }
                ),
                Style::default().fg(status_color),
            )),
            Line::from(Span::styled(app.usage.clone(), Style::default().fg(MUTED))),
        ])
        .alignment(Alignment::Right),
        header[2],
    );
    let columns = Layout::horizontal(if area.width >= 112 {
        vec![Constraint::Min(40), Constraint::Length(29)]
    } else {
        vec![Constraint::Min(20)]
    })
    .spacing(2)
    .split(regions[1]);
    let transcript = columns[0];
    if app.entries.is_empty() {
        welcome(frame, transcript, &app.engine);
    } else {
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
    let composer = card(if app.running {
        " Compose next prompt · wait or Esc to cancel "
    } else {
        " Prompt "
    })
    .border_style(Style::default().fg(if app.running { EDGE } else { ACCENT }));
    let inner = composer.inner(regions[2]);
    frame.render_widget(composer, regions[2]);
    let input = Rect {
        x: inner.x + 1,
        width: inner.width.saturating_sub(2),
        height: inner.height.saturating_sub(1),
        ..inner
    };
    let (lines, (col, row)) = app.editor.layout(input.width as usize);
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
        height: 1,
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
    frame.render_widget(
        Paragraph::new(if app.notice.is_empty() {
            if app.engine == "demo" {
                " Offline demo · no model calls   |   Ctrl+Q quit".into()
            } else {
                " Journal saved locally   |   F1 help   |   Ctrl+Q quit".into()
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
fn welcome(frame: &mut Frame, area: Rect, engine: &str) {
    let top = area.y + area.height.saturating_sub(14) / 2;
    let region = Rect {
        x: area.x + 3,
        y: top,
        width: area.width.saturating_sub(6),
        height: area.height.min(16),
    };
    let lines = vec![
        Line::from(Span::styled("  ╭──╮", Style::default().fg(ACCENT))),
        Line::from(Span::styled(
            "  │ ◇│  Your workspace. One conversation.",
            Style::default().fg(FG).bold(),
        )),
        Line::from(Span::styled("  ╰──╯", Style::default().fg(ACCENT))),
        Line::default(),
        Line::from("  Read code, work through a change, and review the result."),
        Line::from(Span::styled(
            format!(
                "  {} · native terminal · no browser",
                if engine == "demo" {
                    "Offline demo"
                } else {
                    engine
                }
            ),
            Style::default().fg(MUTED),
        )),
        Line::default(),
        Line::from(Span::styled(
            "  START WITH A QUESTION",
            Style::default().fg(MUTED),
        )),
        Line::from("  Explain the structure of this project"),
        Line::from("  Find a bug and suggest a focused fix"),
        Line::default(),
        Line::from(Span::styled(
            "  /help  shortcuts      Ctrl+P  commands",
            Style::default().fg(ACCENT),
        )),
    ];
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), region);
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
        Line::from(match &app.goal {
            Some(goal) => format!(
                " {:?} · {}/{} turns",
                goal.status,
                goal.turns,
                lc_core::goal::MAX_GOAL_TURNS
            ),
            None => " No active goal".into(),
        }),
        Line::default(),
        Line::from(Span::styled(" QUICK COMMANDS", Style::default().fg(MUTED))),
        Line::from(" /model      Model / provider"),
        Line::from(" /mode       Permission mode"),
        Line::from(" /new        Fresh context"),
        Line::from(" /session    Session details"),
        Line::from(" /export     Save journal"),
        Line::from(" /reconnect  Resume vendor"),
        Line::default(),
        Line::from(Span::styled(" CONTROL", Style::default().fg(MUTED))),
        Line::from(" Esc         Cancel turn"),
        Line::from(" PgUp/PgDn   Read history"),
        Line::from(" Ctrl+Q      Save and quit"),
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
    let area = modal(area, 76, 24);
    frame.render_widget(Clear, area);
    let text="LocalCode terminal preview\n\nEnter send · Alt+Enter / Ctrl+J newline\nArrows, Home, End edit; ↑ ↓ browse prompt history\nCtrl+U clear draft · PgUp/PgDn scroll conversation\nCtrl+End follow · Esc/Ctrl+C cancel turn\nCtrl+Q quit · Ctrl+Z suspend (return with fg)\n\n/model [provider] <name> · /model default\n/mode [ask|accept-edits|auto|full-access] · Shift+Tab cycles\n/goal <objective> · /goal status|pause|resume\n/goal complete (audit) · /goal clear\n/new · /reconnect · /session · /export [path]\n/approval-demo: offline permission dialog\n\nApproval: A allow once · D/Esc deny\nJournals retain older output beyond the viewport.\nEsc or F1 closes help";
    frame.render_widget(
        Paragraph::new(text)
            .block(card(" Help "))
            .style(Style::default().bg(PANEL).fg(FG))
            .wrap(Wrap { trim: false }),
        area,
    );
}
pub const COMMANDS: [(&str, &str); 9] = [
    ("/help", "Keyboard shortcuts"),
    ("/model", "Switch model or provider"),
    (
        "/mode",
        "Permission mode: ask, accept-edits, auto, full-access",
    ),
    ("/goal", "Inspect or manage an autonomous goal"),
    ("/session", "Session ID and journal path"),
    ("/export", "Export journal to a new file"),
    ("/new", "Start a fresh conversation"),
    ("/reconnect", "Reconnect to the vendor session"),
    ("/quit", "Save and exit"),
];
fn palette(frame: &mut Frame, area: Rect, selected: usize) {
    let area = modal(area, 66, 14);
    frame.render_widget(Clear, area);
    let mut lines = vec![Line::default()];
    for (index, (command, description)) in COMMANDS.iter().enumerate() {
        lines.push(Line::from(Span::styled(
            format!(
                " {} {:<14} {}",
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
        Paragraph::new("\n A  Allow once     D / Esc  Deny     Ctrl+Q  Quit")
            .style(Style::default().fg(AMBER).bg(PANEL)),
        parts[2],
    );
}
#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};
    #[test]
    fn renders_narrow_wide_and_approval_without_panics() {
        for (w, h) in [(30, 8), (40, 12), (80, 24), (120, 36)] {
            let c = lc_core::Config {
                engine: "demo".into(),
                binary: "demo".into(),
                cwd: "/tmp/project".into(),
                model: None,
                resume: None,
                mode: lc_core::Mode::Ask,
            };
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
        let c = lc_core::Config {
            engine: "demo".into(),
            binary: "demo".into(),
            cwd: "/tmp".into(),
            model: None,
            resume: None,
            mode: lc_core::Mode::Ask,
        };
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
            &lc_core::Config {
                engine: "claude".into(),
                binary: "claude".into(),
                cwd: "/tmp".into(),
                model: Some("sonnet".into()),
                resume: None,
                mode: lc_core::Mode::Ask,
            },
            "/tmp/journal".into(),
        )
    }
    #[test]
    fn full_model_details_survive_long_ids_and_reconnection_clears_metadata() {
        let mut a = app();
        let id = format!("provider-{}-version", "long".repeat(40));
        a.event(Event::Models(vec![lc_core::ModelInfo {
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
            &lc_core::Config {
                engine: "codex".into(),
                binary: "codex".into(),
                cwd: "/tmp".into(),
                model: None,
                resume: None,
                mode: lc_core::Mode::Ask,
            },
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
                .map(|n| lc_core::ModelInfo {
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
        let c = lc_core::Config {
            engine: "claude".into(),
            binary: "claude".into(),
            cwd: "/tmp".into(),
            model: None,
            resume: None,
            mode: lc_core::Mode::Ask,
        };
        let mut a = App::new(&c, "journal".into());
        a.mode_pending = Some(lc_core::Mode::Auto);
        a.event(Event::ModeChanged(lc_core::Mode::Auto));
        assert_eq!((a.mode, a.mode_pending), (lc_core::Mode::Auto, None));
        a.mode_pending = Some(lc_core::Mode::Ask);
        a.event(Event::Stopped);
        assert_eq!((a.mode, a.mode_pending), (lc_core::Mode::Auto, None));
    }
    #[test]
    fn header_shows_confirmed_and_pending_mode() {
        let c = lc_core::Config {
            engine: "codex".into(),
            binary: "codex".into(),
            cwd: "/tmp/project".into(),
            model: None,
            resume: None,
            mode: lc_core::Mode::Auto,
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
        a.mode_pending = Some(lc_core::Mode::Ask);
        t.draw(|f| draw(f, &mut a)).unwrap();
        assert!(screen(&t).contains("ask…"));
        a.event(Event::ModeChanged(lc_core::Mode::FullAccess));
        assert_eq!(mode_chip(&a).style.fg, Some(AMBER));
        assert!(COMMANDS.iter().any(|(name, _)| *name == "/mode"));
    }
}
