//! Octet: the nine agent-mode poses of the header mascot, drawn with Unicode half blocks.
//! Poses change on activity events; no animation clock or image protocol needed.
use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};

mod art;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Idle,
    Thinking,
    Coding,
    Searching,
    Delegating,
    Approval,
    Success,
    Error,
    Sleeping,
}
impl State {
    #[cfg(test)]
    pub const ALL: [State; 9] = [
        State::Idle,
        State::Thinking,
        State::Coding,
        State::Searching,
        State::Delegating,
        State::Approval,
        State::Success,
        State::Error,
        State::Sleeping,
    ];
    pub fn label(self) -> &'static str {
        match self {
            Self::Idle => "IDLE",
            Self::Thinking => "THINKING",
            Self::Coding => "CODING",
            Self::Searching => "SEARCHING",
            Self::Delegating => "DELEGATING",
            Self::Approval => "APPROVAL",
            Self::Success => "SUCCESS",
            Self::Error => "ERROR",
            Self::Sleeping => "SLEEPING",
        }
    }
    pub fn tool(detail: &str) -> Self {
        // Inspect the tool name and command line, not arbitrary tool output.
        let name = detail.lines().next().unwrap_or("").to_ascii_lowercase();
        let command = detail.lines().nth(1).unwrap_or("").trim();
        if ["dispatch", "delegate", "spawn_agent", "task", "collabagent"]
            .iter()
            .any(|word| name.contains(word))
        {
            Self::Delegating
        } else if ["search", "read", "glob", "grep"]
            .iter()
            .any(|word| name.contains(word))
            || ["$ rg ", "$ grep ", "$ find ", "$ cat ", "$ ls"]
                .iter()
                .any(|prefix| command.starts_with(prefix))
        {
            Self::Searching
        } else {
            Self::Coding
        }
    }
}

/// The mini Octet's colours, keyed by the characters used in `art.rs`.
const PALETTE: [(char, (u8, u8, u8)); 5] = [
    ('K', (0x0e, 0x10, 0x14)),
    ('d', (0x82, 0x19, 0x2f)),
    ('r', (0xc4, 0x2b, 0x40)),
    ('o', (0xf5, 0x6a, 0x4e)),
    ('c', (0xf6, 0xde, 0xbf)),
];

fn color(pixel: char, background: Color) -> Color {
    PALETTE
        .iter()
        .find(|(symbol, _)| *symbol == pixel)
        .map_or(background, |(_, (r, g, b))| Color::Rgb(*r, *g, *b))
}

/// The 9 × 3-cell mini Octet for a pose: one pixel per half cell, each upper
/// half block showing the top pixel in front and the one below behind it.
pub fn mini(state: State, background: Color) -> Vec<Line<'static>> {
    // The art is ASCII, one byte per pixel: read in place, no grid built
    // per frame. Each cell is a half block, the top pixel over the bottom.
    art::mini(state)
        .chunks(2)
        .map(|pair| {
            let (top, bottom) = (pair[0].as_bytes(), pair[1].as_bytes());
            Line::from_iter(top.iter().zip(bottom).map(|(&top, &bottom)| {
                Span::styled(
                    "▀",
                    Style::default()
                        .fg(color(char::from(top), background))
                        .bg(color(char::from(bottom), background)),
                )
            }))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn the_art_is_ascii() {
        for state in State::ALL {
            assert!(art::mini(state).iter().all(|row| row.is_ascii()));
        }
    }
    #[test]
    fn poses_all_look_different() {
        let drawn: Vec<Vec<Line>> = State::ALL
            .iter()
            .map(|state| mini(*state, Color::Black))
            .collect();
        for (a, first) in drawn.iter().enumerate() {
            for (b, second) in drawn.iter().enumerate().skip(a + 1) {
                assert_ne!(
                    first,
                    second,
                    "{} and {} draw the same",
                    State::ALL[a].label(),
                    State::ALL[b].label()
                );
            }
        }
    }
    #[test]
    fn mini_is_nine_by_three_cells_of_its_pixels() {
        for state in State::ALL {
            let pixel = |x: usize, y: usize| {
                color(art::mini(state)[y].chars().nth(x).unwrap(), Color::Black)
            };
            let lines = mini(state, Color::Black);
            assert_eq!(lines.len(), 3, "{}", state.label());
            for (row, line) in lines.iter().enumerate() {
                assert_eq!(line.width(), 9, "{}", state.label());
                for (x, span) in line.spans.iter().enumerate() {
                    assert_eq!(span.style.fg, Some(pixel(x, 2 * row)));
                    assert_eq!(span.style.bg, Some(pixel(x, 2 * row + 1)));
                }
            }
        }
    }
    #[test]
    fn mini_uses_only_its_palette() {
        for state in State::ALL {
            for row in art::mini(state) {
                assert_eq!(row.chars().count(), 9);
                for symbol in row.chars() {
                    assert!(
                        symbol == '.' || PALETTE.iter().any(|(s, _)| *s == symbol),
                        "{symbol:?}"
                    );
                }
            }
        }
    }
    #[test]
    fn classifies_provider_tools_without_reading_output() {
        assert_eq!(State::tool("Read\n{}"), State::Searching);
        assert_eq!(
            State::tool("commandExecution · running\n$ rg needle src"),
            State::Searching
        );
        assert_eq!(State::tool("Bash\n{}\nsearch results"), State::Coding);
        assert_eq!(State::tool("Task\n{}"), State::Delegating);
        assert_eq!(
            State::tool("collabAgentToolCall · running\n{}"),
            State::Delegating
        );
    }
}
