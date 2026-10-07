//! Slash commands, as the user types them.
use super::*;

#[tokio::test]
async fn model_command_rejects_busy_switch_and_preserves_current_session() {
    let mut app = app();
    app.conn.start_turn();
    assert!(matches!(
        command(&mut app, "/model claude example").await,
        Action::Continue
    ));
    assert_eq!(app.conn.engine, octet_core::Engine::CODEX);
    assert_eq!(app.conn.session, "thread-1");
    app.conn.phase = ConnPhase::Idle;
    let Action::Exit(Exit::Model(selection)) = command(&mut app, "/model claude example").await
    else {
        panic!("expected a model switch");
    };
    assert_eq!(selection.provider, octet_core::Engine::CLAUDE);
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
    app.goals.set_goal(Some(
        octet_core::goal::Goal::new("Ship the project").unwrap(),
    ));
    assert!(matches!(
        command(&mut app, "/goal pause").await,
        Action::Continue
    ));
    assert!(
        app.status_line
            .starts_with("Goal paused, but saving it failed:"),
        "{}",
        app.status_line
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
    app.goals.set_goal(Some(goal));
    command(&mut app, "/goal pause").await;
    assert_eq!(
        app.goals.goal().unwrap().status,
        octet_core::goal::Status::Complete
    );
    assert!(matches!(
        command(&mut app, "/goal resume").await,
        Action::Continue
    ));
}
#[tokio::test]
async fn copy_reports_the_size_of_the_last_reply() {
    let mut app = app();
    assert!(matches!(command(&mut app, "/copy").await, Action::Continue));
    assert_eq!(app.status_line, "Nothing to copy yet");
    app.event(octet_core::Event::Started);
    app.event(octet_core::Event::Text("hello".into()));
    app.event(octet_core::Event::Finished {
        outcome: octet_core::Outcome::Completed,
    });
    assert_eq!(app.last_reply(), Some("hello"));
    assert!(matches!(command(&mut app, "/copy").await, Action::Continue));
    assert_eq!(app.status_line, "Copied 5 B to the clipboard");
}
#[tokio::test]
async fn remote_control_reports_without_changing_the_session() {
    let mut app = app();
    let action = command(&mut app, "/remote-control").await;
    assert!(matches!(&action, Action::Job(job) if job.label == "Checking phone access…"));
    // Under test the checks run stand-in program names that never exist,
    // so the result doesn't depend on what this machine has installed.
    run_job(&mut app, action).await;
    let text = app.entries_text();
    assert!(text.contains("Remote control setup"), "{text}");
    assert!(text.contains("[!!] Tailscale isn't connected"), "{text}");
    assert!(
        app.status_line.starts_with("Remote control: "),
        "{}",
        app.status_line
    );
    assert!(
        app.status_line.ends_with("problems (report above)"),
        "{}",
        app.status_line
    );
    assert_eq!(app.conn.session, "thread-1");
}
#[tokio::test]
async fn goal_commands_start_pause_resume_and_clear_without_a_vendor_turn() {
    let mut app = app();
    assert!(matches!(
        sends(&mut app, "/goal Ship the project").await[..],
        [Command::PromptWithDisplay { .. }]
    ));
    assert_eq!(app.goals.goal().unwrap().objective, "Ship the project");
    assert!(matches!(
        command(&mut app, "/goal pause").await,
        Action::Continue
    ));
    assert_eq!(
        app.goals.goal().unwrap().status,
        octet_core::goal::Status::Paused
    );
    app.conn.phase = ConnPhase::Idle;
    assert!(matches!(
        sends(&mut app, "/goal resume").await[..],
        [Command::PromptWithDisplay { .. }]
    ));
    let goal = app.goals.goal_mut().unwrap();
    goal.status = octet_core::goal::Status::Paused;
    goal.turns = octet_core::goal::MAX_GOAL_TURNS;
    assert!(matches!(
        command(&mut app, "/goal resume").await,
        Action::Continue
    ));
    assert_eq!(
        app.goals.goal().unwrap().status,
        octet_core::goal::Status::Paused
    );
    assert!(matches!(
        command(&mut app, "/goal clear").await,
        Action::Continue
    ));
    assert!(app.goals.goal().is_none());
}
#[tokio::test]
async fn mode_command_switches_live_modes_and_rejects_unknown() {
    let mut app = app();
    assert_eq!(
        sends(&mut app, "/mode auto").await,
        [Command::SetMode(octet_core::Mode::Auto)]
    );
    assert_eq!(app.conn.mode_pending, Some(octet_core::Mode::Auto));
    app.conn.mode_pending = None;
    app.conn.start_turn();
    assert_eq!(
        sends(&mut app, "/mode accept-edits").await,
        [Command::SetMode(octet_core::Mode::AcceptEdits)]
    );
    app.conn.mode_pending = None;
    assert!(matches!(
        command(&mut app, "/mode yolo").await,
        Action::Continue
    ));
    assert!(app.status_line.contains("Unknown mode"));
    assert!(matches!(command(&mut app, "/mode").await, Action::Continue));
    assert!(app.status_line.contains("auto_review"));
}
#[tokio::test]
async fn full_access_requires_idle_ready_session() {
    let mut app = app();
    app.conn.start_turn();
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Continue
    ));
    app.conn.phase = ConnPhase::Idle;
    app.overlay.approvals.push_back((1, "x".into()));
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Continue
    ));
    app.overlay.approvals.clear();
    app.conn.phase = ConnPhase::Connecting;
    assert!(matches!(
        command(&mut app, "/mode full-access").await,
        Action::Continue
    ));
    app.conn.phase = ConnPhase::Idle;
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
#[tokio::test]
async fn cycle_is_ignored_while_a_switch_is_pending() {
    let mut app = app();
    app.conn.mode_pending = Some(octet_core::Mode::AcceptEdits);
    assert!(matches!(
        cycle_mode(&mut app, &RecordingVendor::default()),
        Action::Continue
    ));
    assert!(app.status_line.contains("pending"));
    assert!(matches!(
        command(&mut app, "/mode auto").await,
        Action::Continue
    ));
    assert!(app.status_line.contains("pending"));
}
#[tokio::test]
async fn stopped_session_can_always_leave_full_access() {
    let mut app = app();
    app.conn.mode = octet_core::Mode::FullAccess;
    app.conn.phase = ConnPhase::Connecting;
    app.conn.phase = ConnPhase::Stopped;
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
#[tokio::test]
async fn slash_queue_lists_and_clears() {
    let mut app = app();
    app.composer
        .queue
        .push_back(Prompt::plain("first queued".into()));
    command(&mut app, "/queue").await;
    assert!(app.entries_text().contains("first queued"));
    command(&mut app, "/queue clear").await;
    assert!(app.composer.queue.is_empty());
}
#[tokio::test]
async fn effort_command_shows_and_sets() {
    let mut app = app();
    command(&mut app, "/effort").await;
    assert!(
        app.entries_text()
            .contains("Reasoning effort: vendor default")
    );
    // Codex takes effort per turn: no reconnect.
    assert_eq!(
        sends(&mut app, "/effort high").await,
        [Command::SetEffort(Some("high".into()))]
    );
    assert_eq!(app.conn.effort.as_deref(), Some("high"));
    // Claude takes it at launch: reconnect to the same session.
    let mut claude = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    claude.conn.phase = ConnPhase::Idle;
    assert!(matches!(
        command(&mut claude, "/effort max").await,
        Action::Exit(Exit::Effort(Some(ref level))) if level == "max"
    ));
    assert!(matches!(
        command(&mut app, "/effort two words").await,
        Action::Continue
    ));
}
#[tokio::test]
async fn fork_needs_an_idle_vendor_session() {
    let mut app = app();
    assert!(matches!(
        command(&mut app, "/fork").await,
        Action::Exit(Exit::Fork)
    ));
    app.conn.start_turn();
    assert!(matches!(command(&mut app, "/fork").await, Action::Continue));
    assert_eq!(app.status_line, TURN_OPEN);
    let mut fresh = self::app();
    fresh.conn.session.clear();
    assert!(matches!(
        command(&mut fresh, "/fork").await,
        Action::Continue
    ));
    assert!(
        fresh
            .entries_text()
            .contains("No vendor session to fork yet")
    );
    let mut demo = crate::test_support::app_for(octet_core::Engine::DEMO);
    demo.conn.phase = ConnPhase::Idle;
    assert!(matches!(
        command(&mut demo, "/fork").await,
        Action::Continue
    ));
    assert!(
        demo.entries_text()
            .contains("The offline demo has no context to fork")
    );
}
#[tokio::test]
async fn compact_needs_an_idle_session() {
    let mut app = app();
    assert_eq!(sends(&mut app, "/compact").await, [Command::Compact]);
    assert!(app.conn.is_running());
    app.conn.start_turn();
    assert!(matches!(
        command(&mut app, "/compact").await,
        Action::Continue
    ));
    assert_eq!(app.status_line, TURN_OPEN);
}
#[tokio::test]
async fn image_attaches_and_shows_in_the_title() {
    let dir = image_workspace("octet-image-title", &["shot.png"]);
    let mut app = app();
    app.composer.root = dir.path().to_path_buf();
    command(&mut app, "/image shot.png").await;
    assert_eq!(app.composer.images.len(), 1);
    assert!(crate::view::composer_title(&app).contains("+ image shot.png"));
    let (_temp, session) = demo_session("octet-image-send").await;
    app.conn.start_turn();
    assert!(app.composer.editor.insert("look"));
    key_action(&mut app, &session.handle, key(KeyCode::Enter)).await;
    assert!(app.composer.images.is_empty());
    let queued = app.composer.queue.front().unwrap();
    assert_eq!(queued.wire, "look");
    assert_eq!(queued.display, "look\n[+ image shot.png]");
    assert_eq!(queued.images.len(), 1);
}
#[tokio::test]
async fn oversized_or_unknown_images_are_refused() {
    let dir = image_workspace(
        "octet-image-refuse",
        &["notes.txt", "a.png", "b.png", "c.png", "d.png", "e.png"],
    );
    std::fs::File::create(dir.path().join("big.png"))
        .unwrap()
        .set_len(octet_core::IMAGE_LIMIT + 1)
        .unwrap();
    let mut app = app();
    app.composer.root = dir.path().to_path_buf();
    command(&mut app, "/image notes.txt").await;
    command(&mut app, "/image big.png").await;
    command(&mut app, "/image gone.png").await;
    assert!(app.composer.images.is_empty());
    let text = app.entries_text();
    assert!(text.contains("notes.txt is not a PNG, JPEG, GIF or WebP image"));
    assert!(text.contains("big.png is over 5 MiB"));
    assert!(text.contains("Cannot read gone.png"));
    for file in ["a.png", "b.png", "c.png", "d.png", "e.png"] {
        command(&mut app, &format!("/image {file}")).await;
    }
    assert_eq!(app.composer.images.len(), 4);
    assert!(
        app.entries_text()
            .contains("A prompt takes at most 4 images")
    );
    assert!(crate::view::composer_title(&app).contains("+4 images"));
    // Esc on an empty prompt drops them.
    let (_temp, session) = demo_session("octet-image-esc").await;
    key_action(&mut app, &session.handle, key(KeyCode::Esc)).await;
    assert!(app.composer.images.is_empty());
}
#[tokio::test]
async fn resume_picks_the_listed_session() {
    let temp = octet_testkit::TempDir::new("octet-tui-sessions");
    let dir = temp.path();
    std::fs::create_dir_all(dir).unwrap();
    for (stamp, engine, session, prompt) in [
        (1, "codex", "t-1", "first codex prompt"),
        (2, "claude", "c-1", "first claude prompt"),
    ] {
        let data = serde_json::json!({"engine":engine,"cwd":"/work","resume":null,"model":"m1","mode":"ask"});
        let records = [
            ("session", data),
            ("ready", serde_json::json!(session)),
            ("user", serde_json::json!(prompt)),
        ];
        octet_testkit::write_journal(dir, stamp, &records, "");
    }
    let mut app = App::new(
        &Config::new(Engine::CODEX, "codex", "/work"),
        dir.join("session-3-1.jsonl"),
    );
    app.conn.phase = ConnPhase::Idle;
    app.conn.session = "t-1".into();
    assert!(matches!(
        command(&mut app, "/resume 1").await,
        Action::Continue
    ));
    assert!(app.entries_text().contains("Run /sessions first"));
    let action = command(&mut app, "/sessions").await;
    run_job(&mut app, action).await;
    let text = app.entries_text();
    assert!(text.contains("1. claude · c-1"), "{text}");
    assert!(text.contains("first claude prompt"));
    assert!(text.contains("2. codex · t-1 (this session)"), "{text}");
    match command(&mut app, "/resume 1").await {
        Action::Exit(Exit::Resume {
            engine,
            session,
            model,
        }) => {
            assert_eq!(engine, Engine::CLAUDE);
            assert_eq!(session, "c-1");
            assert_eq!(model.as_deref(), Some("m1"));
        }
        _ => panic!("expected a resume"),
    }
    assert!(matches!(
        command(&mut app, "/resume 2").await,
        Action::Continue
    ));
    assert!(app.entries_text().contains("That is this session"));
    command(&mut app, "/resume 9").await;
    assert!(
        app.entries_text()
            .contains("No session 9; /sessions listed 2")
    );
}
#[tokio::test]
async fn claude_refuses_images_it_cannot_take_inline() {
    let dir = image_workspace("octet-image-inline", &[]);
    for name in ["a.png", "b.png", "big.png"] {
        let size = if name == "big.png" { 4 } else { 3 } * 1024 * 1024;
        std::fs::File::create(dir.path().join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let mut app = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    app.composer.root = dir.path().to_path_buf();
    command(&mut app, "/image big.png").await;
    assert!(
        app.entries_text()
            .contains("big.png is over 3.75 MiB, the largest image Claude accepts")
    );
    command(&mut app, "/image a.png").await;
    command(&mut app, "/image b.png").await;
    assert_eq!(app.composer.images.len(), 1);
    assert!(app.entries_text().contains("over 5.25 MiB together"));
    // Codex reads the files itself, so its limit is the file size.
    let mut codex = self::app();
    codex.composer.root = dir.path().to_path_buf();
    command(&mut codex, "/image big.png").await;
    assert_eq!(codex.composer.images.len(), 1);
}
#[tokio::test]
async fn up_recalls_a_prompt_with_its_images() {
    let dir = image_workspace("octet-image-recall", &["shot.png"]);
    let mut app = app();
    app.composer.root = dir.path().to_path_buf();
    let (_temp, session) = demo_session("octet-image-recall-session").await;
    command(&mut app, "/image shot.png").await;
    app.conn.start_turn();
    assert!(app.composer.editor.insert("look"));
    key_action(&mut app, &session.handle, key(KeyCode::Enter)).await;
    assert!(app.composer.images.is_empty());
    app.recall(true);
    assert_eq!(app.composer.editor.text, "look");
    assert_eq!(app.composer.images.len(), 1);
    assert!(crate::view::composer_title(&app).contains("+ image shot.png"));
    // Back down: the empty draft, without the recalled images.
    app.recall(false);
    assert!(app.composer.editor.text.is_empty());
    assert!(app.composer.images.is_empty());
}
#[tokio::test]
async fn effort_refuses_a_level_claude_does_not_take() {
    let mut claude = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    claude.conn.phase = ConnPhase::Idle;
    assert!(matches!(
        command(&mut claude, "/effort bogus").await,
        Action::Continue
    ));
    assert!(
        claude
            .entries_text()
            .contains("Claude takes effort low, medium, high, xhigh or max")
    );
}
#[tokio::test]
async fn every_command_needing_no_open_turn_refuses_the_same_way() {
    let guarded: Vec<&Spec> = COMMANDS
        .iter()
        .filter(|spec| spec.requires != Requires::Nothing)
        .collect();
    assert!(guarded.len() >= 6, "the registry lost its guards");
    for spec in guarded {
        for open in ["running", "approval"] {
            let mut app = app();
            if open == "running" {
                app.conn.start_turn();
            } else {
                app.overlay.approvals.push_back((1, "x".into()));
            }
            let vendor = RecordingVendor::default();
            let action = commands::command(&mut app, &vendor, &format!("{} 1", spec.name)).await;
            assert!(matches!(action, Action::Continue), "{} ({open})", spec.name);
            assert_eq!(app.status_line, TURN_OPEN, "{} ({open})", spec.name);
            assert!(vendor.sent.borrow().is_empty(), "{} sent", spec.name);
        }
    }
}
#[tokio::test]
async fn sessions_listing_runs_off_the_loop() {
    let mut app = app();
    let action = command(&mut app, "/sessions").await;
    // Nothing is read yet: the journals are read by the job, not the key.
    assert!(matches!(&action, Action::Job(job) if job.label == "Reading recent sessions…"));
    assert!(app.conn.listed.is_empty());
    assert!(!app.entries_text().contains("No earlier vendor sessions"));
    run_job(&mut app, action).await;
    assert!(app.entries_text().contains("No earlier vendor sessions"));
}

/// A job that ends with `done` after `delay`, or never.
fn job_after(delay: Option<Duration>, done: fn() -> crate::jobs::Done) -> crate::jobs::Job {
    crate::jobs::Job {
        label: "Exporting the journal…",
        work: Box::pin(async move {
            match delay {
                Some(delay) => tokio::time::sleep(delay).await,
                None => std::future::pending().await,
            }
            done()
        }),
    }
}
fn exported() -> crate::jobs::Done {
    crate::jobs::Done::Exported {
        path: "out.jsonl".into(),
        result: Ok(()),
    }
}
#[tokio::test]
async fn a_second_job_keeps_the_draft() {
    let mut app = app();
    let (_temp, session) = demo_session("octet-second-job").await;
    app.job = Some(crate::jobs::Running::spawn(job_after(None, exported)));
    assert!(app.composer.editor.insert("/sessions"));
    key_action(&mut app, &session.handle, key(KeyCode::Enter)).await;
    assert_eq!(app.composer.editor.text, "/sessions");
    assert_eq!(app.status_line, "Wait for: Exporting the journal…");
}
#[tokio::test]
async fn a_session_ending_waits_for_its_job_and_reports_it() {
    let running = crate::jobs::Running::spawn(job_after(Some(Duration::from_millis(50)), exported));
    let ended = crate::jobs::finish(Some(running), Duration::from_secs(5)).await;
    // Shown in whichever interface comes next.
    let mut next = app();
    crate::jobs::apply(&mut next, ended.expect("a job was running"));
    assert!(
        next.entries_text()
            .contains("Exported journal to out.jsonl")
    );
}
#[tokio::test]
async fn a_job_that_outlives_its_session_is_stopped_with_a_note() {
    let running = crate::jobs::Running::spawn(job_after(None, exported));
    let started = std::time::Instant::now();
    let ended = crate::jobs::finish(Some(running), Duration::from_millis(50)).await;
    assert!(started.elapsed() < Duration::from_secs(2));
    let mut next = app();
    crate::jobs::apply(&mut next, ended.expect("a job was running"));
    assert!(
        next.entries_text()
            .contains("Exporting the journal… did not finish"),
        "{}",
        next.entries_text()
    );
    assert!(
        crate::jobs::finish(None, Duration::from_secs(5))
            .await
            .is_none()
    );
}
