//! What each slash command does. The table of commands is in `registry`;
//! `try_command` checks a row's requirement once, then dispatches on `Cmd`.
use crate::{
    app::App,
    input::{copy_reply, submit},
    jobs::Job,
    registry::{Cmd, Requires, Spec, NOT_CONNECTED, TURN_OPEN},
    vendor::{By, Vendor},
    Action, Exit,
};
use octet_core::{Command, Mode};
use std::path::PathBuf;
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
        app.goal_save_failed(error, false);
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
        Cmd::Sessions => return start_job(app, sessions_job(app)),
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
            "" | "status" => return start_job(app, Job::remote()),
            _ => app.note("Use /remote-control or /remote-control status"),
        },
        Cmd::Export => return start_job(app, export_job(app, argument)),
    }
    Some(Action::Continue)
}
/// `/effort`: show the reasoning effort, or change it. Codex takes it per
/// turn; Claude takes it at launch, so a change reconnects there.
fn effort_command(app: &mut App, vendor: &dyn Vendor, argument: &str) -> Action {
    if argument.is_empty() {
        let current = app.conn.effort.as_deref().unwrap_or("vendor default");
        let levels = match app.conn.engine.provider().efforts {
            [] => "the levels your model offers".to_owned(),
            levels => levels.join(", "),
        };
        app.note(format!(
            "Reasoning effort: {current}. Use /effort <level> ({levels}) or /effort default."
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
    if running && app.conn.engine.provider().steer && !app.conn.is_cancelling() {
        if let Err(error) = vendor.send(Command::Steer(text)) {
            app.error(error.to_string());
        }
        return;
    }
    if submit(app, vendor, text) && running {
        app.note(if app.conn.is_cancelling() {
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
        app.note(format!(
            "Usage: /image PATH (PNG, JPEG, GIF or WebP, up to {} MiB)",
            octet_core::IMAGE_LIMIT / (1024 * 1024)
        ));
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

/// Starts `job`, unless one is running: then the draft stays for later.
fn start_job(app: &mut App, job: Job) -> Option<Action> {
    if let Some(running) = &app.job {
        app.hint(format!("Wait for: {}", running.label));
        return None;
    }
    Some(Action::Job(job))
}

/// `/sessions`: this workspace's recent vendor sessions, from the journals
/// beside this one.
fn sessions_job(app: &App) -> Job {
    let directory = app
        .conn
        .journal
        .parent()
        .map(std::path::Path::to_path_buf)
        .unwrap_or_default();
    Job::sessions(directory, app.composer.root.clone(), SESSIONS_LISTED)
}

/// Lists what `/sessions` found, keeping it for `/resume N`.
pub(crate) fn show_sessions(app: &mut App, listed: Vec<octet_core::RecentSession>) {
    app.conn.listed = listed;
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
            app.note(format!("Cleared {}", crate::app::queued_prompts(dropped)));
        }
        _ => app.note("Use /queue or /queue clear"),
    }
}

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
                // Tightening out of full access is allowed once the vendor has
                // stopped, so a session that failed can still leave it.
                let can_switch =
                    app.conn.is_ready() || (app.conn.is_stopped() && target != Mode::FullAccess);
                if app.turn_open() {
                    app.hint(TURN_OPEN);
                } else if !can_switch {
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
                .goal()
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
            Err(error) => app.goal_save_failed(error, false),
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
fn export_job(app: &App, argument: &str) -> Job {
    let path = if argument.trim().is_empty() {
        std::env::current_dir()
            .unwrap_or_default()
            .join(app.conn.journal.file_name().unwrap_or_default())
    } else {
        PathBuf::from(argument.trim())
    };
    Job::export(app.conn.journal.clone(), path)
}
pub(crate) async fn command(app: &mut App, vendor: &dyn Vendor, input: &str) -> Action {
    try_command(app, vendor, input)
        .await
        .unwrap_or(Action::Continue)
}
