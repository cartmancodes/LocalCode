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
mod mascot;
mod remote;
mod shell;
mod text;
mod view;
use app::App;
use commands::{goal_send_failed, send_goal_prompt, COMMANDS};
use crossterm::{
    event::{DisableBracketedPaste, EnableBracketedPaste, Event as Input, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use input::{key_action, paste, refresh_completion};
use octet_core::goal::Next;
use octet_core::{Command, Config, Session};
use ratatui::{backend::CrosstermBackend, Terminal};
use std::{
    io::{self, Stdout},
    path::PathBuf,
    time::Duration,
};
use tokio::time::Instant;
// One thread owns both poll and read. A finite poll deadline avoids stale
// wakeups after SIGCONT without adding any timer to the render loop.
// Crossterm use-dev-tty selects level-triggered poll: resize and keyboard
// readiness cannot consume each other's edge notification.
struct InputReader {
    events: tokio::sync::mpsc::Receiver<io::Result<Input>>,
    stopping: std::sync::Arc<std::sync::atomic::AtomicBool>,
    task: Option<std::thread::JoinHandle<()>>,
}
impl InputReader {
    fn new() -> Self {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        let (tx, events) = tokio::sync::mpsc::channel(64);
        let stopping = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&stopping);
        let task = std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                match crossterm::event::poll(Duration::from_millis(100)) {
                    Ok(false) => {}
                    Ok(true) => {
                        if tx.blocking_send(crossterm::event::read()).is_err() {
                            break;
                        }
                    }
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                    Err(e) => {
                        let _ = tx.blocking_send(Err(e));
                        break;
                    }
                }
            }
        });
        Self {
            events,
            stopping,
            task: Some(task),
        }
    }
}
impl Drop for InputReader {
    fn drop(&mut self) {
        self.stopping
            .store(true, std::sync::atomic::Ordering::Relaxed);
        self.events.close();
        if let Some(task) = self.task.take() {
            let _ = task.join();
        }
    }
}
struct TerminalGuard;
impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        if let Err(e) = execute!(io::stdout(), EnterAlternateScreen, EnableBracketedPaste) {
            Self::restore();
            return Err(e);
        }
        Ok(Self)
    }
    fn restore() {
        let _ = execute!(
            io::stdout(),
            DisableBracketedPaste,
            LeaveAlternateScreen,
            crossterm::cursor::Show
        );
        let _ = disable_raw_mode();
    }
    fn resume(&self) -> io::Result<()> {
        enable_raw_mode()?;
        execute!(io::stdout(), EnterAlternateScreen, EnableBracketedPaste)
    }
    fn suspend(&self) -> io::Result<()> {
        Self::restore();
        // SAFETY: raise only delivers SIGSTOP to this process.
        unsafe {
            libc::raise(libc::SIGSTOP);
        }
        self.resume()
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        Self::restore();
    }
}
/// Shown after the first Ctrl+C on an idle, empty prompt, as in Claude Code.
pub(crate) const QUIT_HINT: &str = "Press Ctrl+C again to quit";
/// How long that first press stays armed.
pub(crate) const QUIT_WINDOW: Duration = Duration::from_millis(1500);
/// What a key or command asks the session loop to do.
pub(crate) enum Action {
    Continue,
    Suspend,
    SetMode(octet_core::Mode),
    GoalPrompt(String),
    /// Run a `!` line; `attach` keeps its output for the next prompt.
    RunShell {
        command: String,
        attach: bool,
    },
    /// Stop the running `!` command.
    CancelShell,
    /// Run the `/remote-control` checks off the event loop.
    RemoteControl,
    /// Add text to the running turn, queue it, or send it (`/steer`).
    Steer(String),
    /// Change the reasoning effort live (`/effort`, Codex).
    Effort(Option<String>),
    /// Compact the vendor's context (`/compact`).
    Compact,
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
/// Runs the interface until the user quits, reconnecting for `/new`,
/// `/reconnect`, `/model` and full-access changes. Journals go in `directory`.
///
/// # Errors
///
/// Fails if the terminal cannot be set up or read, or a session cannot
/// start (for example, the journal directory is not writable).
pub async fn run(mut config: Config, directory: PathBuf) -> io::Result<()> {
    let old = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        TerminalGuard::restore();
        old(info);
    }));
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    let mut retained_app: Option<App> = None;
    // Shown in a fresh interface, after a switch that drops the transcript.
    let mut opening_notice: Option<String> = None;
    let mut binaries = std::collections::HashMap::from([(config.engine, config.binary.clone())]);
    let goal_store = octet_core::goal::GoalStore::new(&directory, &config.cwd);
    loop {
        let mut session = Session::open(config.clone(), directory.clone())
            .await
            .map_err(io::Error::other)?;
        let mut app = retained_app
            .take()
            .unwrap_or_else(|| App::new(&config, session.journal.clone()));
        if let Some(notice) = opening_notice.take() {
            app.notice(notice);
        }
        if !app.goals.is_attached() {
            attach_goal_store(&mut app, goal_store.clone()).await;
        }
        app.connection(&config, session.journal.clone());
        let result = run_session(&mut terminal, &guard, &mut app, &mut session).await;
        session.shutdown().await;
        // Carry the last vendor-confirmed mode, never an unconfirmed pending one.
        config.mode = app.conn.mode;
        config.effort = app.conn.effort.clone();
        // A fork happens once, at connect; later reconnects resume the fork.
        config.fork = false;
        match result? {
            Exit::Model(selection) => {
                pause_active_goal(&mut app, "model switch").await;
                let cross_provider = selection.provider != config.engine;
                let binary = binaries.get(&selection.provider).cloned();
                let next = selection.configure(&config, &app.conn.session, binary);
                binaries.insert(next.engine, next.binary.clone());
                app.notice(format!(
                    "Model → {} / {}. {} Previous journal: {}",
                    next.engine,
                    next.model.as_deref().unwrap_or("vendor default"),
                    if cross_provider {
                        "New provider context; earlier displayed messages are not sent to this provider."
                    } else {
                        "Resuming the same vendor context."
                    },
                    app.conn.journal.display()
                ));
                config = next;
                retained_app = Some(app);
            }
            Exit::Mode(mode) => {
                pause_active_goal(&mut app, "mode switch").await;
                app.notice(full_access_notice(mode, config.engine, &app.conn.session));
                config.mode = mode;
                if let Some(id) = resume_id(&app, &config) {
                    config.resume = Some(id);
                }
                retained_app = Some(app);
            }
            Exit::Effort(level) => {
                pause_active_goal(&mut app, "effort change").await;
                app.notice(format!(
                    "Reasoning effort → {}. Reconnecting to the same session…",
                    level.as_deref().unwrap_or("vendor default")
                ));
                config.effort = level;
                if let Some(id) = resume_id(&app, &config) {
                    config.resume = Some(id);
                }
                retained_app = Some(app);
            }
            Exit::Fork => {
                pause_active_goal(&mut app, "fork").await;
                app.notice(format!(
                    "Forked from {}. Continuing in a new vendor session…",
                    app.conn.session
                ));
                config.resume = Some(app.conn.session.clone());
                config.fork = true;
                retained_app = Some(app);
            }
            Exit::Resume {
                engine,
                session,
                model,
            } => {
                pause_active_goal(&mut app, "resume").await;
                let binary = binaries
                    .get(&engine)
                    .cloned()
                    .unwrap_or_else(|| PathBuf::from(engine.provider().default_binary));
                binaries.insert(engine, binary.clone());
                opening_notice = Some(format!(
                    "Resumed {engine} session {session}. Previous journal: {}",
                    app.conn.journal.display()
                ));
                config.engine = engine;
                config.binary = binary;
                config.model = model;
                config.resume = Some(session);
            }
            Exit::New => config.resume = None,
            Exit::Reconnect => {
                if let Some(id) = resume_id(&app, &config) {
                    config.resume = Some(id);
                }
                pause_active_goal(&mut app, "reconnect").await;
                app.notice(reconnect_notice(config.resume.is_some(), &app.conn.journal));
                retained_app = Some(app);
            }
            Exit::Quit => break,
        }
    }
    drop(terminal);
    drop(guard);
    Ok(())
}
/// The vendor session a reconnect resumes, once there is one.
fn resume_id(app: &App, config: &Config) -> Option<String> {
    (!app.conn.session.is_empty() && config.engine.is_vendor()).then(|| app.conn.session.clone())
}
/// Sizes ratatui to the terminal again after something else used it.
fn fit(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
    terminal.resize(terminal.size()?.into())
}
/// Takes the terminal back from the editor: raw mode, a fresh frame and a
/// new key reader.
fn regain_terminal(
    guard: &TerminalGuard,
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    input: &mut Option<InputReader>,
) -> io::Result<()> {
    guard.resume()?;
    fit(terminal)?;
    *input = Some(InputReader::new());
    Ok(())
}
/// Loads the stored goal on the first connection. A load failure leaves chat
/// usable and the file in place.
async fn attach_goal_store(app: &mut App, store: octet_core::goal::GoalStore) {
    match app.goals.attach(store).await {
        Err(error) => app.notice(format!(
            "Stored goal could not be loaded: {error}. Chat is available. Use /goal clear to remove the saved goal, or /goal <objective> to replace it."
        )),
        Ok(())
            if app
                .goals
                .goal
                .as_ref()
                .is_some_and(|goal| goal.status == octet_core::goal::Status::Paused) =>
        {
            // Rewrites a stored "active" as "paused".
            if let Err(error) = app.goals.save().await {
                app.notice(format!("Goal persistence failed: {error}"));
            }
            app.notice("Stored goal loaded in paused state. Use /goal resume to continue.");
        }
        Ok(()) => {}
    }
}
/// A `!` command running in the background.
struct ShellTask {
    task: tokio::task::JoinHandle<Result<shell::Ran, shell::ShellError>>,
    cancel: Option<tokio::sync::oneshot::Sender<()>>,
    attach: bool,
}
async fn run_session(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    guard: &TerminalGuard,
    app: &mut App,
    session: &mut Session,
) -> io::Result<Exit> {
    let mut input = Some(InputReader::new());
    let mut dirty = true;
    let mut last_paint = Instant::now() - Duration::from_secs(1);
    let frame_time = Duration::from_millis(33);
    let mut events_open = true;
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut hup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
    let mut suspend =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::from_raw(libc::SIGTSTP))?;
    // The `/remote-control` checks, running while the screen stays live.
    let mut remote_check: Option<tokio::task::JoinHandle<remote::Checks>> = None;
    // The running `!` command, if any.
    let mut shell_task: Option<ShellTask> = None;
    // The workspace file index being built for `@`.
    let mut index_task: Option<tokio::task::JoinHandle<files::Index>> = None;
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
                if app.notice == QUIT_HINT {
                    app.notice.clear();
                }
                dirty = true;
            }
            event = session.events.recv(), if events_open => {
                match event {
                    Some(event) => {
                        // Turn ends paint at once rather than waiting for the frame timer.
                        if matches!(event, octet_core::Event::Finished { .. } | octet_core::Event::Error(_)) {
                            last_paint = Instant::now() - frame_time;
                        }
                        session_event(app, session, event).await;
                    }
                    None => {
                        events_open = false;
                        app.event(octet_core::Event::Stopped);
                    }
                }
                dirty = true;
            }
            checks = async {
                match remote_check.as_mut() {
                    Some(task) => task.await,
                    None => std::future::pending().await,
                }
            } => {
                remote_check = None;
                match checks {
                    Ok(checks) => show_remote_report(app, &checks),
                    Err(error) => app.notice(format!("Phone-access check failed: {error}")),
                }
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
                    let _ = disable_raw_mode();
                    regain_terminal(guard, terminal, &mut input)?;
                    match edit.finish(status.is_ok_and(|status| status.success())) {
                        Ok(text) => app.composer.editor.set(text),
                        Err(error) => app.notice(error.to_string()),
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
                        key_action(app, session, key).await
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
                    Action::GoalPrompt(prompt) => send_goal_prompt(app, session, prompt).await,
                    Action::RemoteControl => start_remote_check(app, &mut remote_check),
                    Action::Steer(text) => steer(app, session, text),
                    Action::Compact => match session.handle.send(Command::Compact) {
                        Ok(()) => {
                            app.goals.user_prompt_sent();
                            app.conn.start_turn();
                            app.conn.status = "compacting".into();
                        }
                        Err(error) => app.notice(error.to_string()),
                    },
                    Action::Effort(level) => match session.handle.send(Command::SetEffort(level.clone())) {
                        Ok(()) => app.conn.effort = level,
                        Err(error) => app.notice(error.to_string()),
                    },
                    Action::ExternalEditor => {
                        match external::prepare(&app.composer.editor.text, external::editor_command()) {
                            Err(error) => app.notice(error.to_string()),
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
                                        app.notice(format!(
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
                        app.notice = format!("Running {command} · Esc to stop");
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
                    Action::SetMode(mode) => match session.handle.send(Command::SetMode(mode)) {
                        Ok(()) => app.conn.mode_pending = Some(mode),
                        Err(e) => app.notice(e.to_string()),
                    },
                    Action::Exit(exit) => return Ok(exit),
                }
                if matches!(app.composer.files, files::Files::Wanted) {
                    let root = app.composer.root.clone();
                    index_task = Some(tokio::task::spawn_blocking(move || {
                        files::Index::build(&root)
                    }));
                    app.composer.files = files::Files::Building;
                }
                dirty = true;
            }
            // While an editor waits in cooked mode, Ctrl+C is meant for it.
            _ = quit_signal() => {
                if editing.is_none() {
                    return Ok(Exit::Quit);
                }
            }
            _ = unix_signal(&mut term) => {
                stop_editor(&mut editing).await;
                return Ok(Exit::Quit);
            }
            _ = unix_signal(&mut hup) => {
                stop_editor(&mut editing).await;
                return Ok(Exit::Quit);
            }
            _ = unix_signal(&mut suspend) => {
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
async fn session_event(app: &mut App, session: &Session, event: octet_core::Event) {
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
    if failed {
        if let Err(error) = app.goals.turn_failed().await {
            app.notice(format!("Goal persistence failed; paused: {error}"));
        }
    }
    let Some(outcome) = outcome else {
        return;
    };
    match app.goals.turn_finished(&outcome).await {
        Ok(Next::Idle) => send_queued(app, session),
        Ok(Next::Stopped(summary)) => {
            app.notice(summary);
            send_queued(app, session);
        }
        Ok(Next::Continue { prompt, display }) => {
            let command = Command::PromptWithDisplay {
                wire: prompt,
                display,
                images: Vec::new(),
            };
            match session.handle.send(command) {
                Ok(()) => {
                    app.goals.goal_prompt_sent();
                    app.conn.start_turn();
                    app.conn.status = "continuing goal".into();
                }
                Err(error) => goal_send_failed(app, error).await,
            }
        }
        Err(error) => app.notice(format!("Goal persistence failed; paused: {error}")),
    }
}
/// `/steer`: adds `text` to the running turn where the provider can, queues
/// it as a follow-up where it cannot, and sends it as a prompt when idle.
fn steer(app: &mut App, session: &Session, text: String) {
    if app.conn.is_running() {
        if app.conn.engine.provider().steer {
            if let Err(error) = session.handle.send(Command::Steer(text)) {
                app.notice(error.to_string());
            }
        } else if app.composer.queue.len() >= app::QUEUE_LIMIT {
            app.notice(format!(
                "The queue is full ({} prompts); wait for the turn or press Esc",
                app::QUEUE_LIMIT
            ));
        } else {
            app.composer.queue.push_back(Command::Prompt(text));
            let title = app.conn.engine.title();
            app.notice(format!(
                "{title} cannot steer a running turn; queued as a follow-up"
            ));
        }
    } else if app.is_idle() {
        match session.handle.send(Command::Prompt(text.clone())) {
            Ok(()) => {
                app.goals.user_prompt_sent();
                app.conn.start_turn();
                app.conn.status = "sending".into();
                app.remember(text);
            }
            Err(error) => app.notice(error.to_string()),
        }
    } else {
        app.notice("Wait for the connection, or /reconnect");
    }
}
/// Sends the next prompt queued behind the turn that just ended.
fn send_queued(app: &mut App, session: &Session) {
    if !app.is_idle() {
        return;
    }
    let Some(command) = app.composer.queue.pop_front() else {
        return;
    };
    match session.handle.send(command) {
        Ok(()) => {
            app.goals.user_prompt_sent();
            app.conn.start_turn();
            app.conn.status = "sending".into();
        }
        Err(error) => app.notice(format!("A queued prompt could not be sent: {error}")),
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
            app.notice = format!("$ {} · {}", ran.command, ran.summary());
            if attach {
                app.attach(ran);
            }
        }
        Err(error) => app.error(error.to_string()),
    }
}
/// Starts the `/remote-control` checks in the background. They take up to
/// about 2.5 seconds, and the loop must keep draining vendor events meanwhile.
fn start_remote_check(app: &mut App, check: &mut Option<tokio::task::JoinHandle<remote::Checks>>) {
    if check.is_some() {
        app.notice = "Phone-access check already running".into();
        return;
    }
    app.notice = "Checking phone access…".into();
    *check = Some(tokio::spawn(remote::probe()));
}
/// The full report goes in the conversation; the status line, one row high,
/// gets the count of problems.
fn show_remote_report(app: &mut App, checks: &remote::Checks) {
    let report = remote::report(checks);
    app.notice(report.as_str());
    app.notice = remote::summary(&report);
}
/// Ring once when an approval starts waiting; a burst of requests behind it
/// rings no more.
fn should_alert(app: &App, event: &octet_core::Event) -> bool {
    matches!(event, octet_core::Event::Approval { .. }) && app.overlay.approvals.is_empty()
}
/// A bell plus a desktop notification (OSC 9) for an approval the user isn't
/// watching. The bell passes through tmux and mosh to a phone; tmux drops the
/// OSC 9, which helps only a desktop terminal connected directly. Write errors
/// are ignored: the alert is a courtesy, never a reason to stop.
const ALERT: &[u8] = b"\x07\x1b]9;Octet: approval needed\x07";
fn alert() {
    write_terminal(ALERT);
}
/// Writes control bytes straight to the terminal, outside a frame. Errors are
/// ignored: these are courtesies, never a reason to stop.
pub(crate) fn write_terminal(bytes: &[u8]) {
    use std::io::Write;
    let mut stdout = io::stdout();
    let _ = stdout.write_all(bytes).and_then(|()| stdout.flush());
}
async fn quit_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
async fn unix_signal(signal: &mut tokio::signal::unix::Signal) {
    signal.recv().await;
}
/// A new connection must never continue an autonomous goal by itself.
async fn pause_active_goal(app: &mut App, why: &str) {
    match app.goals.pause_active().await {
        Ok(true) => app.notice(format!(
            "Goal paused for {why}. Use /goal resume to continue."
        )),
        Ok(false) => {}
        Err(error) => app.notice(format!(
            "Goal paused for {why}, but saving it failed: {error}"
        )),
    }
}
/// The retained screen spans two journals after a reconnect; say where the
/// earlier part is, because /export and /session cover only the new one.
fn reconnect_notice(resumed: bool, previous: &std::path::Path) -> String {
    format!(
        "{} Earlier messages stay visible. Previous journal: {}",
        if resumed {
            "Reconnecting to the same vendor session."
        } else {
            "Reconnecting. No vendor session ID yet, so the vendor starts fresh."
        },
        previous.display()
    )
}
/// Says whether a full-access change resumes the vendor session or starts over.
fn full_access_notice(mode: octet_core::Mode, engine: octet_core::Engine, session: &str) -> String {
    let change = if mode == octet_core::Mode::FullAccess {
        "Full access: the agent can run any command and edit any file without asking.".to_owned()
    } else {
        format!("Leaving full access for {}.", mode.label())
    };
    let next = if engine.offline() {
        "Restarting the offline demo…"
    } else if session.is_empty() {
        "Starting a new session (no session ID yet)…"
    } else {
        "Reconnecting to the same session…"
    };
    format!("{change} {next}")
}

#[cfg(test)]
mod tests;
