//! Every slash command, once. The palette, help, Tab, the sidebar and the
//! CLI help are built from this table; `try_command` dispatches on `Cmd`.
use crate::{app::App, input::copy_reply, Action, Exit};
use octet_core::{Command, Session};
use std::path::PathBuf;

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
    pub(crate) const ALL: [Cmd; 14] = [
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
        Cmd::Quit,
    ];
    /// The command a typed name (or alias) names.
    pub fn parse(name: &str) -> Option<Cmd> {
        COMMANDS
            .iter()
            .find(|spec| spec.name == name || spec.aliases.contains(&name))
            .map(|spec| spec.id)
    }
}

pub(crate) async fn send_goal_prompt(app: &mut App, session: &Session, prompt: String) {
    let command = Command::PromptWithDisplay {
        wire: prompt,
        display: app.goals.prompt_display(),
    };
    match session.handle.send(command) {
        Ok(()) => {
            app.goals.goal_prompt_sent();
            app.conn.start_turn();
            app.conn.status = "working on goal".into();
        }
        Err(error) => goal_send_failed(app, error).await,
    }
}
/// Pauses the goal whose prompt could not be sent and says why.
pub(crate) async fn goal_send_failed(app: &mut App, error: octet_core::SendError) {
    app.notice(format!("Goal paused: {error}"));
    if let Err(error) = app.goals.send_failed().await {
        app.notice(format!("Goal persistence failed: {error}"));
    }
}
/// Runs a slash command. `None` means the name is not a command, so the caller
/// keeps the draft: it may be a prompt that merely starts with a slash.
pub(crate) async fn try_command(app: &mut App, input: &str) -> Option<Action> {
    let (name, argument) = input.split_once(' ').unwrap_or((input, ""));
    let argument = argument.trim();
    let Some(cmd) = Cmd::parse(name) else {
        app.notice(format!(
            "Unknown command {name}. Use /help, or edit the draft: \
             only a path such as /usr/lib can start a prompt with a slash."
        ));
        return None;
    };
    match cmd {
        Cmd::Quit => return Some(Action::Exit(Exit::Quit)),
        Cmd::Help => app.overlay.help = true,
        Cmd::Queue => queue_command(app, argument),
        Cmd::Effort => return Some(effort_command(app, argument)),
        Cmd::Steer if argument.is_empty() => app.notice("Use /steer <text>"),
        Cmd::Steer => return Some(Action::Steer(argument.to_owned())),
        Cmd::Copy => copy_reply(app),
        Cmd::Model => return Some(model_command(app, argument)),
        Cmd::Mode => return Some(mode_command(app, argument)),
        Cmd::New | Cmd::Reconnect => {
            if app.conn.is_running() {
                app.notice = "Cancel the active turn before changing sessions".into();
            } else {
                return Some(if cmd == Cmd::New {
                    Action::Exit(Exit::New)
                } else {
                    Action::Exit(Exit::Reconnect)
                });
            }
        }
        Cmd::Session => app.notice(format!(
            "{}\nSession: {}\nJournal: {}\nWorkspace: {}",
            app.model_details(),
            if app.conn.session.is_empty() {
                "not assigned"
            } else {
                &app.conn.session
            },
            app.conn.journal.display(),
            app.conn.workspace
        )),
        Cmd::Goal => return Some(goal_command(app, argument).await),
        Cmd::RemoteControl => match argument {
            "" | "status" => return Some(Action::RemoteControl),
            _ => app.notice("Use /remote-control or /remote-control status"),
        },
        Cmd::Export => return Some(export_command(app, argument).await),
    }
    Some(Action::Continue)
}
/// `/effort`: show the reasoning effort, or change it. Codex takes it per
/// turn; Claude takes it at launch, so a change reconnects there.
fn effort_command(app: &mut App, argument: &str) -> Action {
    if argument.is_empty() {
        let current = app.conn.effort.as_deref().unwrap_or("vendor default");
        app.notice(format!(
            "Reasoning effort: {current}. Use /effort <level> (low, medium, high, xhigh, max) \
             or /effort default."
        ));
        return Action::Continue;
    }
    let level = if argument == "default" {
        None
    } else if octet_core::valid_effort(argument) {
        Some(argument.to_owned())
    } else {
        app.notice("Effort must be one word, at most 64 bytes");
        return Action::Continue;
    };
    let provider = app.conn.engine.provider();
    if provider.offline {
        app.notice("The offline demo has no reasoning effort");
        Action::Continue
    } else if provider.effort_live {
        Action::Effort(level)
    } else if app.conn.is_running() || !app.overlay.approvals.is_empty() {
        app.notice("Finish or cancel the turn before changing effort");
        Action::Continue
    } else if app.is_connecting() {
        app.notice("Wait for the connection before changing effort");
        Action::Continue
    } else {
        Action::Exit(Exit::Effort(level))
    }
}

/// `/queue`: list the prompts waiting for the running turn, or clear them.
fn queue_command(app: &mut App, argument: &str) {
    match argument {
        "" if app.composer.queue.is_empty() => app.notice("No prompts are queued"),
        "" => {
            let lines: Vec<String> = app
                .composer
                .queue
                .iter()
                .enumerate()
                .map(|(i, command)| format!("{}. {}", i + 1, queued_text(command)))
                .collect();
            app.notice(format!("Queued prompts:\n{}", lines.join("\n")));
        }
        "clear" => {
            let dropped = std::mem::take(&mut app.composer.queue).len();
            app.notice(format!("Cleared {dropped} queued prompts"));
        }
        _ => app.notice("Use /queue or /queue clear"),
    }
}

/// A queued prompt as the transcript shows it, on one line.
fn queued_text(command: &Command) -> String {
    let text = match command {
        Command::Prompt(text) => text.as_str(),
        Command::PromptWithDisplay { display, .. } => display.as_str(),
        _ => "",
    };
    let line = text.lines().next().unwrap_or("");
    if line.chars().count() > 60 {
        format!("{}…", line.chars().take(60).collect::<String>())
    } else {
        line.to_owned()
    }
}

fn model_command(app: &mut App, argument: &str) -> Action {
    if argument.trim().is_empty() {
        app.show_models(1);
    } else if argument == "list" || argument.starts_with("list ") {
        match argument.strip_prefix("list").unwrap_or("").trim() {
            "" => app.show_models(1),
            page => match page.parse::<usize>() {
                Ok(page) => app.show_models(page),
                Err(_) => app.notice("Use /model list <page number>"),
            },
        }
    } else if app.conn.is_running() || !app.overlay.approvals.is_empty() {
        app.notice = "Cancel or finish the current turn before switching models".into();
    } else if app.is_connecting() {
        app.notice = "Wait for connection, or cancel it, before switching models".into();
    } else {
        match octet_core::model::Selection::parse(argument, app.conn.engine) {
            Ok(selection) => return Action::Exit(Exit::Model(selection)),
            Err(error) => app.notice(error.to_string()),
        }
    }
    Action::Continue
}
fn mode_command(app: &mut App, argument: &str) -> Action {
    use octet_core::Mode;
    if argument.is_empty() {
        app.notice(app.mode_details());
    } else if app.conn.mode_pending.is_some() {
        app.notice("Mode change pending; wait for the vendor to confirm");
    } else {
        match Mode::parse(argument) {
            None => app.notice(format!(
                "Unknown mode {argument}. Use ask, accept-edits, auto or full-access."
            )),
            Some(target) if target == app.conn.mode && target == Mode::FullAccess => {
                app.notice("Already in full-access mode")
            }
            Some(target) if target == Mode::FullAccess || app.conn.mode == Mode::FullAccess => {
                if app.conn.is_running() || !app.overlay.approvals.is_empty() {
                    app.notice =
                        "Cancel or finish the current turn before changing full access".into();
                }
                // Tightening out of full access is always allowed once the vendor has stopped.
                else if !app.conn.is_ready()
                    && !(app.conn.is_stopped() && target != Mode::FullAccess)
                {
                    app.notice = "Wait for a ready session before changing full access".into();
                } else {
                    return Action::Exit(Exit::Mode(target));
                }
            }
            Some(target) => return Action::SetMode(target),
        }
    }
    Action::Continue
}
async fn goal_command(app: &mut App, argument: &str) -> Action {
    use octet_core::goal::{Goal, GoalStep};
    let idle = app.is_idle();
    match argument {
        "" | "status" => app.notice(
            app.goals
                .goal
                .as_ref()
                .map(Goal::summary)
                .unwrap_or_else(|| "No goal set. Use /goal <objective>.".into()),
        ),
        "pause" => match app.goals.pause().await {
            Ok(notice) => app.notice(notice),
            // The goal is paused in memory even though the file is stale.
            Err(error) => app.notice(format!("Goal paused, but saving it failed: {error}")),
        },
        "clear" => match app.goals.clear().await {
            Ok(()) => app.notice("Goal cleared. Current vendor turn may finish."),
            Err(error) => app.notice(format!("Goal persistence failed: {error}")),
        },
        "resume" | "complete" if !idle => {
            app.notice("Wait for a ready, idle session before resuming or auditing a goal")
        }
        "resume" | "complete" => {
            let step = if argument == "complete" {
                GoalStep::Audit
            } else {
                GoalStep::Continue
            };
            match app.goals.resume(step).await {
                Ok(prompt) => return Action::GoalPrompt(prompt),
                Err(error) => app.notice(error.to_string()),
            }
        }
        _ if !idle => app.notice("Wait for a ready, idle session before starting a goal"),
        objective => match app.goals.start(objective).await {
            Ok(prompt) => return Action::GoalPrompt(prompt),
            Err(error) => app.notice(error.to_string()),
        },
    }
    Action::Continue
}
async fn export_command(app: &mut App, argument: &str) -> Action {
    if app.conn.is_running() {
        app.notice = "Wait for completion or cancel before exporting".into();
        return Action::Continue;
    }
    let path = if argument.trim().is_empty() {
        std::env::current_dir()
            .unwrap_or_default()
            .join(app.conn.journal.file_name().unwrap_or_default())
    } else {
        PathBuf::from(argument.trim())
    };
    match octet_core::export_journal(&app.conn.journal, &path).await {
        Ok(()) => app.notice(format!("Exported journal to {}", path.display())),
        Err(error) => app.notice(format!("Export failed: {error}")),
    }
    Action::Continue
}
pub(crate) async fn command(app: &mut App, input: &str) -> Action {
    try_command(app, input).await.unwrap_or(Action::Continue)
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
    #[test]
    fn every_command_has_exactly_one_registry_row() {
        for cmd in Cmd::ALL {
            let rows = COMMANDS.iter().filter(|spec| spec.id == cmd).count();
            assert_eq!(rows, 1, "{cmd:?}");
        }
        assert_eq!(Cmd::ALL.len(), COMMANDS.len());
    }
}
