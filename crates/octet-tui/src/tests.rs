use super::*;
use crate::{commands::*, input::*};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use octet_core::Engine;
fn app() -> App {
    let config = Config::new(Engine::Codex, "codex", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.conn.ready = true;
    app.conn.session = "thread-1".into();
    app
}
#[tokio::test]
async fn model_command_rejects_busy_switch_and_preserves_current_session() {
    let mut app = app();
    app.conn.running = true;
    assert!(matches!(
        command(&mut app, "/model claude example").await,
        Action::Continue
    ));
    assert_eq!(app.conn.engine, octet_core::Engine::Codex);
    assert_eq!(app.conn.session, "thread-1");
    app.conn.running = false;
    let Action::Exit(Exit::Model(selection)) = command(&mut app, "/model claude example").await
    else {
        panic!("expected a model switch");
    };
    assert_eq!(selection.provider, octet_core::Engine::Claude);
    assert_eq!(selection.model.as_deref(), Some("example"));
}
#[tokio::test]
async fn aliases_dispatch_like_their_command() {
    let mut app = app();
    assert!(matches!(
        command(&mut app, "/exit").await,
        Action::Exit(Exit::Quit)
    ));
}
#[tokio::test]
async fn model_help_and_invalid_syntax_never_restart_a_session() {
    let mut app = app();
    for input in ["/model", "/model unknown model", "/model claude/"] {
        assert!(matches!(command(&mut app, input).await, Action::Continue));
        assert_eq!(app.conn.session, "thread-1");
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
    let (_temp, session) = demo_session("octet-at").await;
    app.composer.files = crate::files::Files::Ready(crate::files::Index::from_paths(vec![
        "README.md".into(),
        "src/main.rs".into(),
    ]));
    for c in "see @mai".chars() {
        key_action(&mut app, &session, key(KeyCode::Char(c))).await;
    }
    let completion = app.composer.completion.as_ref().expect("popup open");
    assert_eq!(completion.items, ["src/main.rs"]);
    key_action(&mut app, &session, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text, "see @src/main.rs ");
    assert!(app.composer.completion.is_none());
    assert!(!app.conn.running, "Enter accepted instead of sending");
}
#[tokio::test]
async fn the_palette_offers_the_editor_mentions_and_shell() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-palette-keys").await;
    let open_at = |app: &mut App, index: usize| {
        app.overlay.palette = true;
        app.overlay.selection = index;
    };
    open_at(&mut app, COMMANDS.len() + 1);
    key_action(&mut app, &session, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text, "@");
    assert!(app.composer.completion.is_some(), "@ opens the file popup");
    app.composer.completion = None;
    app.composer.editor.take();
    open_at(&mut app, COMMANDS.len() + 2);
    key_action(&mut app, &session, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text, "!");
    open_at(&mut app, COMMANDS.len());
    assert!(matches!(
        key_action(&mut app, &session, key(KeyCode::Enter)).await,
        Action::ExternalEditor
    ));
    app.overlay.palette = true;
    app.overlay.selection = 0;
    for _ in 0..40 {
        key_action(&mut app, &session, key(KeyCode::Down)).await;
    }
    assert_eq!(
        app.overlay.selection,
        COMMANDS.len() + view::PALETTE_KEYS.len() - 1
    );
}
#[tokio::test]
async fn a_prompt_too_long_for_its_attachments_says_so() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-attach-limit").await;
    app.attach(ran("echo hi", "hi\n"));
    app.composer
        .editor
        .set("x".repeat(octet_core::PROMPT_LIMIT - 6));
    key_action(&mut app, &session, key(KeyCode::Enter)).await;
    assert_eq!(
        app.notice,
        "The prompt and its attachments are over 64 KiB. Shorten the prompt, or press Esc on an empty prompt to drop them"
    );
    assert_eq!(app.composer.attachments.len(), 1, "kept");
    assert!(!app.conn.running);
}
#[test]
fn a_finished_command_replaces_the_running_notice() {
    let mut app = app();
    app.composer.shell_running = true;
    app.notice = "Running echo hi · Esc to stop".into();
    shell_finished(&mut app, Ok(ran("echo hi", "hi\n")), true);
    assert!(!app.composer.shell_running);
    assert_eq!(app.notice, "$ echo hi · exit 0");
    assert_eq!(app.composer.attachments.len(), 1);
    shell_finished(&mut app, Err("Cannot run /x: gone".into()), false);
    assert_eq!(app.notice, "Cannot run /x: gone");
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
    // Gave up at its 50 ms limit, long before the 2 s of work would end.
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(off_loop(Duration::from_secs(1), || 5).await, Some(5));
}
#[tokio::test]
async fn the_popup_closes_before_the_editor_and_follows_a_paste() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-at-settle").await;
    app.composer.files =
        crate::files::Files::Ready(crate::files::Index::from_paths(vec!["src/main.rs".into()]));
    for c in "a long draft @ma".chars() {
        key_action(&mut app, &session, key(KeyCode::Char(c))).await;
    }
    assert!(app.composer.completion.is_some());
    assert!(matches!(
        key_action(&mut app, &session, ctrl('g')).await,
        Action::ExternalEditor
    ));
    assert!(
        app.composer.completion.is_none(),
        "the edited draft must not meet a stale popup"
    );
    app.composer.editor.set("/re".into());
    app.composer.completion = Some(composer::Completion {
        kind: composer::Kind::Command,
        items: vec!["/reconnect".into()],
        selected: 0,
        start: 0,
    });
    paste(&mut app, "port the bug");
    assert!(
        app.composer.completion.is_none(),
        "a paste closes a command popup"
    );
    key_action(&mut app, &session, key(KeyCode::Char('x'))).await;
    assert_eq!(app.composer.editor.text, "/report the bugx");
}
#[tokio::test]
async fn the_first_at_asks_for_the_index_and_esc_closes_the_popup() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-at-index").await;
    key_action(&mut app, &session, key(KeyCode::Char('@'))).await;
    assert!(matches!(app.composer.files, crate::files::Files::Wanted));
    assert!(app.composer.completion.is_some());
    key_action(&mut app, &session, key(KeyCode::Esc)).await;
    assert!(app.composer.completion.is_none());
    assert_eq!(app.composer.editor.text, "@");
}
#[tokio::test]
async fn an_at_inside_a_word_opens_nothing() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-at-word").await;
    for c in "me@host".chars() {
        key_action(&mut app, &session, key(KeyCode::Char(c))).await;
    }
    assert!(app.composer.completion.is_none());
}
#[tokio::test]
async fn bang_lines_run_locally_and_double_bang_does_not_attach() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-bang").await;
    app.composer.editor.set("!echo hi".into());
    assert!(matches!(
        key_action(&mut app, &session, key(KeyCode::Enter)).await,
        Action::RunShell { ref command, attach: true } if command == "echo hi"
    ));
    assert!(app.composer.editor.text.is_empty());
    assert_eq!(
        app.composer.history.back().map(String::as_str),
        Some("!echo hi")
    );
    app.composer.editor.set("!!  pwd ".into());
    assert!(matches!(
        key_action(&mut app, &session, key(KeyCode::Enter)).await,
        Action::RunShell { ref command, attach: false } if command == "pwd"
    ));
    app.composer.editor.set("!".into());
    assert!(matches!(
        key_action(&mut app, &session, key(KeyCode::Enter)).await,
        Action::Continue
    ));
    assert_eq!(app.notice, "Type a command after !");
    app.composer.shell_running = true;
    app.composer.editor.set("!ls".into());
    assert!(matches!(
        key_action(&mut app, &session, key(KeyCode::Enter)).await,
        Action::Continue
    ));
    assert_eq!(app.notice, "A command is already running");
    assert!(matches!(
        key_action(&mut app, &session, key(KeyCode::Esc)).await,
        Action::CancelShell
    ));
}
#[tokio::test]
async fn attachments_go_with_the_next_prompt_then_clear() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-attach").await;
    app.attach(ran("echo hi", "hi\n"));
    app.composer.editor.set("explain".into());
    key_action(&mut app, &session, key(KeyCode::Enter)).await;
    assert!(app.composer.attachments.is_empty());
    assert_eq!(next_user_text(&mut session).await, "explain\n\n[+ echo hi]");
}
#[tokio::test]
async fn esc_on_an_empty_idle_draft_removes_attachments() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-detach").await;
    app.attach(ran("ls", ""));
    key_action(&mut app, &session, key(KeyCode::Esc)).await;
    assert!(app.composer.attachments.is_empty());
    assert_eq!(app.notice, "Attachments removed");
}
#[tokio::test]
async fn goal_prompts_never_carry_attachments() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-goal-attach").await;
    app.attach(ran("ls", "a\n"));
    send_goal_prompt(&mut app, &session, "continue the goal".into()).await;
    assert_eq!(
        app.composer.attachments.len(),
        1,
        "kept for the user's next prompt"
    );
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
    assert_eq!(app.conn.session, "thread-1");
}
#[tokio::test]
async fn ctrl_c_twice_on_an_idle_empty_prompt_quits() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-quit-twice").await;
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    assert_eq!(app.notice, QUIT_HINT);
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Exit(Exit::Quit)
    ));
    session.shutdown().await;
}
#[tokio::test]
async fn ctrl_c_clears_a_draft_before_it_can_quit() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-quit-draft").await;
    assert!(app.composer.editor.insert("half-written prompt"));
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    assert!(app.composer.editor.text.is_empty());
    assert_ne!(app.notice, QUIT_HINT);
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Exit(Exit::Quit)
    ));
    session.shutdown().await;
}
#[tokio::test]
async fn another_key_or_an_expired_window_disarms_quit() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-quit-disarm").await;
    key_action(&mut app, &session, ctrl('c')).await;
    let left = KeyEvent::new(KeyCode::Left, KeyModifiers::NONE);
    key_action(&mut app, &session, left).await;
    assert_ne!(app.notice, QUIT_HINT, "another key clears the hint");
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    // The window has passed: the press arms again instead of quitting.
    app.quit_armed = Some(Instant::now() - Duration::from_millis(1));
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    assert_eq!(app.notice, QUIT_HINT);
    session.shutdown().await;
}
#[tokio::test]
async fn ctrl_c_closes_help_and_the_palette_without_quitting() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-quit-overlay").await;
    app.overlay.help = true;
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    assert!(!app.overlay.help);
    app.overlay.palette = true;
    assert!(matches!(
        key_action(&mut app, &session, ctrl('c')).await,
        Action::Continue
    ));
    assert!(!app.overlay.palette);
    assert_ne!(app.notice, QUIT_HINT);
    session.shutdown().await;
}
#[tokio::test]
async fn ctrl_c_interrupts_a_running_turn_instead_of_quitting() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-quit-busy").await;
    app.conn.running = true;
    for _ in 0..2 {
        assert!(matches!(
            key_action(&mut app, &session, ctrl('c')).await,
            Action::Continue
        ));
        assert_eq!(app.notice, crate::app::CANCELLING);
    }
    session.shutdown().await;
}
#[tokio::test]
async fn ctrl_q_no_longer_quits() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-quit-q").await;
    assert!(matches!(
        key_action(&mut app, &session, ctrl('q')).await,
        Action::Continue
    ));
    session.shutdown().await;
}
#[tokio::test]
async fn ctrl_c_in_approval_dialog_pauses_the_active_goal() {
    let mut app = app();
    app.goals.goal = Some(octet_core::goal::Goal::new("Ship the project").unwrap());
    app.goals.goal_prompt_sent();
    app.conn.running = true;
    app.overlay.approvals.push_back((1, "command".into()));
    let temp = octet_testkit::TempDir::new("octet-goal-cancel");
    let directory = temp.path().to_path_buf();
    let config = Config::new(Engine::Demo, "demo", directory.clone());
    let mut session = Session::open(config, directory.clone()).await.unwrap();
    key_action(
        &mut app,
        &session,
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
    app.conn.running = true;
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
    app.conn.running = true;
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Continue
    ));
    app.conn.running = false;
    app.overlay.approvals.push_back((1, "x".into()));
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Continue
    ));
    app.overlay.approvals.clear();
    app.conn.ready = false;
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Continue
    ));
    app.conn.ready = true;
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Exit(Exit::Mode(octet_core::Mode::FullAccess))
    ));
    app.conn.mode = octet_core::Mode::FullAccess;
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
    app.conn.mode = octet_core::Mode::Auto;
    assert!(matches!(
        cycle_mode(&mut app),
        Action::SetMode(octet_core::Mode::Ask)
    ));
    app.conn.mode = octet_core::Mode::FullAccess;
    assert!(matches!(cycle_mode(&mut app), Action::Continue));
    assert!(app.notice.contains("/mode"));
}
#[tokio::test]
async fn cycle_is_ignored_while_a_switch_is_pending() {
    let mut app = app();
    app.conn.mode_pending = Some(octet_core::Mode::AcceptEdits);
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
    app.conn.mode = octet_core::Mode::FullAccess;
    app.conn.ready = false;
    app.conn.stopped = true;
    assert!(matches!(
        command(&mut app, "/mode ask").await,
        Action::Exit(Exit::Mode(octet_core::Mode::Ask))
    ));
    app.conn.mode = octet_core::Mode::Ask;
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
    assert!(app
        .composer
        .editor
        .insert("/nope is not a command, keep my words"));
    assert!(matches!(
        key_action(&mut app, &session, enter).await,
        Action::Continue
    ));
    assert_eq!(
        app.composer.editor.text,
        "/nope is not a command, keep my words"
    );
    assert!(app.notice.contains("Unknown command"));
    app.composer.editor.take();
    // A path is a prompt, not a command.
    app.conn.ready = true;
    assert!(app
        .composer
        .editor
        .insert("/usr/lib is where this breaks, please look"));
    assert!(matches!(
        key_action(&mut app, &session, enter).await,
        Action::Continue
    ));
    assert!(
        app.composer.editor.text.is_empty() && app.conn.running,
        "{}",
        app.notice
    );
    assert_eq!(
        app.composer.history.back().map(String::as_str),
        Some("/usr/lib is where this breaks, please look")
    );
    app.conn.running = false;
    // A prompt starting with a multi-byte character is an ordinary prompt.
    assert!(app.composer.editor.insert("界 means world"));
    assert!(matches!(
        key_action(&mut app, &session, enter).await,
        Action::Continue
    ));
    assert_eq!(
        app.composer.history.back().map(String::as_str),
        Some("界 means world")
    );
    app.conn.running = false;
    assert!(app.composer.editor.insert("/session"));
    assert!(matches!(
        key_action(&mut app, &session, enter).await,
        Action::Continue
    ));
    assert!(app.composer.editor.text.is_empty());
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
        fresh.contains("starts fresh") && fresh.contains("Previous journal: /data/session-1.jsonl"),
        "{fresh}"
    );
}
