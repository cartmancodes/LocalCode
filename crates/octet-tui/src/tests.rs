use super::*;
use crate::app::ConnPhase;
use crate::{
    commands::{self, *},
    input::*,
    reconnect::*,
    registry::*,
    vendor::{Prompt, RecordingVendor},
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use octet_core::Engine;
/// Finishes the off-loop job `action` asked for, as the event loop would.
async fn run_job(app: &mut App, action: Action) {
    let Action::Job(job) = action else {
        panic!("no job was started")
    };
    crate::jobs::apply(app, crate::jobs::Ended::Done(job.work.await));
}
/// Runs a command; what it sent the session.
async fn sends(app: &mut App, input: &str) -> Vec<Command> {
    let vendor = RecordingVendor::default();
    commands::command(app, &vendor, input).await;
    vendor.sent.take()
}
/// Runs a command against a stand-in session that records what it sent.
async fn command(app: &mut App, input: &str) -> Action {
    commands::command(app, &RecordingVendor::default(), input).await
}
fn app() -> App {
    let config = Config::new(Engine::CODEX, "codex", "/tmp");
    let mut app = App::new(&config, "journal".into());
    app.conn.phase = ConnPhase::Idle;
    app.conn.session = "thread-1".into();
    app
}
fn ctrl(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
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
#[test]
fn a_finished_command_replaces_the_running_notice() {
    let mut app = app();
    app.composer.shell_running = true;
    app.status_line = "Running echo hi · Esc to stop".into();
    shell_finished(&mut app, Ok(ran("echo hi", "hi\n")), true);
    assert!(!app.composer.shell_running);
    assert_eq!(app.status_line, "$ echo hi · exit 0");
    assert_eq!(app.composer.attachments.len(), 1);
    let gone = shell::ShellError::Run {
        shell: "/x".into(),
        source: std::io::Error::other("gone"),
    };
    shell_finished(&mut app, Err(gone), false);
    assert_eq!(app.status_line, "Cannot run /x: gone");
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
async fn goal_prompts_never_carry_attachments() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-goal-attach").await;
    app.attach(ran("ls", "a\n"));
    send_goal_prompt(&mut app, &session.handle, "continue the goal".into()).await;
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
    let config = Config::new(Engine::DEMO, "demo", directory.clone());
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
#[test]
fn full_access_notice_says_what_the_reconnect_does() {
    use octet_core::Mode;
    let entering = full_access_notice(Mode::FullAccess, Engine::CLAUDE, "session-1");
    assert!(entering.starts_with("Full access:"), "{entering}");
    assert!(
        entering.ends_with("Reconnecting to the same session…"),
        "{entering}"
    );
    assert!(
        full_access_notice(Mode::FullAccess, Engine::CLAUDE, "")
            .ends_with("Starting a new session (no session ID yet)…")
    );
    assert!(
        full_access_notice(Mode::Ask, Engine::DEMO, "demo · offline")
            .ends_with("Restarting the offline demo…")
    );
    assert!(
        full_access_notice(Mode::Ask, Engine::CODEX, "thread")
            .starts_with("Leaving full access for ask.")
    );
}
#[tokio::test]
async fn reconnect_pauses_an_active_goal_and_names_the_previous_journal() {
    let mut app = app();
    app.goals
        .set_goal(Some(octet_core::goal::Goal::new("Ship it").unwrap()));
    pause_active_goal(&mut app, "reconnect").await;
    assert_eq!(
        app.goals.goal().unwrap().status(),
        octet_core::goal::Status::Paused
    );
    assert!(app.status_line.contains("Goal paused for reconnect"));
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
#[tokio::test]
async fn a_finished_turn_sends_the_next_queued_prompt() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-queue-send").await;
    app.conn.start_turn();
    app.composer
        .queue
        .push_back(Prompt::plain("queued one".into()));
    session_event(
        &mut app,
        &session.handle,
        octet_core::Event::Finished {
            outcome: octet_core::Outcome::Completed,
        },
    )
    .await;
    assert_eq!(next_user_text(&mut session).await, "queued one");
    assert!(app.composer.queue.is_empty());
    assert!(app.conn.is_running());
}
#[tokio::test]
async fn steer_on_claude_queues_a_follow_up() {
    let config = Config::new(Engine::CLAUDE, "claude", "/tmp");
    let mut app = App::new(&config, "journal".into());
    let (_temp, session) = demo_session("octet-steer-claude").await;
    app.conn.start_turn();
    steer(&mut app, &session.handle, "also check docs".into());
    assert_eq!(app.composer.queue.len(), 1);
    assert!(
        app.entries_text().contains("Claude cannot steer"),
        "{}",
        app.entries_text()
    );
}
#[tokio::test]
async fn steer_when_idle_sends_a_prompt() {
    let mut app = app();
    let (_temp, mut session) = demo_session("octet-steer-idle").await;
    app.conn.phase = ConnPhase::Idle;
    steer(&mut app, &session.handle, "plain prompt".into());
    assert_eq!(next_user_text(&mut session).await, "plain prompt");
    assert!(app.conn.is_running());
}
/// A workspace holding `files`, each one byte long.
fn image_workspace(prefix: &str, files: &[&str]) -> octet_testkit::TempDir {
    let dir = octet_testkit::TempDir::new(prefix);
    std::fs::create_dir_all(dir.path()).unwrap();
    for file in files {
        std::fs::write(dir.path().join(file), b"x").unwrap();
    }
    dir
}
#[test]
fn image_paths_resolve_against_the_workspace_and_home() {
    use std::path::Path;
    let root = Path::new("/work");
    let home = Some(Path::new("/home/me"));
    assert_eq!(
        commands::image_path(root, home, "shots/a.png"),
        Path::new("/work/shots/a.png")
    );
    assert_eq!(
        commands::image_path(root, home, "~/a.png"),
        Path::new("/home/me/a.png")
    );
    assert_eq!(
        commands::image_path(root, home, "/abs/a.png"),
        Path::new("/abs/a.png")
    );
    assert_eq!(
        commands::image_path(root, None, "~/a.png"),
        Path::new("/work/~/a.png")
    );
}
#[test]
fn a_stopped_session_drops_the_queue() {
    let mut app = app();
    app.conn.start_turn();
    app.composer.queue.push_back(Prompt::plain("a".into()));
    app.composer.queue.push_back(Prompt::plain("b".into()));
    app.event(octet_core::Event::Stopped);
    assert!(app.composer.queue.is_empty());
    assert!(app.entries_text().contains("Dropped 2 queued prompts"));
}
#[test]
fn dragged_image_paths_are_unquoted() {
    use std::path::Path;
    let root = Path::new("/work");
    let home = Some(Path::new("/home/me"));
    for (typed, wanted) in [
        ("'/x/Shot 1.png'", "/x/Shot 1.png"),
        ("\"/x/Shot 1.png\"", "/x/Shot 1.png"),
        (r"/x/Shot\ 1.png", "/x/Shot 1.png"),
        (r"~/My\ Shots/a.png", "/home/me/My Shots/a.png"),
    ] {
        assert_eq!(
            commands::image_path(root, home, typed),
            Path::new(wanted),
            "{typed}"
        );
    }
}
#[test]
fn a_fork_stays_pending_until_the_vendor_names_it() {
    let mut config = Config::new(Engine::CLAUDE, "claude", "/tmp");
    config.resume = Some("original".into());
    config.fork = true;
    settle_fork(&mut config, "");
    assert!(config.fork);
    settle_fork(&mut config, "original");
    assert!(config.fork);
    settle_fork(&mut config, "forked");
    assert!(!config.fork);
}
#[test]
fn a_fork_is_announced_when_the_vendor_names_it() {
    let mut app = app();
    app.conn.forking_from = Some("original".into());
    // Claude reports no session until the fork's first turn.
    app.event(octet_core::Event::Ready {
        session: String::new(),
    });
    assert!(!app.entries_text().contains("Forked from"));
    app.event(octet_core::Event::Ready {
        session: "forked".into(),
    });
    assert!(
        app.entries_text()
            .contains("Forked from original into forked")
    );
    assert!(app.conn.forking_from.is_none());
    let mut failed = self::app();
    failed.conn.forking_from = Some("original".into());
    failed.event(octet_core::Event::Stopped);
    assert!(
        failed
            .entries_text()
            .contains("The fork from original did not open; /reconnect tries again")
    );
}
#[test]
fn resume_opens_the_session_without_forking_and_with_an_effort_it_takes() {
    let mut config = Config::new(Engine::CODEX, "codex", "/tmp");
    // A fork Codex had not named yet must not turn the resume into a fork.
    config.resume = Some("original".into());
    config.fork = true;
    config.effort = Some("minimal".into());
    let notice = resume_into(
        &mut config,
        Engine::CLAUDE,
        "c-1".into(),
        Some("m1".into()),
        "claude".into(),
    );
    assert_eq!(config.engine, Engine::CLAUDE);
    assert_eq!(config.resume.as_deref(), Some("c-1"));
    assert!(!config.fork);
    assert_eq!(config.model.as_deref(), Some("m1"));
    assert_eq!(config.effort, None);
    assert_eq!(
        notice.as_deref(),
        Some("Claude does not take effort minimal; using its default")
    );
    config.effort = Some("high".into());
    assert_eq!(
        resume_into(
            &mut config,
            Engine::CLAUDE,
            "c-2".into(),
            None,
            "claude".into()
        ),
        None
    );
    assert_eq!(config.effort.as_deref(), Some("high"));
}
#[test]
fn a_dropped_effort_is_named() {
    assert_eq!(
        dropped_effort(Some("minimal"), Engine::CLAUDE, None).as_deref(),
        Some("Claude does not take effort minimal; using its default")
    );
    assert_eq!(
        dropped_effort(Some("high"), Engine::CLAUDE, Some("high")),
        None
    );
    assert_eq!(dropped_effort(None, Engine::CLAUDE, None), None);
}
#[test]
fn browsing_history_keeps_the_drafts_own_images() {
    let dir = image_workspace("octet-image-browse", &["draft.png"]);
    let mut app = app();
    app.remember("earlier".into());
    let image = octet_core::ImageAttachment::open(&dir.path().join("draft.png")).unwrap();
    app.composer.images.push(image);
    app.recall(true);
    assert_eq!(app.composer.editor.text, "earlier");
    assert_eq!(
        app.composer.images.len(),
        1,
        "the draft's image stays attached"
    );
    app.recall(false);
    assert_eq!(app.composer.images.len(), 1);
}
#[test]
fn begin_turn_marks_nothing_when_the_send_fails() {
    let mut app = app();
    let vendor = RecordingVendor::default();
    vendor.refuse.set(true);
    let sent = app.begin_turn(
        &vendor,
        Command::Prompt("hello".into()),
        crate::vendor::By::User,
        "sending",
    );
    assert!(sent.is_err());
    assert!(app.is_idle(), "a refused send must not start a turn");
    assert_ne!(app.conn.status, "sending");
}
#[tokio::test]
async fn idle_steer_sends_attachments_like_enter() {
    let mut app = app();
    app.attach(crate::shell::Ran {
        command: "git status".into(),
        status: crate::shell::Status::Exited(0),
        output: "clean\n".into(),
    });
    let vendor = RecordingVendor::default();
    steer(&mut app, &vendor, "look".into());
    match &vendor.sent.borrow()[..] {
        [Command::PromptWithDisplay { wire, display, .. }] => {
            assert!(wire.contains("clean"), "{wire}");
            assert_eq!(display, "look\n\n[+ git status]");
        }
        other => panic!("{other:?}"),
    }
    assert!(app.composer.attachments.is_empty());
}
#[test]
fn one_dropped_prompt_is_singular() {
    let mut app = app();
    app.conn.start_turn();
    app.composer.queue.push_back(Prompt::plain("a".into()));
    app.event(octet_core::Event::Stopped);
    let text = app.entries_text();
    assert!(
        text.contains("Dropped 1 queued prompt: the session stopped"),
        "{text}"
    );
}

mod keys;
mod slash;
