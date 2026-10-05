mod clipboard;
mod composer;
mod editor;
mod external;
mod files;
mod mascot;
mod remote;
mod shell;
mod text;
mod view;
use crossterm::{
    event::{
        DisableBracketedPaste, EnableBracketedPaste, Event as Input, KeyCode, KeyEvent,
        KeyEventKind, KeyModifiers,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use octet_core::goal::Next;
use octet_core::{Command, Config, Session};
use ratatui::{backend::CrosstermBackend, Terminal};
use std::{
    io::{self, Stdout},
    path::PathBuf,
    time::Duration,
};
use tokio::time::Instant;
use view::{App, COMMANDS};
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
const QUIT_HINT: &str = "Press Ctrl+C again to quit";
/// How long that first press stays armed.
const QUIT_WINDOW: Duration = Duration::from_millis(1500);

/// What a key or command asks the session loop to do.
enum Action {
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
    /// Hand the terminal to the user's editor for the draft.
    ExternalEditor,
    Exit(Exit),
}
/// Why a session ended; `run` decides whether to reconnect.
enum Exit {
    Quit,
    New,
    Reconnect,
    Model(octet_core::model::Selection),
    Mode(octet_core::Mode),
}
pub async fn run(mut config: Config, directory: PathBuf) -> io::Result<()> {
    let old = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        TerminalGuard::restore();
        old(info);
    }));
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    let mut retained_app: Option<App> = None;
    let mut binaries = std::collections::HashMap::from([(config.engine, config.binary.clone())]);
    let goal_store = octet_core::goal::GoalStore::new(&directory, &config.cwd);
    loop {
        let mut session = Session::open(config.clone(), directory.clone())
            .await
            .map_err(io::Error::other)?;
        let mut app = retained_app
            .take()
            .unwrap_or_else(|| App::new(&config, session.journal.clone()));
        if !app.goals.is_attached() {
            attach_goal_store(&mut app, goal_store.clone()).await;
        }
        app.connection(&config, session.journal.clone());
        let result = run_session(&mut terminal, &guard, &mut app, &mut session).await;
        session.shutdown().await;
        // Carry the last vendor-confirmed mode, never an unconfirmed pending one.
        config.mode = app.mode;
        match result? {
            Exit::Model(selection) => {
                pause_active_goal(&mut app, "model switch").await;
                let cross_provider = selection.provider != config.engine;
                let binary = binaries.get(&selection.provider).cloned();
                let next = selection.configure(&config, &app.session, binary);
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
                    app.journal.display()
                ));
                config = next;
                retained_app = Some(app);
            }
            Exit::Mode(mode) => {
                pause_active_goal(&mut app, "mode switch").await;
                app.notice(full_access_notice(mode, config.engine, &app.session));
                config.mode = mode;
                if !app.session.is_empty() && config.engine.is_vendor() {
                    config.resume = Some(app.session.clone());
                }
                retained_app = Some(app);
            }
            Exit::New => config.resume = None,
            Exit::Reconnect => {
                if !app.session.is_empty() && config.engine.is_vendor() {
                    config.resume = Some(app.session.clone());
                }
                pause_active_goal(&mut app, "reconnect").await;
                app.notice(reconnect_notice(config.resume.is_some(), &app.journal));
                retained_app = Some(app);
            }
            Exit::Quit => break,
        }
    }
    drop(terminal);
    drop(guard);
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
    task: tokio::task::JoinHandle<Result<shell::Ran, String>>,
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
                app.files = match index {
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
                app.shell_running = false;
                match result {
                    Ok(Ok(ran)) => {
                        app.shell_output(&ran);
                        if attach {
                            app.attach(ran);
                        }
                    }
                    Ok(Err(error)) => app.error(error),
                    Err(error) => app.error(format!("The command failed: {error}")),
                }
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
                    guard.resume()?;
                    terminal.resize(terminal.size()?.into())?;
                    input = Some(InputReader::new());
                    match edit.finish(status.is_ok_and(|status| status.success())) {
                        Ok(text) => app.editor.set(text),
                        Err(error) => app.notice(error),
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
                        terminal.resize(terminal.size()?.into())?;
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
                    Action::ExternalEditor => {
                        match external::prepare(&app.editor.text, external::editor_command()) {
                            Err(error) => app.notice(error),
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
                                        guard.resume()?;
                                        terminal.resize(terminal.size()?.into())?;
                                        input = Some(InputReader::new());
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
                        let root = app.root.clone();
                        app.shell_running = true;
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
                        terminal.resize(terminal.size()?.into())?;
                    }
                    Action::SetMode(mode) => match session.handle.send(Command::SetMode(mode)) {
                        Ok(()) => app.mode_pending = Some(mode),
                        Err(e) => app.notice(e.to_string()),
                    },
                    Action::Exit(exit) => return Ok(exit),
                }
                if matches!(app.files, files::Files::Wanted) {
                    let root = app.root.clone();
                    index_task = Some(tokio::task::spawn_blocking(move || {
                        files::Index::build(&root)
                    }));
                    app.files = files::Files::Building;
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
                    terminal.resize(terminal.size()?.into())?;
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
        Ok(Next::Idle) => {}
        Ok(Next::Stopped(summary)) => app.notice(summary),
        Ok(Next::Continue { prompt, display }) => {
            let command = Command::PromptWithDisplay {
                wire: prompt,
                display,
            };
            match session.handle.send(command) {
                Ok(()) => {
                    app.goals.goal_prompt_sent();
                    app.running = true;
                    app.status = "continuing goal".into();
                }
                Err(error) => goal_send_failed(app, error).await,
            }
        }
        Err(error) => app.notice(format!("Goal persistence failed; paused: {error}")),
    }
}
/// How long Tab waits for a folder listing.
const TAB_WAIT: Duration = Duration::from_millis(500);
/// Runs blocking `work` on its own thread, waiting at most `limit`. A plain
/// thread, not spawn_blocking, so work stuck on a dead mount cannot hold up
/// the runtime's shutdown.
async fn off_loop<T: Send + 'static>(
    limit: Duration,
    work: impl FnOnce() -> T + Send + 'static,
) -> Option<T> {
    let (done, result) = tokio::sync::oneshot::channel();
    std::thread::spawn(move || {
        let _ = done.send(work());
    });
    tokio::time::timeout(limit, result).await.ok()?.ok()
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
/// A key chosen from the palette.
fn palette_key(app: &mut App, key: &str) -> Action {
    match key {
        "Ctrl+G" => {
            app.completion = None;
            return Action::ExternalEditor;
        }
        "@" => {
            let after_word = app.editor.text[..app.editor.cursor]
                .chars()
                .next_back()
                .is_some_and(|c| !c.is_whitespace());
            let mention = if after_word { " @" } else { "@" };
            if !app.editor.insert(mention) {
                app.notice = "Prompt limit reached".into();
            } else if let Some((start, _)) =
                composer::mention_at(&app.editor.text, app.editor.cursor)
            {
                open_mentions(app, start);
            }
        }
        _ if app.editor.text.is_empty() => app.editor.set("!".into()),
        _ => app.notice = "Clear the prompt to start a ! command".into(),
    }
    Action::Continue
}
/// Opens the `@` popup for the mention starting at `start`, asking for the
/// index on first use.
fn open_mentions(app: &mut App, start: usize) {
    if matches!(app.files, files::Files::Unbuilt) {
        app.files = files::Files::Wanted;
    }
    app.completion = Some(composer::Completion {
        kind: composer::Kind::File,
        items: Vec::new(),
        selected: 0,
        start,
    });
    refresh_completion(app);
}
/// Re-ranks the `@` popup for the current draft, closing it when the cursor
/// has left the mention.
fn refresh_completion(app: &mut App) {
    let Some(completion) = app.completion.as_mut() else {
        return;
    };
    if completion.kind != composer::Kind::File {
        return;
    }
    match composer::mention_at(&app.editor.text, app.editor.cursor) {
        Some((start, query)) if start == completion.start => {
            completion.items = match &app.files {
                files::Files::Ready(index) => {
                    index.rank(query).into_iter().map(str::to_owned).collect()
                }
                _ => Vec::new(),
            };
            completion.selected = completion
                .selected
                .min(completion.items.len().saturating_sub(1));
        }
        _ => app.completion = None,
    }
}
/// Puts the selected suggestion into the draft and closes the popup.
fn accept_completion(app: &mut App) {
    let Some(completion) = app.completion.take() else {
        return;
    };
    let Some(item) = completion.items.get(completion.selected) else {
        return;
    };
    let text = match completion.kind {
        composer::Kind::File => composer::mention(item),
        composer::Kind::Path => item.clone(),
        composer::Kind::Command => format!("{item} "),
    };
    if !app.editor.replace(completion.start, &text) {
        app.notice = "Prompt limit reached".into();
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
    matches!(event, octet_core::Event::Approval { .. }) && app.approvals.is_empty()
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
fn write_terminal(bytes: &[u8]) {
    use std::io::Write;
    let mut stdout = io::stdout();
    let _ = stdout.write_all(bytes).and_then(|()| stdout.flush());
}
/// `/copy` and Ctrl+X: the last reply to the clipboard.
fn copy_reply(app: &mut App) {
    let Some((bytes, cut, size)) = app.last_reply().and_then(|text| {
        clipboard::osc52(text).map(|(bytes, cut)| (bytes, cut, text.len().min(clipboard::LIMIT)))
    }) else {
        app.notice = "Nothing to copy yet".into();
        return;
    };
    write_terminal(&bytes);
    app.notice = if cut {
        "Copied the first 100 KB to the clipboard".into()
    } else {
        format!("Copied {} to the clipboard", size_label(size))
    };
}
fn size_label(bytes: usize) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    }
}
async fn send_goal_prompt(app: &mut App, session: &Session, prompt: String) {
    let command = Command::PromptWithDisplay {
        wire: prompt,
        display: app.goals.prompt_display(),
    };
    match session.handle.send(command) {
        Ok(()) => {
            app.goals.goal_prompt_sent();
            app.running = true;
            app.status = "working on goal".into();
        }
        Err(error) => goal_send_failed(app, error).await,
    }
}
/// Pauses the goal whose prompt could not be sent and says why.
async fn goal_send_failed(app: &mut App, error: octet_core::SendError) {
    app.notice(format!("Goal paused: {error}"));
    if let Err(error) = app.goals.send_failed().await {
        app.notice(format!("Goal persistence failed: {error}"));
    }
}
/// Pasted text goes into the draft unless a dialog has focus.
fn paste(app: &mut App, value: &str) {
    if app.approvals.is_empty()
        && !app.help
        && !app.palette
        && !app.editor.insert(&text::clean(value))
    {
        app.notice = "Paste exceeds the 64 KiB prompt limit; draft preserved".into();
    }
    // The file popup follows the pasted text; path and command popups close.
    if app
        .completion
        .as_ref()
        .is_some_and(|completion| completion.kind != composer::Kind::File)
    {
        app.completion = None;
    }
    refresh_completion(app);
}
async fn quit_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
async fn unix_signal(signal: &mut tokio::signal::unix::Signal) {
    signal.recv().await;
}
async fn key_action(app: &mut App, session: &mut Session, key: KeyEvent) -> Action {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    let ctrl_c = ctrl && key.code == KeyCode::Char('c');
    // Any key ends a pending quit; only a second Ctrl+C in time completes it.
    let quit_armed = app.quit_armed.take();
    if quit_armed.is_some() && app.notice == QUIT_HINT {
        app.notice.clear();
    }
    if ctrl && key.code == KeyCode::Char('z') {
        return Action::Suspend;
    }
    if app.help {
        if matches!(key.code, KeyCode::Esc | KeyCode::F(1)) || ctrl_c {
            app.help = false;
        }
        return Action::Continue;
    }
    // Approval keys never leak into the composer. A queued modal takes priority.
    if let Some((id, _)) = app.approvals.front() {
        let answer = match key.code {
            KeyCode::Char('a' | 'A') => Some(true),
            KeyCode::Char('d' | 'D') | KeyCode::Esc => Some(false),
            _ => None,
        };
        if let Some(allow) = answer {
            match session.handle.send(Command::Answer { id: *id, allow }) {
                Ok(()) => {
                    app.approvals.pop_front();
                    app.approval_scroll = 0;
                }
                Err(error) => app.notice = error.to_string(),
            }
        } else if key.code == KeyCode::PageDown {
            app.approval_scroll = app.approval_scroll.saturating_add(8);
        } else if key.code == KeyCode::PageUp {
            app.approval_scroll = app.approval_scroll.saturating_sub(8);
        } else if ctrl_c {
            cancel_turn(app, session).await;
        }
        return Action::Continue;
    }
    if app.palette {
        match key.code {
            _ if ctrl_c => app.palette = false,
            KeyCode::Esc => app.palette = false,
            KeyCode::Up => app.selection = app.selection.saturating_sub(1),
            KeyCode::Down => {
                app.selection = (app.selection + 1).min(view::palette_entries().count() - 1)
            }
            KeyCode::Enter => {
                app.palette = false;
                if let Some((name, _)) = COMMANDS.get(app.selection) {
                    return command(app, name).await;
                }
                return palette_key(app, view::PALETTE_KEYS[app.selection - COMMANDS.len()].0);
            }
            _ => {}
        }
        return Action::Continue;
    }
    if let Some(completion) = app.completion.as_mut() {
        match key.code {
            KeyCode::Up => {
                completion.selected = completion.selected.saturating_sub(1);
                return Action::Continue;
            }
            KeyCode::Down => {
                completion.selected =
                    (completion.selected + 1).min(completion.items.len().saturating_sub(1));
                return Action::Continue;
            }
            KeyCode::Tab | KeyCode::Enter => {
                accept_completion(app);
                return Action::Continue;
            }
            KeyCode::Esc => {
                app.completion = None;
                return Action::Continue;
            }
            // Path and command popups close on any other key; the file popup
            // follows the edit below.
            _ if completion.kind != composer::Kind::File => app.completion = None,
            _ => {}
        }
    }
    match key.code {
        KeyCode::F(1) => app.help = true,
        KeyCode::BackTab => return cycle_mode(app),
        KeyCode::Char('p') if ctrl => app.palette = true,
        KeyCode::Char('x') if ctrl => copy_reply(app),
        KeyCode::Char('g') if ctrl => {
            // The edited draft must not meet a popup about the old one.
            app.completion = None;
            return Action::ExternalEditor;
        }
        KeyCode::Char('u') if ctrl => {
            app.editor.take();
        }
        // Like Claude Code: stop what runs, else clear the draft, else ask
        // for a second press within the window to quit.
        KeyCode::Char('c') if ctrl => {
            if app.shell_running {
                return Action::CancelShell;
            } else if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = "Cancelling…".into();
            } else if !app.editor.text.is_empty() {
                app.editor.take();
            } else if quit_armed.is_some_and(|deadline| Instant::now() < deadline) {
                return Action::Exit(Exit::Quit);
            } else {
                app.quit_armed = Some(Instant::now() + QUIT_WINDOW);
                app.notice = QUIT_HINT.into();
            }
        }
        KeyCode::Esc => {
            if app.shell_running {
                return Action::CancelShell;
            } else if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = "Cancelling…".into();
            } else if app.editor.text.is_empty() && !app.attachments.is_empty() {
                app.attachments.clear();
                app.notice = "Attachments removed".into();
            } else {
                app.scroll = 0;
            }
        }
        KeyCode::PageUp => app.scroll = app.scroll.saturating_add(10).min(65536),
        KeyCode::PageDown => app.scroll = app.scroll.saturating_sub(10),
        KeyCode::End if ctrl => app.scroll = 0,
        KeyCode::Enter if alt || key.modifiers.contains(KeyModifiers::SHIFT) => {
            if !app.editor.insert("\n") {
                app.notice = "Prompt limit reached".into();
            }
        }
        KeyCode::Char('j') if ctrl => {
            if !app.editor.insert("\n") {
                app.notice = "Prompt limit reached".into();
            }
        }
        KeyCode::Enter => {
            let draft = app.editor.text.trim().to_owned();
            if draft.is_empty() {
                return Action::Continue;
            }
            if let Some(rest) = draft.strip_prefix('!') {
                let (attach, command) = match rest.strip_prefix('!') {
                    Some(command) => (false, command.trim()),
                    None => (true, rest.trim()),
                };
                if command.is_empty() {
                    app.notice = "Type a command after !".into();
                    return Action::Continue;
                }
                if app.shell_running {
                    app.notice = "A command is already running".into();
                    return Action::Continue;
                }
                let command = command.to_owned();
                app.editor.take();
                app.history_index = None;
                if app.history.back() != Some(&draft) {
                    app.history.push_back(draft);
                    if app.history.len() > 50 {
                        app.history.pop_front();
                    }
                }
                return Action::RunShell { command, attach };
            }
            // "/usr/lib is broken" is a prompt: no command name contains a slash.
            let path_like = draft
                .split_whitespace()
                .next()
                .and_then(|first| first.strip_prefix('/'))
                .is_some_and(|name| name.contains('/'));
            if draft.starts_with('/') && draft != "/approval-demo" && !path_like {
                return match try_command(app, &draft).await {
                    Some(action) => {
                        app.editor.take();
                        action
                    }
                    None => Action::Continue,
                };
            }
            if !app.ready || app.running || app.stopped {
                app.notice = "Wait for the current turn, press Esc to cancel, or /reconnect".into();
                return Action::Continue;
            }
            let command = if app.attachments.is_empty() {
                Command::Prompt(draft.clone())
            } else {
                let (wire, display) =
                    composer::with_attachments(&draft, &app.attachments, octet_core::PROMPT_LIMIT);
                if wire.len().max(display.len()) > octet_core::PROMPT_LIMIT {
                    app.notice = "The prompt and its attachments are over 64 KiB. Shorten the prompt, or press Esc on an empty prompt to drop them".into();
                    return Action::Continue;
                }
                Command::PromptWithDisplay { wire, display }
            };
            match session.handle.send(command) {
                Ok(()) => {
                    app.attachments.clear();
                    app.goals.user_prompt_sent();
                    app.editor.take();
                    app.running = true;
                    app.status = "sending".into();
                    app.history_index = None;
                    if app.history.back() != Some(&draft) {
                        app.history.push_back(draft);
                        if app.history.len() > 50 {
                            app.history.pop_front();
                        }
                    }
                }
                Err(error) => app.notice = error.to_string(),
            }
        }
        KeyCode::Up if app.editor.text.contains('\n') => app.editor.vertical(false),
        KeyCode::Down if app.editor.text.contains('\n') => app.editor.vertical(true),
        KeyCode::Up => app.recall(true),
        KeyCode::Down => app.recall(false),
        KeyCode::Left => app.editor.left(),
        KeyCode::Right => app.editor.right(),
        KeyCode::Home => app.editor.home(),
        KeyCode::End => app.editor.end(),
        KeyCode::Backspace => app.editor.backspace(),
        KeyCode::Delete => app.editor.delete(),
        KeyCode::Tab => {
            let home = std::env::var_os("HOME").map(PathBuf::from);
            let (text, cursor, root) =
                (app.editor.text.clone(), app.editor.cursor, app.root.clone());
            let tab = off_loop(TAB_WAIT, move || {
                composer::tab(&text, cursor, &root, home.as_deref())
            })
            .await;
            match tab.unwrap_or_else(|| {
                app.notice = "That folder is slow to read; Tab gave up".into();
                composer::Tab::Nothing
            }) {
                composer::Tab::Replace { start, text, popup } => {
                    if app.editor.replace(start, &text) {
                        app.completion = popup;
                    } else {
                        app.notice = "Prompt limit reached".into();
                    }
                }
                composer::Tab::Popup(completion) => app.completion = Some(completion),
                composer::Tab::Mention(start) => open_mentions(app, start),
                composer::Tab::Nothing => {}
            }
        }
        KeyCode::Char(c) if !ctrl && !alt && !app.editor.insert(&c.to_string()) => {
            app.notice = "Prompt limit reached".into();
        }
        _ => {}
    }
    if key.code == KeyCode::Char('@') && app.completion.is_none() {
        if let Some((start, "")) = composer::mention_at(&app.editor.text, app.editor.cursor) {
            open_mentions(app, start);
        }
    }
    refresh_completion(app);
    Action::Continue
}
/// Interrupts the turn; a goal working on it is paused first.
async fn cancel_turn(app: &mut App, session: &Session) {
    if let Err(error) = app.goals.pause_running_turn().await {
        app.notice(format!("Goal persistence failed: {error}"));
    }
    session.handle.interrupt();
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
    let next = if engine == octet_core::Engine::Demo {
        "Restarting the offline demo…"
    } else if session.is_empty() {
        "Starting a new session (no session ID yet)…"
    } else {
        "Reconnecting to the same session…"
    };
    format!("{change} {next}")
}
fn cycle_mode(app: &mut App) -> Action {
    if app.mode == octet_core::Mode::FullAccess {
        app.notice = "Use /mode to leave full access".into();
    } else if app.mode_pending.is_some() {
        app.notice = "Mode change pending; wait for the vendor to confirm".into();
    } else {
        return Action::SetMode(app.mode.cycle());
    }
    Action::Continue
}
/// Runs a slash command. `None` means the name is not a command, so the caller
/// keeps the draft: it may be a prompt that merely starts with a slash.
async fn try_command(app: &mut App, input: &str) -> Option<Action> {
    let (name, argument) = input.split_once(' ').unwrap_or((input, ""));
    let argument = argument.trim();
    match name {
        "/quit" | "/exit" => return Some(Action::Exit(Exit::Quit)),
        "/help" => app.help = true,
        "/copy" => copy_reply(app),
        "/model" => {
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
            } else if app.running || !app.approvals.is_empty() {
                app.notice = "Cancel or finish the current turn before switching models".into();
            } else if app.is_connecting() {
                app.notice = "Wait for connection, or cancel it, before switching models".into();
            } else {
                match octet_core::model::Selection::parse(argument, app.engine) {
                    Ok(selection) => return Some(Action::Exit(Exit::Model(selection))),
                    Err(error) => app.notice(error),
                }
            }
        }
        "/mode" => {
            use octet_core::Mode;
            if argument.is_empty() {
                app.notice(app.mode_details());
            } else if app.mode_pending.is_some() {
                app.notice("Mode change pending; wait for the vendor to confirm");
            } else {
                match Mode::parse(argument) {
                    None => app.notice(format!(
                        "Unknown mode {argument}. Use ask, accept-edits, auto or full-access."
                    )),
                    Some(target) if target == app.mode && target == Mode::FullAccess => {
                        app.notice("Already in full-access mode")
                    }
                    Some(target) if target == Mode::FullAccess || app.mode == Mode::FullAccess => {
                        if app.running || !app.approvals.is_empty() {
                            app.notice =
                                "Cancel or finish the current turn before changing full access"
                                    .into();
                        }
                        // Tightening out of full access is always allowed once the vendor has stopped.
                        else if !app.ready && !(app.stopped && target != Mode::FullAccess) {
                            app.notice =
                                "Wait for a ready session before changing full access".into();
                        } else {
                            return Some(Action::Exit(Exit::Mode(target)));
                        }
                    }
                    Some(target) => return Some(Action::SetMode(target)),
                }
            }
        }
        "/new" | "/reconnect" => {
            if app.running {
                app.notice = "Cancel the active turn before changing sessions".into();
            } else {
                return Some(if name == "/new" {
                    Action::Exit(Exit::New)
                } else {
                    Action::Exit(Exit::Reconnect)
                });
            }
        }
        "/session" => app.notice(format!(
            "{}\nSession: {}\nJournal: {}\nWorkspace: {}",
            app.model_details(),
            if app.session.is_empty() {
                "not assigned"
            } else {
                &app.session
            },
            app.journal.display(),
            app.workspace
        )),
        "/goal" => {
            use octet_core::goal::{Goal, GoalStep};
            let idle = !app.running && app.ready && !app.stopped;
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
                        Ok(prompt) => return Some(Action::GoalPrompt(prompt)),
                        Err(error) => app.notice(error),
                    }
                }
                _ if !idle => app.notice("Wait for a ready, idle session before starting a goal"),
                objective => match app.goals.start(objective).await {
                    Ok(prompt) => return Some(Action::GoalPrompt(prompt)),
                    Err(error) => app.notice(error),
                },
            }
        }
        "/remote-control" => match argument {
            "" | "status" => return Some(Action::RemoteControl),
            _ => app.notice("Use /remote-control or /remote-control status"),
        },
        "/export" => {
            if app.running {
                app.notice = "Wait for completion or cancel before exporting".into();
                return Some(Action::Continue);
            }
            let path = if argument.trim().is_empty() {
                std::env::current_dir()
                    .unwrap_or_default()
                    .join(app.journal.file_name().unwrap_or_default())
            } else {
                PathBuf::from(argument.trim())
            };
            match octet_core::export_journal(&app.journal, &path).await {
                Ok(()) => app.notice(format!("Exported journal to {}", path.display())),
                Err(error) => app.notice(format!("Export failed: {error}")),
            }
        }
        _ => {
            app.notice(format!("Unknown command {name}. Use /help, or edit the draft: only a path such as /usr/lib can start a prompt with a slash."));
            return None;
        }
    }
    Some(Action::Continue)
}
async fn command(app: &mut App, input: &str) -> Action {
    try_command(app, input).await.unwrap_or(Action::Continue)
}

#[cfg(test)]
mod model_tests {
    use super::*;
    use octet_core::Engine;
    fn app() -> App {
        let config = Config::new(Engine::Codex, "codex", "/tmp");
        let mut app = App::new(&config, "journal".into());
        app.ready = true;
        app.session = "thread-1".into();
        app
    }
    #[tokio::test]
    async fn model_command_rejects_busy_switch_and_preserves_current_session() {
        let mut app = app();
        app.running = true;
        assert!(matches!(
            command(&mut app, "/model claude example").await,
            Action::Continue
        ));
        assert_eq!(app.engine, octet_core::Engine::Codex);
        assert_eq!(app.session, "thread-1");
        app.running = false;
        let Action::Exit(Exit::Model(selection)) = command(&mut app, "/model claude example").await
        else {
            panic!("expected a model switch");
        };
        assert_eq!(selection.provider, octet_core::Engine::Claude);
        assert_eq!(selection.model.as_deref(), Some("example"));
    }
    #[tokio::test]
    async fn model_help_and_invalid_syntax_never_restart_a_session() {
        let mut app = app();
        for input in ["/model", "/model unknown model", "/model claude/"] {
            assert!(matches!(command(&mut app, input).await, Action::Continue));
            assert_eq!(app.session, "thread-1");
        }
    }

    #[tokio::test]
    async fn failed_goal_pause_still_says_the_goal_stopped() {
        let dir = octet_testkit::TempDir::new("octet-tui-goal-unwritable");
        // A file where the store expects its directory makes every save fail.
        std::fs::write(dir.path(), b"not a directory").unwrap();
        let mut app = app();
        let store = octet_core::goal::GoalStore::new(dir.path(), std::path::Path::new("/project"));
        assert!(app.goals.attach(store).await.is_err());
        app.goals.goal = Some(octet_core::goal::Goal::new("Ship the project").unwrap());
        assert!(matches!(
            command(&mut app, "/goal pause").await,
            Action::Continue
        ));
        assert!(
            app.notice.starts_with("Goal paused, but saving it failed:"),
            "{}",
            app.notice
        );
        assert!(!app.goals.is_active());
    }
    #[tokio::test]
    async fn pausing_a_completed_goal_preserves_completion() {
        let mut app = app();
        let mut goal = octet_core::goal::Goal::new("Ship the project").unwrap();
        goal.finish_turn(
            &octet_core::Outcome::Completed,
            "Verified tests.\n[[OCTET_GOAL_COMPLETE]]",
        );
        app.goals.goal = Some(goal);
        command(&mut app, "/goal pause").await;
        assert_eq!(
            app.goals.goal.as_ref().unwrap().status,
            octet_core::goal::Status::Complete
        );
        assert!(matches!(
            command(&mut app, "/goal resume").await,
            Action::Continue
        ));
    }
    fn ctrl(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
    }
    #[tokio::test]
    async fn copy_reports_the_size_of_the_last_reply() {
        let mut app = app();
        assert!(matches!(command(&mut app, "/copy").await, Action::Continue));
        assert_eq!(app.notice, "Nothing to copy yet");
        app.event(octet_core::Event::Started);
        app.event(octet_core::Event::Text("hello".into()));
        app.event(octet_core::Event::Finished {
            outcome: octet_core::Outcome::Completed,
        });
        assert_eq!(app.last_reply(), Some("hello"));
        assert!(matches!(command(&mut app, "/copy").await, Action::Continue));
        assert_eq!(app.notice, "Copied 5 B to the clipboard");
    }
    #[test]
    fn sizes_read_naturally() {
        assert_eq!(size_label(5), "5 B");
        assert_eq!(size_label(1229), "1.2 KB");
        assert_eq!(size_label(100 * 1024), "100.0 KB");
    }
    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    fn ran(command: &str, output: &str) -> crate::shell::Ran {
        crate::shell::Ran {
            command: command.into(),
            status: crate::shell::Status::Exited(0),
            output: output.into(),
        }
    }
    async fn next_user_text(session: &mut Session) -> String {
        loop {
            if let octet_core::Event::User(text) = session.events.recv().await.unwrap() {
                return text;
            }
        }
    }
    #[tokio::test]
    async fn at_opens_the_file_popup_and_enter_accepts() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at").await;
        app.files = crate::files::Files::Ready(crate::files::Index::from_paths(vec![
            "README.md".into(),
            "src/main.rs".into(),
        ]));
        for c in "see @mai".chars() {
            key_action(&mut app, &mut session, key(KeyCode::Char(c))).await;
        }
        let completion = app.completion.as_ref().expect("popup open");
        assert_eq!(completion.items, ["src/main.rs"]);
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert_eq!(app.editor.text, "see @src/main.rs ");
        assert!(app.completion.is_none());
        assert!(!app.running, "Enter accepted instead of sending");
    }
    #[tokio::test]
    async fn the_palette_offers_the_editor_mentions_and_shell() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-palette-keys").await;
        let open_at = |app: &mut App, index: usize| {
            app.palette = true;
            app.selection = index;
        };
        open_at(&mut app, COMMANDS.len() + 1);
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert_eq!(app.editor.text, "@");
        assert!(app.completion.is_some(), "@ opens the file popup");
        app.completion = None;
        app.editor.take();
        open_at(&mut app, COMMANDS.len() + 2);
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert_eq!(app.editor.text, "!");
        open_at(&mut app, COMMANDS.len());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::ExternalEditor
        ));
        app.palette = true;
        app.selection = 0;
        for _ in 0..40 {
            key_action(&mut app, &mut session, key(KeyCode::Down)).await;
        }
        assert_eq!(app.selection, COMMANDS.len() + view::PALETTE_KEYS.len() - 1);
    }
    #[tokio::test]
    async fn a_prompt_too_long_for_its_attachments_says_so() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-attach-limit").await;
        app.attach(ran("echo hi", "hi\n"));
        app.editor.set("x".repeat(octet_core::PROMPT_LIMIT - 6));
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert_eq!(
            app.notice,
            "The prompt and its attachments are over 64 KiB. Shorten the prompt, or press Esc on an empty prompt to drop them"
        );
        assert_eq!(app.attachments.len(), 1, "kept");
        assert!(!app.running);
    }
    #[tokio::test]
    async fn slow_work_off_the_loop_gives_up_at_its_limit() {
        let started = std::time::Instant::now();
        let slow = off_loop(Duration::from_millis(50), || {
            std::thread::sleep(Duration::from_secs(2));
            1
        })
        .await;
        assert_eq!(slow, None);
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(off_loop(Duration::from_secs(1), || 5).await, Some(5));
    }
    #[tokio::test]
    async fn the_popup_closes_before_the_editor_and_follows_a_paste() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at-settle").await;
        app.files =
            crate::files::Files::Ready(crate::files::Index::from_paths(vec!["src/main.rs".into()]));
        for c in "a long draft @ma".chars() {
            key_action(&mut app, &mut session, key(KeyCode::Char(c))).await;
        }
        assert!(app.completion.is_some());
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('g')).await,
            Action::ExternalEditor
        ));
        assert!(
            app.completion.is_none(),
            "the edited draft must not meet a stale popup"
        );
        app.editor.set("/re".into());
        app.completion = Some(composer::Completion {
            kind: composer::Kind::Command,
            items: vec!["/reconnect".into()],
            selected: 0,
            start: 0,
        });
        paste(&mut app, "port the bug");
        assert!(app.completion.is_none(), "a paste closes a command popup");
        key_action(&mut app, &mut session, key(KeyCode::Char('x'))).await;
        assert_eq!(app.editor.text, "/report the bugx");
    }
    #[tokio::test]
    async fn the_first_at_asks_for_the_index_and_esc_closes_the_popup() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at-index").await;
        key_action(&mut app, &mut session, key(KeyCode::Char('@'))).await;
        assert!(matches!(app.files, crate::files::Files::Wanted));
        assert!(app.completion.is_some());
        key_action(&mut app, &mut session, key(KeyCode::Esc)).await;
        assert!(app.completion.is_none());
        assert_eq!(app.editor.text, "@");
    }
    #[tokio::test]
    async fn an_at_inside_a_word_opens_nothing() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-at-word").await;
        for c in "me@host".chars() {
            key_action(&mut app, &mut session, key(KeyCode::Char(c))).await;
        }
        assert!(app.completion.is_none());
    }
    #[tokio::test]
    async fn bang_lines_run_locally_and_double_bang_does_not_attach() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-bang").await;
        app.editor.set("!echo hi".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::RunShell { ref command, attach: true } if command == "echo hi"
        ));
        assert!(app.editor.text.is_empty());
        assert_eq!(app.history.back().map(String::as_str), Some("!echo hi"));
        app.editor.set("!!  pwd ".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::RunShell { ref command, attach: false } if command == "pwd"
        ));
        app.editor.set("!".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::Continue
        ));
        assert_eq!(app.notice, "Type a command after !");
        app.shell_running = true;
        app.editor.set("!ls".into());
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Enter)).await,
            Action::Continue
        ));
        assert_eq!(app.notice, "A command is already running");
        assert!(matches!(
            key_action(&mut app, &mut session, key(KeyCode::Esc)).await,
            Action::CancelShell
        ));
    }
    #[tokio::test]
    async fn attachments_go_with_the_next_prompt_then_clear() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-attach").await;
        app.attach(ran("echo hi", "hi\n"));
        app.editor.set("explain".into());
        key_action(&mut app, &mut session, key(KeyCode::Enter)).await;
        assert!(app.attachments.is_empty());
        assert_eq!(next_user_text(&mut session).await, "explain\n\n[+ echo hi]");
    }
    #[tokio::test]
    async fn esc_on_an_empty_idle_draft_removes_attachments() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-detach").await;
        app.attach(ran("ls", ""));
        key_action(&mut app, &mut session, key(KeyCode::Esc)).await;
        assert!(app.attachments.is_empty());
        assert_eq!(app.notice, "Attachments removed");
    }
    #[tokio::test]
    async fn goal_prompts_never_carry_attachments() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-goal-attach").await;
        app.attach(ran("ls", "a\n"));
        send_goal_prompt(&mut app, &session, "continue the goal".into()).await;
        assert_eq!(app.attachments.len(), 1, "kept for the user's next prompt");
        let shown = next_user_text(&mut session).await;
        assert!(!shown.contains("[+ ls]"), "{shown}");
    }
    async fn demo_session(name: &str) -> (octet_testkit::TempDir, Session) {
        let temp = octet_testkit::TempDir::new(name);
        let directory = temp.path().to_path_buf();
        let config = Config::new(Engine::Demo, "demo", directory.clone());
        let session = Session::open(config, directory).await.unwrap();
        (temp, session)
    }
    #[test]
    fn only_the_first_waiting_approval_rings() {
        let mut app = app();
        let approval = |id| octet_core::Event::Approval {
            id,
            detail: "run tests".into(),
        };
        assert!(should_alert(&app, &approval(1)));
        app.event(approval(1));
        assert!(!should_alert(&app, &approval(2)), "one is already waiting");
        assert!(!should_alert(&app, &octet_core::Event::Started));
    }
    #[tokio::test]
    async fn remote_control_reports_without_changing_the_session() {
        let mut app = app();
        assert!(matches!(
            command(&mut app, "/remote-control").await,
            Action::RemoteControl
        ));
        let mut check = None;
        start_remote_check(&mut app, &mut check);
        assert_eq!(app.notice, "Checking phone access…");
        // A second request while one runs starts nothing new.
        start_remote_check(&mut app, &mut check);
        assert_eq!(app.notice, "Phone-access check already running");
        // Under test the checks run stand-in program names that never exist,
        // so the result doesn't depend on what this machine has installed.
        let checks = check.take().unwrap().await.unwrap();
        show_remote_report(&mut app, &checks);
        let text = app.entries_text();
        assert!(text.contains("Remote control setup"), "{text}");
        assert!(text.contains("[!!] Tailscale isn't connected"), "{text}");
        assert!(app.notice.starts_with("Remote control: "), "{}", app.notice);
        assert!(
            app.notice.ends_with("problems (report above)"),
            "{}",
            app.notice
        );
        assert_eq!(app.session, "thread-1");
    }
    #[tokio::test]
    async fn ctrl_c_twice_on_an_idle_empty_prompt_quits() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-quit-twice").await;
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        assert_eq!(app.notice, QUIT_HINT);
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Exit(Exit::Quit)
        ));
        session.shutdown().await;
    }
    #[tokio::test]
    async fn ctrl_c_clears_a_draft_before_it_can_quit() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-quit-draft").await;
        assert!(app.editor.insert("half-written prompt"));
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        assert!(app.editor.text.is_empty());
        assert_ne!(app.notice, QUIT_HINT);
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Exit(Exit::Quit)
        ));
        session.shutdown().await;
    }
    #[tokio::test]
    async fn another_key_or_an_expired_window_disarms_quit() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-quit-disarm").await;
        key_action(&mut app, &mut session, ctrl('c')).await;
        let left = KeyEvent::new(KeyCode::Left, KeyModifiers::NONE);
        key_action(&mut app, &mut session, left).await;
        assert_ne!(app.notice, QUIT_HINT, "another key clears the hint");
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        // The window has passed: the press arms again instead of quitting.
        app.quit_armed = Some(Instant::now() - Duration::from_millis(1));
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        assert_eq!(app.notice, QUIT_HINT);
        session.shutdown().await;
    }
    #[tokio::test]
    async fn ctrl_c_closes_help_and_the_palette_without_quitting() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-quit-overlay").await;
        app.help = true;
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        assert!(!app.help);
        app.palette = true;
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('c')).await,
            Action::Continue
        ));
        assert!(!app.palette);
        assert_ne!(app.notice, QUIT_HINT);
        session.shutdown().await;
    }
    #[tokio::test]
    async fn ctrl_c_interrupts_a_running_turn_instead_of_quitting() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-quit-busy").await;
        app.running = true;
        for _ in 0..2 {
            assert!(matches!(
                key_action(&mut app, &mut session, ctrl('c')).await,
                Action::Continue
            ));
            assert_eq!(app.notice, "Cancelling…");
        }
        session.shutdown().await;
    }
    #[tokio::test]
    async fn ctrl_q_no_longer_quits() {
        let mut app = app();
        let (_temp, mut session) = demo_session("octet-quit-q").await;
        assert!(matches!(
            key_action(&mut app, &mut session, ctrl('q')).await,
            Action::Continue
        ));
        session.shutdown().await;
    }
    #[tokio::test]
    async fn ctrl_c_in_approval_dialog_pauses_the_active_goal() {
        let mut app = app();
        app.goals.goal = Some(octet_core::goal::Goal::new("Ship the project").unwrap());
        app.goals.goal_prompt_sent();
        app.running = true;
        app.approvals.push_back((1, "command".into()));
        let temp = octet_testkit::TempDir::new("octet-goal-cancel");
        let directory = temp.path().to_path_buf();
        let config = Config::new(Engine::Demo, "demo", directory.clone());
        let mut session = Session::open(config, directory.clone()).await.unwrap();
        key_action(
            &mut app,
            &mut session,
            KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
        )
        .await;
        session.shutdown().await;
        assert_eq!(
            app.goals.goal.as_ref().unwrap().status,
            octet_core::goal::Status::Paused
        );
    }
    #[tokio::test]
    async fn goal_commands_start_pause_resume_and_clear_without_a_vendor_turn() {
        let mut app = app();
        assert!(matches!(
            command(&mut app, "/goal Ship the project").await,
            Action::GoalPrompt(_)
        ));
        assert_eq!(
            app.goals.goal.as_ref().unwrap().objective,
            "Ship the project"
        );
        assert!(matches!(
            command(&mut app, "/goal pause").await,
            Action::Continue
        ));
        assert_eq!(
            app.goals.goal.as_ref().unwrap().status,
            octet_core::goal::Status::Paused
        );
        assert!(matches!(
            command(&mut app, "/goal resume").await,
            Action::GoalPrompt(_)
        ));
        let goal = app.goals.goal.as_mut().unwrap();
        goal.status = octet_core::goal::Status::Paused;
        goal.turns = octet_core::goal::MAX_GOAL_TURNS;
        assert!(matches!(
            command(&mut app, "/goal resume").await,
            Action::Continue
        ));
        assert_eq!(
            app.goals.goal.as_ref().unwrap().status,
            octet_core::goal::Status::Paused
        );
        assert!(matches!(
            command(&mut app, "/goal clear").await,
            Action::Continue
        ));
        assert!(app.goals.goal.is_none());
    }
    #[tokio::test]
    async fn mode_command_switches_live_modes_and_rejects_unknown() {
        let mut app = app();
        assert!(matches!(
            command(&mut app, "/mode auto").await,
            Action::SetMode(octet_core::Mode::Auto)
        ));
        app.running = true;
        assert!(matches!(
            command(&mut app, "/mode accept-edits").await,
            Action::SetMode(octet_core::Mode::AcceptEdits)
        ));
        assert!(matches!(
            command(&mut app, "/mode yolo").await,
            Action::Continue
        ));
        assert!(app.notice.contains("Unknown mode"));
        assert!(matches!(command(&mut app, "/mode").await, Action::Continue));
        assert!(app.notice.contains("auto_review"));
    }
    #[tokio::test]
    async fn full_access_requires_idle_ready_session() {
        let mut app = app();
        app.running = true;
        assert!(matches!(
            command(&mut app, "/mode full-access").await,
            Action::Continue
        ));
        app.running = false;
        app.approvals.push_back((1, "x".into()));
        assert!(matches!(
            command(&mut app, "/mode full-access").await,
            Action::Continue
        ));
        app.approvals.clear();
        app.ready = false;
        assert!(matches!(
            command(&mut app, "/mode full-access").await,
            Action::Continue
        ));
        app.ready = true;
        assert!(matches!(
            command(&mut app, "/mode full-access").await,
            Action::Exit(Exit::Mode(octet_core::Mode::FullAccess))
        ));
        app.mode = octet_core::Mode::FullAccess;
        assert!(matches!(
            command(&mut app, "/mode auto").await,
            Action::Exit(Exit::Mode(octet_core::Mode::Auto))
        ));
        assert!(matches!(
            command(&mut app, "/mode full-access").await,
            Action::Continue
        ));
    }
    #[test]
    fn cycle_follows_order_and_never_reaches_full_access() {
        let mut app = app();
        assert!(matches!(
            cycle_mode(&mut app),
            Action::SetMode(octet_core::Mode::AcceptEdits)
        ));
        app.mode = octet_core::Mode::Auto;
        assert!(matches!(
            cycle_mode(&mut app),
            Action::SetMode(octet_core::Mode::Ask)
        ));
        app.mode = octet_core::Mode::FullAccess;
        assert!(matches!(cycle_mode(&mut app), Action::Continue));
        assert!(app.notice.contains("/mode"));
    }
    #[tokio::test]
    async fn cycle_is_ignored_while_a_switch_is_pending() {
        let mut app = app();
        app.mode_pending = Some(octet_core::Mode::AcceptEdits);
        assert!(matches!(cycle_mode(&mut app), Action::Continue));
        assert!(app.notice.contains("pending"));
        assert!(matches!(
            command(&mut app, "/mode auto").await,
            Action::Continue
        ));
        assert!(app.notice.contains("pending"));
    }
    #[tokio::test]
    async fn stopped_session_can_always_leave_full_access() {
        let mut app = app();
        app.mode = octet_core::Mode::FullAccess;
        app.ready = false;
        app.stopped = true;
        assert!(matches!(
            command(&mut app, "/mode ask").await,
            Action::Exit(Exit::Mode(octet_core::Mode::Ask))
        ));
        app.mode = octet_core::Mode::Ask;
        assert!(matches!(
            command(&mut app, "/mode full-access").await,
            Action::Continue
        ));
    }
    #[test]
    fn full_access_notice_says_what_the_reconnect_does() {
        use octet_core::Mode;
        let entering = full_access_notice(Mode::FullAccess, Engine::Claude, "session-1");
        assert!(entering.starts_with("Full access:"), "{entering}");
        assert!(
            entering.ends_with("Reconnecting to the same session…"),
            "{entering}"
        );
        assert!(full_access_notice(Mode::FullAccess, Engine::Claude, "")
            .ends_with("Starting a new session (no session ID yet)…"));
        assert!(
            full_access_notice(Mode::Ask, Engine::Demo, "demo · offline")
                .ends_with("Restarting the offline demo…")
        );
        assert!(full_access_notice(Mode::Ask, Engine::Codex, "thread")
            .starts_with("Leaving full access for ask."));
    }
    #[tokio::test]
    async fn unknown_command_keeps_the_draft() {
        let temp = octet_testkit::TempDir::new("octet-tui-draft");
        let directory = temp.path().to_path_buf();
        let config = Config::new(Engine::Demo, "demo", directory.clone());
        let mut session = Session::open(config.clone(), directory.clone())
            .await
            .unwrap();
        let mut app = App::new(&config, session.journal.clone());
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        assert!(app.editor.insert("/nope is not a command, keep my words"));
        assert!(matches!(
            key_action(&mut app, &mut session, enter).await,
            Action::Continue
        ));
        assert_eq!(app.editor.text, "/nope is not a command, keep my words");
        assert!(app.notice.contains("Unknown command"));
        app.editor.take();
        // A path is a prompt, not a command.
        app.ready = true;
        assert!(app
            .editor
            .insert("/usr/lib is where this breaks, please look"));
        assert!(matches!(
            key_action(&mut app, &mut session, enter).await,
            Action::Continue
        ));
        assert!(app.editor.text.is_empty() && app.running, "{}", app.notice);
        assert_eq!(
            app.history.back().map(String::as_str),
            Some("/usr/lib is where this breaks, please look")
        );
        app.running = false;
        // A prompt starting with a multi-byte character is an ordinary prompt.
        assert!(app.editor.insert("界 means world"));
        assert!(matches!(
            key_action(&mut app, &mut session, enter).await,
            Action::Continue
        ));
        assert_eq!(
            app.history.back().map(String::as_str),
            Some("界 means world")
        );
        app.running = false;
        assert!(app.editor.insert("/session"));
        assert!(matches!(
            key_action(&mut app, &mut session, enter).await,
            Action::Continue
        ));
        assert!(app.editor.text.is_empty());
        session.shutdown().await;
    }
    #[tokio::test]
    async fn reconnect_pauses_an_active_goal_and_names_the_previous_journal() {
        let mut app = app();
        app.goals.goal = Some(octet_core::goal::Goal::new("Ship it").unwrap());
        pause_active_goal(&mut app, "reconnect").await;
        assert_eq!(
            app.goals.goal.as_ref().unwrap().status,
            octet_core::goal::Status::Paused
        );
        assert!(app.notice.contains("Goal paused for reconnect"));
        let previous = std::path::Path::new("/data/session-1.jsonl");
        let resumed = reconnect_notice(true, previous);
        assert!(
            resumed.contains("same vendor session")
                && resumed.ends_with("Previous journal: /data/session-1.jsonl"),
            "{resumed}"
        );
        let fresh = reconnect_notice(false, previous);
        assert!(
            fresh.contains("starts fresh")
                && fresh.contains("Previous journal: /data/session-1.jsonl"),
            "{fresh}"
        );
    }
}
