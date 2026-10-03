//! Stateful sanitizer: escape strings may cross streaming frame boundaries.
#[derive(Default)]
pub struct Sanitizer {
    state: u8,
}
impl Sanitizer {
    pub fn push(&mut self, text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        for c in text.chars() {
            match self.state {
                0 => match c {
                    '\x1b' => self.state = 1,
                    '\u{9b}' => self.state = 2,
                    '\u{9d}' => self.state = 3,
                    '\n' => out.push(c),
                    '\t' => out.push_str("    "),
                    c if !c.is_control()
                        && !matches!(c,'\u{202a}'..='\u{202e}'|'\u{2066}'..='\u{2069}') =>
                    {
                        out.push(c)
                    }
                    _ => {}
                },
                1 => match c {
                    '[' => self.state = 2,
                    ']' | 'P' | 'X' | '^' | '_' => self.state = 3,
                    _ => self.state = 0,
                },
                2 => {
                    if ('@'..='~').contains(&c) {
                        self.state = 0
                    }
                }
                3 => match c {
                    '\x07' | '\u{9c}' => self.state = 0,
                    '\x1b' => self.state = 4,
                    _ => {}
                },
                _ => match c {
                    '\\' => self.state = 0,
                    '\x07' => self.state = 0,
                    _ => self.state = 3,
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
