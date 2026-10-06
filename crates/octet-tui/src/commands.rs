//! Every slash command, once. The palette, help, Tab, the sidebar and the
//! CLI help are built from this table; `try_command` dispatches on `Cmd`.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cmd {
    Help,
    Model,
    Mode,
    Goal,
    Session,
    Export,
    Copy,
    New,
    Reconnect,
    RemoteControl,
    Quit,
}

pub struct Spec {
    pub id: Cmd,
    pub name: &'static str,
    pub aliases: &'static [&'static str],
    /// The help screen's line.
    pub usage: &'static str,
    /// The palette's description.
    pub summary: &'static str,
    /// The sidebar's short label, for the commands it lists.
    pub quick: Option<&'static str>,
}

const fn spec(id: Cmd, name: &'static str, usage: &'static str, summary: &'static str) -> Spec {
    Spec {
        id,
        name,
        aliases: &[],
        usage,
        summary,
        quick: None,
    }
}

impl Spec {
    const fn quick(self, label: &'static str) -> Spec {
        Spec {
            quick: Some(label),
            ..self
        }
    }
    const fn aliases(self, aliases: &'static [&'static str]) -> Spec {
        Spec { aliases, ..self }
    }
}

pub const COMMANDS: &[Spec] = &[
    spec(
        Cmd::Help,
        "/help",
        "/help or F1: this screen",
        "Keyboard shortcuts",
    ),
    spec(
        Cmd::Model,
        "/model",
        "/model [provider] <name> · /model default",
        "Switch model or provider",
    )
    .quick("Switch model"),
    spec(
        Cmd::Mode,
        "/mode",
        "/mode [ask|accept-edits|auto|full-access] · Shift+Tab cycles",
        "Permission mode: ask, accept-edits, auto, full-access",
    )
    .quick("Permissions"),
    spec(
        Cmd::Goal,
        "/goal",
        "/goal <objective> · status · pause · resume · complete (audit) · clear",
        "Inspect or manage an autonomous goal",
    ),
    spec(
        Cmd::Session,
        "/session",
        "/session: session ID and journal path",
        "Session ID and journal path",
    )
    .quick("Session info"),
    spec(
        Cmd::Export,
        "/export",
        "/export [path]: copy the journal to a new file",
        "Export journal to a new file",
    )
    .quick("Save journal"),
    spec(
        Cmd::Copy,
        "/copy",
        "/copy or Ctrl+X: copy the last reply to the clipboard",
        "Copy the last reply (also Ctrl+X)",
    ),
    spec(
        Cmd::New,
        "/new",
        "/new: start a fresh conversation",
        "Start a fresh conversation",
    )
    .quick("Fresh context"),
    spec(
        Cmd::Reconnect,
        "/reconnect",
        "/reconnect: resume the vendor session",
        "Reconnect to the vendor session",
    )
    .quick("Resume vendor"),
    spec(
        Cmd::RemoteControl,
        "/remote-control",
        "/remote-control: check phone access setup",
        "Check phone access (tmux, Tailscale, mosh)",
    ),
    spec(
        Cmd::Quit,
        "/quit",
        "/quit or Ctrl+C twice: save and exit",
        "Save and exit",
    )
    .aliases(&["/exit"]),
];

impl Cmd {
    /// The command a typed name (or alias) names.
    pub fn parse(name: &str) -> Option<Cmd> {
        COMMANDS
            .iter()
            .find(|spec| spec.name == name || spec.aliases.contains(&name))
            .map(|spec| spec.id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_entry_parses_to_its_id_and_ids_are_unique() {
        for spec in COMMANDS {
            assert_eq!(Cmd::parse(spec.name), Some(spec.id), "{}", spec.name);
            for alias in spec.aliases {
                assert_eq!(Cmd::parse(alias), Some(spec.id), "{alias}");
            }
        }
        for (i, a) in COMMANDS.iter().enumerate() {
            assert!(COMMANDS[i + 1..]
                .iter()
                .all(|b| b.id != a.id && b.name != a.name));
        }
        assert_eq!(Cmd::parse("/bogus"), None);
    }
}
