//! Every slash command, once. The palette, help, Tab, the sidebar and the
//! CLI help are built from this table; `try_command` dispatches on `Cmd`.
use crate::{
    app::App,
    input::{copy_reply, submit},
    vendor::{By, Vendor},
    Action, Exit,
};
use octet_core::{Command, Mode};
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

pub(crate) async fn send_goal_prompt(app: &mut App, vendor: &dyn Vendor, prompt: String) {
    let command = Command::PromptWithDisplay {
        wire: prompt,
        display: app.goals.prompt_display(),
        images: Vec::new(),
    };
    if let Err(error) = app.begin_turn(vendor, command, By::Goal, "working on goal") {
        goal_send_failed(app, error).await;
    }
}
/// Pauses the goal whose prompt could not be sent and says why.
pub(crate) async fn goal_send_failed(app: &mut App, error: octet_core::SendError) {
    app.note(format!("Goal paused: {error}"));
    if let Err(error) = app.goals.send_failed().await {
        app.note(format!("Goal persistence failed: {error}"));
    }
}
/// Runs a slash command. `None` means the caller keeps the draft: the name is
/// not a command (it may be a prompt that merely starts with a slash), or the
/// command failed on input worth correcting (a `/image` path).
pub(crate) async fn try_command(app: &mut App, vendor: &dyn Vendor, input: &str) -> Option<Action> {
    let (name, argument) = input.split_once(' ').unwrap_or((input, ""));
    let argument = argument.trim();
    let Some(spec) = Spec::find(name) else {
        app.note(format!(
            "Unknown command {name}. Use /help, or edit the draft: \
             only a path such as /usr/lib can start a prompt with a slash."
        ));
        return None;
    };
    let cmd = spec.id;
    let refusal = match spec.requires {
        Requires::NoTurn | Requires::Idle if app.turn_open() => Some(TURN_OPEN),
        Requires::Idle if !app.is_idle() => Some(NOT_CONNECTED),
        _ => None,
    };
    if let Some(refusal) = refusal {
        app.hint(refusal);
        return Some(Action::Continue);
    }
    match cmd {
        Cmd::Quit => return Some(Action::Exit(Exit::Quit)),
        Cmd::Help => app.overlay.help = true,
        Cmd::Queue => queue_command(app, argument),
        Cmd::Effort => return Some(effort_command(app, vendor, argument)),
        Cmd::Fork => return Some(fork_command(app)),
        Cmd::Compact => compact_command(app, vendor),
        // A failed /image leaves the line in the prompt box to correct.
        Cmd::Image if !image_command(app, argument) => return None,
        Cmd::Image => {}
        Cmd::Sessions => sessions_command(app).await,
        Cmd::Resume => return Some(resume_command(app, argument)),
        Cmd::Steer if argument.is_empty() => app.note("Use /steer <text>"),
        Cmd::Steer => steer(app, vendor, argument.to_owned()),
        Cmd::Copy => copy_reply(app),
        Cmd::Model => return Some(model_command(app, argument)),
        Cmd::Mode => return Some(mode_command(app, vendor, argument)),
        Cmd::New => return Some(Action::Exit(Exit::New)),
        Cmd::Reconnect => return Some(Action::Exit(Exit::Reconnect)),
        Cmd::Session => app.note(format!(
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
        Cmd::Goal => goal_command(app, vendor, argument).await,
        Cmd::RemoteControl => match argument {
            "" | "status" => return Some(Action::RemoteControl),
            _ => app.note("Use /remote-control or /remote-control status"),
        },
        Cmd::Export => return Some(export_command(app, argument).await),
    }
    Some(Action::Continue)
}
/// `/effort`: show the reasoning effort, or change it. Codex takes it per
/// turn; Claude takes it at launch, so a change reconnects there.
fn effort_command(app: &mut App, vendor: &dyn Vendor, argument: &str) -> Action {
    if argument.is_empty() {
        let current = app.conn.effort.as_deref().unwrap_or("vendor default");
        app.note(format!(
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
        app.note("Effort must be one word, at most 64 bytes");
        return Action::Continue;
    };
    if let Some(Err(error)) = level.as_deref().map(|l| app.conn.engine.check_effort(l)) {
        app.note(error);
        return Action::Continue;
    }
    let provider = app.conn.engine.provider();
    if provider.offline {
        app.note("The offline demo has no reasoning effort");
        Action::Continue
    } else if provider.effort_live {
        match vendor.send(Command::SetEffort(level.clone())) {
            Ok(()) => app.conn.effort = level,
            Err(error) => app.error(error.to_string()),
        }
        Action::Continue
    } else if app.turn_open() {
        app.hint(TURN_OPEN);
        Action::Continue
    } else if app.is_connecting() {
        app.hint(NOT_CONNECTED);
        Action::Continue
    } else {
        Action::Exit(Exit::Effort(level))
    }
}

/// `/fork`: reconnect as a new vendor session that continues this one.
fn fork_command(app: &mut App) -> Action {
    if app.conn.engine.offline() {
        app.note("The offline demo has no context to fork");
    } else if app.conn.session.is_empty() {
        app.note("No vendor session to fork yet; send a prompt first");
    } else {
        return Action::Exit(Exit::Fork);
    }
    Action::Continue
}

/// `/compact`: the vendor compacts its context in a turn of its own.
fn compact_command(app: &mut App, vendor: &dyn Vendor) {
    if app.conn.engine.offline() {
        app.note("The offline demo has no context to compact");
    } else if let Err(error) = app.begin_turn(vendor, Command::Compact, By::User, "compacting") {
        app.error(error.to_string());
    }
}

/// `/steer`: adds `text` to the running turn where the provider can; queues
/// it as the next prompt where it cannot, or while the turn is stopping; and
/// sends it as a prompt when idle.
pub(crate) fn steer(app: &mut App, vendor: &dyn Vendor, text: String) {
    let running = app.conn.is_running();
    if running && app.conn.engine.provider().steer && !app.conn.cancelling {
        if let Err(error) = vendor.send(Command::Steer(text)) {
            app.error(error.to_string());
        }
        return;
    }
    if submit(app, vendor, text) && running {
        app.note(if app.conn.cancelling {
            "The turn is stopping; queued the steer as the next prompt".to_owned()
        } else {
            format!(
                "{} cannot steer a running turn; queued as a follow-up",
                app.conn.engine.title()
            )
        });
    }
}

/// Switches the permission mode live; the vendor confirms it later.
pub(crate) fn switch_mode(app: &mut App, vendor: &dyn Vendor, target: Mode) {
    match vendor.send(Command::SetMode(target)) {
        Ok(()) => app.conn.mode_pending = Some(target),
        Err(error) => app.error(error.to_string()),
    }
}

/// `/image PATH`: attach an image to the next prompt; whether it did.
fn image_command(app: &mut App, argument: &str) -> bool {
    if argument.is_empty() {
        app.note("Usage: /image PATH (PNG, JPEG, GIF or WebP, up to 5 MiB)");
        return false;
    }
    if app.composer.images.len() >= octet_core::IMAGES_PER_PROMPT {
        app.note(format!(
            "A prompt takes at most {} images; press Esc on an empty prompt to drop them",
            octet_core::IMAGES_PER_PROMPT
        ));
        return false;
    }
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    let path = image_path(&app.composer.root, home.as_deref(), argument);
    let provider = app.conn.engine.provider();
    let opened = octet_core::ImageAttachment::open(&path).and_then(|image| {
        if provider.inline_images {
            let sizes = app
                .composer
                .images
                .iter()
                .chain([&image])
                .map(|i| (i.name.as_str(), octet_core::encoded_len(i.bytes)));
            octet_core::check_inline(provider.title, sizes)?;
        }
        Ok(image)
    });
    match opened {
        Ok(image) => {
            app.note(format!("Attached {} to the next prompt", image.name));
            app.composer.images.push(image);
            true
        }
        Err(error) => {
            app.note(error.to_string());
            false
        }
    }
}

/// Where `/image` looks: `~/` is the home directory, and a relative path is
/// in the workspace. A path dragged into the terminal arrives quoted or with
/// backslash escapes; both are undone.
pub(crate) fn image_path(
    root: &std::path::Path,
    home: Option<&std::path::Path>,
    argument: &str,
) -> std::path::PathBuf {
    let quoted = ['\'', '"'].into_iter().find_map(|quote| {
        argument
            .strip_prefix(quote)
            .and_then(|rest| rest.strip_suffix(quote))
    });
    let argument = match quoted {
        Some(inner) => inner.to_owned(),
        None => unescape(argument),
    };
    match (argument.strip_prefix("~/"), home) {
        (Some(rest), Some(home)) => home.join(rest),
        _ => root.join(argument),
    }
}

/// How many sessions `/sessions` lists.
const SESSIONS_LISTED: usize = 20;

/// `/sessions`: this workspace's recent vendor sessions, from the journals
/// beside this one.
async fn sessions_command(app: &mut App) {
    let directory = app
        .conn
        .journal
        .parent()
        .map(std::path::Path::to_path_buf)
        .unwrap_or_default();
    app.conn.listed =
        octet_core::recent_sessions(&directory, &app.composer.root, SESSIONS_LISTED).await;
    if app.conn.listed.is_empty() {
        app.note("No earlier vendor sessions in this workspace");
        return;
    }
    let now = std::time::SystemTime::now();
    let lines: Vec<String> = app
        .conn
        .listed
        .iter()
        .enumerate()
        .map(|(index, entry)| {
            let summary = &entry.summary;
            let this = if summary.session == app.conn.session {
                " (this session)"
            } else {
                ""
            };
            let prompt = summary
                .first_prompt
                .as_deref()
                .map_or_else(|| "(no prompt)".to_owned(), |p| cut(p, 60));
            format!(
                "{}. {} · {}{this} · {} · {}\n   {prompt}",
                index + 1,
                entry.engine,
                summary.session,
                summary.model.as_deref().unwrap_or("default model"),
                age(now, summary.started),
            )
        })
        .collect();
    app.note(format!(
        "Recent sessions (/resume N reconnects):\n{}",
        lines.join("\n")
    ));
}

/// `/resume N`: reconnect to entry N of the last `/sessions` listing.
fn resume_command(app: &mut App, argument: &str) -> Action {
    if app.conn.listed.is_empty() {
        app.note("Run /sessions first, then /resume N");
        return Action::Continue;
    }
    let count = app.conn.listed.len();
    let Some(entry) = argument
        .parse::<usize>()
        .ok()
        .and_then(|n| n.checked_sub(1))
        .and_then(|i| app.conn.listed.get(i))
    else {
        app.note(format!("No session {argument}; /sessions listed {count}"));
        return Action::Continue;
    };
    if entry.summary.session == app.conn.session {
        app.note("That is this session");
        return Action::Continue;
    }
    Action::Exit(Exit::Resume {
        engine: entry.engine,
        session: entry.summary.session.clone(),
        model: entry.summary.model.clone(),
    })
}

/// `text` on one line, cut to `limit` characters.
fn cut(text: &str, limit: usize) -> String {
    let line: String = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if line.chars().count() <= limit {
        line
    } else {
        format!("{}…", line.chars().take(limit).collect::<String>())
    }
}

/// How long ago `then` was, in the largest whole unit.
fn age(now: std::time::SystemTime, then: std::time::SystemTime) -> String {
    let seconds = now.duration_since(then).map_or(0, |d| d.as_secs());
    match seconds {
        0..60 => "just now".into(),
        60..3_600 => format!("{} min ago", seconds / 60),
        3_600..86_400 => format!("{} h ago", seconds / 3_600),
        _ => format!("{} d ago", seconds / 86_400),
    }
}

/// `text` with each backslash escape replaced by the character it escapes.
fn unescape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        out.push(if c == '\\' {
            chars.next().unwrap_or(c)
        } else {
            c
        });
    }
    out
}

/// `/queue`: list the prompts waiting for the running turn, or clear them.
fn queue_command(app: &mut App, argument: &str) {
    match argument {
        "" if app.composer.queue.is_empty() => app.note("No prompts are queued"),
        "" => {
            let lines: Vec<String> = app
                .composer
                .queue
                .iter()
                .enumerate()
                .map(|(i, command)| format!("{}. {}", i + 1, queued_text(command)))
                .collect();
            app.note(format!("Queued prompts:\n{}", lines.join("\n")));
        }
        "clear" => {
            let dropped = std::mem::take(&mut app.composer.queue).len();
            app.note(format!("Cleared {dropped} queued prompts"));
        }
        _ => app.note("Use /queue or /queue clear"),
    }
}

/// A queued prompt as the transcript shows it, on one line.
/// A queued prompt's first line, as the queue lists it.
fn queued_text(prompt: &crate::vendor::Prompt) -> String {
    cut(prompt.display.lines().next().unwrap_or(""), 60)
}

fn model_command(app: &mut App, argument: &str) -> Action {
    if argument.trim().is_empty() {
        app.show_models(1);
    } else if argument == "list" || argument.starts_with("list ") {
        match argument.strip_prefix("list").unwrap_or("").trim() {
            "" => app.show_models(1),
            page => match page.parse::<usize>() {
                Ok(page) => app.show_models(page),
                Err(_) => app.note("Use /model list <page number>"),
            },
        }
    } else if app.turn_open() {
        app.hint(TURN_OPEN);
    } else if app.is_connecting() {
        app.hint(NOT_CONNECTED);
    } else {
        match octet_core::model::Selection::parse(argument, app.conn.engine) {
            Ok(selection) => return Action::Exit(Exit::Model(selection)),
            Err(error) => app.note(error.to_string()),
        }
    }
    Action::Continue
}
fn mode_command(app: &mut App, vendor: &dyn Vendor, argument: &str) -> Action {
    if argument.is_empty() {
        app.note(app.mode_details());
    } else if app.conn.mode_pending.is_some() {
        app.note("Mode change pending; wait for the vendor to confirm");
    } else {
        match Mode::parse(argument) {
            None => app.note(format!(
                "Unknown mode {argument}. Use ask, accept-edits, auto or full-access."
            )),
            Some(target) if target == app.conn.mode && target == Mode::FullAccess => {
                app.note("Already in full-access mode")
            }
            Some(target) if target == Mode::FullAccess || app.conn.mode == Mode::FullAccess => {
                if app.turn_open() {
                    app.hint(TURN_OPEN);
                }
                // Tightening out of full access is always allowed once the vendor has stopped.
                else if !app.conn.is_ready()
                    && !(app.conn.is_stopped() && target != Mode::FullAccess)
                {
                    app.hint(NOT_CONNECTED);
                } else {
                    return Action::Exit(Exit::Mode(target));
                }
            }
            Some(target) => switch_mode(app, vendor, target),
        }
    }
    Action::Continue
}
async fn goal_command(app: &mut App, vendor: &dyn Vendor, argument: &str) {
    use octet_core::goal::{Goal, GoalStep};
    let idle = app.is_idle();
    match argument {
        "" | "status" => app.note(
            app.goals
                .goal
                .as_ref()
                .map(Goal::summary)
                .unwrap_or_else(|| "No goal set. Use /goal <objective>.".into()),
        ),
        "pause" => match app.goals.pause().await {
            Ok(notice) => app.note(notice),
            // The goal is paused in memory even though the file is stale.
            Err(error) => app.note(format!("Goal paused, but saving it failed: {error}")),
        },
        "clear" => match app.goals.clear().await {
            Ok(()) => app.note("Goal cleared. Current vendor turn may finish."),
            Err(error) => app.note(format!("Goal persistence failed: {error}")),
        },
        "resume" | "complete" if !idle => {
            app.note("Wait for a ready, idle session before resuming or auditing a goal")
        }
        "resume" | "complete" => {
            let step = if argument == "complete" {
                GoalStep::Audit
            } else {
                GoalStep::Continue
            };
            match app.goals.resume(step).await {
                Ok(prompt) => send_goal_prompt(app, vendor, prompt).await,
                Err(error) => app.note(error.to_string()),
            }
        }
        _ if !idle => app.note("Wait for a ready, idle session before starting a goal"),
        objective => match app.goals.start(objective).await {
            Ok(prompt) => send_goal_prompt(app, vendor, prompt).await,
            Err(error) => app.note(error.to_string()),
        },
    }
}
async fn export_command(app: &mut App, argument: &str) -> Action {
    let path = if argument.trim().is_empty() {
        std::env::current_dir()
            .unwrap_or_default()
            .join(app.conn.journal.file_name().unwrap_or_default())
    } else {
        PathBuf::from(argument.trim())
    };
    match octet_core::export_journal(&app.conn.journal, &path).await {
        Ok(()) => app.note(format!("Exported journal to {}", path.display())),
        Err(error) => app.note(format!("Export failed: {error}")),
    }
    Action::Continue
}
pub(crate) async fn command(app: &mut App, vendor: &dyn Vendor, input: &str) -> Action {
    try_command(app, vendor, input)
        .await
        .unwrap_or(Action::Continue)
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
}
