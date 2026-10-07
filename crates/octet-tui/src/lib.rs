//! The terminal interface: the session loop that joins keys, vendor events,
//! `!` commands and the screen.
mod app;
mod clipboard;
mod commands;
mod composer;
mod editor;
mod event_loop;
mod external;
mod files;
mod handoff;
mod input;
mod jobs;
mod mascot;
mod reconnect;
mod registry;
mod remote;
mod shell;
mod terminal;
mod text;
mod vendor;
mod view;
use app::App;
use commands::goal_send_failed;
use octet_core::goal::Next;
use octet_core::{Command, Config, Session};
use ratatui::{Terminal, backend::CrosstermBackend};
use registry::COMMANDS;
use std::{io, path::PathBuf, time::Duration};
pub(crate) use terminal::write_terminal;
use terminal::{TerminalGuard, alert};
use vendor::{By, Vendor};
/// Shown after the first Ctrl+C on an idle, empty prompt, as in Claude Code.
pub(crate) const QUIT_HINT: &str = "Press Ctrl+C again to quit";
/// How long that first press stays armed.
pub(crate) const QUIT_WINDOW: Duration = Duration::from_millis(1500);
/// What a key or command asks the session loop to do. Dropping one loses
/// work (a job, an exit), so it must be used.
#[must_use]
pub(crate) enum Action {
    Continue,
    Suspend,
    /// Run a `!` line; `attach` keeps its output for the next prompt.
    RunShell {
        command: String,
        attach: bool,
    },
    /// Stop the running `!` command.
    CancelShell,
    /// Run disk or program work off the event loop.
    Job(jobs::Job),
    /// Hand the terminal to the user's editor for the draft.
    ExternalEditor,
    Exit(Exit),
}
/// Why a session ended; `run` decides whether to reconnect.
pub(crate) enum Exit {
    Quit,
    New,
    Reconnect,
    Model(octet_core::model::Selection),
    Mode(octet_core::Mode),
    /// Reconnect with a new reasoning effort (providers that take it at launch).
    Effort(Option<String>),
    /// Reconnect as a new vendor session continuing this one (`/fork`).
    Fork,
    /// Reconnect to a session `/sessions` listed (`/resume N`).
    Resume {
        engine: octet_core::Engine,
        session: String,
        model: Option<String>,
    },
}
/// Every slash command's name, for the CLI help.
pub fn command_names() -> impl Iterator<Item = &'static str> {
    COMMANDS.iter().map(|spec| spec.name)
}
/// Runs the interface until the user quits. When a session ends for a
/// reconnect (`/new`, `/reconnect`, `/model`, `/mode`, `/effort`, `/fork`,
/// `/resume`), `reconnect::plan` decides the next one. Journals go in
/// `directory`.
///
/// # Errors
///
/// Fails if the terminal cannot be set up or read, or a session cannot
/// start (for example, the journal directory is not writable).
pub async fn run(mut config: Config, directory: PathBuf) -> io::Result<()> {
    let old = std::panic::take_hook();
    let owner = std::thread::current().id();
    std::panic::set_hook(Box::new(move |info| {
        if panic_restores_terminal(owner) {
            TerminalGuard::restore();
        }
        old(info);
    }));
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    let mut retained_app: Option<App> = None;
    // Shown in a fresh interface, after a switch that drops the transcript.
    let mut opening_notice: Option<String> = None;
    let mut binaries = std::collections::HashMap::from([(config.engine, config.binary.clone())]);
    let goal_store = octet_core::goal::GoalStore::new(&directory, &config.cwd);
    // Registered once: a signal that arrives while one session ends and the
    // next opens is kept for the check below, not lost.
    let mut signals = Signals::new()?;
    // A job still running when a session ended, for whichever interface
    // comes next.
    let mut carried_job: Option<jobs::Running> = None;
    loop {
        if signals.stop_pending().await {
            jobs::on_quit(carried_job.take()).await;
            break;
        }
        let mut session = Session::open(config.clone(), directory.clone())
            .await
            .map_err(io::Error::other)?;
        let mut app = retained_app
            .take()
            .unwrap_or_else(|| App::new(&config, session.journal().to_path_buf()));
        if let Some(notice) = opening_notice.take() {
            app.note(notice);
        }
        if let Some(job) = carried_job.take() {
            app.job = Some(job);
        }
        if !app.goals.is_attached() {
            attach_goal_store(&mut app, goal_store.clone()).await;
        }
        app.connection(&config, session.journal().to_path_buf());
        let result =
            event_loop::run_session(&mut terminal, &guard, &mut app, &mut session, &mut signals)
                .await;
        session.shutdown().await;
        let exit = match result {
            Ok(exit) => exit,
            Err(error) => {
                jobs::on_quit(app.job.take()).await;
                return Err(error);
            }
        };
        let ended = reconnect::Ended {
            session: &app.conn.session,
            journal: &app.conn.journal,
            mode: app.conn.mode,
            effort: app.conn.effort.clone(),
        };
        let Some(plan) = reconnect::plan(exit, &config, &ended, &mut binaries) else {
            jobs::on_quit(app.job.take()).await;
            break;
        };
        if let Some(why) = plan.pause {
            pause_active_goal(&mut app, why).await;
        }
        for note in plan.notes {
            app.note(note);
        }
        if plan.carry_conversation && app.carry_conversation() {
            app.note("The earlier conversation goes with your next prompt.");
        }
        opening_notice = plan.opening;
        config = plan.config;
        // A job moves with the interface: a fresh one takes it over.
        if !plan.keep_app {
            carried_job = app.job.take();
        }
        retained_app = plan.keep_app.then_some(app);
    }
    drop(terminal);
    drop(guard);
    Ok(())
}
/// Whether a panic on this thread should restore the terminal: only one on
/// the event loop's thread (`owner`) ends the interface. A background job or
/// index thread that panics is reported and survived, so the terminal must
/// stay as the interface left it.
fn panic_restores_terminal(owner: std::thread::ThreadId) -> bool {
    std::thread::current().id() == owner
}
/// Loads the stored goal on the first connection. A load failure leaves chat
/// usable and the file in place.
async fn attach_goal_store(app: &mut App, store: octet_core::goal::GoalStore) {
    match app.goals.attach(store).await {
        Err(error) => app.note(format!(
            "Stored goal could not be loaded: {error}. Chat is available. Use /goal clear to remove the saved goal, or /goal <objective> to replace it."
        )),
        Ok(())
            if app
                .goals.goal()
                .is_some_and(|goal| goal.status() == octet_core::goal::Status::Paused) =>
        {
            // Rewrites a stored "active" as "paused".
            if let Err(error) = app.goals.save().await {
                app.goal_save_failed(error, false);
            }
            app.note("Stored goal loaded in paused state. Use /goal resume to continue.");
        }
        Ok(()) => {}
    }
}
/// The signals the interface handles, registered once for the whole run.
struct Signals {
    interrupt: tokio::signal::unix::Signal,
    term: tokio::signal::unix::Signal,
    hup: tokio::signal::unix::Signal,
    suspend: tokio::signal::unix::Signal,
}
impl Signals {
    fn new() -> io::Result<Self> {
        use tokio::signal::unix::{SignalKind, signal};
        Ok(Self {
            interrupt: signal(SignalKind::interrupt())?,
            term: signal(SignalKind::terminate())?,
            hup: signal(SignalKind::hangup())?,
            suspend: signal(SignalKind::from_raw(libc::SIGTSTP))?,
        })
    }
    /// Whether a signal that stops the interface arrived between sessions.
    async fn stop_pending(&mut self) -> bool {
        tokio::select! {
            biased;
            _ = self.interrupt.recv() => true,
            _ = self.term.recv() => true,
            _ = self.hup.recv() => true,
            () = std::future::ready(()) => false,
        }
    }
}
/// Applies a vendor event, then advances a goal whose turn just ended.
async fn session_event(app: &mut App, vendor: &dyn Vendor, event: octet_core::Event) {
    if let octet_core::Event::Text(text) = &event {
        app.goals.observe_text(text);
    }
    let outcome = match &event {
        octet_core::Event::Finished { outcome } => Some(outcome.clone()),
        _ => None,
    };
    let failed = matches!(event, octet_core::Event::Error(_));
    if should_alert(app, &event) {
        alert();
    }
    app.event(event);
    if failed && let Err(error) = app.goals.turn_failed().await {
        app.goal_save_failed(error, true);
    }
    let Some(outcome) = outcome else {
        return;
    };
    match app.goals.turn_finished(&outcome).await {
        Ok(Next::Idle) => send_queued(app, vendor),
        Ok(Next::Stopped(summary)) => {
            app.note(summary);
            send_queued(app, vendor);
        }
        Ok(Next::Continue { prompt, display }) => {
            let command = Command::PromptWithDisplay {
                wire: prompt,
                display,
                images: Vec::new(),
            };
            if let Err(error) = app.begin_turn(vendor, command, By::Goal, "continuing goal") {
                goal_send_failed(app, error).await;
                send_queued(app, vendor);
            }
        }
        Err(error) => {
            app.goal_save_failed(error, true);
            send_queued(app, vendor);
        }
    }
}
/// Sends the next prompt queued behind the turn that just ended.
fn send_queued(app: &mut App, vendor: &dyn Vendor) {
    if !app.is_idle() {
        return;
    }
    let Some(prompt) = app.composer.queue.pop_front() else {
        return;
    };
    // A failed send keeps the prompt at the front, to go with the next turn.
    if let Err(error) = app.begin_turn(vendor, prompt.clone().into_command(), By::User, "sending") {
        app.composer.queue.push_front(prompt);
        app.error(format!("A queued prompt could not be sent: {error}"));
    }
}
/// A `!` command's result: in the transcript, on the status line in place
/// of "Running …", and attached when asked.
fn shell_finished(app: &mut App, result: Result<shell::Ran, shell::ShellError>) {
    let attach = app.shell.take().is_some_and(|running| running.attach);
    match result {
        Ok(ran) => {
            app.shell_output(&ran);
            app.hint(format!("$ {} · {}", ran.command, ran.summary()));
            if attach {
                app.attach(ran);
            }
        }
        Err(error) => app.error(error.to_string()),
    }
}
/// Ring once when an approval starts waiting; a burst of requests behind it
/// rings no more.
fn should_alert(app: &App, event: &octet_core::Event) -> bool {
    matches!(event, octet_core::Event::Approval { .. }) && app.overlay.approvals.is_empty()
}
/// A new connection must never continue an autonomous goal by itself.
async fn pause_active_goal(app: &mut App, why: &str) {
    match app.goals.pause_active().await {
        Ok(true) => app.note(format!(
            "Goal paused for {why}. Use /goal resume to continue."
        )),
        Ok(false) => {}
        Err(error) => app.note(format!(
            "Goal paused for {why}, but saving it failed: {error}"
        )),
    }
}
#[cfg(test)]
mod test_support;
#[cfg(test)]
mod tests;
