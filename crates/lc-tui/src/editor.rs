use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
#[derive(Default)]
pub struct Editor {
    pub text: String,
    pub cursor: usize,
}
impl Editor {
    pub fn insert(&mut self, text: &str) -> bool {
        if self.text.len() + text.len() > lc_core::PROMPT_LIMIT {
            return false;
        }
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
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
                if width + g.width() > column {
                    break;
                }
                width += g.width();
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
            if g != "\n" && col + g.width() > width {
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
                lines.last_mut().unwrap().push_str(g);
                col += g.width();
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
        e.insert("a👩‍💻e\u{301}");
        e.backspace();
        assert_eq!(e.text, "a👩‍💻");
        e.left();
        e.delete();
        assert_eq!(e.text, "a");
        e.insert("界");
        assert_eq!(e.layout(3).1, (0, 1));
    }
    #[test]
    fn preserves_pasted_newlines_and_vertical_column() {
        let mut e = Editor::default();
        e.insert("first\n界x");
        e.vertical(false);
        assert_eq!(e.cursor, 3);
        e.end();
        assert_eq!(e.cursor, 5);
        e.home();
        assert_eq!(e.cursor, 0);
    }
    #[test]
    fn oversized_paste_does_not_destroy_draft() {
        let mut e = Editor::default();
        e.insert("draft");
        assert!(!e.insert(&"x".repeat(lc_core::PROMPT_LIMIT)));
        assert_eq!(e.text, "draft");
    }
}
