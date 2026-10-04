mod editor;
mod mascot;
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
    fn suspend(&self) -> io::Result<()> {
        Self::restore();
        // SAFETY: raise only delivers SIGSTOP to this process.
        unsafe {
            libc::raise(libc::SIGSTOP);
        }
        enable_raw_mode()?;
        execute!(io::stdout(), EnterAlternateScreen, EnableBracketedPaste)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        Self::restore();
    }
}
/// What a key or command asks the session loop to do.
enum Action {
    Continue,
    Suspend,
    SetMode(octet_core::Mode),
    GoalPrompt(String),
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
async fn run_session(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    guard: &TerminalGuard,
    app: &mut App,
    session: &mut Session,
) -> io::Result<Exit> {
    let mut input = InputReader::new();
    let mut dirty = true;
    let mut last_paint = Instant::now() - Duration::from_secs(1);
    let frame_time = Duration::from_millis(33);
    let mut events_open = true;
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut hup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
    let mut suspend =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::from_raw(libc::SIGTSTP))?;
    loop {
        if dirty && last_paint.elapsed() >= frame_time {
            terminal.draw(|f| view::draw(f, app))?;
            last_paint = Instant::now();
            dirty = false;
        }
        tokio::select! {
            _ = tokio::time::sleep_until(last_paint + frame_time), if dirty => {}
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
            event = input.events.recv() => {
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
                dirty = true;
            }
            _ = quit_signal() => return Ok(Exit::Quit),
            _ = unix_signal(&mut term) => return Ok(Exit::Quit),
            _ = unix_signal(&mut hup) => return Ok(Exit::Quit),
            _ = unix_signal(&mut suspend) => {
                guard.suspend()?;
                terminal.resize(terminal.size()?.into())?;
                dirty = true;
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
    if ctrl && key.code == KeyCode::Char('q') {
        return Action::Exit(Exit::Quit);
    }
    if ctrl && key.code == KeyCode::Char('z') {
        return Action::Suspend;
    }
    if app.help {
        if matches!(key.code, KeyCode::Esc | KeyCode::F(1)) {
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
        } else if ctrl && key.code == KeyCode::Char('c') {
            cancel_turn(app, session).await;
        }
        return Action::Continue;
    }
    if app.palette {
        match key.code {
            KeyCode::Esc => app.palette = false,
            KeyCode::Up => app.selection = app.selection.saturating_sub(1),
            KeyCode::Down => app.selection = (app.selection + 1).min(COMMANDS.len() - 1),
            KeyCode::Enter => {
                app.palette = false;
                return command(app, COMMANDS[app.selection].0).await;
            }
            _ => {}
        }
        return Action::Continue;
    }
    match key.code {
        KeyCode::F(1) => app.help = true,
        KeyCode::BackTab => return cycle_mode(app),
        KeyCode::Char('p') if ctrl => app.palette = true,
        KeyCode::Char('u') if ctrl => {
            app.editor.take();
        }
        KeyCode::Char('c') if ctrl => {
            if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = "Cancelling…".into();
            } else {
                app.editor.take();
            }
        }
        KeyCode::Esc => {
            if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = "Cancelling…".into();
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
            match session.handle.send(Command::Prompt(draft.clone())) {
                Ok(()) => {
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
        KeyCode::Char(c) if !ctrl && !alt && !app.editor.insert(&c.to_string()) => {
            app.notice = "Prompt limit reached".into();
        }
        _ => {}
    }
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
