//! Adversarial wire transcripts exercise the actual gate executable offline.
#![cfg(unix)]
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};

/// A probe expected to fail waits out its whole budget, so that budget is short.
fn probe_engine(engine: &str, scenario: &str, frames: Vec<Value>) -> Value {
    probe_engine_within(engine, scenario, frames, 2)
}
/// A probe expected to pass returns as soon as the terminal event arrives, so it
/// can afford a budget that survives a slow process launch on a busy machine.
fn passing_probe_engine(engine: &str, scenario: &str, frames: Vec<Value>) -> Value {
    probe_engine_within(engine, scenario, frames, 20)
}
fn probe_engine_within(engine: &str, scenario: &str, frames: Vec<Value>, seconds: u64) -> Value {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let root = std::env::temp_dir().join(format!(
        "octet-gate-test-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&root).unwrap();
    let executable = root.join("vendor");
    let wire = frames
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
    fs::write(&executable, format!("#!/bin/sh\nif [ \"$1\" = --version ]; then echo fixture; exit; fi\ncat <<'WIRE'\n{wire}\nWIRE\ncat >/dev/null\n")).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_protocol-gate"))
        .args(["--engine", engine, "--scenario", scenario, "--binary"])
        .arg(&executable)
        .args(["--timeout", &seconds.to_string()])
        .output()
        .unwrap();
    fs::remove_dir_all(root).unwrap();
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| panic!("gate output: {:?}", output))
}
fn probe(scenario: &str, events: Vec<Value>) -> Value {
    probe_engine("codex", scenario, codex_frames(events))
}
fn passing_probe(scenario: &str, events: Vec<Value>) -> Value {
    passing_probe_engine("codex", scenario, codex_frames(events))
}
fn codex_frames(mut events: Vec<Value>) -> Vec<Value> {
    let mut frames = vec![
        json!({"id":1,"result":{}}),
        json!({"id":2,"result":{"thread":{"id":"t"},"model":"fixture"}}),
        json!({"id":3,"result":{"turn":{"id":"u"}}}),
        json!({"method":"turn/started","params":{"threadId":"t","turn":{"id":"u"}}}),
    ];
    frames.append(&mut events);
    frames
}
fn terminal(thread: &str, turn: Value, status: &str) -> Value {
    json!({"method":"turn/completed","params":{"threadId":thread,"turn":{"id":turn,"status":status}}})
}
#[test]
fn failure_probe_rejects_success_and_uncorrelated_terminals() {
    for frame in [
        terminal("t", json!("u"), "completed"),
        terminal("other", json!("u"), "failed"),
        terminal("t", Value::Null, "failed"),
    ] {
        let evidence = probe("failure", vec![frame]);
        assert!(
            evidence["status"].as_str().unwrap().starts_with("failed:"),
            "{evidence}"
        );
        assert_eq!(evidence["child_cleanup"], true);
    }
    assert_eq!(
        passing_probe("failure", vec![terminal("t", json!("u"), "failed")])["status"],
        "passed"
    );
}
#[test]
fn interrupt_terminal_can_precede_acknowledgement() {
    let evidence = passing_probe(
        "interrupt",
        vec![
            terminal("t", json!("u"), "interrupted"),
            json!({"id":4,"result":{}}),
        ],
    );
    assert_eq!(evidence["status"], "passed", "{evidence}");
    assert_eq!(evidence["child_cleanup"], true);
}

#[test]
fn simple_probe_requires_expected_reply() {
    let done = terminal("t", json!("u"), "completed");
    assert!(probe("simple", vec![done.clone()])["status"]
        .as_str()
        .unwrap()
        .starts_with("failed:"));
    let answer = json!({"method":"item/completed","params":{"threadId":"t","turnId":"u","item":{"type":"agentMessage","text":"READY"}}});
    assert_eq!(
        passing_probe("simple", vec![answer, done])["status"],
        "passed"
    );
}

#[test]
fn claude_checks_result_and_accepts_terminal_before_interrupt_ack() {
    let init =
        json!({"type":"control_response","response":{"request_id":"init-1","subtype":"success"}});
    let done = json!({"type":"result","is_error":false,"result":"READY"});
    let ack = json!({"type":"control_response","response":{"request_id":"interrupt-2","subtype":"success"}});
    assert_eq!(
        passing_probe_engine("claude", "interrupt", vec![init.clone(), done.clone(), ack])
            ["status"],
        "passed"
    );
    assert_eq!(
        passing_probe_engine("claude", "simple", vec![init.clone(), done])["status"],
        "passed"
    );
    assert!(probe_engine(
        "claude",
        "simple",
        vec![init, json!({"type":"result","result":"READY"})]
    )["status"]
        .as_str()
        .unwrap()
        .starts_with("failed:"));
}
