//! The conversation as wrapped, coloured lines, cached per entry and width.
use super::{ACCENT, AMBER, FG, MUTED};
use crate::app::{App, Role};
use ratatui::prelude::*;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

impl App {
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
pub(crate) fn wrap(text: &str, width: usize) -> Vec<String> {
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
