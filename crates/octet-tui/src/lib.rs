//! The terminal interface: the session loop that joins keys, vendor events,
//! `!` commands and the screen.
mod app;
mod clipboard;
mod commands;
mod composer;
mod editor;
mod external;
mod files;
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
use crossterm::event::{Event as Input, KeyEventKind};
use input::{key_action, paste, refresh_completion};
use octet_core::goal::Next;
use octet_core::{Command, Config, Session};
use ratatui::{Terminal, backend::CrosstermBackend};
use registry::COMMANDS;
use std::{
    io::{self, Stdout},
    path::PathBuf,
    time::Duration,
};
pub(crate) use terminal::write_terminal;
use terminal::{InputReader, TerminalGuard, alert, fit, regain_terminal};
use tokio::time::Instant;
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
        let result = run_session(&mut terminal, &guard, &mut app, &mut session, &mut signals).await;
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
/// A `!` command running in the background.
struct ShellTask {
    task: tokio::task::JoinHandle<Result<shell::Ran, shell::ShellError>>,
    cancel: Option<tokio::sync::oneshot::Sender<()>>,
    attach: bool,
}
impl Drop for ShellTask {
    /// A session that ends (a reconnect, a quit) takes its command with it:
    /// aborting drops the run, whose guard kills the command's group.
    fn drop(&mut self) {
        self.task.abort();
    }
}
async fn run_session(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    guard: &TerminalGuard,
    app: &mut App,
    session: &mut Session,
    signals: &mut Signals,
) -> io::Result<Exit> {
    let mut input = Some(InputReader::new());
    let mut dirty = true;
    let mut last_paint = Instant::now() - Duration::from_secs(1);
    let frame_time = Duration::from_millis(33);
    let mut events_open = true;
    // The running `!` command, if any.
    let mut shell_task: Option<ShellTask> = None;
    // The @ index, built on a plain thread: a walk stuck on a dead mount
    // must not hold up the runtime's shutdown, as spawn_blocking would.
    let mut index_task: Option<tokio::sync::oneshot::Receiver<files::Index>> = None;
    // The external editor, while it has the terminal.
    let mut editing: Option<(tokio::process::Child, external::Edit)> = None;
    loop {
        if dirty && editing.is_none() && last_paint.elapsed() >= frame_time {
            terminal.draw(|f| view::draw(f, app))?;
            last_paint = Instant::now();
            dirty = false;
        }
        let quit_deadline = app.quit_armed;
        tokio::select! {
            _ = tokio::time::sleep_until(last_paint + frame_time), if dirty && editing.is_none() => {}
            // The only timer outside painting: the quit hint expires.
            _ = tokio::time::sleep_until(quit_deadline.unwrap_or(last_paint)), if quit_deadline.is_some() => {
                app.quit_armed = None;
                app.clear_status(app::StatusKind::QuitHint);
                dirty = true;
            }
            event = session.events.recv(), if events_open => {
                match event {
                    Some(event) => {
                        // Turn ends paint at once rather than waiting for the frame timer.
                        if matches!(event, octet_core::Event::Finished { .. } | octet_core::Event::Error(_)) {
                            last_paint = Instant::now() - frame_time;
                        }
                        session_event(app, &session.handle, event).await;
                    }
                    None => {
                        events_open = false;
                        app.event(octet_core::Event::Stopped);
                    }
                }
                dirty = true;
            }
            ended = async {
                match app.job.as_mut() {
                    Some(running) => running.wait().await,
                    None => std::future::pending().await,
                }
            } => {
                app.job = None;
                jobs::apply(app, ended);
                dirty = true;
            }
            index = async {
                match index_task.as_mut() {
                    Some(task) => task.await,
                    None => std::future::pending().await,
                }
            } => {
                index_task = None;
                app.composer.files = match index {
                    Ok(index) => files::Files::Ready(index),
                    Err(_) => files::Files::Unbuilt,
                };
                refresh_completion(app);
                dirty = true;
            }
            result = async {
                match shell_task.as_mut() {
                    Some(running) => (&mut running.task).await,
                    None => std::future::pending().await,
                }
            } => {
                let attach = shell_task.take().is_some_and(|running| running.attach);
                let result = result.unwrap_or_else(|error| Err(error.into()));
                shell_finished(app, result, attach);
                dirty = true;
            }
            status = async {
                match editing.as_mut() {
                    Some((child, _)) => child.wait().await,
                    None => std::future::pending().await,
                }
            } => {
                if let Some((_, edit)) = editing.take() {
                    // The editor may have changed the terminal behind
                    // crossterm's record of it; clear the record first.
                    let _ = crossterm::terminal::disable_raw_mode();
                    regain_terminal(guard, terminal, &mut input)?;
                    match edit.finish(status.is_ok_and(|status| status.success())) {
                        Ok(text) => app.composer.editor.set(text),
                        Err(error) => app.error(error.to_string()),
                    }
                }
                dirty = true;
            }
            event = async {
                match input.as_mut() {
                    Some(reader) => reader.events.recv().await,
                    None => std::future::pending().await,
                }
            } => {
                let action = match event {
                    Some(Ok(Input::Key(key))) if key.kind != KeyEventKind::Release => {
                        key_action(app, &session.handle, key).await
                    }
                    Some(Ok(Input::Paste(value))) => {
                        paste(app, &value);
                        Action::Continue
                    }
                    Some(Ok(Input::Resize(_, _))) => {
                        fit(terminal)?;
                        Action::Continue
                    }
                    Some(Err(e)) => return Err(e),
                    None => return Ok(Exit::Quit),
                    _ => Action::Continue,
                };
                match action {
                    Action::Continue => {}
                    // The command checked that no job is running.
                    Action::Job(next) => {
                        app.hint(next.label);
                        app.job = Some(jobs::Running::spawn(next));
                    }
                    Action::ExternalEditor => {
                        match external::prepare(app.composer.editor.text(), external::editor_command()) {
                            Err(error) => app.error(error.to_string()),
                            Ok(edit) => {
                                // Stop reading keys so the editor gets them all.
                                input = None;
                                TerminalGuard::restore();
                                match tokio::process::Command::new(&edit.program)
                                    .args(&edit.args)
                                    .arg(&edit.path)
                                    .kill_on_drop(true)
                                    .spawn()
                                {
                                    Ok(child) => editing = Some((child, edit)),
                                    Err(error) => {
                                        regain_terminal(guard, terminal, &mut input)?;
                                        app.note(format!(
                                            "Cannot start {}: {error}",
                                            edit.program
                                        ));
                                    }
                                }
                            }
                        }
                    }
                    Action::RunShell { command, attach } => {
                        let (cancel, cancelled) = tokio::sync::oneshot::channel();
                        let root = app.composer.root.clone();
                        app.composer.shell_running = true;
                        app.hint(format!("Running {command} · Esc to stop"));
                        shell_task = Some(ShellTask {
                            task: tokio::spawn(async move {
                                shell::run(&command, &root, cancelled).await
                            }),
                            cancel: Some(cancel),
                            attach,
                        });
                    }
                    Action::CancelShell => {
                        if let Some(cancel) =
                            shell_task.as_mut().and_then(|running| running.cancel.take())
                        {
                            let _ = cancel.send(());
                        }
                    }
                    Action::Suspend => {
                        guard.suspend()?;
                        fit(terminal)?;
                    }
                    Action::Exit(exit) => return Ok(exit),
                }
                if matches!(app.composer.files, files::Files::Wanted) {
                    let root = app.composer.root.clone();
                    let (built, index) = tokio::sync::oneshot::channel();
                    std::thread::spawn(move || {
                        let _ = built.send(files::Index::build(&root));
                    });
                    index_task = Some(index);
                    app.composer.files = files::Files::Building;
                }
                dirty = true;
            }
            // While an editor waits in cooked mode, Ctrl+C is meant for it.
            _ = signals.interrupt.recv() => {
                if editing.is_none() {
                    return Ok(Exit::Quit);
                }
            }
            _ = signals.term.recv() => {
                stop_editor(&mut editing).await;
                return Ok(Exit::Quit);
            }
            _ = signals.hup.recv() => {
                stop_editor(&mut editing).await;
                return Ok(Exit::Quit);
            }
            _ = signals.suspend.recv() => {
                if editing.is_some() {
                    // The editor owns the terminal and restores it on fg;
                    // Octet only stops alongside it.
                    // SAFETY: raise only delivers SIGSTOP to this process.
                    unsafe {
                        libc::raise(libc::SIGSTOP);
                    }
                } else {
                    guard.suspend()?;
                    fit(terminal)?;
                    dirty = true;
                }
            }
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
/// Asks a running editor to quit, so it can put the terminal back, and
/// kills it if it has not within a second.
async fn stop_editor(editing: &mut Option<(tokio::process::Child, external::Edit)>) {
    let Some((child, _)) = editing.as_mut() else {
        return;
    };
    if let Some(pid) = child.id().and_then(|pid| libc::pid_t::try_from(pid).ok()) {
        // SAFETY: signals only the editor this session started.
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
    }
    if tokio::time::timeout(Duration::from_secs(1), child.wait())
        .await
        .is_err()
    {
        let _ = child.kill().await;
    }
}
/// A `!` command's result: in the transcript, on the status line in place
/// of "Running …", and attached when asked.
fn shell_finished(app: &mut App, result: Result<shell::Ran, shell::ShellError>, attach: bool) {
    app.composer.shell_running = false;
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
