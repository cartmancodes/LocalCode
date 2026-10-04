use lc_engine::live::{spawn, Command, Config, Event, Mode};
use std::{path::PathBuf, sync::OnceLock, time::Duration};
use tokio::{sync::mpsc, time::timeout};
fn config() -> Config {
    static CHILD: OnceLock<PathBuf> = OnceLock::new();
    let binary = CHILD
        .get_or_init(|| {
            let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
            assert!(std::process::Command::new("cargo")
                .args([
                    "build",
                    "--quiet",
                    "-p",
                    "lc-testkit",
                    "--bin",
                    "protocol-child"
                ])
                .current_dir(&root)
                .status()
                .unwrap()
                .success());
            std::env::var_os("CARGO_TARGET_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| root.join("target"))
                .join("debug/protocol-child")
        })
        .clone();
    Config {
        engine: "codex".into(),
        binary,
        cwd: std::env::temp_dir(),
        model: None,
        resume: None,
        mode: Default::default(),
    }
}
async fn next(events: &mut mpsc::Receiver<Event>) -> Event {
    timeout(Duration::from_secs(5), events.recv())
        .await
        .unwrap()
        .unwrap()
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
                    assert_eq!(outcome, "completed");
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
                assert_eq!(outcome, "interrupted");
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
    config.engine = "claude".into();
    let (handle, mut events, task) = spawn(config);
    assert!(matches!(next(&mut events).await, Event::Ready { .. }));
    for _ in 0..2 {
        handle.send(Command::Prompt("hello".into())).unwrap();
        let mut text = String::new();
        loop {
            match next(&mut events).await {
                Event::Text(t) => text.push_str(&t),
                Event::Finished { outcome } => {
                    assert_eq!(outcome, "completed");
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
    cfg.engine = "claude".into();
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
                assert_eq!(outcome, "interrupted");
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
    c.engine = "claude".into();
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
    c.engine = "claude".into();
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
    c.engine = "claude".into();
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
    c.engine = "claude".into();
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
    c.engine = "claude".into();
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
