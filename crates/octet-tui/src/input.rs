//! Keys, paste and the prompt box's popups: what each key does in each state.
use crate::{
    app::{App, CANCELLING, QUEUE_LIMIT},
    clipboard,
    commands::{command, try_command, COMMANDS},
    composer, files, text, view, write_terminal, Action, Exit, QUIT_HINT, QUIT_WINDOW,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use octet_core::{Command, Session};
use std::{path::PathBuf, time::Duration};
use tokio::time::Instant;

/// How long Tab waits for a folder listing.
pub(crate) const TAB_WAIT: Duration = Duration::from_millis(500);
/// Runs blocking `work` on its own thread, waiting at most `limit`. A plain
/// thread, not spawn_blocking, so work stuck on a dead mount cannot hold up
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
/// A key chosen from the palette.
pub(crate) fn palette_key(app: &mut App, key: &str) -> Action {
    match key {
        "Ctrl+G" => {
            app.composer.completion = None;
            return Action::ExternalEditor;
        }
        "@" => {
            let after_word = app.composer.editor.text[..app.composer.editor.cursor]
                .chars()
                .next_back()
                .is_some_and(|c| !c.is_whitespace());
            let mention = if after_word { " @" } else { "@" };
            if app.insert_or_warn(mention) {
                if let Some((start, _)) =
                    composer::mention_at(&app.composer.editor.text, app.composer.editor.cursor)
                {
                    open_mentions(app, start);
                }
            }
        }
        _ if app.composer.editor.text.is_empty() => app.composer.editor.set("!".into()),
        _ => app.notice = "Clear the prompt to start a ! command".into(),
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
    match composer::mention_at(&app.composer.editor.text, app.composer.editor.cursor) {
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
pub(crate) fn size_label(bytes: usize) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    }
}
/// Pasted text goes into the draft unless a dialog has focus.
pub(crate) fn paste(app: &mut App, value: &str) {
    if app.overlay.approvals.is_empty()
        && !app.overlay.help
        && !app.overlay.palette
        && !app.composer.editor.insert(&text::clean(value))
    {
        app.notice = "Paste exceeds the 64 KiB prompt limit; draft preserved".into();
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
pub(crate) async fn key_action(app: &mut App, session: &Session, key: KeyEvent) -> Action {
    // Any key ends a pending quit; only a second Ctrl+C in time completes it.
    let quit_armed = app.quit_armed.take();
    if quit_armed.is_some() && app.notice == QUIT_HINT {
        app.notice.clear();
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
        approval_key(app, session, key, id).await;
        return Action::Continue;
    }
    if app.overlay.palette {
        return palette_press(app, key).await;
    }
    if let Some(action) = completion_key(app, key) {
        return action;
    }
    composer_key(app, session, key, quit_armed).await
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
async fn approval_key(app: &mut App, session: &Session, key: KeyEvent, id: u64) {
    let ctrl_c = ctrl(key, 'c');
    let answer = match key.code {
        KeyCode::Char('a' | 'A') => Some(true),
        KeyCode::Char('d' | 'D') | KeyCode::Esc => Some(false),
        _ => None,
    };
    if let Some(allow) = answer {
        match session.handle.send(Command::Answer { id, allow }) {
            Ok(()) => {
                app.overlay.approvals.pop_front();
                app.overlay.approval_scroll = 0;
            }
            Err(error) => app.notice = error.to_string(),
        }
    } else if key.code == KeyCode::PageDown {
        app.overlay.approval_scroll = app.overlay.approval_scroll.saturating_add(8);
    } else if key.code == KeyCode::PageUp {
        app.overlay.approval_scroll = app.overlay.approval_scroll.saturating_sub(8);
    } else if ctrl_c {
        cancel_turn(app, session).await;
    }
}
async fn palette_press(app: &mut App, key: KeyEvent) -> Action {
    let ctrl_c = ctrl(key, 'c');
    match key.code {
        _ if ctrl_c => app.overlay.palette = false,
        KeyCode::Esc => app.overlay.palette = false,
        KeyCode::Up => app.overlay.selection = app.overlay.selection.saturating_sub(1),
        KeyCode::Down => {
            app.overlay.selection =
                (app.overlay.selection + 1).min(view::palette_entries().count() - 1)
        }
        KeyCode::Enter => {
            app.overlay.palette = false;
            if let Some(spec) = COMMANDS.get(app.overlay.selection) {
                return command(app, spec.name).await;
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
async fn composer_key(
    app: &mut App,
    session: &Session,
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
        KeyCode::BackTab => return cycle_mode(app),
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
        // Like Claude Code: stop what runs, else clear the draft, else ask
        // for a second press within the window to quit.
        KeyCode::Char('c') if ctrl => {
            if app.composer.shell_running {
                return Action::CancelShell;
            } else if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = CANCELLING.into();
            } else if !app.composer.editor.text.is_empty() {
                app.composer.editor.take();
            } else if quit_armed.is_some_and(|deadline| Instant::now() < deadline) {
                return Action::Exit(Exit::Quit);
            } else {
                app.quit_armed = Some(Instant::now() + QUIT_WINDOW);
                app.notice = QUIT_HINT.into();
            }
        }
        KeyCode::Esc => {
            if app.composer.shell_running {
                return Action::CancelShell;
            } else if app.is_busy() {
                cancel_turn(app, session).await;
                app.notice = CANCELLING.into();
            } else if app.composer.editor.text.is_empty()
                && !(app.composer.attachments.is_empty() && app.composer.images.is_empty())
            {
                app.composer.attachments.clear();
                app.composer.images.clear();
                app.notice = "Attachments removed".into();
            } else {
                app.chat.scroll = 0;
            }
        }
        KeyCode::PageUp => app.scroll_by(10),
        KeyCode::PageDown => app.scroll_by(-10),
        KeyCode::End if ctrl => app.follow_latest(),
        _ if newline => {
            app.insert_or_warn("\n");
        }
        KeyCode::Enter => {
            let draft = app.composer.editor.text.trim().to_owned();
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
                if app.composer.shell_running {
                    app.notice = "A command is already running".into();
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
            if draft.starts_with('/') && draft != "/approval-demo" && !path_like {
                return match try_command(app, &draft).await {
                    Some(action) => {
                        app.composer.editor.take();
                        action
                    }
                    None => Action::Continue,
                };
            }
            if !app.is_idle() && !app.conn.is_running() {
                app.notice = "Wait for the connection, or /reconnect".into();
                return Action::Continue;
            }
            if app.conn.is_running() && app.composer.queue.len() >= QUEUE_LIMIT {
                app.notice = format!(
                    "The queue is full ({QUEUE_LIMIT} prompts); wait for the turn or press Esc"
                );
                return Action::Continue;
            }
            let command = if app.composer.attachments.is_empty() && app.composer.images.is_empty() {
                Command::Prompt(draft.clone())
            } else {
                let (wire, mut display) = composer::with_attachments(
                    &draft,
                    &app.composer.attachments,
                    octet_core::PROMPT_LIMIT,
                );
                for image in &app.composer.images {
                    display.push_str(&format!("\n[+ image {}]", image.name));
                }
                if wire.len().max(display.len()) > octet_core::PROMPT_LIMIT {
                    app.notice = "The prompt and its attachments are over 64 KiB. \
                                  Shorten the prompt, or press Esc on an empty prompt to drop them"
                        .into();
                    return Action::Continue;
                }
                Command::PromptWithDisplay {
                    wire,
                    display,
                    images: app.composer.images.clone(),
                }
            };
            if app.conn.is_running() {
                // The running turn keeps going; this one goes when it ends.
                app.composer.queue.push_back(command);
                app.composer.attachments.clear();
                app.composer.images.clear();
                app.composer.editor.take();
                app.remember(draft);
                app.notice = format!("Queued ({} waiting)", app.composer.queue.len());
                return Action::Continue;
            }
            match session.handle.send(command) {
                Ok(()) => {
                    app.composer.attachments.clear();
                    app.composer.images.clear();
                    app.goals.user_prompt_sent();
                    app.composer.editor.take();
                    app.conn.start_turn();
                    app.conn.status = "sending".into();
                    app.remember(draft);
                }
                Err(error) => app.notice = error.to_string(),
            }
        }
        KeyCode::Up if app.composer.editor.text.contains('\n') => {
            app.composer.editor.vertical(false)
        }
        KeyCode::Down if app.composer.editor.text.contains('\n') => {
            app.composer.editor.vertical(true)
        }
        KeyCode::Up => app.recall(true),
        KeyCode::Down => app.recall(false),
        KeyCode::Left => app.composer.editor.left(),
        KeyCode::Right => app.composer.editor.right(),
        KeyCode::Home => app.composer.editor.home(),
        KeyCode::End => app.composer.editor.end(),
        KeyCode::Backspace => app.composer.editor.backspace(),
        KeyCode::Delete => app.composer.editor.delete(),
        KeyCode::Tab => {
            let home = std::env::var_os("HOME").map(PathBuf::from);
            let (text, cursor, root) = (
                app.composer.editor.text.clone(),
                app.composer.editor.cursor,
                app.composer.root.clone(),
            );
            let tab = off_loop(TAB_WAIT, move || {
                composer::tab(&text, cursor, &root, home.as_deref())
            })
            .await;
            match tab.unwrap_or_else(|| {
                app.notice = "That folder is slow to read; Tab gave up".into();
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
        KeyCode::Char(c) if !ctrl && !alt => {
            app.insert_or_warn(&c.to_string());
        }
        _ => {}
    }
    if key.code == KeyCode::Char('@') && app.composer.completion.is_none() {
        if let Some((start, "")) =
            composer::mention_at(&app.composer.editor.text, app.composer.editor.cursor)
        {
            open_mentions(app, start);
        }
    }
    refresh_completion(app);
    Action::Continue
}
/// Interrupts the turn; a goal working on it is paused first, and prompts
/// queued behind it are dropped: stopping the agent stops what was lined up.
pub(crate) async fn cancel_turn(app: &mut App, session: &Session) {
    if let Err(error) = app.goals.pause_running_turn().await {
        app.notice(format!("Goal persistence failed: {error}"));
    }
    let dropped = std::mem::take(&mut app.composer.queue).len();
    if dropped > 0 {
        let plural = if dropped == 1 { "" } else { "s" };
        app.notice(format!("Dropped {dropped} queued prompt{plural}"));
    }
    app.conn.cancelling = app.conn.is_running();
    session.handle.interrupt();
}
pub(crate) fn cycle_mode(app: &mut App) -> Action {
    if app.conn.mode == octet_core::Mode::FullAccess {
        app.notice = "Use /mode to leave full access".into();
    } else if app.conn.mode_pending.is_some() {
        app.notice = "Mode change pending; wait for the vendor to confirm".into();
    } else {
        return Action::SetMode(app.conn.mode.cycle());
    }
    Action::Continue
}
