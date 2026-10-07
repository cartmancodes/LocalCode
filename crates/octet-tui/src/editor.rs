use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
/// The prompt being written: its text and a cursor that is always on a
/// grapheme boundary, which every edit relies on (so the fields are private).
#[derive(Default)]
pub struct Editor {
    text: String,
    cursor: usize,
}
/// Columns between tab stops, as the composer shows a tab.
const TAB_STOP: usize = 4;
/// How many columns grapheme `g` takes at column `col`: a tab reaches the
/// next tab stop.
fn columns(g: &str, col: usize) -> usize {
    if g == "\t" {
        TAB_STOP - col % TAB_STOP
    } else {
        g.width()
    }
}
impl Editor {
    /// The draft.
    pub fn text(&self) -> &str {
        &self.text
    }
    /// Where the cursor is, as a byte offset into `text`.
    pub fn cursor(&self) -> usize {
        self.cursor
    }
    #[must_use = "false means the text did not fit the prompt limit"]
    pub fn insert(&mut self, text: &str) -> bool {
        if self.text.len() + text.len() > octet_core::PROMPT_LIMIT {
            return false;
        }
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
        true
    }
    /// Replaces `start..cursor` with `text`; false when it would not fit the
    /// prompt limit, or `start` is not a character boundary before the cursor.
    #[must_use = "false means the text did not fit the prompt limit"]
    pub fn replace(&mut self, start: usize, text: &str) -> bool {
        if start > self.cursor || !self.text.is_char_boundary(start) {
            return false;
        }
        if self.text.len() - (self.cursor - start) + text.len() > octet_core::PROMPT_LIMIT {
            return false;
        }
        self.text.replace_range(start..self.cursor, text);
        self.cursor = start + text.len();
        true
    }
    pub fn left(&mut self) {
        self.cursor = self.text[..self.cursor]
            .grapheme_indices(true)
            .next_back()
            .map(|(i, _)| i)
            .unwrap_or(0);
    }
    pub fn right(&mut self) {
        if let Some(g) = self.text[self.cursor..].graphemes(true).next() {
            self.cursor += g.len();
        }
    }
    pub fn backspace(&mut self) {
        let end = self.cursor;
        self.left();
        self.text.replace_range(self.cursor..end, "");
    }
    pub fn delete(&mut self) {
        let start = self.cursor;
        self.right();
        self.text.replace_range(start..self.cursor, "");
        self.cursor = start;
    }
    pub fn home(&mut self) {
        self.cursor = self.text[..self.cursor]
            .rfind('\n')
            .map(|i| i + 1)
            .unwrap_or(0);
    }
    pub fn end(&mut self) {
        self.cursor += self.text[self.cursor..]
            .find('\n')
            .unwrap_or(self.text.len() - self.cursor);
    }
    pub fn set(&mut self, text: String) {
        self.cursor = text.len();
        self.text = text;
    }
    pub fn take(&mut self) -> String {
        self.cursor = 0;
        std::mem::take(&mut self.text)
    }
    pub fn vertical(&mut self, down: bool) {
        let start = self.text[..self.cursor]
            .rfind('\n')
            .map(|i| i + 1)
            .unwrap_or(0);
        let column = UnicodeWidthStr::width(&self.text[start..self.cursor]);
        let target = if down {
            self.text[self.cursor..]
                .find('\n')
                .map(|i| self.cursor + i + 1)
        } else if start > 0 {
            Some(
                self.text[..start - 1]
                    .rfind('\n')
                    .map(|i| i + 1)
                    .unwrap_or(0),
            )
        } else {
            None
        };
        if let Some(target) = target {
            let line = self.text[target..].split('\n').next().unwrap_or("");
            let mut width = 0;
            self.cursor = target;
            for g in line.graphemes(true) {
                let w = columns(g, width);
                if width + w > column {
                    break;
                }
                width += w;
                self.cursor += g.len();
            }
        }
    }
    pub fn layout(&self, width: usize) -> (Vec<String>, (usize, usize)) {
        let width = width.max(1);
        let mut lines = vec![String::new()];
        let mut col = 0;
        let mut cursor = (0, 0);
        let mut byte = 0;
        for g in self.text.graphemes(true) {
            if g != "\n" && col + columns(g, col) > width {
                lines.push(String::new());
                col = 0;
            }
            if byte == self.cursor {
                cursor = (col, lines.len() - 1);
            }
            if g == "\n" {
                lines.push(String::new());
                col = 0;
            } else {
                // A tab is shown as spaces to the next stop; the text keeps it.
                let w = columns(g, col);
                let line = lines.last_mut().expect("lines starts non-empty");
                if g == "\t" {
                    line.push_str(&" ".repeat(w));
                } else {
                    line.push_str(g);
                }
                col += w;
            }
            byte += g.len();
        }
        if col >= width {
            lines.push(String::new());
            col = 0;
        }
        if byte == self.cursor {
            cursor = (col, lines.len() - 1);
        }
        (lines, cursor)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn edits_unicode_graphemes_without_splitting() {
        let mut e = Editor::default();
        assert!(e.insert("a👩‍💻e\u{301}"));
        e.backspace();
        assert_eq!(e.text, "a👩‍💻");
        e.left();
        e.delete();
        assert_eq!(e.text, "a");
        assert!(e.insert("界"));
        assert_eq!(e.layout(3).1, (0, 1));
    }
    #[test]
    fn preserves_pasted_newlines_and_vertical_column() {
        let mut e = Editor::default();
        assert!(e.insert("first\n界x"));
        e.vertical(false);
        assert_eq!(e.cursor, 3);
        e.end();
        assert_eq!(e.cursor, 5);
        e.home();
        assert_eq!(e.cursor, 0);
    }
    #[test]
    fn editor_layout_measures_like_ratatui() {
        let mut e = Editor::default();
        assert!(e.insert(&"لا".repeat(20)));
        for line in e.layout(7).0 {
            let drawn: usize = line.graphemes(true).map(UnicodeWidthStr::width).sum();
            assert!(drawn <= 7, "{line:?} is {drawn} wide");
        }
    }
    #[test]
    fn tabs_are_laid_out_to_the_next_stop() {
        let mut e = Editor::default();
        assert!(e.insert("a\tb"));
        let (lines, cursor) = e.layout(20);
        assert_eq!(lines, ["a   b"]);
        assert_eq!(cursor, (5, 0));
        assert_eq!(e.text(), "a\tb");
    }
    #[test]
    fn oversized_paste_does_not_destroy_draft() {
        let mut e = Editor::default();
        assert!(e.insert("draft"));
        assert!(!e.insert(&"x".repeat(octet_core::PROMPT_LIMIT)));
        assert_eq!(e.text, "draft");
    }
    #[test]
    fn replace_swaps_the_text_before_the_cursor() {
        let mut editor = Editor::default();
        assert!(editor.insert("see @ma now"));
        editor.cursor = 7;
        assert!(editor.replace(4, "@src/main.rs "));
        assert_eq!(editor.text, "see @src/main.rs  now");
        assert_eq!(editor.cursor, 17);
        assert!(!editor.replace(0, &"x".repeat(octet_core::PROMPT_LIMIT + 1)));
    }
    #[test]
    fn replace_refuses_a_start_past_the_cursor_or_inside_a_character() {
        let mut editor = Editor::default();
        assert!(editor.insert("héllo"));
        editor.cursor = 1;
        assert!(!editor.replace(3, "x"), "start after the cursor");
        editor.cursor = editor.text.len();
        assert!(!editor.replace(2, "x"), "start inside é");
        assert_eq!(editor.text, "héllo");
    }
}
