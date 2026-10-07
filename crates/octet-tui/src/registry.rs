//! Every slash command, once. The palette, help, Tab, the sidebar and the
//! CLI help are built from this table, and each row says what the session
//! must be doing for the command to run.
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
    Queue,
    Steer,
    Effort,
    Fork,
    Compact,
    Image,
    Sessions,
    Resume,
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
    /// What the session must be doing for the command to run.
    pub requires: Requires,
}

/// What a command needs of the session, checked once in `try_command`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Requires {
    /// Runs at any time.
    Nothing,
    /// No turn running and no approval waiting.
    NoTurn,
    /// As `NoTurn`, and connected.
    Idle,
}

/// The refusal for a command that needs the turn to be over.
pub(crate) const TURN_OPEN: &str = "Finish or cancel the current turn first";
/// The refusal for a command that needs a connected session.
pub(crate) const NOT_CONNECTED: &str = "Wait for the connection, or /reconnect";

const fn spec(id: Cmd, name: &'static str, usage: &'static str, summary: &'static str) -> Spec {
    Spec {
        id,
        name,
        aliases: &[],
        usage,
        summary,
        quick: None,
        requires: Requires::Nothing,
    }
}

impl Spec {
    const fn quick(self, label: &'static str) -> Spec {
        Spec {
            quick: Some(label),
            ..self
        }
    }
    const fn requires(self, requires: Requires) -> Spec {
        Spec { requires, ..self }
    }
    /// The row a typed name (or alias) names.
    pub fn find(name: &str) -> Option<&'static Spec> {
        COMMANDS
            .iter()
            .find(|spec| spec.name == name || spec.aliases.contains(&name))
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
        "/goal <objective> · status|pause|resume|complete|clear",
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
    .quick("Save journal")
    .requires(Requires::NoTurn),
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
    .quick("Fresh context")
    .requires(Requires::NoTurn),
    spec(
        Cmd::Reconnect,
        "/reconnect",
        "/reconnect: resume the vendor session",
        "Reconnect to the vendor session",
    )
    .quick("Resume vendor")
    .requires(Requires::NoTurn),
    spec(
        Cmd::RemoteControl,
        "/remote-control",
        "/remote-control: check phone access setup",
        "Check phone access (tmux, Tailscale, mosh)",
    ),
    spec(
        Cmd::Queue,
        "/queue",
        "/queue · /queue clear: prompts waiting for the running turn",
        "Show or clear queued prompts",
    ),
    spec(
        Cmd::Steer,
        "/steer",
        "/steer <text>: add to the running turn (Codex), else queue it",
        "Add to the running turn",
    ),
    spec(
        Cmd::Effort,
        "/effort",
        "/effort [level|default]: reasoning effort (low, medium, high, xhigh, max)",
        "Reasoning effort",
    ),
    spec(
        Cmd::Fork,
        "/fork",
        "/fork: continue this conversation in a new vendor session",
        "Fork session",
    )
    .requires(Requires::NoTurn),
    spec(
        Cmd::Compact,
        "/compact",
        "/compact: ask the vendor to compact its context",
        "Compact context",
    )
    .requires(Requires::Idle),
    spec(
        Cmd::Image,
        "/image",
        "/image PATH: attach a PNG, JPEG, GIF or WebP image (up to 5 MiB) to the next prompt",
        "Attach an image",
    ),
    spec(
        Cmd::Sessions,
        "/sessions",
        "/sessions: list this workspace's recent vendor sessions",
        "Recent sessions",
    ),
    spec(
        Cmd::Resume,
        "/resume",
        "/resume N: reconnect to session N from /sessions",
        "Resume a session",
    )
    .requires(Requires::NoTurn),
    spec(
        Cmd::Quit,
        "/quit",
        "/quit or Ctrl+C twice: save and exit",
        "Save and exit",
    )
    .aliases(&["/exit"]),
];

impl Cmd {
    /// Every command, for the registry coverage test.
    #[cfg(test)]
    pub(crate) const ALL: [Cmd; 19] = [
        Cmd::Help,
        Cmd::Model,
        Cmd::Mode,
        Cmd::Goal,
        Cmd::Session,
        Cmd::Export,
        Cmd::Copy,
        Cmd::New,
        Cmd::Reconnect,
        Cmd::RemoteControl,
        Cmd::Queue,
        Cmd::Steer,
        Cmd::Effort,
        Cmd::Fork,
        Cmd::Compact,
        Cmd::Image,
        Cmd::Sessions,
        Cmd::Resume,
        Cmd::Quit,
    ];
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_entry_parses_to_its_id_and_ids_are_unique() {
        for spec in COMMANDS {
            assert_eq!(
                Spec::find(spec.name).map(|s| s.id),
                Some(spec.id),
                "{}",
                spec.name
            );
            for alias in spec.aliases {
                assert_eq!(Spec::find(alias).map(|s| s.id), Some(spec.id), "{alias}");
            }
        }
        for (i, a) in COMMANDS.iter().enumerate() {
            assert!(COMMANDS[i + 1..]
                .iter()
                .all(|b| b.id != a.id && b.name != a.name));
        }
        assert!(Spec::find("/bogus").is_none());
    }
    #[test]
    fn every_command_has_exactly_one_registry_row() {
        for cmd in Cmd::ALL {
            let rows = COMMANDS.iter().filter(|spec| spec.id == cmd).count();
            assert_eq!(rows, 1, "{cmd:?}");
        }
        assert_eq!(Cmd::ALL.len(), COMMANDS.len());
    }
    #[test]
    fn help_text_names_the_real_limits() {
        let usage = |cmd| COMMANDS.iter().find(|s| s.id == cmd).unwrap().usage;
        let mib = octet_core::IMAGE_LIMIT / (1024 * 1024);
        assert!(usage(Cmd::Image).contains(&format!("up to {mib} MiB")));
        let levels = octet_core::Engine::CLAUDE.provider().efforts.join(", ");
        assert!(usage(Cmd::Effort).contains(&format!("({levels})")));
    }
}
