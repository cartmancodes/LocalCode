//! Drawing: the header, conversation, prompt box, sidebar and dialogs. The
//! state they draw is in `app`.
use crate::{
    app::App,
    mascot,
    registry::{COMMANDS, Cmd},
};
use ratatui::{
    prelude::*,
    widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap},
};
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
/// A count of rows or columns as a terminal coordinate, saturating instead
/// of wrapping.
fn cells(count: usize) -> u16 {
    u16::try_from(count).unwrap_or(u16::MAX)
}
fn card(title: &str) -> Block<'_> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(EDGE))
        .title(Span::styled(title, Style::default().fg(MUTED)))
}
fn mode_chip(app: &App) -> Span<'static> {
    let color = match app.conn.mode {
        octet_core::Mode::Ask => MUTED,
        octet_core::Mode::AcceptEdits | octet_core::Mode::Auto => ACCENT,
        octet_core::Mode::FullAccess => AMBER,
    };
    let text = match app.conn.mode_pending {
        Some(pending) => format!("{}…", pending.label()),
        None => app.conn.mode.label().to_owned(),
    };
    Span::styled(text, Style::default().fg(color).bold())
}
pub fn draw(frame: &mut Frame, app: &mut App) {
    paint(frame, app);
    if app.monochrome {
        without_colour(frame.buffer_mut());
    }
}
/// NO_COLOR: every cell takes the terminal's own colours. A selection, shown
/// only by its background, becomes reverse video instead.
fn without_colour(buffer: &mut Buffer) {
    for cell in &mut buffer.content {
        if cell.bg == SELECTED {
            cell.modifier.insert(Modifier::REVERSED);
        }
        cell.fg = Color::Reset;
        cell.bg = Color::Reset;
    }
}
fn paint(frame: &mut Frame, app: &mut App) {
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
    let draft = app.composer.editor.layout(draft_width(area.width));
    let regions = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(3),
        Constraint::Length(composer_height(draft.0.len())),
        Constraint::Length(1),
    ])
    .margin(1)
    .split(area);
    header(frame, regions[0], app);
    conversation(frame, regions[1], area.width, app);
    let cursor = composer(frame, regions[2], area.width, app, draft);
    status_line(frame, regions[3], app);
    overlays(frame, area, app, cursor);
}
/// Banner on the left, status and usage on the right.
fn header(frame: &mut Frame, area: Rect, app: &App) {
    let status = format!(
        "● {}",
        if app.overlay.approvals.is_empty() {
            app.conn.status.as_str()
        } else {
            "approval needed"
        }
    );
    // Status and usage keep the right edge, sized to their text so a narrow
    // terminal never cuts them; the banner takes the rest.
    let status_width = cells(status.width().max(app.conn.usage.width())).clamp(12, 22);
    let header = Layout::horizontal([Constraint::Min(6), Constraint::Length(status_width)])
        .spacing(1)
        .split(area);
    banner(frame, header[0], app);
    let status_color = if !app.overlay.approvals.is_empty() {
        AMBER
    } else if app.conn.is_stopped() {
        MUTED
    } else {
        ACCENT
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(status, Style::default().fg(status_color))),
            Line::from(Span::styled(
                app.conn.usage.clone(),
                Style::default().fg(MUTED),
            )),
        ])
        .alignment(Alignment::Right),
        header[1],
    );
}
/// The transcript, and the sidebar on wide terminals.
fn conversation(frame: &mut Frame, area: Rect, width: u16, app: &mut App) {
    let columns = Layout::horizontal(if width >= 112 {
        vec![Constraint::Min(40), Constraint::Length(29)]
    } else {
        vec![Constraint::Min(20)]
    })
    .spacing(2)
    .split(area);
    let transcript = columns[0];
    // Before the first message the conversation area stays blank, reserved
    // for what the conversation will fill.
    if !app.chat.entries.is_empty() {
        let block = card(if app.chat.scroll > 0 {
            " Conversation · scrollback "
        } else {
            " Conversation "
        });
        let inner = block.inner(transcript);
        frame.render_widget(block, transcript);
        let lines = app.visible_lines(inner.width.saturating_sub(2), inner.height as usize);
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
}
/// The prompt box and its popup; returns where the cursor goes.
fn composer(
    frame: &mut Frame,
    area: Rect,
    width: u16,
    app: &App,
    draft: (Vec<String>, (usize, usize)),
) -> (u16, u16) {
    let title = composer_title(app);
    let composer = card(&title).border_style(Style::default().fg(if app.conn.is_running() {
        EDGE
    } else {
        ACCENT
    }));
    let inner = composer.inner(area);
    frame.render_widget(composer, area);
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
    if app.composer.editor.text().is_empty() {
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
        Paragraph::new(if width >= 80 {
            "Enter send   Alt+Enter newline   ↑ history   Ctrl+P commands   Esc cancel"
        } else {
            "Enter send · Ctrl+J newline · F1 help"
        })
        .style(Style::default().fg(MUTED)),
        hints,
    );
    completion_popup(frame, area, app);
    (
        input.x + cells(col).min(input.width.saturating_sub(1)),
        input.y + cells(row - offset).min(input.height.saturating_sub(1)),
    )
}
/// The bottom line: the latest notice, or the standing hints.
fn status_line(frame: &mut Frame, area: Rect, app: &App) {
    frame.render_widget(
        Paragraph::new(if app.status_line.is_empty() {
            if app.conn.engine.offline() {
                " Offline demo · no model calls   |   Ctrl+C twice to quit".into()
            } else {
                " Journal saved locally   |   F1 help   |   Ctrl+C twice to quit".into()
            }
        } else {
            app.status_line.clone()
        })
        .style(Style::default().fg(MUTED)),
        area,
    );
}
/// Help, the palette or an approval over everything; else the cursor.
fn overlays(frame: &mut Frame, area: Rect, app: &mut App, cursor: (u16, u16)) {
    if app.overlay.help {
        help(frame, area, &mut app.overlay.help_scroll);
    } else if app.overlay.palette {
        palette(frame, area, app.overlay.selection);
    } else if let Some((id, detail)) = app.overlay.approvals.front() {
        let count = app.overlay.approvals.len();
        approval(
            frame,
            area,
            *id,
            detail,
            &mut app.overlay.approval_scroll,
            count,
        );
    } else {
        frame.set_cursor_position(cursor);
    }
}
/// The `@`, path or command suggestions, just above the prompt box.
fn completion_popup(frame: &mut Frame, composer: Rect, app: &App) {
    let Some(completion) = &app.composer.completion else {
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
        let message = match (&completion.kind, &app.composer.files) {
            (crate::composer::Kind::File, crate::files::Files::Ready(_)) => " No matching files",
            (crate::composer::Kind::File, _) => " Indexing files…",
            _ => " No matches",
        };
        lines.push(Line::from(Span::styled(
            message,
            Style::default().fg(MUTED),
        )));
    }
    if let crate::files::Files::Ready(index) = &app.composer.files
        && index.capped
        && completion.kind == crate::composer::Kind::File
    {
        lines.push(Line::from(Span::styled(
            format!(" Indexed the first {} files", crate::files::LIMIT),
            Style::default().fg(MUTED),
        )));
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
    let height = cells(lines.len()).saturating_add(2);
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
/// The prompt box's title: what Enter will do, then what waits to go.
pub(crate) fn composer_title(app: &App) -> String {
    let mut parts = vec![if app.conn.is_running() {
        "Next prompt · Enter queues it · Esc cancels the turn".to_owned()
    } else {
        "Prompt".to_owned()
    }];
    parts.extend(crate::composer::chip(&app.composer.attachments));
    match app.composer.images.as_slice() {
        [] => {}
        [one] => parts.push(format!("+ image {}", one.name)),
        many => parts.push(format!("+{} images", many.len())),
    }
    if !app.composer.queue.is_empty() {
        parts.push(format!("+{} queued", app.composer.queue.len()));
    }
    format!(" {} ", parts.join(" · "))
}
/// The width the draft wraps at: margin, border and padding take three
/// columns on each side.
fn draft_width(terminal_width: u16) -> usize {
    terminal_width.saturating_sub(6) as usize
}
/// The composer's height from the draft's wrapped rows: borders, 1–4 draft
/// rows, and the hint row. An empty prompt takes 4 rows, a long draft 7.
fn composer_height(draft_rows: usize) -> u16 {
    2 + cells(draft_rows.clamp(1, 4)) + 1
}
/// The top-left banner, after Claude Code's: the mini Octet (or a text mark
/// without colour) beside three lines. The permission mode leads its line so
/// a long model name can never push it off a narrow screen.
fn banner(frame: &mut Frame, area: Rect, app: &App) {
    let lines = vec![
        Line::from(vec![
            Span::styled("Octet", Style::default().fg(ACCENT).bold()),
            Span::styled(
                format!(" v{} · preview", env!("CARGO_PKG_VERSION")),
                Style::default().fg(MUTED),
            ),
        ]),
        Line::from(vec![
            mode_chip(app),
            Span::styled(
                format!("  ·  {}  /  {}", app.conn.engine, app.conn.model),
                Style::default().fg(MUTED),
            ),
        ]),
        Line::from(vec![
            Span::styled(app.mascot_state().label(), Style::default().fg(ACCENT)),
            Span::styled(
                format!("  ·  {}", app.conn.workspace),
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
    let id = if app.conn.session.is_empty() {
        "Waiting for session…"
    } else {
        app.conn.session.as_str()
    };
    let mut lines = vec![
        Line::default(),
        Line::from(Span::styled(" ENGINE", Style::default().fg(MUTED))),
        Line::from(format!(" {}", app.conn.engine)),
        Line::default(),
        Line::from(Span::styled(" MODEL", Style::default().fg(MUTED))),
        Line::from(format!(" {}", app.conn.model)),
        Line::from(" /session for full details"),
        Line::default(),
        Line::from(Span::styled(" SESSION", Style::default().fg(MUTED))),
        Line::from(format!(" {id}")),
        Line::default(),
        Line::from(Span::styled(" GOAL", Style::default().fg(MUTED))),
        Line::from(match app.goals.goal() {
            Some(goal) => format!(
                " {} · {}/{} turns",
                goal.status(),
                goal.turns(),
                octet_core::goal::MAX_GOAL_TURNS
            ),
            None => " No active goal".into(),
        }),
        Line::default(),
        Line::from(Span::styled(" QUICK COMMANDS", Style::default().fg(MUTED))),
    ];
    lines.extend(
        COMMANDS
            .iter()
            .filter_map(|spec| Some(Line::from(format!(" {:<11} {}", spec.name, spec.quick?)))),
    );
    lines.extend([
        Line::default(),
        Line::from(Span::styled(" CONTROL", Style::default().fg(MUTED))),
        Line::from(" Esc         Cancel turn"),
        Line::from(" PgUp/PgDn   Read history"),
        Line::from(" Ctrl+C ×2   Save and quit"),
    ]);
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
/// The help screen: keys, then every command's usage, then the rest.
pub fn help_lines() -> Vec<String> {
    let keys = [
        "Octet terminal preview",
        "",
        "Enter send · Alt+Enter / Ctrl+J newline",
        "Arrows, Home, End edit; ↑ ↓ browse prompt history",
        "Ctrl+U clear draft · PgUp/PgDn scroll conversation",
        "Ctrl+End follow · Esc/Ctrl+C cancel turn",
        "Ctrl+C twice quit · Ctrl+Z suspend (return with fg)",
        "Ctrl+P commands · Ctrl+G write the prompt in $EDITOR",
        "!cmd run and attach output · !!cmd run only · Esc stops",
        "@ mention a file · Tab completes paths and /commands",
        "",
    ];
    let rest = [
        "/approval-demo: offline permission dialog",
        "",
        "Approval: A allow once · D/Esc deny",
        "Journals retain older output beyond the viewport.",
        "Esc or F1 closes help",
    ];
    keys.into_iter()
        .chain(
            COMMANDS
                .iter()
                .filter(|spec| spec.id != Cmd::Help)
                .map(|spec| spec.usage),
        )
        .chain(rest)
        .map(str::to_owned)
        .collect()
}
/// The help screen: as tall as its wrapped lines allow, scrolled when the
/// terminal is shorter. How to scroll and close is on the border, so it
/// shows at any size.
fn help(frame: &mut Frame, area: Rect, scroll: &mut u16) {
    let width = 76.min(area.width.saturating_sub(4));
    let inner = usize::from(width.saturating_sub(2));
    // Wrapped here, once, so the rows counted are the rows drawn.
    let rows = wrapped(&help_lines(), inner);
    let area = modal(area, 76, cells(rows.len()).saturating_add(2));
    let visible = area.height.saturating_sub(2);
    *scroll = (*scroll).min(cells(rows.len()).saturating_sub(visible));
    frame.render_widget(Clear, area);
    let title = if cells(rows.len()) > visible {
        " Help · PgUp/PgDn scroll · Esc closes "
    } else {
        " Help · Esc closes "
    };
    frame.render_widget(
        Paragraph::new(rows.join("\n"))
            .block(card(title))
            .style(Style::default().bg(PANEL).fg(FG))
            .scroll((*scroll, 0)),
        area,
    );
}
/// Keys the palette also offers, after the commands.
pub const PALETTE_KEYS: [(&str, &str); 3] = [
    ("Ctrl+G", "Write the prompt in $EDITOR"),
    ("@", "Mention a file"),
    ("!", "Run a shell command"),
];
/// The palette's rows: every command, then the keys it also offers.
pub fn palette_entries() -> impl Iterator<Item = (&'static str, &'static str)> {
    COMMANDS
        .iter()
        .map(|spec| (spec.name, spec.summary))
        .chain(PALETTE_KEYS.iter().copied())
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
    // borders and a blank line above and below the list.
    let area = modal(
        area,
        cells(name_width + description_width + 7),
        cells(palette_entries().count()).saturating_add(4),
    );
    frame.render_widget(Clear, area);
    // On a short screen the list scrolls to keep the selected row in sight.
    let window = usize::from(area.height.saturating_sub(4)).max(1);
    let first = selected.saturating_sub(window - 1);
    let mut lines = vec![Line::default()];
    for (index, (command, description)) in palette_entries().enumerate().skip(first).take(window) {
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
    frame.render_widget(
        Paragraph::new(lines)
            .block(card(" Commands · ↑ ↓ choose · Enter run · Esc close "))
            .style(Style::default().bg(PANEL)),
        area,
    );
}
/// `lines`, each wrapped to `width` as the transcript wraps.
fn wrapped<S: AsRef<str>>(lines: &[S], width: usize) -> Vec<String> {
    lines
        .iter()
        .flat_map(|line| transcript::wrap(line.as_ref(), width))
        .collect()
}
fn approval(frame: &mut Frame, area: Rect, id: u64, detail: &str, scroll: &mut u16, count: usize) {
    let area = modal(area, 86, 24);
    frame.render_widget(Clear, area);
    let parts = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(2),
        Constraint::Length(3),
    ])
    .split(area);
    let heading = format!(
        "Permission required  #{id}   ·   {count} pending\nReview the requested action before allowing it."
    );
    frame.render_widget(
        Paragraph::new(heading).style(Style::default().fg(AMBER).bg(PANEL)),
        parts[0],
    );
    // Wrapped here so the scroll can stop at the last row.
    let inner = usize::from(parts[1].width.saturating_sub(2));
    let rows = wrapped(&detail.lines().collect::<Vec<_>>(), inner);
    let visible = parts[1].height.saturating_sub(2);
    *scroll = (*scroll).min(cells(rows.len()).saturating_sub(visible));
    frame.render_widget(
        Paragraph::new(rows.join("\n"))
            .scroll((*scroll, 0))
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

mod transcript;

#[cfg(test)]
mod tests;
