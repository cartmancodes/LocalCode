//! The live driver against the fake vendor: connection, turns, approvals,
//! modes, timeouts and shutdown for both protocols.
// Test code: an unwrap that fails is the test failing.
#![allow(clippy::unwrap_used)]
use octet_engine::live::{
    spawn, spawn_with_limits, Command, Config, Engine, Event, ImageAttachment, Limits, Mode,
    Outcome,
};
use std::time::Duration;
use tokio::{sync::mpsc, time::timeout};
fn config() -> Config {
    Config::new(
        Engine::CODEX,
        octet_testkit::protocol_child(),
        std::env::temp_dir(),
    )
}
async fn next(events: &mut mpsc::Receiver<Event>) -> Event {
    timeout(Duration::from_secs(5), events.recv())
        .await
        .unwrap()
        .unwrap()
}
/// A fake vendor from a shell script, in a directory that lives as long as
/// the returned guard.
fn script_vendor(name: &str, body: &str) -> (octet_testkit::TempDir, std::path::PathBuf) {
    use std::os::unix::fs::PermissionsExt;
    let dir = octet_testkit::TempDir::new(name);
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.path().join("vendor");
    std::fs::write(&path, format!("#!/bin/sh\n{body}")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    (dir, path)
}
#[tokio::test]
async fn codex_opening_the_session_before_the_handshake_is_an_error() {
    // Codex answers thread/start (id 2) before initialize (id 1).
    let (_dir, vendor) = script_vendor(
        "octet-live-out-of-order",
        "read line\n\
         echo '{\"id\":2,\"result\":{\"thread\":{\"id\":\"t1\"}}}'\n\
         echo '{\"id\":1,\"result\":{}}'\n\
         exec sleep 30\n",
    );
    let (_handle, mut events, task) =
        spawn(Config::new(Engine::CODEX, vendor, std::env::temp_dir()));
    loop {
        match next(&mut events).await {
            Event::Error(error) => {
                assert_eq!(error, "Unexpected protocol initialization order");
                break;
            }
            Event::Ready { session } if session != "t1" => {
                panic!("a second session opened: {session}")
            }
            _ => {}
        }
    }
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn a_repeated_claude_handshake_mid_turn_does_not_end_the_turn() {
    let init = r#"{"type":"control_response","response":{"subtype":"success","request_id":"octet-init","response":{}}}"#;
    let result = r#"{"type":"result","is_error":false,"result":"done","session_id":"s1"}"#;
    let (_dir, vendor) = script_vendor(
        "octet-live-repeated-init",
        &format!(
            "read line\necho '{init}'\nread line\necho '{init}'\necho '{result}'\nexec sleep 30\n"
        ),
    );
    let mut config = Config::new(Engine::CLAUDE, vendor, std::env::temp_dir());
    config.mode = Mode::Ask;
    let (handle, mut events, task) = spawn(config);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("hello".into())).unwrap();
    let finished = wait_for(&mut events, |e| matches!(e, Event::Finished { .. })).await;
    assert!(
        matches!(
            finished,
            Event::Finished {
                outcome: Outcome::Completed
            }
        ),
        "{finished:?}"
    );
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn cancel_while_handshaken_is_cancelled() {
    // Codex answers initialize but never opens the session.
    let (_dir, vendor) = script_vendor(
        "octet-live-handshaken",
        "read line\necho '{\"id\":1,\"result\":{}}'\nexec sleep 30\n",
    );
    let (handle, mut events, task) =
        spawn(Config::new(Engine::CODEX, vendor, std::env::temp_dir()));
    tokio::time::sleep(Duration::from_millis(300)).await;
    handle.interrupt();
    loop {
        match next(&mut events).await {
            Event::Error(error) => {
                assert_eq!(error, "Connection cancelled");
                break;
            }
            Event::Ready { .. } => panic!("the session never opened"),
            _ => {}
        }
    }
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn cancel_while_starting_is_cancelled() {
    // A vendor that never answers the handshake.
    use std::os::unix::fs::PermissionsExt;
    let dir = octet_testkit::TempDir::new("octet-live-silent");
    std::fs::create_dir_all(dir.path()).unwrap();
    let silent = dir.path().join("silent-vendor");
    std::fs::write(&silent, "#!/bin/sh\nexec sleep 30\n").unwrap();
    std::fs::set_permissions(&silent, std::fs::Permissions::from_mode(0o755)).unwrap();
    let (handle, mut events, task) =
        spawn(Config::new(Engine::CODEX, silent, std::env::temp_dir()));
    handle.interrupt();
    loop {
        match next(&mut events).await {
            Event::Error(error) => {
                assert_eq!(error, "Connection cancelled");
                break;
            }
            Event::Ready { .. } => panic!("the silent vendor cannot be ready"),
            _ => {}
        }
    }
    assert!(matches!(next(&mut events).await, Event::Stopped));
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn live_driver_keeps_turns_separate_and_waits_for_terminal_after_usage() {
    let (handle, mut events, task) = spawn(config());
    assert!(matches!(next(&mut events).await, Event::Ready { .. }));
    for _ in 0..2 {
        handle.send(Command::Prompt("hello".into())).unwrap();
        let mut text = String::new();
        let mut usage = false;
        loop {
            match next(&mut events).await {
                Event::Text(t) => text.push_str(&t),
                Event::Usage(_) => usage = true,
                Event::Finished { outcome } => {
                    assert_eq!(outcome, Outcome::Completed);
                    break;
                }
                Event::Error(e) => panic!("{e}"),
                _ => {}
            }
        }
        assert_eq!(text, "Hello fixture");
        assert!(usage);
    }
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn approvals_and_cancel_remain_routable_during_turn() {
    let (handle, mut events, task) = spawn(config());
    next(&mut events).await;
    handle.send(Command::Prompt("approval".into())).unwrap();
    let id = loop {
        if let Event::Approval { id, .. } = next(&mut events).await {
            break id;
        }
    };
    handle.send(Command::Answer { id, allow: false }).unwrap();
    let mut denied = false;
    loop {
        match next(&mut events).await {
            Event::Text(t) => denied |= t == "decline",
            Event::Finished { .. } => break,
            _ => {}
        }
    }
    assert!(denied);
    handle.send(Command::Prompt("hold".into())).unwrap();
    while !matches!(next(&mut events).await, Event::Started) {}
    handle.interrupt();
    loop {
        match next(&mut events).await {
            Event::Finished { outcome } => {
                assert_eq!(outcome, Outcome::Interrupted);
                break;
            }
            Event::Error(e) => panic!("{e}"),
            _ => {}
        }
    }
    handle.send(Command::Answer { id, allow: true }).unwrap();
    assert!(matches!(next(&mut events).await, Event::Notice(_)));
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn claude_stream_does_not_duplicate_final_assistant_message() {
    let mut config = config();
    config.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(config);
    assert!(matches!(next(&mut events).await, Event::Ready { .. }));
    for _ in 0..2 {
        handle.send(Command::Prompt("hello".into())).unwrap();
        let mut text = String::new();
        loop {
            match next(&mut events).await {
                Event::Text(t) => text.push_str(&t),
                Event::Finished { outcome } => {
                    assert_eq!(outcome, Outcome::Completed);
                    break;
                }
                Event::Error(e) => panic!("{e}"),
                _ => {}
            }
        }
        assert_eq!(text, "Hello Claude");
    }
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn codex_discovers_all_pages_and_uses_model_not_picker_id() {
    let (handle, mut events, task) = spawn(config());
    let mut confirmed = None;
    loop {
        match next(&mut events).await {
            Event::ModelSelected(id) => confirmed = Some(id),
            Event::Models(models) if models.len() == 2 => {
                assert_eq!(models[0].selection, "fixture");
                assert_eq!(models[0].id.as_deref(), Some("fixture"));
                assert_eq!(models[0].name, "Fixture Model");
                assert_eq!(models[1].selection, "other-full-id");
                assert_eq!(confirmed.as_deref(), Some("fixture"));
                break;
            }
            Event::Error(e) => panic!("{e}"),
            _ => {}
        }
    }
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn claude_catalog_alias_is_distinct_from_confirmed_session_model() {
    let mut cfg = config();
    cfg.engine = Engine::CLAUDE;
    cfg.model = Some("sonnet".into());
    let (handle, mut events, task) = spawn(cfg);
    loop {
        if let Event::Models(models) = next(&mut events).await {
            assert_eq!(models[0].selection, "sonnet");
            assert_eq!(models[0].id.as_deref(), Some("claude-fixture-full-id"));
            assert_eq!(models[0].name, "Fixture Sonnet");
            break;
        }
    }
    handle.send(Command::Prompt("hello".into())).unwrap();
    loop {
        if let Event::ModelSelected(id) = next(&mut events).await {
            assert_eq!(id, "claude-fixture-full-id");
            break;
        }
    }
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn provider_receives_wire_prompt_while_transcript_keeps_user_facing_text() {
    let (handle, mut events, task) = spawn(config());
    while !matches!(next(&mut events).await, Event::Ready { .. }) {}
    handle
        .send(Command::PromptWithDisplay {
            wire: "hold".into(),
            display: "hello".into(),
            images: Vec::new(),
        })
        .unwrap();
    let mut seen_user = false;
    loop {
        match next(&mut events).await {
            Event::User(text) => {
                assert_eq!(text, "hello");
                seen_user = true;
            }
            Event::Started => {
                handle.interrupt();
            }
            Event::Finished { outcome } => {
                assert_eq!(outcome, Outcome::Interrupted);
                break;
            }
            Event::Error(e) => panic!("{e}"),
            _ => {}
        }
    }
    assert!(seen_user);
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}

async fn wait_for(events: &mut mpsc::Receiver<Event>, wanted: impl Fn(&Event) -> bool) -> Event {
    loop {
        let event = next(events).await;
        if let Event::Error(error) = &event {
            panic!("{error}");
        }
        if wanted(&event) {
            return event;
        }
    }
}
async fn turn_text(events: &mut mpsc::Receiver<Event>) -> String {
    let mut text = String::new();
    loop {
        match next(events).await {
            Event::Text(t) => text.push_str(&t),
            Event::Finished { .. } => return text,
            Event::Error(e) => panic!("{e}"),
            _ => {}
        }
    }
}
#[tokio::test]
async fn codex_launches_and_turns_with_the_configured_mode() {
    let mut c = config();
    c.mode = Mode::Auto;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Auto))).await;
    handle.send(Command::Prompt("params".into())).unwrap();
    let echo: serde_json::Value = serde_json::from_str(&turn_text(&mut events).await).unwrap();
    assert_eq!(echo["thread"]["sandbox"], "workspace-write");
    assert_eq!(echo["thread"]["approvalPolicy"], "on-request");
    assert_eq!(echo["thread"]["approvalsReviewer"], "auto_review");
    assert_eq!(echo["turn"]["approvalPolicy"], "on-request");
    assert_eq!(echo["turn"]["approvalsReviewer"], "auto_review");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_launches_with_the_mapped_permission_flag() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.mode = Mode::AcceptEdits;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| {
        matches!(e, Event::ModeChanged(Mode::AcceptEdits))
    })
    .await;
    handle.send(Command::Prompt("argv".into())).unwrap();
    let argv = turn_text(&mut events).await;
    assert!(argv.contains("--permission-mode acceptEdits"), "{argv}");
    assert!(!argv.contains("--permission-mode default"), "{argv}");
    assert!(!argv.contains("dangerously"), "{argv}");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn codex_live_switch_applies_to_the_next_turn() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Auto))).await;
    handle.send(Command::Prompt("params".into())).unwrap();
    let echo: serde_json::Value = serde_json::from_str(&turn_text(&mut events).await).unwrap();
    assert_eq!(echo["thread"]["approvalsReviewer"], "user");
    assert_eq!(echo["turn"]["approvalPolicy"], "on-request");
    assert_eq!(echo["turn"]["approvalsReviewer"], "auto_review");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_live_switch_uses_set_permission_mode() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Auto))).await;
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_refusal_keeps_the_previous_mode() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("reject-mode".into());
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let notice = wait_for(&mut events, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&notice, Event::Notice(text) if text.contains("refused")),
        "{notice:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Ask)
    ));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn driver_refuses_live_full_access() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::FullAccess)).unwrap();
    let event = wait_for(&mut events, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("/mode")),
        "{event:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Ask)
    ));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_header_follows_the_mode_claude_reports() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("report-auto".into());
    let (handle, mut events, task) = spawn(c);
    let event = wait_for(&mut events, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("reports auto")),
        "{event:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Auto)
    ));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn unmapped_claude_mode_keeps_the_requested_mode_with_a_notice() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("report-plan".into());
    let (handle, mut events, task) = spawn(c);
    let event = wait_for(&mut events, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("plan")),
        "{event:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Ask)
    ));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn codex_header_follows_a_stricter_reported_policy() {
    let mut c = config();
    c.mode = Mode::Auto;
    c.model = Some("report-stricter".into());
    let (handle, mut events, task) = spawn(c);
    let event = wait_for(&mut events, |e| {
        matches!(e, Event::Notice(text) if text.contains("reports"))
            || matches!(e, Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("reports ask")),
        "{event:?}"
    );
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    handle.send(Command::Prompt("params".into())).unwrap();
    let echo: serde_json::Value = serde_json::from_str(&turn_text(&mut events).await).unwrap();
    assert_eq!(echo["turn"]["approvalPolicy"], "untrusted");
    handle.shutdown();
    task.await.unwrap();
}
/// Real limits shortened so timeout paths run in seconds.
fn quick() -> Limits {
    Limits {
        connect: Duration::from_secs(2),
        turn_idle: Duration::from_secs(1),
        mode_confirm: Duration::from_secs(1),
        ..Limits::default()
    }
}
async fn wait_long(
    events: &mut mpsc::Receiver<Event>,
    seconds: u64,
    wanted: impl Fn(&Event) -> bool,
) -> Event {
    timeout(Duration::from_secs(seconds), async {
        loop {
            let event = events.recv().await.expect("driver stopped");
            if let Event::Error(error) = &event {
                panic!("{error}");
            }
            if wanted(&event) {
                return event;
            }
        }
    })
    .await
    .expect("event did not arrive in time")
}
#[tokio::test]
async fn unconfirmed_claude_switch_times_out_then_applies_a_late_confirmation() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("late-mode".into());
    let (handle, mut events, task) = spawn_with_limits(c, quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    // Idle past the connect deadline: the mode timer firing later must not be
    // mistaken for an expired turn.
    tokio::time::sleep(Duration::from_millis(2200)).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let event = wait_long(&mut events, 12, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("has not confirmed") && text.contains("until it does")),
        "{event:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Ask)
    ));
    let event = wait_long(&mut events, 5, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("late")),
        "{event:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Auto)
    ));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn demo_mode_switch_during_approval_keeps_the_dialog_open() {
    let mut c = config();
    c.engine = Engine::DEMO;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle
        .send(Command::Prompt("/approval-demo".into()))
        .unwrap();
    wait_for(&mut events, |e| matches!(e, Event::Approval { .. })).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let event = wait_for(&mut events, |e| {
        matches!(e, Event::ModeChanged(_) | Event::ApprovalClosed(_))
    })
    .await;
    assert!(matches!(event, Event::ModeChanged(Mode::Auto)), "{event:?}");
    handle.send(Command::Answer { id: 1, allow: true }).unwrap();
    assert!(turn_text(&mut events).await.starts_with("Approved."));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_switch_sends_the_vendor_mode_name() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    for (mode, _) in [(Mode::AcceptEdits, ()), (Mode::Auto, ()), (Mode::Ask, ())] {
        handle.send(Command::SetMode(mode)).unwrap();
        wait_for(
            &mut events,
            |e| matches!(e, Event::ModeChanged(m) if *m == mode),
        )
        .await;
    }
    handle.send(Command::Prompt("modes".into())).unwrap();
    assert_eq!(turn_text(&mut events).await, "acceptEdits,auto,default");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_switch_applies_in_the_middle_of_a_turn() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::Prompt("hold".into())).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::Started)).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let event = wait_for(&mut events, |e| {
        matches!(
            e,
            Event::ModeChanged(_) | Event::Finished { .. } | Event::Notice(_)
        )
    })
    .await;
    assert!(matches!(event, Event::ModeChanged(Mode::Auto)), "{event:?}");
    handle.interrupt();
    let event = wait_for(&mut events, |e| matches!(e, Event::Finished { .. })).await;
    assert!(matches!(event, Event::Finished { outcome } if outcome == Outcome::Interrupted));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn codex_resume_carries_the_mode() {
    let mut c = config();
    c.resume = Some("fixture-thread".into());
    c.mode = Mode::FullAccess;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| {
        matches!(e, Event::ModeChanged(Mode::FullAccess))
    })
    .await;
    handle.send(Command::Prompt("params".into())).unwrap();
    let echo: serde_json::Value = serde_json::from_str(&turn_text(&mut events).await).unwrap();
    assert_eq!(echo["thread"]["method"], "thread/resume");
    assert_eq!(echo["thread"]["threadId"], "fixture-thread");
    assert_eq!(echo["thread"]["sandbox"], "danger-full-access");
    assert_eq!(echo["thread"]["approvalPolicy"], "never");
    assert_eq!(echo["turn"]["approvalPolicy"], "never");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn idle_switch_long_after_connect_confirms_promptly() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn_with_limits(c, quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    // Past the connect deadline, which is stale once idle.
    tokio::time::sleep(Duration::from_millis(2200)).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let event = wait_long(&mut events, 12, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    // The first event being the confirmation, not a timeout notice, is the proof.
    assert!(matches!(event, Event::ModeChanged(Mode::Auto)), "{event:?}");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn every_timed_out_switch_can_still_be_confirmed_late() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("hang-mode".into());
    let (handle, mut events, task) = spawn_with_limits(c, quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    for target in [Mode::AcceptEdits, Mode::Auto] {
        handle.send(Command::SetMode(target)).unwrap();
        wait_long(
            &mut events,
            12,
            |e| matches!(e, Event::Notice(t) if t.contains("has not confirmed")),
        )
        .await;
        assert!(matches!(
            next(&mut events).await,
            Event::ModeChanged(Mode::Ask)
        ));
    }
    // Claude accepts the first request late and refuses the second.
    handle.send(Command::Prompt("flush".into())).unwrap();
    wait_for(&mut events, |e| {
        matches!(e, Event::ModeChanged(Mode::AcceptEdits))
    })
    .await;
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn unmapped_switch_reply_keeps_the_target_with_a_notice() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("odd-mode".into());
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(Mode::Ask))).await;
    handle.send(Command::SetMode(Mode::Auto)).unwrap();
    let event = wait_for(&mut events, |e| {
        matches!(e, Event::Notice(_) | Event::ModeChanged(_))
    })
    .await;
    assert!(
        matches!(&event, Event::Notice(text) if text.contains("plan")),
        "{event:?}"
    );
    assert!(matches!(
        next(&mut events).await,
        Event::ModeChanged(Mode::Auto)
    ));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn vendor_exit_reports_its_stderr() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.model = Some("die-stderr".into());
    let (_handle, mut events, task) = spawn(c);
    let error = loop {
        match next(&mut events).await {
            Event::Error(error) => break error,
            Event::Stopped => panic!("stopped without an error"),
            _ => {}
        }
    };
    assert!(
        error.contains("No conversation found with session ID: fixture"),
        "{error}"
    );
    task.await.unwrap();
}
#[tokio::test]
async fn codex_thread_refusal_reports_the_vendor_message() {
    let mut c = config();
    c.model = Some("refuse-thread".into());
    let (_handle, mut events, task) = spawn(c);
    let error = loop {
        match next(&mut events).await {
            Event::Error(error) => break error,
            Event::Stopped => panic!("stopped without an error"),
            _ => {}
        }
    };
    assert!(
        error.contains("no rollout found for thread id fixture"),
        "{error}"
    );
    task.await.unwrap();
}
#[tokio::test]
async fn large_tool_item_is_bounded_and_the_turn_completes() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    handle.send(Command::Prompt("bigtool".into())).unwrap();
    let mut tool_bytes = 0;
    let mut tool = String::new();
    loop {
        match next(&mut events).await {
            Event::Tool(text) => {
                tool_bytes += text.len();
                tool = text;
            }
            Event::Finished { outcome } => {
                assert_eq!(outcome, Outcome::Completed);
                break;
            }
            Event::Error(error) => panic!("{error}"),
            _ => {}
        }
    }
    assert!(tool_bytes > 0 && tool_bytes <= 40 * 1024, "{tool_bytes}");
    // The cut must keep what the command was and how it ended, not only output.
    let head: String = tool.chars().take(200).collect();
    assert!(
        head.contains("cat big") && head.contains("exit 0"),
        "{head}"
    );
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn turn_errors_show_the_vendor_message_not_raw_json() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    for (prompt, expected) in [
        ("fail", "You've hit your usage limit. (usageLimitExceeded)"),
        ("fail-nested", "The model is not supported."),
    ] {
        handle.send(Command::Prompt(prompt.into())).unwrap();
        let error = loop {
            match next(&mut events).await {
                Event::Error(error) => break error,
                Event::Finished { .. } => panic!("finished without an error"),
                _ => {}
            }
        };
        assert_eq!(error, expected);
        assert!(
            matches!(next(&mut events).await, Event::Finished { outcome } if outcome == Outcome::Failed)
        );
    }
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_result_errors_are_shown() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    handle.send(Command::Prompt("errors".into())).unwrap();
    let error = loop {
        match next(&mut events).await {
            Event::Error(error) => break error,
            Event::Finished { .. } => panic!("finished without an error"),
            _ => {}
        }
    };
    assert_eq!(error, "Fixture failure detail");
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn claude_tool_is_announced_once_with_its_input() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    handle.send(Command::Prompt("tool".into())).unwrap();
    let mut tools = Vec::new();
    loop {
        match next(&mut events).await {
            Event::Tool(text) => tools.push(text),
            Event::Finished { .. } => break,
            Event::Error(error) => panic!("{error}"),
            _ => {}
        }
    }
    assert_eq!(tools.len(), 1, "{tools:?}");
    assert!(
        tools[0].starts_with("Bash") && tools[0].contains("echo fixture"),
        "{tools:?}"
    );
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn active_turn_outlives_the_idle_limit() {
    let (handle, mut events, task) = spawn_with_limits(config(), quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    // The fixture streams for about 2.4 seconds; the idle limit is 1 second.
    handle.send(Command::Prompt("slow".into())).unwrap();
    let event = wait_long(&mut events, 6, |e| matches!(e, Event::Finished { .. })).await;
    assert!(matches!(event, Event::Finished { outcome } if outcome == Outcome::Completed));
    handle.shutdown();
    task.await.unwrap();
}
#[tokio::test]
async fn silent_turn_is_stopped_at_the_idle_limit() {
    let (handle, mut events, task) = spawn_with_limits(config(), quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    handle.send(Command::Prompt("hold".into())).unwrap();
    let error = loop {
        match next(&mut events).await {
            Event::Error(error) => break error,
            Event::Stopped => panic!("stopped without an error"),
            _ => {}
        }
    };
    assert!(error.contains("sent nothing for 1 second"), "{error}");
    let _ = handle;
    task.await.unwrap();
}
#[tokio::test]
async fn other_thread_chatter_does_not_keep_a_silent_turn_alive() {
    let (handle, mut events, task) = spawn_with_limits(config(), quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    let sent = tokio::time::Instant::now();
    // Another thread emits frames for about 2.4 seconds; ours is silent.
    handle.send(Command::Prompt("chatter".into())).unwrap();
    let error = loop {
        match next(&mut events).await {
            Event::Error(error) => break error,
            Event::Stopped => panic!("stopped without an error"),
            _ => {}
        }
    };
    assert!(error.contains("sent nothing for 1 second"), "{error}");
    // The 1 s idle limit fired, not a longer one.
    assert!(
        sent.elapsed() < Duration::from_millis(2200),
        "{:?}",
        sent.elapsed()
    );
    let _ = handle;
    task.await.unwrap();
}
#[tokio::test]
async fn waiting_for_the_user_is_not_vendor_silence() {
    let (handle, mut events, task) = spawn_with_limits(config(), quick());
    wait_for(&mut events, |e| matches!(e, Event::ModeChanged(_))).await;
    handle.send(Command::Prompt("approval".into())).unwrap();
    let id = match wait_for(&mut events, |e| matches!(e, Event::Approval { .. })).await {
        Event::Approval { id, .. } => id,
        _ => unreachable!(),
    };
    // Longer than the 1-second idle limit: the vendor is waiting on us.
    tokio::time::sleep(Duration::from_millis(1600)).await;
    handle.send(Command::Answer { id, allow: true }).unwrap();
    let event = wait_for(&mut events, |e| matches!(e, Event::Finished { .. })).await;
    assert!(matches!(event, Event::Finished { outcome } if outcome == Outcome::Completed));
    handle.shutdown();
    task.await.unwrap();
}

#[tokio::test]
async fn spawn_uses_the_configured_approval_window() {
    let config = Config {
        approval_timeout: Duration::from_millis(300),
        ..Config::new(Engine::DEMO, "demo", std::env::temp_dir())
    };
    let (handle, mut events, task) = spawn(config);
    handle
        .send(Command::Prompt("/approval-demo".into()))
        .unwrap();
    loop {
        match next(&mut events).await {
            Event::ApprovalClosed(_) => break,
            Event::Error(e) => panic!("{e}"),
            _ => {}
        }
    }
    handle.shutdown();
    task.await.unwrap();
}

#[tokio::test]
async fn codex_steer_adds_to_the_running_turn() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("hold".into())).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::Started)).await;
    handle
        .send(Command::Steer("look at tests too".into()))
        .unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::User(t) if t == "[steer] look at tests too"),
    )
    .await;
    wait_for(
        &mut events,
        |e| matches!(e, Event::Text(t) if t == "steered:look at tests too"),
    )
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn steer_without_a_turn_is_refused() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Steer("anything".into())).unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::Notice(n) if n.contains("No turn is running")),
    )
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn codex_sends_effort_on_each_turn_and_takes_changes_live() {
    let mut c = config();
    c.effort = Some("high".into());
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("params".into())).unwrap();
    assert!(turn_text(&mut events).await.contains("\"effort\":\"high\""));
    handle.send(Command::SetEffort(Some("low".into()))).unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::Notice(n) if n.contains("low")),
    )
    .await;
    handle.send(Command::Prompt("params".into())).unwrap();
    assert!(turn_text(&mut events).await.contains("\"effort\":\"low\""));
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn claude_launches_with_effort() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.effort = Some("max".into());
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("argv".into())).unwrap();
    assert!(turn_text(&mut events).await.contains("--effort max"));
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn codex_fork_opens_a_new_thread() {
    let mut c = config();
    c.resume = Some("fixture-thread".into());
    c.fork = true;
    let (handle, mut events, task) = spawn(c);
    wait_for(
        &mut events,
        |e| matches!(e, Event::Ready { session } if session == "forked-thread"),
    )
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn codex_compact_runs_as_a_turn() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Compact).unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::User(t) if t == "/compact"),
    )
    .await;
    wait_for(&mut events, |e| matches!(e, Event::Started)).await;
    wait_for(&mut events, |e| {
        matches!(
            e,
            Event::Finished {
                outcome: Outcome::Completed
            }
        )
    })
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn claude_fork_passes_fork_session() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.resume = Some("claude-fixture".into());
    c.fork = true;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("argv".into())).unwrap();
    let argv = turn_text(&mut events).await;
    assert!(
        argv.contains("--resume claude-fixture --fork-session"),
        "{argv}"
    );
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn claude_compact_sends_the_command() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Compact).unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::User(t) if t == "/compact"),
    )
    .await;
    assert_eq!(turn_text(&mut events).await, "Compacted");
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
/// A prompt carrying one image file written with `bytes`.
fn image_prompt(dir: &octet_testkit::TempDir, bytes: &[u8]) -> (Command, ImageAttachment) {
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.path().join("shot.png");
    std::fs::write(&path, bytes).unwrap();
    let image = ImageAttachment::open(&path).unwrap();
    let command = Command::PromptWithDisplay {
        wire: "look".into(),
        display: "look\n[+ image shot.png]".into(),
        images: vec![image.clone()],
    };
    (command, image)
}
#[tokio::test]
async fn codex_receives_local_image_paths() {
    let dir = octet_testkit::TempDir::new("octet-image");
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    let (command, image) = image_prompt(&dir, b"fake png");
    handle.send(command).unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::User(t) if t == "look\n[+ image shot.png]"),
    )
    .await;
    assert_eq!(
        turn_text(&mut events).await,
        format!("images:{}", image.path.display())
    );
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn claude_receives_base64_image_blocks() {
    let dir = octet_testkit::TempDir::new("octet-image");
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    // "abc" is "YWJj" in base64: four bytes on the wire.
    let (command, _) = image_prompt(&dir, b"abc");
    handle.send(command).unwrap();
    assert_eq!(turn_text(&mut events).await, "image:image/png:4");
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn an_image_gone_at_send_time_fails_the_turn() {
    let dir = octet_testkit::TempDir::new("octet-image");
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    let (command, image) = image_prompt(&dir, b"abc");
    std::fs::remove_file(&image.path).unwrap();
    handle.send(command).unwrap();
    let mut error = None;
    loop {
        match next(&mut events).await {
            Event::Error(text) => error = Some(text),
            Event::Finished { outcome } => {
                assert_eq!(outcome, Outcome::Failed);
                break;
            }
            _ => {}
        }
    }
    assert!(error.unwrap().contains("shot.png"));
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn claude_fork_names_the_new_session_only_after_the_first_prompt() {
    let mut c = config();
    c.engine = Engine::CLAUDE;
    c.resume = Some("claude-fixture".into());
    c.fork = true;
    let (handle, mut events, task) = spawn(c);
    // Until Claude names the fork, there is no session to resume: reporting
    // the original would send the next reconnect back into it.
    let ready = wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    assert!(
        matches!(ready, Event::Ready { ref session } if session.is_empty()),
        "{ready:?}"
    );
    handle.send(Command::Prompt("hello".into())).unwrap();
    wait_for(
        &mut events,
        |e| matches!(e, Event::Ready { session } if session == "claude-forked"),
    )
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn codex_rejected_steer_is_reported_with_its_text() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("hold".into())).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::Started)).await;
    // Let Codex name the turn, so the steer goes out at once.
    tokio::time::sleep(Duration::from_millis(200)).await;
    handle.send(Command::Steer("reject".into())).unwrap();
    wait_for(&mut events, |e| {
        matches!(e, Event::Notice(t) if t.contains("Codex did not take the steer") && t.contains("reject"))
    })
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn claude_images_too_large_together_fail_the_turn_not_the_session() {
    let dir = octet_testkit::TempDir::new("octet-image-big");
    std::fs::create_dir_all(dir.path()).unwrap();
    let mut images = Vec::new();
    for name in ["a.png", "b.png"] {
        let path = dir.path().join(name);
        std::fs::File::create(&path)
            .unwrap()
            .set_len(3 * 1024 * 1024)
            .unwrap();
        images.push(ImageAttachment::open(&path).unwrap());
    }
    let mut c = config();
    c.engine = Engine::CLAUDE;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle
        .send(Command::PromptWithDisplay {
            wire: "look".into(),
            display: "look".into(),
            images,
        })
        .unwrap();
    let mut error = String::new();
    loop {
        match next(&mut events).await {
            Event::Error(text) => error = text,
            Event::Finished { outcome } => {
                assert_eq!(outcome, Outcome::Failed);
                break;
            }
            Event::Stopped => panic!("the session stopped: {error}"),
            _ => {}
        }
    }
    assert!(error.contains("together"), "{error}");
    // The session is still usable.
    handle.send(Command::Prompt("hello".into())).unwrap();
    assert_eq!(turn_text(&mut events).await, "Hello Claude");
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn steer_while_interrupting_names_the_text_it_did_not_send() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("hold".into())).unwrap();
    wait_for(&mut events, |e| matches!(e, Event::Started)).await;
    handle.interrupt();
    handle.send(Command::Steer("too late".into())).unwrap();
    wait_for(&mut events, |e| {
        matches!(e, Event::Notice(t) if t.contains("The turn is stopping") && t.contains("too late"))
    })
    .await;
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn a_prompt_cancelled_before_it_starts_is_not_run() {
    let (handle, mut events, task) = spawn(config());
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    // Esc in the same instant as the send: the cancel must still win.
    handle.send(Command::Prompt("hello".into())).unwrap();
    handle.interrupt();
    let outcome = wait_for(&mut events, |e| matches!(e, Event::Finished { .. })).await;
    assert!(
        matches!(
            outcome,
            Event::Finished {
                outcome: Outcome::Interrupted
            }
        ),
        "{outcome:?}"
    );
    // A later prompt is not affected.
    handle.send(Command::Prompt("hello".into())).unwrap();
    assert_eq!(turn_text(&mut events).await, "Hello fixture");
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
#[tokio::test]
async fn the_demo_also_cancels_a_prompt_that_had_not_started() {
    let mut c = config();
    c.engine = Engine::DEMO;
    let (handle, mut events, task) = spawn(c);
    wait_for(&mut events, |e| matches!(e, Event::Ready { .. })).await;
    handle.send(Command::Prompt("hello".into())).unwrap();
    handle.interrupt();
    let outcome = wait_for(&mut events, |e| matches!(e, Event::Finished { .. })).await;
    assert!(
        matches!(
            outcome,
            Event::Finished {
                outcome: Outcome::Interrupted
            }
        ),
        "{outcome:?}"
    );
    handle.shutdown();
    timeout(Duration::from_secs(3), task)
        .await
        .unwrap()
        .unwrap();
}
