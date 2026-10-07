//! Keys, paste and the prompt box's popups: what each key does in each state.
use crate::{
    Action, Exit, QUIT_HINT, QUIT_WINDOW,
    app::{App, CANCELLING, QUEUE_LIMIT, StatusKind},
    clipboard,
    commands::{command, try_command},
    composer, files,
    registry::COMMANDS,
    text, view, write_terminal,
};
use crate::{
    commands::switch_mode,
    vendor::{By, Prompt, Vendor},
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use octet_core::Command;
use std::fmt::Write as _;
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::time::Instant;

/// How long Tab waits for a folder listing.
pub(crate) const TAB_WAIT: Duration = Duration::from_millis(500);
/// Runs blocking `work` on its own thread, waiting at most `limit`. A plain
/// thread, not `spawn_blocking`, so work stuck on a dead mount cannot hold up
/// the runtime's shutdown.
pub(crate) async fn off_loop<T: Send + 'static>(
    limit: Duration,
    work: impl FnOnce() -> T + Send + 'static,
) -> Option<T> {
    let (done, result) = tokio::sync::oneshot::channel();
    std::thread::spawn(move || {
        let _ = done.send(work());
    });
    tokio::time::timeout(limit, result).await.ok()?.ok()
}
/// Marks a Tab listing finished when dropped, however the listing ends.
struct Listing(Arc<AtomicBool>);
impl Drop for Listing {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}
/// A key chosen from the palette.
pub(crate) fn palette_key(app: &mut App, key: &str) -> Action {
    match key {
        "Ctrl+G" => {
            app.composer.completion = None;
            return Action::ExternalEditor;
        }
        "@" => {
            let after_word = app.composer.editor.text()[..app.composer.editor.cursor()]
                .chars()
                .next_back()
                .is_some_and(|c| !c.is_whitespace());
            let mention = if after_word { " @" } else { "@" };
            if app.insert_or_warn(mention)
                && let Some((start, _)) =
                    composer::mention_at(app.composer.editor.text(), app.composer.editor.cursor())
            {
                open_mentions(app, start);
            }
        }
        _ if app.composer.editor.text().is_empty() => app.composer.editor.set("!".into()),
        _ => app.hint("Clear the prompt to start a ! command"),
    }
    Action::Continue
}
/// Opens the `@` popup for the mention starting at `start`, asking for the
/// index on first use.
pub(crate) fn open_mentions(app: &mut App, start: usize) {
    if matches!(app.composer.files, files::Files::Unbuilt) {
        app.composer.files = files::Files::Wanted;
    }
    app.composer.completion = Some(composer::Completion {
        kind: composer::Kind::File,
        items: Vec::new(),
        selected: 0,
        start,
    });
    refresh_completion(app);
}
/// Re-ranks the `@` popup for the current draft, closing it when the cursor
/// has left the mention.
pub(crate) fn refresh_completion(app: &mut App) {
    let Some(completion) = app.composer.completion.as_mut() else {
        return;
    };
    if completion.kind != composer::Kind::File {
        return;
    }
    match composer::mention_at(app.composer.editor.text(), app.composer.editor.cursor()) {
        Some((start, query)) if start == completion.start => {
            completion.items = match &app.composer.files {
                files::Files::Ready(index) => {
                    index.rank(query).into_iter().map(str::to_owned).collect()
                }
                _ => Vec::new(),
            };
            completion.selected = completion
                .selected
                .min(completion.items.len().saturating_sub(1));
        }
        _ => app.composer.completion = None,
    }
}
/// Puts the selected suggestion into the draft and closes the popup.
pub(crate) fn accept_completion(app: &mut App) {
    let Some(completion) = app.composer.completion.take() else {
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
    app.replace_or_warn(completion.start, &text);
}
/// `/copy` and Ctrl+X: the last reply to the clipboard.
pub(crate) fn copy_reply(app: &mut App) {
    let Some((bytes, cut, size)) = app.last_reply().and_then(|text| {
        clipboard::osc52(text).map(|(bytes, cut)| (bytes, cut, text.len().min(clipboard::LIMIT)))
    }) else {
        app.hint("Nothing to copy yet");
        return;
    };
    write_terminal(&bytes);
    app.hint(if cut {
        format!(
            "Copied the first {} KiB to the clipboard",
            clipboard::LIMIT / 1024
        )
    } else {
        format!("Copied {} to the clipboard", size_label(size))
    });
}
#[expect(
    clippy::cast_precision_loss,
    reason = "a label rounded to one decimal; sizes are far below 2^52"
)]
pub(crate) fn size_label(bytes: usize) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else {
        format!("{:.1} KiB", bytes as f64 / 1024.0)
    }
}
/// Pasted text goes into the draft, tabs and all, unless a dialog has
/// focus; then it is dropped with a hint.
pub(crate) fn paste(app: &mut App, value: &str) {
    let dialog = !app.overlay.approvals.is_empty() || app.overlay.help || app.overlay.palette;
    if dialog {
        app.hint("Close the dialog to paste into the prompt");
    } else if !app.composer.editor.insert(&text::strip(value)) {
        app.hint(format!(
            "Paste exceeds the {} KiB prompt limit; draft preserved",
            octet_core::PROMPT_LIMIT / 1024
        ));
    }
    // The file popup follows the pasted text; path and command popups close.
    if app
        .composer
        .completion
        .as_ref()
        .is_some_and(|completion| completion.kind != composer::Kind::File)
    {
        app.composer.completion = None;
    }
    refresh_completion(app);
}
/// One key. Dialogs take keys first (help, an approval, the palette, a
/// popup); otherwise the key edits or sends the draft.
pub(crate) async fn key_action(app: &mut App, vendor: &dyn Vendor, key: KeyEvent) -> Action {
    // Any key ends a pending quit; only a second Ctrl+C in time completes it.
    let quit_armed = app.quit_armed.take();
    if quit_armed.is_some() {
        app.clear_status(StatusKind::QuitHint);
    }
    if ctrl(key, 'z') {
        return Action::Suspend;
    }
    if app.overlay.help {
        help_key(app, key);
        return Action::Continue;
    }
    // Approval keys never leak into the composer. A queued modal takes priority.
    if let Some((id, _)) = app.overlay.approvals.front() {
        let id = *id;
        approval_key(app, vendor, key, id).await;
        return Action::Continue;
    }
    if app.overlay.palette {
        return palette_press(app, vendor, key).await;
    }
    if let Some(action) = completion_key(app, key) {
        return action;
    }
    composer_key(app, vendor, key, quit_armed).await
}
/// Ctrl plus `c`.
fn ctrl(key: KeyEvent, c: char) -> bool {
    key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char(c)
}
fn help_key(app: &mut App, key: KeyEvent) {
    let ctrl_c = ctrl(key, 'c');
    let help = &mut app.overlay;
    match key.code {
        KeyCode::PageDown | KeyCode::Down => help.help_scroll = help.help_scroll.saturating_add(8),
        KeyCode::PageUp | KeyCode::Up => help.help_scroll = help.help_scroll.saturating_sub(8),
        KeyCode::Esc | KeyCode::F(1) => help.help = false,
        _ if ctrl_c => help.help = false,
        _ => {}
    }
    if !help.help {
        help.help_scroll = 0;
    }
}
async fn approval_key(app: &mut App, vendor: &dyn Vendor, key: KeyEvent, id: u64) {
    let ctrl_c = ctrl(key, 'c');
    let answer = match key.code {
        KeyCode::Char('a' | 'A') => Some(true),
        KeyCode::Char('d' | 'D') | KeyCode::Esc => Some(false),
        _ => None,
    };
    if answer.is_some() && !app.overlay.approval_armed() {
        // Typed as the dialog opened: never an answer.
        app.hint("The approval just opened; press A or D again to answer");
    } else if let Some(allow) = answer {
        match vendor.send(Command::Answer { id, allow }) {
            Ok(()) => {
                app.overlay.approvals.pop_front();
                app.overlay.front_changed();
            }
            Err(error) => app.error(error.to_string()),
        }
    } else if key.code == KeyCode::PageDown {
        app.overlay.approval_scroll = app.overlay.approval_scroll.saturating_add(8);
    } else if key.code == KeyCode::PageUp {
        app.overlay.approval_scroll = app.overlay.approval_scroll.saturating_sub(8);
    } else if ctrl_c {
        cancel_turn(app, vendor).await;
    }
}
async fn palette_press(app: &mut App, vendor: &dyn Vendor, key: KeyEvent) -> Action {
    let ctrl_c = ctrl(key, 'c');
    match key.code {
        _ if ctrl_c => app.overlay.palette = false,
        KeyCode::Esc => app.overlay.palette = false,
        KeyCode::Up => app.overlay.selection = app.overlay.selection.saturating_sub(1),
        KeyCode::Down => {
            app.overlay.selection =
                (app.overlay.selection + 1).min(view::palette_entries().count() - 1);
        }
        KeyCode::Enter => {
            app.overlay.palette = false;
            if let Some(spec) = COMMANDS.get(app.overlay.selection) {
                return command(app, vendor, spec.name).await;
            }
            return palette_key(
                app,
                view::PALETTE_KEYS[app.overlay.selection - COMMANDS.len()].0,
            );
        }
        _ => {}
    }
    Action::Continue
}
/// Keys an open popup takes; `None` lets the key reach the draft.
fn completion_key(app: &mut App, key: KeyEvent) -> Option<Action> {
    let completion = app.composer.completion.as_mut()?;
    match key.code {
        KeyCode::Up => completion.selected = completion.selected.saturating_sub(1),
        KeyCode::Down => {
            completion.selected =
                (completion.selected + 1).min(completion.items.len().saturating_sub(1));
        }
        KeyCode::Tab | KeyCode::Enter => accept_completion(app),
        KeyCode::Esc => app.composer.completion = None,
        // Path and command popups close on any other key; the file popup
        // follows the edit below.
        _ => {
            if completion.kind != composer::Kind::File {
                app.composer.completion = None;
            }
            return None;
        }
    }
    Some(Action::Continue)
}
/// The draft: editing, history, sending, and the keys that act on the session.
/// Ctrl+C or Esc: what the press stops.
enum Stop {
    /// Ctrl+C, with when a second press would quit.
    CtrlC(Option<Instant>),
    Esc,
}
/// Like Claude Code: Ctrl+C and Esc stop what runs (a `!` command, then a
/// turn). Otherwise Ctrl+C clears the draft, else asks for a second press
/// within the window to quit; Esc drops attachments, else follows the latest
/// output.
async fn stop_key(app: &mut App, vendor: &dyn Vendor, stop: Stop) -> Action {
    if app.shell_running() {
        return Action::CancelShell;
    }
    if app.is_busy() {
        cancel_turn(app, vendor).await;
        app.status(StatusKind::Cancelling, CANCELLING);
        return Action::Continue;
    }
    match stop {
        Stop::CtrlC(_) if !app.composer.editor.text().is_empty() => {
            app.composer.editor.take();
        }
        Stop::CtrlC(armed) if armed.is_some_and(|deadline| Instant::now() < deadline) => {
            return Action::Exit(Exit::Quit);
        }
        Stop::CtrlC(_) => {
            app.quit_armed = Some(Instant::now() + QUIT_WINDOW);
            app.status(StatusKind::QuitHint, QUIT_HINT);
        }
        Stop::Esc
            if app.composer.editor.text().is_empty()
                && !(app.composer.attachments.is_empty() && app.composer.images.is_empty()) =>
        {
            app.composer.attachments.clear();
            app.composer.images.clear();
            app.hint("Attachments removed");
        }
        Stop::Esc => app.chat.scroll = 0,
    }
    refresh_completion(app);
    Action::Continue
}
/// Enter: runs a `!` line, a `/command`, or sends the draft as a prompt.
async fn submit_draft(app: &mut App, vendor: &dyn Vendor) -> Action {
    let draft = app.composer.editor.text().trim().to_owned();
    if draft.is_empty() {
        return Action::Continue;
    }
    if let Some(rest) = draft.strip_prefix('!') {
        let (attach, command) = match rest.strip_prefix('!') {
            Some(command) => (false, command.trim()),
            None => (true, rest.trim()),
        };
        if command.is_empty() {
            app.hint("Type a command after !");
            return Action::Continue;
        }
        if app.shell_running() {
            app.hint("A command is already running");
            return Action::Continue;
        }
        let command = command.to_owned();
        app.composer.editor.take();
        app.remember(draft);
        return Action::RunShell { command, attach };
    }
    // "/usr/lib is broken" is a prompt: no command name contains a slash.
    let path_like = draft
        .split_whitespace()
        .next()
        .and_then(|first| first.strip_prefix('/'))
        .is_some_and(|name| name.contains('/'));
    if draft.starts_with('/') && draft != octet_core::APPROVAL_DEMO && !path_like {
        return match try_command(app, vendor, &draft).await {
            Some(action) => {
                app.composer.editor.take();
                action
            }
            None => Action::Continue,
        };
    }
    if submit(app, vendor, draft) {
        app.composer.editor.take();
    }
    refresh_completion(app);
    Action::Continue
}
/// Tab: completes a path or a command, off the loop.
async fn tab_key(app: &mut App) {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    app.composer.listing.store(true, Ordering::SeqCst);
    let listing = Listing(Arc::clone(&app.composer.listing));
    let (text, cursor, root) = (
        app.composer.editor.text().to_owned(),
        app.composer.editor.cursor(),
        app.composer.root.clone(),
    );
    let tab = off_loop(TAB_WAIT, move || {
        // Released when the listing ends, even one Tab gave up on.
        let _listing = listing;
        composer::tab(&text, cursor, &root, home.as_deref())
    })
    .await;
    match tab.unwrap_or_else(|| {
        app.hint("That folder is slow to read; Tab gave up");
        composer::Tab::Nothing
    }) {
        composer::Tab::Replace { start, text, popup } => {
            if app.replace_or_warn(start, &text) {
                app.composer.completion = popup;
            }
        }
        composer::Tab::Popup(completion) => app.composer.completion = Some(completion),
        composer::Tab::Mention(start) => open_mentions(app, start),
        composer::Tab::Nothing => {}
    }
}
async fn composer_key(
    app: &mut App,
    vendor: &dyn Vendor,
    key: KeyEvent,
    quit_armed: Option<Instant>,
) -> Action {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    // Alt+Enter, Shift+Enter or Ctrl+J: phone keyboards rarely send the first two.
    let newline = (key.code == KeyCode::Enter
        && (alt || key.modifiers.contains(KeyModifiers::SHIFT)))
        || (ctrl && key.code == KeyCode::Char('j'));
    match key.code {
        KeyCode::F(1) => app.overlay.help = true,
        KeyCode::BackTab => return cycle_mode(app, vendor),
        KeyCode::Char('p') if ctrl => app.overlay.palette = true,
        KeyCode::Char('x') if ctrl => copy_reply(app),
        KeyCode::Char('g') if ctrl => {
            // The edited draft must not meet a popup about the old one.
            app.composer.completion = None;
            return Action::ExternalEditor;
        }
        KeyCode::Char('u') if ctrl => {
            app.composer.editor.take();
        }
        KeyCode::Char('c') if ctrl => return stop_key(app, vendor, Stop::CtrlC(quit_armed)).await,
        KeyCode::Esc => return stop_key(app, vendor, Stop::Esc).await,
        KeyCode::PageUp => app.scroll_by(10),
        KeyCode::PageDown => app.scroll_by(-10),
        KeyCode::End if ctrl => app.follow_latest(),
        _ if newline => {
            app.insert_or_warn("\n");
        }
        KeyCode::Enter => return submit_draft(app, vendor).await,
        KeyCode::Up if app.composer.editor.text().contains('\n') => {
            app.composer.editor.vertical(false);
        }
        KeyCode::Down if app.composer.editor.text().contains('\n') => {
            app.composer.editor.vertical(true);
        }
        KeyCode::Up => app.recall(true),
        KeyCode::Down => app.recall(false),
        KeyCode::Left => app.composer.editor.left(),
        KeyCode::Right => app.composer.editor.right(),
        KeyCode::Home => app.composer.editor.home(),
        KeyCode::End => app.composer.editor.end(),
        KeyCode::Backspace => app.composer.editor.backspace(),
        KeyCode::Delete => app.composer.editor.delete(),
        KeyCode::Tab if app.composer.listing.load(Ordering::SeqCst) => {
            app.hint("Still reading the last folder; try Tab again in a moment");
        }
        KeyCode::Tab => tab_key(app).await,
        KeyCode::Char(c) if !ctrl && !alt => {
            app.insert_or_warn(&c.to_string());
        }
        _ => {}
    }
    if key.code == KeyCode::Char('@')
        && app.composer.completion.is_none()
        && let Some((start, "")) =
            composer::mention_at(app.composer.editor.text(), app.composer.editor.cursor())
    {
        open_mentions(app, start);
    }
    refresh_completion(app);
    Action::Continue
}
/// Interrupts the turn; a goal working on it is paused first, and prompts
/// queued behind it are dropped: stopping the agent stops what was lined up.
pub(crate) async fn cancel_turn(app: &mut App, vendor: &dyn Vendor) {
    // Interrupt first: saving the paused goal must not delay the cancel.
    app.conn.cancel();
    vendor.interrupt();
    let dropped = std::mem::take(&mut app.composer.queue).len();
    if dropped > 0 {
        app.note(format!("Dropped {}", crate::app::queued_prompts(dropped)));
    }
    if let Err(error) = app.goals.pause_running_turn().await {
        app.goal_save_failed(error, false);
    }
}
pub(crate) fn cycle_mode(app: &mut App, vendor: &dyn Vendor) -> Action {
    if app.conn.mode == octet_core::Mode::FullAccess {
        app.hint("Use /mode to leave full access");
    } else if app.conn.mode_pending.is_some() {
        app.hint("Mode change pending; wait for the vendor to confirm");
    } else {
        switch_mode(app, vendor, app.conn.mode.cycle());
    }
    Action::Continue
}
/// The prompt for `draft`, with the waiting `!` output and images, or why it
/// cannot be sent.
pub(crate) fn compose(app: &App, draft: &str) -> Result<Prompt, String> {
    if app.composer.attachments.is_empty() && app.composer.images.is_empty() {
        return Ok(Prompt::plain(draft.to_owned()));
    }
    let (wire, mut display) =
        composer::with_attachments(draft, &app.composer.attachments, octet_core::PROMPT_LIMIT);
    for image in &app.composer.images {
        let _ = write!(display, "\n[+ image {}]", image.name);
    }
    if wire.len().max(display.len()) > octet_core::PROMPT_LIMIT {
        return Err(format!(
            "The prompt and its attachments are over {} KiB. \
             Shorten the prompt, or press Esc on an empty prompt to drop them",
            octet_core::PROMPT_LIMIT / 1024
        ));
    }
    Ok(Prompt {
        wire,
        display,
        images: app.composer.images.clone(),
    })
}
/// Sends `draft` as a prompt, or queues it behind the running turn. Its
/// attachments go with it, and it joins the history. False, with a notice,
/// when it could not go.
pub(crate) fn submit(app: &mut App, vendor: &dyn Vendor, draft: String) -> bool {
    if !app.is_idle() && !app.conn.is_running() {
        app.hint(crate::registry::NOT_CONNECTED);
        return false;
    }
    if app.conn.is_running() && app.composer.queue.len() >= QUEUE_LIMIT {
        app.status_line =
            format!("The queue is full ({QUEUE_LIMIT} prompts); wait for the turn or press Esc");
        return false;
    }
    let prompt = match compose(app, &draft) {
        Ok(prompt) => prompt,
        Err(reason) => {
            app.hint(reason);
            return false;
        }
    };
    if app.conn.is_running() {
        // The running turn keeps going; this one goes when it ends.
        app.composer.queue.push_back(prompt);
        app.hint(format!("Queued ({} waiting)", app.composer.queue.len()));
    } else if let Err(error) = app.begin_turn(vendor, prompt.into_command(), By::User, "sending") {
        app.error(error.to_string());
        return false;
    }
    app.composer.attachments.clear();
    let images = std::mem::take(&mut app.composer.images);
    app.remember_with(draft, images);
    true
}
