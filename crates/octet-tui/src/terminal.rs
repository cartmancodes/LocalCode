//! The terminal itself: raw mode and the alternate screen, the key reader
//! thread, and control bytes written outside a frame.
use crossterm::{
    event::{DisableBracketedPaste, EnableBracketedPaste, Event as Input},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, backend::CrosstermBackend};
use std::{
    io::{self, Stdout},
    time::Duration,
};
// One thread owns both poll and read. A finite poll deadline avoids stale
// wakeups after SIGCONT without adding any timer to the render loop.
// Crossterm use-dev-tty selects level-triggered poll: resize and keyboard
// readiness cannot consume each other's edge notification.
pub(crate) struct InputReader {
    pub(crate) events: tokio::sync::mpsc::Receiver<io::Result<Input>>,
    stopping: std::sync::Arc<std::sync::atomic::AtomicBool>,
    task: Option<std::thread::JoinHandle<()>>,
}
impl InputReader {
    pub(crate) fn new() -> Self {
        use std::sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
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
    /// Joins the reader thread, which sees `stopping` within its poll
    /// interval: this can block the runtime thread for up to about 100 ms,
    /// once per editor start or session end, which is acceptable.
    fn drop(&mut self) {
        self.stopping
            .store(true, std::sync::atomic::Ordering::Relaxed);
        self.events.close();
        if let Some(task) = self.task.take() {
            let _ = task.join();
        }
    }
}
pub(crate) struct TerminalGuard;
impl TerminalGuard {
    pub(crate) fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        if let Err(e) = execute!(io::stdout(), EnterAlternateScreen, EnableBracketedPaste) {
            Self::restore();
            return Err(e);
        }
        Ok(Self)
    }
    pub(crate) fn restore() {
        let _ = execute!(
            io::stdout(),
            DisableBracketedPaste,
            LeaveAlternateScreen,
            crossterm::cursor::Show
        );
        let _ = disable_raw_mode();
    }
    #[expect(
        clippy::unused_self,
        reason = "taking the guard proves the terminal was entered first"
    )]
    pub(crate) fn resume(&self) -> io::Result<()> {
        enable_raw_mode()?;
        execute!(io::stdout(), EnterAlternateScreen, EnableBracketedPaste)
    }
    pub(crate) fn suspend(&self) -> io::Result<()> {
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
/// Sizes ratatui to the terminal again after something else used it.
pub(crate) fn fit(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
    terminal.resize(terminal.size()?.into())
}
/// Takes the terminal back from the editor: raw mode, a fresh frame and a
/// new key reader.
pub(crate) fn regain_terminal(
    guard: &TerminalGuard,
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    input: &mut Option<InputReader>,
) -> io::Result<()> {
    guard.resume()?;
    fit(terminal)?;
    *input = Some(InputReader::new());
    Ok(())
}
/// A bell plus a desktop notification (OSC 9) for an approval the user isn't
/// watching. The bell passes through tmux and mosh to a phone; tmux drops the
/// OSC 9, which helps only a desktop terminal connected directly. Write errors
/// are ignored: the alert is a courtesy, never a reason to stop.
const ALERT: &[u8] = b"\x07\x1b]9;Octet: approval needed\x07";
pub(crate) fn alert() {
    write_terminal(ALERT);
}
/// Writes control bytes straight to the terminal, outside a frame. Errors are
/// ignored: these are courtesies, never a reason to stop.
pub(crate) fn write_terminal(bytes: &[u8]) {
    // Tests capture these bytes: written for real they would reach the
    // terminal running the tests (an OSC 52 would replace its clipboard).
    #[cfg(test)]
    WRITTEN.with(|written| written.borrow_mut().extend_from_slice(bytes));
    #[cfg(not(test))]
    {
        use std::io::Write;
        let mut stdout = io::stdout();
        let _ = stdout.write_all(bytes).and_then(|()| stdout.flush());
    }
}
#[cfg(test)]
thread_local! {
    /// What `write_terminal` would have written, in this test's thread.
    pub(crate) static WRITTEN: std::cell::RefCell<Vec<u8>> = const { std::cell::RefCell::new(Vec::new()) };
}
