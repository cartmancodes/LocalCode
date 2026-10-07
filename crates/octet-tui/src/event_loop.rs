//! One session's event loop. `wake` is the only `select!`: each arm only
//! returns what woke the loop, so every arm is plainly cancel-safe, and the
//! work is done by named handlers afterwards.
use crate::{
    Action, Exit, Signals,
    app::{App, StatusKind},
    external, files,
    input::{key_action, paste, refresh_completion},
    jobs, session_event, shell, shell_finished,
    terminal::{InputReader, TerminalGuard, fit, regain_terminal},
    view,
};
use crossterm::event::{Event as Input, KeyEventKind};
use octet_core::{Event, Session};
use ratatui::{Terminal, backend::CrosstermBackend};
use std::{
    future::Future,
    io::{self, Stdout},
    process::ExitStatus,
    time::Duration,
};
use tokio::time::Instant;

/// How often the screen may repaint (about 30 Hz).
const FRAME_TIME: Duration = Duration::from_millis(33);

/// What woke the loop.
enum Wake {
    /// A frame is due.
    Paint,
    /// The "Press Ctrl+C again" window closed.
    QuitHintExpired,
    /// The vendor sent an event; `None` once the session has stopped.
    Vendor(Option<Event>),
    /// The background job ended.
    Job(jobs::Ended),
    /// The `@` index was built, or its thread went away.
    Index(Option<files::Index>),
    /// The `!` command ended.
    Shell(Result<shell::Ran, shell::ShellError>),
    /// The external editor exited.
    Editor(io::Result<ExitStatus>),
    /// A key, paste or resize; `None` once the terminal closed.
    Input(Option<io::Result<Input>>),
    /// SIGINT.
    Interrupt,
    /// SIGTERM or SIGHUP: stop now.
    Terminate,
    /// SIGTSTP.
    Suspend,
}

/// Awaits `future` if there is one; otherwise waits forever, so a `select!`
/// arm with nothing to wait for never fires.
async fn maybe<F: Future + Unpin>(future: Option<&mut F>) -> F::Output {
    match future {
        Some(future) => future.await,
        None => std::future::pending().await,
    }
}

/// The terminal side of one session: what the loop owns besides the app.
struct Loop<'t> {
    terminal: &'t mut Terminal<CrosstermBackend<Stdout>>,
    guard: &'t TerminalGuard,
    /// Keys; `None` while the external editor has the terminal.
    input: Option<InputReader>,
    /// The external editor, while it has the terminal.
    editing: Option<(tokio::process::Child, external::Edit)>,
    dirty: bool,
    last_paint: Instant,
    /// The session's events can still arrive.
    events_open: bool,
}

/// Runs one session until the user quits or asks for a reconnect.
pub(crate) async fn run_session(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    guard: &TerminalGuard,
    app: &mut App,
    session: &mut Session,
    signals: &mut Signals,
) -> io::Result<Exit> {
    let mut lp = Loop {
        terminal,
        guard,
        input: Some(InputReader::new()),
        editing: None,
        dirty: true,
        last_paint: Instant::now() - Duration::from_secs(1),
        events_open: true,
    };
    loop {
        lp.paint(app)?;
        let wake = lp.wake(app, session, signals).await;
        if let Some(exit) = lp.handle(wake, app, session).await? {
            return Ok(exit);
        }
    }
}

impl Loop<'_> {
    /// Draws a frame if one is due and nothing else has the terminal.
    fn paint(&mut self, app: &mut App) -> io::Result<()> {
        if self.dirty && self.editing.is_none() && self.last_paint.elapsed() >= FRAME_TIME {
            self.terminal.draw(|f| view::draw(f, app))?;
            self.last_paint = Instant::now();
            self.dirty = false;
        }
        Ok(())
    }

    /// Waits for the next thing to handle. Each arm only says what woke it.
    async fn wake(&mut self, app: &mut App, session: &mut Session, signals: &mut Signals) -> Wake {
        let frame_due = self.last_paint + FRAME_TIME;
        let paint_pending = self.dirty && self.editing.is_none();
        let quit_deadline = app.quit_armed;
        // Disjoint borrows, one per arm.
        let job = &mut app.job;
        let shell = &mut app.shell;
        let index = match &mut app.composer.files {
            files::Files::Building(index) => Some(index),
            _ => None,
        };
        let editing = &mut self.editing;
        let input = &mut self.input;
        tokio::select! {
            () = tokio::time::sleep_until(frame_due), if paint_pending => Wake::Paint,
            () = tokio::time::sleep_until(quit_deadline.unwrap_or(frame_due)),
                if quit_deadline.is_some() => Wake::QuitHintExpired,
            event = session.events.recv(), if self.events_open => Wake::Vendor(event),
            ended = async {
                match job.as_mut() {
                    Some(running) => running.wait().await,
                    None => std::future::pending().await,
                }
            } => Wake::Job(ended),
            built = maybe(index) => Wake::Index(built.ok()),
            ran = async {
                match shell.as_mut() {
                    Some(running) => running.wait().await,
                    None => std::future::pending().await,
                }
            } => Wake::Shell(ran),
            status = async {
                match editing.as_mut() {
                    Some((child, _)) => child.wait().await,
                    None => std::future::pending().await,
                }
            } => Wake::Editor(status),
            key = async {
                match input.as_mut() {
                    Some(reader) => reader.events.recv().await,
                    None => std::future::pending().await,
                }
            } => Wake::Input(key),
            _ = signals.interrupt.recv() => Wake::Interrupt,
            _ = signals.term.recv() => Wake::Terminate,
            _ = signals.hup.recv() => Wake::Terminate,
            _ = signals.suspend.recv() => Wake::Suspend,
        }
    }

    /// Acts on what woke the loop; `Some` ends the session.
    async fn handle(
        &mut self,
        wake: Wake,
        app: &mut App,
        session: &Session,
    ) -> io::Result<Option<Exit>> {
        match wake {
            Wake::Paint => return Ok(None),
            Wake::QuitHintExpired => {
                app.quit_armed = None;
                app.clear_status(StatusKind::QuitHint);
            }
            Wake::Vendor(Some(event)) => {
                // Turn ends paint at once rather than waiting for the frame timer.
                if matches!(event, Event::Finished { .. } | Event::Error(_)) {
                    self.last_paint = Instant::now() - FRAME_TIME;
                }
                session_event(app, &session.handle, event).await;
            }
            Wake::Vendor(None) => {
                self.events_open = false;
                app.event(Event::Stopped);
            }
            Wake::Job(ended) => {
                app.job = None;
                jobs::apply(app, ended);
            }
            Wake::Index(built) => {
                app.composer.files = built.map_or(files::Files::Unbuilt, files::Files::Ready);
                refresh_completion(app);
            }
            Wake::Shell(ran) => shell_finished(app, ran),
            Wake::Editor(status) => self.finish_editor(app, &status)?,
            Wake::Input(Some(input)) => return self.input(app, session, input?).await,
            // While an editor waits in cooked mode, Ctrl+C is meant for it.
            Wake::Interrupt if self.editing.is_some() => {}
            Wake::Input(None) | Wake::Interrupt => return Ok(Some(Exit::Quit)),
            Wake::Terminate => {
                self.stop_editor().await;
                return Ok(Some(Exit::Quit));
            }
            Wake::Suspend => self.suspend()?,
        }
        self.dirty = true;
        Ok(None)
    }

    /// A key, paste or resize.
    async fn input(
        &mut self,
        app: &mut App,
        session: &Session,
        input: Input,
    ) -> io::Result<Option<Exit>> {
        let action = match input {
            Input::Key(key) if key.kind != KeyEventKind::Release => {
                key_action(app, &session.handle, key).await
            }
            Input::Paste(value) => {
                paste(app, &value);
                Action::Continue
            }
            Input::Resize(_, _) => {
                fit(self.terminal)?;
                Action::Continue
            }
            _ => Action::Continue,
        };
        let exit = self.apply(action, app)?;
        // A key that asked for the `@` index starts its build.
        if matches!(app.composer.files, files::Files::Wanted) {
            app.composer.files = files::Files::build(app.composer.root.clone());
        }
        self.dirty = true;
        Ok(exit)
    }

    /// Carries out what a key or command asked for.
    fn apply(&mut self, action: Action, app: &mut App) -> io::Result<Option<Exit>> {
        match action {
            Action::Continue => {}
            // The command checked that no job is running.
            Action::Job(next) => {
                app.hint(next.label);
                app.job = Some(jobs::Running::spawn(next));
            }
            Action::ExternalEditor => self.start_editor(app)?,
            Action::RunShell { command, attach } => {
                app.hint(format!("Running {command} · Esc to stop"));
                app.shell = Some(shell::Running::spawn(
                    command,
                    app.composer.root.clone(),
                    attach,
                ));
            }
            Action::CancelShell => {
                if let Some(running) = app.shell.as_mut() {
                    running.cancel();
                }
            }
            Action::Suspend => self.suspend()?,
            Action::Exit(exit) => return Ok(Some(exit)),
        }
        Ok(None)
    }

    /// Hands the terminal to the user's editor for the draft.
    fn start_editor(&mut self, app: &mut App) -> io::Result<()> {
        let edit = match external::prepare(app.composer.editor.text(), &external::editor_command())
        {
            Ok(edit) => edit,
            Err(error) => {
                app.error(error.to_string());
                return Ok(());
            }
        };
        // Stop reading keys so the editor gets them all.
        self.input = None;
        TerminalGuard::restore();
        let (program, args) = edit.command();
        match tokio::process::Command::new(program)
            .args(args)
            .kill_on_drop(true)
            .spawn()
        {
            Ok(child) => self.editing = Some((child, edit)),
            Err(error) => {
                regain_terminal(self.guard, self.terminal, &mut self.input)?;
                app.note(format!("Cannot start {}: {error}", edit.editor));
            }
        }
        Ok(())
    }

    /// The editor exited: takes the terminal back, and the edited draft.
    fn finish_editor(&mut self, app: &mut App, status: &io::Result<ExitStatus>) -> io::Result<()> {
        let Some((_, edit)) = self.editing.take() else {
            return Ok(());
        };
        // The editor may have changed the terminal behind crossterm's record
        // of it; clear the record first.
        let _ = crossterm::terminal::disable_raw_mode();
        regain_terminal(self.guard, self.terminal, &mut self.input)?;
        let code = status.as_ref().ok().and_then(ExitStatus::code);
        match edit.finish(code) {
            Ok(text) => app.composer.editor.set(text),
            Err(error) => app.error(error.to_string()),
        }
        Ok(())
    }

    /// Ctrl+Z or SIGTSTP: suspends to the shell. While the editor has the
    /// terminal it restores it on `fg`; Octet only stops alongside it.
    fn suspend(&mut self) -> io::Result<()> {
        if self.editing.is_some() {
            // SAFETY: raise only delivers SIGSTOP to this process.
            unsafe {
                libc::raise(libc::SIGSTOP);
            }
            return Ok(());
        }
        self.guard.suspend()?;
        fit(self.terminal)
    }

    /// Asks a running editor to quit, so it can put the terminal back, and
    /// kills it if it has not within a second.
    async fn stop_editor(&mut self) {
        let Some((child, _)) = self.editing.as_mut() else {
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
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn maybe_waits_forever_on_none() {
        let none: Option<&mut std::future::Ready<u8>> = None;
        let waited = tokio::time::timeout(Duration::from_millis(50), maybe(none)).await;
        assert!(waited.is_err(), "nothing to wait for must never finish");
        let mut ready = std::future::ready(7);
        assert_eq!(maybe(Some(&mut ready)).await, 7);
    }
}
