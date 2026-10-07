#![expect(
    unused_must_use,
    reason = "tests drive keys and commands for their effect and often ignore the action"
)]
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
    // Connected, as the vendor reports it.
    app.event(octet_core::Event::Ready {
        session: "thread-1".into(),
    });
    app
}
fn ctrl(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
}
#[test]
fn sizes_read_naturally() {
    assert_eq!(size_label(5), "5 B");
    assert_eq!(size_label(1229), "1.2 KiB");
    assert_eq!(size_label(100 * 1024), "100.0 KiB");
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
/// The next prompt the session shows; fails rather than hangs if none comes.
async fn next_user_text(session: &mut Session) -> String {
    let wait = async {
        loop {
            if let octet_core::Event::User(text) = session.events.recv().await.unwrap() {
                return text;
            }
        }
    };
    tokio::time::timeout(Duration::from_secs(5), wait)
        .await
        .expect("no prompt reached the session")
}
#[tokio::test]
async fn a_finished_command_replaces_the_running_notice() {
    let mut app = app();
    app.shell = Some(crate::test_support::running_shell(true));
    app.status_line = "Running echo hi · Esc to stop".into();
    shell_finished(&mut app, Ok(ran("echo hi", "hi\n")));
    assert!(!app.shell_running());
    assert_eq!(app.status_line, "$ echo hi · exit 0");
    assert_eq!(app.composer.attachments.len(), 1);
    let gone = shell::ShellError::Run {
        shell: "/x".into(),
        source: std::io::Error::other("gone"),
    };
    app.shell = Some(crate::test_support::running_shell(false));
    shell_finished(&mut app, Err(gone));
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
    let vendor = RecordingVendor::default();
    app.conn.start_turn();
    steer(&mut app, &vendor, "also check docs".into());
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
    crate::test_support::idle(&mut app);
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
        commands::path_in(root, home, "shots/a.png"),
        Path::new("/work/shots/a.png")
    );
    assert_eq!(
        commands::path_in(root, home, "~/a.png"),
        Path::new("/home/me/a.png")
    );
    assert_eq!(
        commands::path_in(root, home, "/abs/a.png"),
        Path::new("/abs/a.png")
    );
    assert_eq!(
        commands::path_in(root, None, "~/a.png"),
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
            commands::path_in(root, home, typed),
            Path::new(wanted),
            "{typed}"
        );
    }
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
fn browsing_history_keeps_the_drafts_own_images() {
    let dir = image_workspace("octet-image-browse", &["draft.png"]);
    let mut app = app();
    app.remember("earlier".into());
    let image = octet_core::ImageAttachment::open(&dir.path().join("draft.png")).unwrap();
    app.composer.images.push(image);
    app.recall(true);
    assert_eq!(app.composer.editor.text(), "earlier");
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
#[test]
fn the_panic_hook_restores_only_on_the_loop_thread() {
    let owner = std::thread::current().id();
    assert!(crate::panic_restores_terminal(owner));
    // A background job or index thread that panics is survived; the
    // terminal stays as the interface left it.
    let elsewhere = std::thread::spawn(move || crate::panic_restores_terminal(owner))
        .join()
        .unwrap();
    assert!(!elsewhere);
}
#[tokio::test]
async fn shell_running_follows_the_task() {
    let mut app = app();
    assert!(!app.shell_running());
    app.shell = Some(crate::test_support::running_shell(false));
    assert!(app.shell_running());
    // Its end, in whatever way, settles it: no flag to repair later.
    shell_finished(&mut app, Ok(ran("true", "")));
    assert!(!app.shell_running());
}
/// A conversation with Claude, then a switch to Codex.
fn switched_to_codex() -> App {
    let mut app = crate::test_support::app_for(Engine::CLAUDE);
    crate::test_support::idle(&mut app);
    app.event(octet_core::Event::User("Remember OCTET_42".into()));
    app.event(octet_core::Event::Started);
    app.event(octet_core::Event::Text("OCTET_42".into()));
    app.event(octet_core::Event::Finished {
        outcome: octet_core::Outcome::Completed,
    });
    // As the reconnect to another provider leaves the kept interface.
    app.conn.engine = Engine::CODEX;
    assert!(app.carry_conversation());
    app
}
fn sent_wire(vendor: &RecordingVendor) -> Vec<(String, String)> {
    vendor
        .sent
        .borrow()
        .iter()
        .map(|command| match command {
            Command::PromptWithDisplay { wire, display, .. } => (wire.clone(), display.clone()),
            Command::Prompt(text) => (text.clone(), text.clone()),
            other => panic!("{other:?}"),
        })
        .collect()
}
#[tokio::test]
async fn the_first_prompt_after_a_switch_carries_the_transcript() {
    let mut app = switched_to_codex();
    let vendor = RecordingVendor::default();
    assert!(app.composer.editor.insert("What code?"));
    key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
    let sent = sent_wire(&vendor);
    let (wire, display) = &sent[0];
    assert!(wire.starts_with("[Octet handoff]"), "{wire}");
    assert!(wire.contains("User:\nRemember OCTET_42"), "{wire}");
    assert!(wire.contains("Assistant (claude):\nOCTET_42"), "{wire}");
    assert!(wire.ends_with("What code?"), "{wire}");
    assert_eq!(display, "What code?");
    assert!(app.pending_handoff.is_none());
    assert!(
        app.entries_text()
            .contains("Carried the earlier conversation to codex (1 turn"),
        "{}",
        app.entries_text()
    );
}
#[tokio::test]
async fn only_the_first_prompt_carries_it() {
    let mut app = switched_to_codex();
    let vendor = RecordingVendor::default();
    for prompt in ["first", "second"] {
        assert!(app.composer.editor.insert(prompt));
        key_action(&mut app, &vendor, key(KeyCode::Enter)).await;
        crate::test_support::idle(&mut app);
    }
    let sent = sent_wire(&vendor);
    assert!(sent[0].0.starts_with("[Octet handoff]"));
    assert_eq!(sent[1].0, "second");
}
#[test]
fn a_refused_send_keeps_the_handoff() {
    let mut app = switched_to_codex();
    let vendor = RecordingVendor::default();
    vendor.refuse.set(true);
    let refused = app.begin_turn(&vendor, Command::Prompt("hi".into()), By::User, "sending");
    assert!(refused.is_err());
    assert!(app.pending_handoff.is_some());
    vendor.refuse.set(false);
    app.begin_turn(&vendor, Command::Prompt("hi".into()), By::User, "sending")
        .unwrap();
    assert!(sent_wire(&vendor)[0].0.starts_with("[Octet handoff]"));
}
#[test]
fn a_goal_continuation_carries_it() {
    let mut app = switched_to_codex();
    let vendor = RecordingVendor::default();
    let goal = Command::PromptWithDisplay {
        wire: "Continue the goal".into(),
        display: "Goal continuation · turn 2".into(),
        images: Vec::new(),
    };
    app.begin_turn(&vendor, goal, By::Goal, "continuing goal")
        .unwrap();
    let (wire, display) = &sent_wire(&vendor)[0];
    assert!(wire.starts_with("[Octet handoff]") && wire.ends_with("Continue the goal"));
    assert_eq!(display, "Goal continuation · turn 2");
}
#[test]
fn new_drops_the_handoff_and_an_empty_conversation_carries_nothing() {
    // /new opens a fresh interface: nothing pending.
    let mut fresh = crate::test_support::app_for(Engine::CODEX);
    assert!(fresh.pending_handoff.is_none());
    assert!(!fresh.carry_conversation());
    assert!(fresh.pending_handoff.is_none());
}
