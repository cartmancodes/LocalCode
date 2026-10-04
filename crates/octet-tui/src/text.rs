//! Stateful sanitizer: escape strings may cross streaming frame boundaries.
#[derive(Clone, Copy, Default)]
enum State {
    #[default]
    Text,
    /// After ESC.
    Escape,
    /// Inside CSI, until a final byte.
    Csi,
    /// Inside OSC/DCS/SOS/PM/APC, until BEL or ST.
    String,
    /// ESC inside a string: `\` completes ST.
    StringEscape,
}
#[derive(Default)]
pub struct Sanitizer {
    state: State,
}
impl Sanitizer {
    pub fn push(&mut self, text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        for c in text.chars() {
            match self.state {
                State::Text => match c {
                    '\x1b' => self.state = State::Escape,
                    '\u{9b}' => self.state = State::Csi,
                    '\u{9d}' => self.state = State::String,
                    '\n' => out.push(c),
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
                    ']' | 'P' | 'X' | '^' | '_' => self.state = State::String,
                    _ => self.state = State::Text,
                },
                State::Csi => {
                    if ('@'..='~').contains(&c) {
                        self.state = State::Text
                    }
                }
                State::String => match c {
                    '\x07' | '\u{9c}' => self.state = State::Text,
                    '\x1b' => self.state = State::StringEscape,
                    _ => {}
                },
                State::StringEscape => match c {
                    '\\' => self.state = State::Text,
                    '\x07' => self.state = State::Text,
                    _ => self.state = State::String,
                },
            }
        }
        out
    }
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
}
