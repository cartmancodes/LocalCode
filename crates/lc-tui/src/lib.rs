mod editor;
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
use lc_core::{Command, Config, Session};
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
        #[cfg(unix)]
        unsafe {
            libc::kill(libc::getpid(), libc::SIGSTOP);
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
enum Action {
    Continue,
    Quit,
    New,
    Reconnect,
    Suspend,
    Model(lc_core::model::Selection),
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
    let mut binaries =
        std::collections::HashMap::from([(config.engine.clone(), config.binary.clone())]);
    loop {
        let mut session = Session::open(config.clone(), directory.clone())
            .await
            .map_err(io::Error::other)?;
        let mut app = retained_app
            .take()
            .unwrap_or_else(|| App::new(&config, session.journal.clone()));
        app.connection(&config, session.journal.clone());
        let result = run_session(&mut terminal, &guard, &mut app, &mut session).await;
        session.shutdown().await;
        match result? {
            Action::Model(selection) => {
                let cross_provider = selection.provider != config.engine;
                let binary = binaries.get(&selection.provider).cloned();
                let next = selection.configure(&config, &app.session, binary);
                binaries.insert(next.engine.clone(), next.binary.clone());
                app.notice(format!("Model → {} / {}. {} Previous journal: {}",next.engine,next.model.as_deref().unwrap_or("vendor default"),if cross_provider {"New provider context; earlier displayed messages are not sent to this provider."}else{"Resuming the same vendor context."},app.journal.display()));
                config = next;
                retained_app = Some(app);
            }
            Action::New => config.resume = None,
            Action::Reconnect => {
                if !app.session.is_empty() && config.engine != "demo" {
                    config.resume = Some(app.session);
                }
            }
            _ => break,
        }
    }
    drop(terminal);
    drop(guard);
    Ok(())
}
async fn run_session(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    guard: &TerminalGuard,
    app: &mut App,
    session: &mut Session,
) -> io::Result<Action> {
    let mut input = InputReader::new();
    let mut dirty = true;
    let mut last_paint = Instant::now() - Duration::from_secs(1);
    let frame_time = Duration::from_millis(33);
    let mut events_open = true;
    #[cfg(unix)]
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    #[cfg(unix)]
    let mut hup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
    #[cfg(unix)]
    let mut suspend =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::from_raw(libc::SIGTSTP))?;
    loop {
        if dirty && last_paint.elapsed() >= frame_time {
            terminal.draw(|f| view::draw(f, app))?;
            last_paint = Instant::now();
            dirty = false;
        }
        tokio::select! {
            _=tokio::time::sleep_until(last_paint+frame_time),if dirty=>{},
            event=session.events.recv(),if events_open=>{
                if let Some(event)=event{if matches!(event,lc_core::Event::Finished{..}|lc_core::Event::Error(_)){last_paint=Instant::now()-frame_time;}app.event(event);}
                else{events_open=false;app.event(lc_core::Event::Stopped);}
                dirty=true;
            },
            event=input.events.recv()=>{
                let action=match event{
                    Some(Ok(Input::Key(key))) if key.kind!=KeyEventKind::Release=>key_action(app,session,key).await,
                    Some(Ok(Input::Paste(value)))=>{if app.approvals.is_empty()&&!app.help&&!app.palette&&!app.editor.insert(&text::clean(&value)){app.notice="Paste exceeds the 64 KiB prompt limit; draft preserved".into();}Action::Continue},
                    Some(Ok(Input::Resize(_,_)))=>{terminal.resize(terminal.size()?.into())?;Action::Continue},
                    Some(Err(e))=>return Err(e),None=>return Ok(Action::Quit),_=>Action::Continue
                };
                match action{Action::Continue=>{},Action::Suspend=>{guard.suspend()?;terminal.resize(terminal.size()?.into())?;},other=>return Ok(other)}
                dirty=true;
            },
            _=quit_signal()=>return Ok(Action::Quit),
            _=unix_signal(&mut term)=>return Ok(Action::Quit),
            _=unix_signal(&mut hup)=>return Ok(Action::Quit),
            _=unix_signal(&mut suspend)=>{guard.suspend()?;terminal.resize(terminal.size()?.into())?;dirty=true;}
        }
    }
}
async fn quit_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
#[cfg(unix)]
async fn unix_signal(signal: &mut tokio::signal::unix::Signal) {
    signal.recv().await;
}
async fn key_action(app: &mut App, session: &mut Session, key: KeyEvent) -> Action {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    if ctrl && key.code == KeyCode::Char('q') {
        return Action::Quit;
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
                Err(error) => app.notice = error,
            }
        } else if key.code == KeyCode::PageDown {
            app.approval_scroll = app.approval_scroll.saturating_add(8);
        } else if key.code == KeyCode::PageUp {
            app.approval_scroll = app.approval_scroll.saturating_sub(8);
        } else if ctrl && key.code == KeyCode::Char('c') {
            session.handle.interrupt();
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
        KeyCode::Char('p') if ctrl => app.palette = true,
        KeyCode::Char('u') if ctrl => {
            app.editor.take();
        }
        KeyCode::Char('c') if ctrl => {
            if app.running || !app.ready && !app.stopped {
                session.handle.interrupt();
                app.notice = "Cancelling…".into();
            } else {
                app.editor.take();
            }
        }
        KeyCode::Esc => {
            if app.running || !app.ready && !app.stopped {
                session.handle.interrupt();
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
            if draft.starts_with('/') && draft != "/approval-demo" {
                let result = command(app, &draft).await;
                app.editor.take();
                return result;
            }
            if !app.ready || app.running || app.stopped {
                app.notice = "Wait for the current turn, press Esc to cancel, or /reconnect".into();
                return Action::Continue;
            }
            match session.handle.send(Command::Prompt(draft.clone())) {
                Ok(()) => {
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
                Err(error) => app.notice = error,
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
async fn command(app: &mut App, input: &str) -> Action {
    let (name, argument) = input.split_once(' ').unwrap_or((input, ""));
    let argument = argument.trim();
    match name{
        "/quit"|"/exit"=>return Action::Quit,
        "/help"=>app.help=true,
        "/model"=>{
            if argument.trim().is_empty(){app.show_models(1);}
            else if argument=="list" || argument.starts_with("list ") {
                match argument.strip_prefix("list").unwrap_or("").trim() { ""=>app.show_models(1), page=>match page.parse::<usize>() {Ok(page)=>app.show_models(page),Err(_)=>app.notice("Use /model list <page number>")} }
            }
            else if app.running||!app.approvals.is_empty(){app.notice="Cancel or finish the current turn before switching models".into();}
            else if !app.ready&&!app.stopped{app.notice="Wait for connection, or cancel it, before switching models".into();}
            else {match lc_core::model::Selection::parse(argument,&app.engine){Ok(selection)=>return Action::Model(selection),Err(error)=>app.notice(error)}}
        },
        "/new"|"/reconnect"=>{if app.running{app.notice="Cancel the active turn before changing sessions".into();}else{return if name=="/new"{Action::New}else{Action::Reconnect};}},
        "/session"=>app.notice(format!("{}\nSession: {}\nJournal: {}\nWorkspace: {}",app.model_details(),if app.session.is_empty(){"not assigned"}else{&app.session},app.journal.display(),app.workspace)),
        "/export"=>{
            if app.running{app.notice="Wait for completion or cancel before exporting".into();return Action::Continue;}
            let path=if argument.trim().is_empty(){std::env::current_dir().unwrap_or_default().join(app.journal.file_name().unwrap_or_default())}else{PathBuf::from(argument.trim())};
            match lc_core::export_journal(&app.journal,&path).await{Ok(())=>app.notice(format!("Exported journal to {}",path.display())),Err(error)=>app.notice(format!("Export failed: {error}"))}
        },
        _=>app.notice(format!("Unknown command {name}. Use /help. The preview does not implement every legacy command yet."))
    }
    Action::Continue
}

#[cfg(test)]
mod model_tests {
    use super::*;
    fn app() -> App {
        let config = Config {
            engine: "codex".into(),
            binary: "codex".into(),
            cwd: "/tmp".into(),
            model: None,
            resume: None,
        };
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
        assert_eq!(app.engine, "codex");
        assert_eq!(app.session, "thread-1");
        app.running = false;
        assert!(
            matches!(command(&mut app,"/model claude example").await,Action::Model(selection) if selection.provider=="claude" && selection.model.as_deref()==Some("example"))
        );
    }
    #[tokio::test]
    async fn model_help_and_invalid_syntax_never_restart_a_session() {
        let mut app = app();
        for input in ["/model", "/model unknown model", "/model claude/"] {
            assert!(matches!(command(&mut app, input).await, Action::Continue));
            assert_eq!(app.session, "thread-1");
        }
    }
}
