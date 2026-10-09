//! Keys, paste and prompt submission.
use super::*;

#[tokio::test]
async fn at_opens_the_file_popup_and_enter_accepts() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.composer.files = crate::files::Files::Ready(crate::files::Index::from_paths(vec![
        "README.md".into(),
        "src/main.rs".into(),
    ]));
    for c in "see @mai".chars() {
        key_action(&mut app, &vendor, key(KeyCode::Char(c))).await;
    }
    let completion = app.composer.completion.as_ref().expect("popup open");
    assert_eq!(completion.items, ["src/main.rs"]);
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text(), "see @src/main.rs ");
    assert!(app.composer.completion.is_none());
    assert!(!app.conn.is_running(), "Enter accepted instead of sending");
}
#[tokio::test]
async fn the_palette_offers_the_editor_mentions_and_shell() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    let open_at = |app: &mut App, index: usize| {
        app.overlay.palette = true;
        app.overlay.selection = index;
    };
    open_at(&mut app, COMMANDS.len() + 1);
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text(), "@");
    assert!(app.composer.completion.is_some(), "@ opens the file popup");
    app.composer.completion = None;
    app.composer.editor.take();
    open_at(&mut app, COMMANDS.len() + 2);
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text(), "!");
    open_at(&mut app, COMMANDS.len());
    assert!(matches!(
        key_action(&mut app, &vendor, key(KeyCode::Enter)).await,
        Action::ExternalEditor
    ));
    app.overlay.palette = true;
    app.overlay.selection = 0;
    for _ in 0..40 {
        key_action(&mut app, &vendor, key(KeyCode::Down)).await;
    }
    assert_eq!(
        app.overlay.selection,
        COMMANDS.len() + view::PALETTE_KEYS.len() - 1
    );
}
#[tokio::test]
async fn a_prompt_too_long_for_its_attachments_says_so() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.attach(ran("echo hi", "hi\n"));
    app.composer
        .editor
        .set("x".repeat(octet_core::PROMPT_LIMIT - 6));
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(
        app.status_line,
        "The prompt and its attachments are over 64 KiB. Shorten the prompt, or press Esc on an empty prompt to drop them"
    );
    assert_eq!(app.composer.attachments.len(), 1, "kept");
    assert!(!app.conn.is_running());
}
#[tokio::test]
async fn the_popup_closes_before_the_editor_and_follows_a_paste() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.composer.files =
        crate::files::Files::Ready(crate::files::Index::from_paths(vec!["src/main.rs".into()]));
    for c in "a long draft @ma".chars() {
        key_action(&mut app, &vendor, key(KeyCode::Char(c))).await;
    }
    assert!(app.composer.completion.is_some());
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('g')).await,
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
    key_action(&mut app, &vendor, key(KeyCode::Char('x'))).await;
    assert_eq!(app.composer.editor.text(), "/report the bugx");
}
#[tokio::test]
async fn the_first_at_asks_for_the_index_and_esc_closes_the_popup() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    key_action(&mut app, &vendor, key(KeyCode::Char('@'))).await;
    assert!(matches!(app.composer.files, crate::files::Files::Wanted));
    assert!(app.composer.completion.is_some());
    key_action(&mut app, &vendor, key(KeyCode::Esc)).await;
    assert!(app.composer.completion.is_none());
    assert_eq!(app.composer.editor.text(), "@");
}
#[tokio::test]
async fn an_at_inside_a_word_opens_nothing() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    for c in "me@host".chars() {
        key_action(&mut app, &vendor, key(KeyCode::Char(c))).await;
    }
    assert!(app.composer.completion.is_none());
}
#[tokio::test]
async fn bang_lines_run_locally_and_double_bang_does_not_attach() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.composer.editor.set("!echo hi".into());
    assert!(matches!(
        key_action(&mut app, &vendor, key(KeyCode::Enter)).await,
        Action::RunShell { ref command, attach: true } if command == "echo hi"
    ));
    assert!(app.composer.editor.text().is_empty());
    assert_eq!(
        app.composer.history.back().map(|sent| sent.text.as_str()),
        Some("!echo hi")
    );
    app.composer.editor.set("!!  pwd ".into());
    assert!(matches!(
        key_action(&mut app, &vendor, key(KeyCode::Enter)).await,
        Action::RunShell { ref command, attach: false } if command == "pwd"
    ));
    app.composer.editor.set("!".into());
    assert!(matches!(
        key_action(&mut app, &vendor, key(KeyCode::Enter)).await,
        Action::Continue
    ));
    assert_eq!(app.status_line, "Type a command after !");
    app.shell = Some(crate::test_support::running_shell(false));
    app.composer.editor.set("!ls".into());
    assert!(matches!(
        key_action(&mut app, &vendor, key(KeyCode::Enter)).await,
        Action::Continue
    ));
    assert_eq!(app.status_line, "A command is already running");
    assert!(matches!(
        key_action(&mut app, &vendor, key(KeyCode::Esc)).await,
        Action::CancelShell
    ));
}
#[tokio::test]
async fn attachments_go_with_the_next_prompt_then_clear() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-attach").await;
    app.attach(ran("echo hi", "hi\n"));
    app.composer.editor.set("explain".into());
    key_action(&mut app, &session.handle, key(KeyCode::Enter)).await;
    assert!(app.composer.attachments.is_empty());
    assert_eq!(next_user_text(&mut session).await, "explain\n\n[+ echo hi]");
}
#[tokio::test]
async fn esc_on_an_empty_idle_draft_removes_attachments() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.attach(ran("ls", ""));
    key_action(&mut app, &vendor, key(KeyCode::Esc)).await;
    assert!(app.composer.attachments.is_empty());
    assert_eq!(app.status_line, "Attachments removed");
}
#[tokio::test]
async fn ctrl_c_twice_on_an_idle_empty_prompt_quits() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    assert_eq!(app.status_line, QUIT_HINT);
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Exit(Exit::Quit)
    ));
}
#[tokio::test]
async fn ctrl_c_clears_a_draft_before_it_can_quit() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    assert!(app.composer.editor.insert("half-written prompt"));
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    assert!(app.composer.editor.text().is_empty());
    assert_ne!(app.status_line, QUIT_HINT);
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Exit(Exit::Quit)
    ));
}
#[tokio::test]
async fn another_key_or_an_expired_window_disarms_quit() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    key_action(&mut app, &vendor, ctrl('c')).await;
    let left = KeyEvent::new(KeyCode::Left, KeyModifiers::NONE);
    key_action(&mut app, &vendor, left).await;
    assert_ne!(app.status_line, QUIT_HINT, "another key clears the hint");
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    // The window has passed: the press arms again instead of quitting.
    app.quit_armed = Some(tokio::time::Instant::now() - Duration::from_millis(1));
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    assert_eq!(app.status_line, QUIT_HINT);
}
#[tokio::test]
async fn ctrl_c_closes_help_and_the_palette_without_quitting() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.overlay.help = true;
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    assert!(!app.overlay.help);
    app.overlay.palette = true;
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('c')).await,
        Action::Continue
    ));
    assert!(!app.overlay.palette);
    assert_ne!(app.status_line, QUIT_HINT);
}
#[tokio::test]
async fn ctrl_c_interrupts_a_running_turn_instead_of_quitting() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.conn.start_turn();
    for _ in 0..2 {
        assert!(matches!(
            key_action(&mut app, &vendor, ctrl('c')).await,
            Action::Continue
        ));
        assert_eq!(app.status_line, crate::app::CANCELLING);
    }
}
#[tokio::test]
async fn ctrl_q_no_longer_quits() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    assert!(matches!(
        key_action(&mut app, &vendor, ctrl('q')).await,
        Action::Continue
    ));
}
#[tokio::test]
async fn ctrl_c_in_approval_dialog_pauses_the_active_goal() {
    let mut app = app();
    app.goals.set_goal(Some(
        octet_core::goal::Goal::new("Ship the project").unwrap(),
    ));
    app.goals.goal_prompt_sent();
    app.conn.start_turn();
    app.overlay.approvals.push_back((1, "command".into()));
    let temp = octet_testkit::TempDir::new("octet-goal-cancel");
    let directory = temp.path().to_path_buf();
    let config = Config::new(Engine::DEMO, "demo", directory.clone());
    let session = Session::open(config, directory.clone()).await.unwrap();
    key_action(
        &mut app,
        &session.handle,
        KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
    )
    .await;
    session.shutdown().await;
    assert_eq!(
        app.goals.goal().unwrap().status(),
        octet_core::goal::Status::Paused
    );
}
#[test]
fn cycle_follows_order_and_never_reaches_full_access() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    cycle_mode(&mut app, &vendor);
    app.conn.mode_pending = None;
    app.conn.mode = octet_core::Mode::Auto;
    cycle_mode(&mut app, &vendor);
    app.conn.mode_pending = None;
    app.conn.mode = octet_core::Mode::FullAccess;
    cycle_mode(&mut app, &vendor);
    assert!(app.status_line.contains("/mode"));
    assert_eq!(
        vendor.sent.take(),
        [
            Command::SetMode(octet_core::Mode::AcceptEdits),
            Command::SetMode(octet_core::Mode::Ask)
        ]
    );
}
#[tokio::test]
async fn unknown_command_keeps_the_draft() {
    let temp = octet_testkit::TempDir::new("octet-tui-draft");
    let directory = temp.path().to_path_buf();
    let config = Config::new(Engine::DEMO, "demo", directory.clone());
    let session = Session::open(config.clone(), directory.clone())
        .await
        .unwrap();
    let mut app = App::new(&config, session.journal().to_path_buf());
    let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
    assert!(
        app.composer
            .editor
            .insert("/nope is not a command, keep my words")
    );
    assert!(matches!(
        key_action(&mut app, &session.handle, enter).await,
        Action::Continue
    ));
    assert_eq!(
        app.composer.editor.text(),
        "/nope is not a command, keep my words"
    );
    assert!(app.status_line.contains("Unknown command"));
    app.composer.editor.take();
    // A path is a prompt, not a command.
    crate::test_support::idle(&mut app);
    assert!(
        app.composer
            .editor
            .insert("/usr/lib is where this breaks, please look")
    );
    assert!(matches!(
        key_action(&mut app, &session.handle, enter).await,
        Action::Continue
    ));
    assert!(
        app.composer.editor.text().is_empty() && app.conn.is_running(),
        "{}",
        app.status_line
    );
    assert_eq!(
        app.composer.history.back().map(|sent| sent.text.as_str()),
        Some("/usr/lib is where this breaks, please look")
    );
    crate::test_support::idle(&mut app);
    // A prompt starting with a multi-byte character is an ordinary prompt.
    assert!(app.composer.editor.insert("界 means world"));
    assert!(matches!(
        key_action(&mut app, &session.handle, enter).await,
        Action::Continue
    ));
    assert_eq!(
        app.composer.history.back().map(|sent| sent.text.as_str()),
        Some("界 means world")
    );
    crate::test_support::idle(&mut app);
    assert!(app.composer.editor.insert("/session"));
    assert!(matches!(
        key_action(&mut app, &session.handle, enter).await,
        Action::Continue
    ));
    assert!(app.composer.editor.text().is_empty());
    session.shutdown().await;
}
#[tokio::test]
async fn enter_while_running_queues_the_prompt() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.conn.start_turn();
    assert!(app.composer.editor.insert("next please"));
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.queue.len(), 1);
    assert!(app.composer.editor.text().is_empty());
    assert!(crate::view::composer_title(&app).contains("+1 queued"));
}
#[tokio::test]
async fn cancel_drops_the_queue() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.conn.start_turn();
    app.composer.queue.push_back(Prompt::plain("a".into()));
    app.composer.queue.push_back(Prompt::plain("b".into()));
    key_action(&mut app, &vendor, key(KeyCode::Esc)).await;
    assert!(app.composer.queue.is_empty());
    assert!(app.entries_text().contains("Dropped 2 queued prompts"));
}
#[tokio::test]
async fn the_queue_is_bounded() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.conn.start_turn();
    for i in 0..8 {
        app.composer.queue.push_back(Prompt::plain(format!("p{i}")));
    }
    assert!(app.composer.editor.insert("one more"));
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.queue.len(), 8);
    assert_eq!(app.composer.editor.text(), "one more");
    assert!(
        app.status_line.contains("queue is full"),
        "{}",
        app.status_line
    );
}
#[tokio::test]
async fn a_failed_image_keeps_the_draft() {
    let dir = image_workspace("octet-image-draft", &["notes.txt"]);
    let mut app = app();
    app.composer.root = dir.path().to_path_buf();
    let vendor = RecordingVendor::default();
    assert!(app.composer.editor.insert("/image notes.txt"));
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text(), "/image notes.txt");
    assert!(app.entries_text().contains("not a PNG"));
    app.composer.editor.take();
    std::fs::write(dir.path().join("ok.png"), b"x").unwrap();
    assert!(app.composer.editor.insert("/image ok.png"));
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    assert!(app.composer.editor.text().is_empty());
}
#[tokio::test]
async fn steer_while_cancelling_queues_it() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.conn.start_turn();
    cancel_turn(&mut app, &vendor).await;
    steer(&mut app, &vendor, "look at the tests".into());
    assert_eq!(
        app.composer.queue.front(),
        Some(&Prompt::plain("look at the tests".into()))
    );
    assert!(app.entries_text().contains("The turn is stopping"));
    // The turn's end clears the state; the queued prompt then goes.
    app.event(octet_core::Event::Finished {
        outcome: octet_core::Outcome::Interrupted,
    });
    assert!(!app.conn.is_cancelling());
}
#[tokio::test]
async fn one_tab_listing_at_a_time() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    // A listing still stuck on a slow folder from an earlier Tab.
    app.composer
        .listing
        .store(true, std::sync::atomic::Ordering::SeqCst);
    assert!(app.composer.editor.insert("/tm"));
    key_action(&mut app, &vendor, key(KeyCode::Tab)).await;
    assert!(
        app.status_line.starts_with("Still reading"),
        "{}",
        app.status_line
    );
    assert_eq!(app.composer.editor.text(), "/tm");
    // A listing that finishes frees the next Tab.
    app.composer
        .listing
        .store(false, std::sync::atomic::Ordering::SeqCst);
    key_action(&mut app, &vendor, key(KeyCode::Tab)).await;
    assert!(
        !app.composer
            .listing
            .load(std::sync::atomic::Ordering::SeqCst)
    );
}
#[tokio::test]
async fn an_answer_key_right_after_an_approval_is_ignored() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    app.event(octet_core::Event::Approval {
        id: 7,
        detail: "rm -rf build".into(),
    });
    // A letter typed as the dialog opens must not answer it.
    key_action(&mut app, &vendor, key(KeyCode::Char('a'))).await;
    assert!(
        vendor.sent.borrow().is_empty(),
        "{:?}",
        vendor.sent.borrow()
    );
    assert_eq!(app.overlay.approvals.len(), 1);
    // Once the dialog has been up a moment, the same key answers.
    // Shown on screen a moment ago.
    app.overlay.approval_shown = std::time::Instant::now().checked_sub(Duration::from_secs(1));
    key_action(&mut app, &vendor, key(KeyCode::Char('a'))).await;
    assert!(matches!(
        vendor.sent.borrow().as_slice(),
        [Command::Answer { id: 7, allow: true }]
    ));
}
#[tokio::test]
async fn esc_while_connecting_does_not_leave_cancelling() {
    let mut app = app();
    // No event returns a live session to connecting without resetting the
    // state under test, so the phase is set.
    app.conn.phase = ConnPhase::Connecting;
    let vendor = RecordingVendor::default();
    key_action(&mut app, &vendor, key(KeyCode::Esc)).await;
    assert_eq!(app.status_line, crate::app::CANCELLING);
    // The cancelled connection reports an error and stops; no turn finishes.
    app.event(octet_core::Event::Error("Connection cancelled".into()));
    app.event(octet_core::Event::Stopped);
    assert_ne!(app.status_line, crate::app::CANCELLING);
}
#[test]
fn paste_keeps_tabs() {
    let mut app = app();
    paste(&mut app, "all:\n\tmake build");
    assert_eq!(app.composer.editor.text(), "all:\n\tmake build");
}
#[test]
fn paste_into_an_overlay_hints() {
    let mut app = app();
    app.overlay.help = true;
    paste(&mut app, "lost");
    assert!(app.status_line.contains("Close"), "{}", app.status_line);
}
#[test]
fn size_label_says_kib() {
    assert_eq!(size_label(2048), "2.0 KiB");
    assert_eq!(size_label(10), "10 B");
}
#[test]
fn a_queued_prompt_that_fails_to_send_is_kept() {
    let mut app = app();
    app.composer
        .queue
        .push_back(crate::vendor::Prompt::plain("next".into()));
    let vendor = RecordingVendor::default();
    vendor.refuse.set(true);
    crate::send_queued(&mut app, &vendor);
    assert_eq!(app.composer.queue.len(), 1, "the prompt was lost");
}
#[tokio::test]
async fn cancel_interrupts_before_saving_the_goal() {
    /// Notes what the goal file said when the interrupt arrived.
    struct Watching {
        file: std::path::PathBuf,
        seen: std::cell::RefCell<String>,
    }
    impl crate::vendor::Vendor for Watching {
        fn send(&self, _: Command) -> Result<(), octet_core::SendError> {
            Ok(())
        }
        fn interrupt(&self) {
            *self.seen.borrow_mut() = std::fs::read_to_string(&self.file).unwrap_or_default();
        }
    }
    let temp = octet_testkit::TempDir::new("octet-cancel-order");
    let mut app = app();
    let store = octet_core::goal::GoalStore::new(temp.path(), std::path::Path::new("/w"));
    app.goals.attach(store).await.unwrap();
    app.goals.start("Ship it").await.unwrap();
    app.goals.goal_prompt_sent();
    let file = std::fs::read_dir(temp.path())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().is_some_and(|e| e == "json"))
        .unwrap();
    let vendor = Watching {
        file,
        seen: std::cell::RefCell::default(),
    };
    cancel_turn(&mut app, &vendor).await;
    // The cancel went out before the pause was written.
    assert!(
        vendor.seen.borrow().contains("\"active\""),
        "{}",
        vendor.seen.borrow()
    );
}
#[test]
fn accepting_a_model_suggestion_inserts_only_the_name() {
    let mut app = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    app.composer.editor.set("/model gpt".into());
    app.composer.completion = Some(composer::Completion {
        kind: composer::Kind::Model,
        items: vec!["gpt-6-astra · codex".into()],
        selected: 0,
        start: 7,
    });
    crate::input::accept_completion(&mut app);
    assert_eq!(app.composer.editor.text(), "/model gpt-6-astra ");
}
