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
        // History shorter than the view needs: scroll no further than a
        // full page from its start, rather than showing emptiness below.
        if lines.len() < needed {
            self.chat.scroll = self.chat.scroll.min(lines.len().saturating_sub(height));
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
        // Measured as ratatui draws it, grapheme by grapheme: the string
        // width of a word can be less (Arabic lam-alef counts as one).
        let word_width: usize = word.graphemes(true).map(UnicodeWidthStr::width).sum();
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

#[cfg(test)]
mod tests {
    use super::*;
    /// A row's width as ratatui draws it: the sum of its graphemes' widths.
    fn drawn_width(row: &str) -> usize {
        row.graphemes(true).map(UnicodeWidthStr::width).sum()
    }
    #[test]
    fn wrap_never_exceeds_the_width() {
        let samples = [
            "لا ".repeat(40),
            "界".repeat(50),
            "👩‍💻 ".repeat(30),
            "e\u{301} ".repeat(50),
            "mixed 界 لا 👩‍💻 words and more words".repeat(4),
        ];
        for text in &samples {
            for width in 2..30 {
                for row in wrap(text, width) {
                    let one_wide_grapheme = row.graphemes(true).count() == 1;
                    assert!(
                        drawn_width(&row) <= width || one_wide_grapheme,
                        "{row:?} is {} wide at {width}",
                        drawn_width(&row)
                    );
                }
            }
        }
    }
}
