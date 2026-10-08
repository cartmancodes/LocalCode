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
                rows += entry.rows(width).len();
            }
            self.chat.scroll = rows.saturating_sub(height);
        }
        let needed = self.chat.scroll.saturating_add(height);
        // First bring the caches up to date as far back as the view reaches,
        // then borrow rows from them and clone only those on screen.
        let mut available = 0usize;
        for entry in self.chat.entries.iter_mut().rev() {
            available += entry.rows(width).len();
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

#[cfg(test)]
thread_local! {
    /// Bytes of entry text wrapped on this thread, for tests.
    pub(crate) static WRAPPED: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}
/// Where an entry's last source line starts, so text appended to the entry
/// re-wraps from there rather than from the top.
#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct Tail {
    /// The line's byte offset in the entry's text.
    start: usize,
    /// The rows it wrapped to, at the end of the cache.
    rows: usize,
    /// Whether it starts inside a code fence.
    code: bool,
    /// Text was appended since the rows were wrapped.
    pub(crate) grown: bool,
}
impl crate::app::Entry {
    /// Its rows at `width`, wrapped only as far as they changed: appended
    /// text re-wraps the last source line onwards.
    pub(crate) fn rows(&mut self, width: u16) -> &[Line<'static>] {
        if self.width != width {
            let (lines, tail) = wrapped(self.role, &self.text, width);
            self.cache = lines;
            self.tail = tail;
            self.width = width;
        } else if self.tail.grown {
            self.cache.truncate(self.cache.len() - self.tail.rows);
            let Tail { start, code, .. } = self.tail;
            let (lines, tail) = body(self.role, &self.text[start..], width, code, start);
            self.cache.extend(lines);
            self.tail = tail;
        }
        &self.cache
    }
}
#[cfg(test)]
fn entry_lines(role: Role, text: &str, width: u16) -> Vec<Line<'static>> {
    wrapped(role, text, width).0
}
/// An entry's rows at `width`, and where its last source line starts.
fn wrapped(role: Role, text: &str, width: u16) -> (Vec<Line<'static>>, Tail) {
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
    let (lines, tail) = body(role, text, width, false, 0);
    result.extend(lines);
    (result, tail)
}
/// The rows of `text`, which starts at byte `offset` of its entry, `code`
/// saying whether it starts inside a fence; and where its last line starts.
fn body(
    role: Role,
    text: &str,
    width: u16,
    mut code: bool,
    offset: usize,
) -> (Vec<Line<'static>>, Tail) {
    #[cfg(test)]
    WRAPPED.set(WRAPPED.get() + text.len());
    let mut result = Vec::new();
    let mut tail = Tail::default();
    let mut start = offset;
    for line in text.split('\n') {
        let before = result.len();
        tail = Tail {
            start,
            rows: 0,
            code,
            grown: false,
        };
        start += line.len() + 1;
        if line.starts_with("```") {
            code = !code;
            result.push(Line::from(Span::styled(
                line.to_owned(),
                Style::default().fg(MUTED),
            )));
        } else {
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
        tail.rows = result.len() - before;
    }
    (result, tail)
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
    use crate::app::Entry;
    use octet_core::Engine;
    use std::fmt::Write as _;
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

    /// A reply with fences, wide characters, a long word and blank lines.
    fn sample() -> String {
        let mut text =
            String::from("Intro with some words\n```rust\nfn main() { let 界 = \"😀😀\"; }\n```\n");
        text.push_str(&"a".repeat(300));
        text.push_str("\n\n# Heading\n");
        for n in 0..40 {
            let _ = writeln!(
                text,
                "line {n} wide 界界界 text that wraps across the narrow view"
            );
        }
        text.push_str("```\nunclosed fence at the end");
        text
    }

    fn streamed(text: &str, chunk: usize, widths: [u16; 2]) {
        let chars: Vec<char> = text.chars().collect();
        let mut entry = Entry::new(Role::Assistant, Engine::CLAUDE, "");
        for (n, piece) in chars.chunks(chunk).enumerate() {
            entry.append(&piece.iter().collect::<String>());
            // Halfway, the view changes width.
            let width = if n * chunk < chars.len() / 2 {
                widths[0]
            } else {
                widths[1]
            };
            let full = entry_lines(entry.role, &entry.text, width);
            assert_eq!(
                entry.rows(width),
                full.as_slice(),
                "chunk {chunk}, piece {n}"
            );
        }
    }

    #[test]
    fn streamed_text_wraps_as_a_full_rewrap_does() {
        let text = sample();
        for chunk in [1, 2, 5, 13, 64, 500] {
            streamed(&text, chunk, [7, 7]);
            streamed(&text, chunk, [40, 23]);
        }
    }

    #[test]
    fn appending_rewraps_only_the_last_line() {
        let mut entry = Entry::new(
            Role::Assistant,
            Engine::CLAUDE,
            &"word ".repeat(12_000).replace("word word ", "word\nword "),
        );
        let _ = entry.rows(80);
        WRAPPED.set(0);
        entry.append("more words");
        let _ = entry.rows(80);
        assert!(WRAPPED.get() < 1024, "re-wrapped {} bytes", WRAPPED.get());
    }
}
