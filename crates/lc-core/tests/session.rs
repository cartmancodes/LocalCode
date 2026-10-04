use lc_core::{Command, Config, Engine, Event, Session};
use std::time::Duration;
#[tokio::test]
async fn demo_turn_is_journaled_and_export_never_overwrites() {
    let temp = lc_testkit::TempDir::new("lc-core-test");
    let directory = temp.path().to_path_buf();
    let config = Config::new(Engine::Demo, "unused", directory.clone());
    let mut session = Session::open(config, directory.clone()).await.unwrap();
    assert!(matches!(
        session.events.recv().await,
        Some(Event::Ready { .. })
    ));
    session
        .handle
        .send(Command::Prompt("hello journal".into()))
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(event) = session.events.recv().await {
            if matches!(event, Event::Finished { .. }) {
                break;
            }
        }
    })
    .await
    .unwrap();
    let exported = directory.join("export.jsonl");
    lc_core::export_journal(&session.journal, &exported)
        .await
        .unwrap();
    assert!(lc_core::export_journal(&session.journal, &exported)
        .await
        .is_err());
    let text = tokio::fs::read_to_string(&exported).await.unwrap();
    assert!(text.contains("hello journal"));
    let first: serde_json::Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
    assert_eq!(first["data"]["mode"], "ask");
    assert!(text.contains("finished"));
    session.shutdown().await;
}
#[tokio::test]
async fn repeated_mode_events_are_journaled_once_but_all_delivered() {
    let temp = lc_testkit::TempDir::new("lc-core-mode");
    let directory = temp.path().to_path_buf();
    let config = Config::new(Engine::Demo, "unused", directory.clone());
    let mut session = Session::open(config, directory.clone()).await.unwrap();
    let mut delivered = Vec::new();
    for command in [
        lc_core::Mode::FullAccess,
        lc_core::Mode::Ask,
        lc_core::Mode::Auto,
    ] {
        session.handle.send(Command::SetMode(command)).unwrap();
    }
    tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(event) = session.events.recv().await {
            if let Event::ModeChanged(mode) = event {
                delivered.push(mode.label());
                if mode == lc_core::Mode::Auto {
                    break;
                }
            }
        }
    })
    .await
    .unwrap();
    assert_eq!(delivered, ["ask", "ask", "ask", "auto"]);
    let text = tokio::fs::read_to_string(&session.journal).await.unwrap();
    let journaled: Vec<String> = text
        .lines()
        .map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap())
        .filter(|v| v["type"] == "mode")
        .map(|v| v["data"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(journaled, ["ask", "auto"]);
    session.shutdown().await;
}
#[tokio::test]
async fn journal_keeps_engine_and_outcome_spelling() {
    let temp = lc_testkit::TempDir::new("lc-journal-spelling");
    let config = Config::new(Engine::Demo, "demo", temp.path());
    let mut session = Session::open(config, temp.path().to_path_buf())
        .await
        .unwrap();
    session.handle.send(Command::Prompt("hi".into())).unwrap();
    tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(event) = session.events.recv().await {
            if matches!(event, Event::Finished { .. }) {
                break;
            }
        }
    })
    .await
    .unwrap();
    session.shutdown().await;
    let journal = std::fs::read_to_string(&session.journal).unwrap();
    let records: Vec<serde_json::Value> = journal
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records[0]["data"]["engine"], "demo");
    assert!(records
        .iter()
        .any(|record| record["type"] == "finished" && record["data"] == "completed"));
}
