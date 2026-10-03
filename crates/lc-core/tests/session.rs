use lc_core::{Command, Config, Event, Session};
use std::time::Duration;
#[tokio::test]
async fn demo_turn_is_journaled_and_export_never_overwrites() {
    let directory = std::env::temp_dir().join(format!("lc-core-test-{}", std::process::id()));
    let config = Config {
        engine: "demo".into(),
        binary: "unused".into(),
        cwd: directory.clone(),
        model: None,
        resume: None,
    };
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
    assert!(text.contains("finished"));
    session.shutdown().await;
    tokio::fs::remove_dir_all(directory).await.unwrap();
}
