//! Stateful sanitizer: escape strings may cross streaming frame boundaries.
#[derive(Clone, Copy, Default)]
enum State {
    #[default]
    Text,
    /// After ESC.
    Escape,
    /// After ESC and intermediate bytes (0x20–0x2F), until a final byte
    /// (0x30–0x7E): `ESC ( B`, which `tput sgr0` writes, is one of these.
    EscIntermediate,
    /// Inside CSI, until a final byte.
    Csi,
    /// Inside OSC/DCS/SOS/PM/APC, until BEL or ST, having dropped `len`
    /// characters of it.
    String { len: usize },
    /// ESC inside a string: `\` completes ST.
    StringEscape { len: usize },
}

/// The most of one escape string dropped before it is taken as unterminated:
/// a stray ESC must not hide the rest of a reply.
const STRING_LIMIT: usize = 4096;
#[derive(Default)]
pub struct Sanitizer {
    state: State,
    /// Keep tabs, for text that leaves Octet (the clipboard, the vendor);
    /// the screen gets four spaces.
    tabs: bool,
}
impl Sanitizer {
    pub fn keeping_tabs() -> Self {
        Self {
            tabs: true,
            ..Self::default()
        }
    }
    pub fn push(&mut self, text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        for c in text.chars() {
            match self.state {
                State::Text => match c {
                    '\x1b' => self.state = State::Escape,
                    '\u{9b}' => self.state = State::Csi,
                    // C1 string introducers: DCS, SOS, OSC, PM, APC.
                    '\u{90}' | '\u{98}' | '\u{9d}' | '\u{9e}' | '\u{9f}' => {
                        self.state = State::String { len: 0 };
                    }
                    '\n' => out.push(c),
                    '\t' if self.tabs => out.push(c),
                    '\t' => out.push_str("    "),
                    c if !c.is_control()
                        && !matches!(c,'\u{202a}'..='\u{202e}'|'\u{2066}'..='\u{2069}') =>
                    {
                        out.push(c)
                    }
                    _ => {}
                },
                State::Escape => match c {
                    '[' => self.state = State::Csi,
                    ']' | 'P' | 'X' | '^' | '_' => self.state = State::String { len: 0 },
                    ' '..='/' => self.state = State::EscIntermediate,
                    _ => self.state = State::Text,
                },
                State::EscIntermediate => {
                    if !(' '..='/').contains(&c) {
                        self.state = State::Text;
                    }
                }
                State::Csi => {
                    if ('@'..='~').contains(&c) {
                        self.state = State::Text
                    }
                }
                State::String { len } => match c {
                    '\x07' | '\u{9c}' => self.state = State::Text,
                    '\x1b' => self.state = State::StringEscape { len },
                    // Unterminated: the line it began on ends it.
                    '\n' => {
                        self.state = State::Text;
                        out.push(c);
                    }
                    _ if len >= STRING_LIMIT => self.state = State::Text,
                    _ => self.state = State::String { len: len + 1 },
                },
                State::StringEscape { len } => match c {
                    '\\' | '\x07' => self.state = State::Text,
                    _ => self.state = State::String { len },
                },
            }
        }
        out
    }
}
/// Like `clean`, keeping tabs.
pub fn strip(text: &str) -> String {
    Sanitizer::keeping_tabs().push(text)
}
pub fn clean(text: &str) -> String {
    Sanitizer::default().push(text)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strips_split_osc_and_csi() {
        let mut s = Sanitizer::default();
        assert_eq!(s.push("ok\x1b]52;c;"), "ok");
        assert_eq!(s.push("payload\x07safe\x1b[3"), "safe");
        assert_eq!(s.push("1mred\x1b[0m"), "red");
        assert_eq!(clean("x\r\x08\u{202e}y"), "xy");
    }
    #[test]
    fn sanitizer_drops_intermediate_escapes() {
        // `tput sgr0` on xterm-256color: ESC ( B, then ESC [ m.
        assert_eq!(clean("a\x1b(B\x1b[mb"), "ab");
        assert_eq!(clean("a\x1b#8b\x1b %Gc"), "abc");
    }
    #[test]
    fn an_unterminated_string_ends_at_newline() {
        // A stray OSC never closed must not hide the rest of a reply.
        assert_eq!(
            clean("x\x1b]8;;http://e\nrest of reply"),
            "x\nrest of reply"
        );
        let long = format!("y\x1b]{}z", "p".repeat(5000));
        assert!(
            clean(&long).ends_with('z'),
            "a string is cut off after 4 KiB"
        );
    }
    #[test]
    fn c1_strings_are_dropped() {
        for introducer in ['\u{90}', '\u{98}', '\u{9e}', '\u{9f}'] {
            let text = format!("a{introducer}payload\u{9c}b");
            assert_eq!(clean(&text), "ab", "{:?}", introducer);
        }
    }
    #[test]
    fn strip_keeps_tabs_for_text_that_leaves_octet() {
        assert_eq!(strip("a\tb\x1b[31mc"), "a\tbc");
        assert_eq!(clean("a\tb"), "a    b");
    }
}
