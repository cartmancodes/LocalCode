use lc_engine::live::{spawn, Command, Config, Event};
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
