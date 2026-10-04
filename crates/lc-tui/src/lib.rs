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
    SetMode(lc_core::Mode),
    Mode(lc_core::Mode),
    GoalPrompt(String),
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
    let goal_store = lc_core::goal::GoalStore::new(&directory, &config.cwd);
    loop {
        let mut session = Session::open(config.clone(), directory.clone())
            .await
            .map_err(io::Error::other)?;
        let mut app = retained_app
            .take()
            .unwrap_or_else(|| App::new(&config, session.journal.clone()));
        if app.goal_store.is_none() {
            app.goal_store = Some(goal_store.clone());
            app.goal = goal_store.load().await.map_err(io::Error::other)?;
            if app
                .goal
                .as_ref()
                .is_some_and(|g| g.status == lc_core::goal::Status::Paused)
            {
                app.save_goal().await.map_err(io::Error::other)?;
                app.notice("Stored goal loaded in paused state. Use /goal resume to continue.");
            }
        }
        app.connection(&config, session.journal.clone());
        let result = run_session(&mut terminal, &guard, &mut app, &mut session).await;
        session.shutdown().await;
        match result? {
            Action::Model(selection) => {
                if let Some(goal) = &mut app.goal {
                    if goal.status == lc_core::goal::Status::Active {
                        goal.status = lc_core::goal::Status::Paused;
                        app.save_goal().await.map_err(io::Error::other)?;
                        app.notice("Goal paused for model switch. Use /goal resume to continue.");
                    }
                }
                let cross_provider = selection.provider != config.engine;
                let binary = binaries.get(&selection.provider).cloned();
                let next = selection.configure(&config, &app.session, binary);
                binaries.insert(next.engine.clone(), next.binary.clone());
                app.notice(format!("Model → {} / {}. {} Previous journal: {}",next.engine,next.model.as_deref().unwrap_or("vendor default"),if cross_provider {"New provider context; earlier displayed messages are not sent to this provider."}else{"Resuming the same vendor context."},app.journal.display()));
                config = next;
                retained_app = Some(app);
            }
            Action::Mode(mode) => {
                if let Some(goal) = &mut app.goal {
                    if goal.status == lc_core::goal::Status::Active {
                        goal.status = lc_core::goal::Status::Paused;
                        app.save_goal().await.map_err(io::Error::other)?;
                        app.notice("Goal paused for mode switch. Use /goal resume to continue.");
                    }
                }
                app.notice(if mode == lc_core::Mode::FullAccess {
                    "Full access: the agent can run any command and edit any file without asking. Reconnecting…".to_owned()
                } else {
                    format!("Leaving full access for {}. Reconnecting…", mode.label())
                });
                config.mode = mode;
                if !app.session.is_empty() && config.engine != "demo" {
                    config.resume = Some(app.session.clone());
                }
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
                if let Some(event)=event{
                    if matches!(event,lc_core::Event::Finished{..}|lc_core::Event::Error(_)){last_paint=Instant::now()-frame_time;}
                    if app.goal_running {
                        if let lc_core::Event::Text(text)=&event {
                            app.goal_output.push_str(text);
                            if app.goal_output.len()>64*1024 {
                                let mut start=app.goal_output.len()-64*1024;
                                while !app.goal_output.is_char_boundary(start) {start+=1;}
                                app.goal_output.drain(..start);
                            }
                        }
                    }
                    let outcome=if let lc_core::Event::Finished{outcome}=&event {Some(outcome.clone())} else {None};
                    let failed=matches!(&event,lc_core::Event::Error(_));
                    app.event(event);
                    if failed && app.goal_running {
                        if let Some(goal)=&mut app.goal {goal.status=lc_core::goal::Status::Paused;}
                        app.goal_running=false;
                        if let Err(e)=app.save_goal().await {if let Some(goal)=&mut app.goal {goal.status=lc_core::goal::Status::Paused;}app.notice(format!("Goal persistence failed; paused: {e}"));}
                    }
                    if let Some(outcome)=outcome.filter(|_|app.goal_running) {
                        app.goal_running=false;
                        let continue_goal=app.goal.as_mut().is_some_and(|g|g.finish_turn(&outcome,&app.goal_output));
                        if let Err(e)=app.save_goal().await {if let Some(goal)=&mut app.goal {goal.status=lc_core::goal::Status::Paused;}app.notice(format!("Goal persistence failed; paused: {e}"));}
                        else if continue_goal {
                            if let Some(goal)=&app.goal {
                                let prompt=goal.prompt(false,false);
                                let display=format!("Goal continuation · turn {}",goal.turns+1);
                                match session.handle.send(Command::PromptWithDisplay{wire:prompt,display}) {
                                    Ok(())=>{app.goal_running=true;app.goal_output.clear();app.running=true;app.status="continuing goal".into();}
                                    Err(e)=>{if let Some(g)=&mut app.goal {g.status=lc_core::goal::Status::Paused;}let _=app.save_goal().await;app.notice(format!("Goal paused: {e}"));}
                                }
                            }
                        } else if let Some(goal)=&app.goal {app.notice(goal.summary());}
                    }
                }
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
                match action{
                    Action::Continue=>{},
                    Action::GoalPrompt(prompt)=>match session.handle.send(Command::PromptWithDisplay{wire:prompt,display:app.goal.as_ref().map(|g|format!("Goal: {}",g.objective)).unwrap_or_else(||"Goal audit".into())}) {
                        Ok(())=>{app.goal_running=true;app.goal_output.clear();app.running=true;app.status="working on goal".into();},
                        Err(e)=>{if let Some(goal)=&mut app.goal {goal.status=lc_core::goal::Status::Paused;}let _=app.save_goal().await;app.notice(format!("Goal paused: {e}"));}
                    },
                    Action::Suspend=>{guard.suspend()?;terminal.resize(terminal.size()?.into())?;},
                    Action::SetMode(mode)=>match session.handle.send(Command::SetMode(mode)) {
                        Ok(())=>app.mode_pending=Some(mode),
                        Err(e)=>app.notice(e),
                    },
                    other=>return Ok(other)
                }
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
        KeyCode::BackTab => return cycle_mode(app),
        KeyCode::Char('p') if ctrl => app.palette = true,
        KeyCode::Char('u') if ctrl => {
            app.editor.take();
        }
        KeyCode::Char('c') if ctrl => {
            if app.running || !app.ready && !app.stopped {
                if app.goal_running {
                    if let Some(goal) = &mut app.goal {
                        goal.status = lc_core::goal::Status::Paused;
                    }
                    let _ = app.save_goal().await;
                }
                session.handle.interrupt();
                app.notice = "Cancelling…".into();
            } else {
                app.editor.take();
            }
        }
        KeyCode::Esc => {
            if app.running || !app.ready && !app.stopped {
                if app.goal_running {
                    if let Some(goal) = &mut app.goal {
                        goal.status = lc_core::goal::Status::Paused;
                    }
                    let _ = app.save_goal().await;
                }
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
                    app.goal_running = app
                        .goal
                        .as_ref()
                        .is_some_and(|goal| goal.status == lc_core::goal::Status::Active);
                    app.goal_output.clear();
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
fn cycle_mode(app: &mut App) -> Action {
    if app.mode == lc_core::Mode::FullAccess {
        app.notice = "Use /mode to leave full access".into();
    } else if app.mode_pending.is_some() {
        app.notice = "Mode change pending; wait for the vendor to confirm".into();
    } else {
        return Action::SetMode(app.mode.cycle());
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
        "/mode"=>{
            use lc_core::Mode;
            if argument.is_empty(){app.notice(app.mode_details());}
            else if app.mode_pending.is_some(){app.notice("Mode change pending; wait for the vendor to confirm");}
            else {match Mode::parse(argument){
                None=>app.notice(format!("Unknown mode {argument}. Use ask, accept-edits, auto or full-access.")),
                Some(target) if target==app.mode && target==Mode::FullAccess=>app.notice("Already in full-access mode"),
                Some(target) if target==Mode::FullAccess || app.mode==Mode::FullAccess=>{
                    if app.running||!app.approvals.is_empty(){app.notice="Cancel or finish the current turn before changing full access".into();}
                    else if !app.ready{app.notice="Wait for a ready session before changing full access".into();}
                    else {return Action::Mode(target);}
                },
                Some(target)=>return Action::SetMode(target),
            }}
        },
        "/new"|"/reconnect"=>{if app.running{app.notice="Cancel the active turn before changing sessions".into();}else{return if name=="/new"{Action::New}else{Action::Reconnect};}},
        "/session"=>app.notice(format!("{}\nSession: {}\nJournal: {}\nWorkspace: {}",app.model_details(),if app.session.is_empty(){"not assigned"}else{&app.session},app.journal.display(),app.workspace)),
        "/goal"=>{
            use lc_core::goal::{Goal,Status};
            match argument {
                ""|"status"=>app.notice(app.goal.as_ref().map(Goal::summary).unwrap_or_else(||"No goal set. Use /goal <objective>.".into())),
                "pause"=>{if let Some(goal)=&mut app.goal {goal.status=Status::Paused;app.notice("Goal paused. Current vendor turn may finish; no next turn will start.");}else{app.notice("No goal set");}},
                "clear"=>{app.goal=None;app.goal_running=false;app.notice("Goal cleared. Current vendor turn may finish.");},
                "resume"|"complete"=>{
                    if app.running||!app.ready||app.stopped {app.notice("Wait for a ready, idle session before resuming or auditing a goal");}
                    else if let Some(goal)=&mut app.goal {
                        if goal.status==Status::Complete {app.notice("Goal is already complete; set a new goal to continue.");}
                        else if goal.turns>=lc_core::goal::MAX_GOAL_TURNS {app.notice("Goal reached the 200-turn guard. Set a new goal to continue.");}
                        else {goal.status=Status::Active;let prompt=goal.prompt(false,argument=="complete");if let Err(e)=app.save_goal().await {if let Some(goal)=&mut app.goal {goal.status=Status::Paused;}app.notice(format!("Goal persistence failed; paused: {e}"));return Action::Continue;}else{return Action::GoalPrompt(prompt);}}
                    }else{app.notice("No goal set");}
                },
                objective=>{
                    if app.running||!app.ready||app.stopped {app.notice("Wait for a ready, idle session before starting a goal");}
                    else if app.goal.as_ref().is_some_and(|g|g.status==Status::Active) {app.notice("Pause or clear the active goal before replacing it");}
                    else {match Goal::new(objective){Ok(goal)=>{let prompt=goal.prompt(true,false);app.goal=Some(goal);if let Err(e)=app.save_goal().await {app.goal=None;app.notice(format!("Goal persistence failed: {e}"));return Action::Continue;}else{return Action::GoalPrompt(prompt);}},Err(e)=>app.notice(e)}}
                }
            }
            if let Err(e)=app.save_goal().await {app.notice(format!("Goal persistence failed: {e}"));}
        },
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
            mode: lc_core::Mode::Ask,
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

    #[tokio::test]
    async fn goal_commands_start_pause_resume_and_clear_without_a_vendor_turn() {
        let mut app = app();
        assert!(matches!(
            command(&mut app, "/goal Ship the project").await,
            Action::GoalPrompt(_)
        ));
        assert_eq!(app.goal.as_ref().unwrap().objective, "Ship the project");
        assert!(matches!(
            command(&mut app, "/goal pause").await,
            Action::Continue
        ));
        assert_eq!(
            app.goal.as_ref().unwrap().status,
            lc_core::goal::Status::Paused
        );
        assert!(matches!(
            command(&mut app, "/goal resume").await,
            Action::GoalPrompt(_)
        ));
        let goal = app.goal.as_mut().unwrap();
        goal.status = lc_core::goal::Status::Paused;
        goal.turns = lc_core::goal::MAX_GOAL_TURNS;
        assert!(matches!(
            command(&mut app, "/goal resume").await,
            Action::Continue
        ));
        assert_eq!(
            app.goal.as_ref().unwrap().status,
            lc_core::goal::Status::Paused
        );
        assert!(matches!(
            command(&mut app, "/goal clear").await,
            Action::Continue
        ));
        assert!(app.goal.is_none());
    }
    #[tokio::test]
    async fn mode_command_switches_live_modes_and_rejects_unknown() {
        let mut app = app();
        assert!(matches!(
            command(&mut app, "/mode auto").await,
            Action::SetMode(lc_core::Mode::Auto)
        ));
        app.running = true;
        assert!(matches!(
            command(&mut app, "/mode accept-edits").await,
            Action::SetMode(lc_core::Mode::AcceptEdits)
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
            Action::Mode(lc_core::Mode::FullAccess)
        ));
        app.mode = lc_core::Mode::FullAccess;
        assert!(matches!(
            command(&mut app, "/mode auto").await,
            Action::Mode(lc_core::Mode::Auto)
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
            Action::SetMode(lc_core::Mode::AcceptEdits)
        ));
        app.mode = lc_core::Mode::Auto;
        assert!(matches!(
            cycle_mode(&mut app),
            Action::SetMode(lc_core::Mode::Ask)
        ));
        app.mode = lc_core::Mode::FullAccess;
        assert!(matches!(cycle_mode(&mut app), Action::Continue));
        assert!(app.notice.contains("/mode"));
    }
    #[tokio::test]
    async fn cycle_is_ignored_while_a_switch_is_pending() {
        let mut app = app();
        app.mode_pending = Some(lc_core::Mode::AcceptEdits);
        assert!(matches!(cycle_mode(&mut app), Action::Continue));
        assert!(app.notice.contains("pending"));
        assert!(matches!(
            command(&mut app, "/mode auto").await,
            Action::Continue
        ));
        assert!(app.notice.contains("pending"));
    }
}
