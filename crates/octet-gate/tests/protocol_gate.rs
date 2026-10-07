//! Adversarial wire transcripts exercise the actual gate executable offline.
#![cfg(unix)]
// Test code: an unwrap that fails is the test failing.
#![allow(clippy::unwrap_used)]
use serde_json::{Value, json};
use std::process::Command;

/// A probe expected to fail waits out its whole budget, so that budget is short.
fn probe_engine(engine: &str, scenario: &str, frames: &[Value]) -> Value {
    probe_engine_within(engine, scenario, frames, 2)
}
/// A probe expected to pass returns as soon as the terminal event arrives, so it
/// can afford a budget that survives a slow process launch on a busy machine.
fn passing_probe_engine(engine: &str, scenario: &str, frames: &[Value]) -> Value {
    probe_engine_within(engine, scenario, frames, 20)
}
fn probe_engine_within(engine: &str, scenario: &str, frames: &[Value], seconds: u64) -> Value {
    let root = octet_testkit::TempDir::new("octet-gate-test");
    let wire = frames
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
    let executable = octet_testkit::write_script(
        root.path(),
        "vendor",
        &format!(
            "if [ \"$1\" = --version ]; then echo fixture; exit; fi\ncat <<'WIRE'\n{wire}\nWIRE\ncat >/dev/null"
        ),
    );
    let output = Command::new(env!("CARGO_BIN_EXE_protocol-gate"))
        .args(["--engine", engine, "--scenario", scenario, "--binary"])
        .arg(&executable)
        .args(["--timeout", &seconds.to_string()])
        .output()
        .unwrap();
    serde_json::from_slice(&output.stdout).unwrap_or_else(|_| panic!("gate output: {:?}", output))
}
fn probe(scenario: &str, events: Vec<Value>) -> Value {
    probe_engine("codex", scenario, &codex_frames(events))
}
fn passing_probe(scenario: &str, events: Vec<Value>) -> Value {
    passing_probe_engine("codex", scenario, &codex_frames(events))
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
fn terminal(thread: &str, turn: &Value, status: &str) -> Value {
    json!({"method":"turn/completed","params":{"threadId":thread,"turn":{"id":turn,"status":status}}})
}
#[test]
fn failure_probe_rejects_success_and_uncorrelated_terminals() {
    for frame in [
        terminal("t", &json!("u"), "completed"),
        terminal("other", &json!("u"), "failed"),
        terminal("t", &Value::Null, "failed"),
    ] {
        let evidence = probe("failure", vec![frame]);
        assert!(
            evidence["status"].as_str().unwrap().starts_with("failed:"),
            "{evidence}"
        );
        assert_eq!(evidence["child_cleanup"], true);
    }
    assert_eq!(
        passing_probe("failure", vec![terminal("t", &json!("u"), "failed")])["status"],
        "passed"
    );
}
#[test]
fn interrupt_terminal_can_precede_acknowledgement() {
    let evidence = passing_probe(
        "interrupt",
        vec![
            terminal("t", &json!("u"), "interrupted"),
            json!({"id":4,"result":{}}),
        ],
    );
    assert_eq!(evidence["status"], "passed", "{evidence}");
    assert_eq!(evidence["child_cleanup"], true);
}

#[test]
fn simple_probe_requires_expected_reply() {
    let done = terminal("t", &json!("u"), "completed");
    assert!(
        probe("simple", vec![done.clone()])["status"]
            .as_str()
            .unwrap()
            .starts_with("failed:")
    );
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
        passing_probe_engine("claude", "interrupt", &[init.clone(), done.clone(), ack])["status"],
        "passed"
    );
    assert_eq!(
        passing_probe_engine("claude", "simple", &[init.clone(), done])["status"],
        "passed"
    );
    assert!(
        probe_engine(
            "claude",
            "simple",
            &[init, json!({"type":"result","result":"READY"})]
        )["status"]
            .as_str()
            .unwrap()
            .starts_with("failed:")
    );
}
