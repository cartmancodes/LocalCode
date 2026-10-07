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

/// Runs the gate against `frames` with its temporary workspace under `tmp`;
/// the gate's exit code and its parsed evidence, if it printed any.
fn probe_in(
    tmp: &std::path::Path,
    engine: &str,
    scenario: &str,
    body: &str,
) -> (Option<i32>, Option<Value>) {
    let executable = octet_testkit::write_script(tmp, "vendor", body);
    let output = Command::new(env!("CARGO_BIN_EXE_protocol-gate"))
        .args(["--engine", engine, "--scenario", scenario, "--binary"])
        .arg(&executable)
        .args(["--timeout", "5"])
        .env("TMPDIR", tmp)
        .output()
        .unwrap();
    (
        output.status.code(),
        serde_json::from_slice(&output.stdout).ok(),
    )
}

fn wire_script(frames: &[Value]) -> String {
    let wire = frames
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "if [ \"$1\" = --version ]; then echo fixture; exit; fi\n\
         for arg in \"$@\"; do printf '%s\\n' \"$arg\"; done > \"$(dirname \"$0\")/argv\"\n\
         cat <<'WIRE'\n{wire}\nWIRE\ncat >/dev/null"
    )
}

#[test]
fn the_gate_launches_claude_as_octet_does() {
    let root = octet_testkit::TempDir::new("octet-gate-argv");
    std::fs::create_dir_all(root.path()).unwrap();
    let init =
        json!({"type":"control_response","response":{"request_id":"init-1","subtype":"success"}});
    let done = json!({"type":"result","is_error":false,"result":"READY"});
    let (_, evidence) = probe_in(root.path(), "claude", "simple", &wire_script(&[init, done]));
    assert_eq!(evidence.unwrap()["status"], "passed");
    let argv = std::fs::read_to_string(root.path().join("argv")).unwrap();
    let argv: Vec<&str> = argv.lines().collect();
    let config =
        octet_engine::live::Config::new(octet_engine::live::Engine::CLAUDE, "claude", root.path());
    let octet: Vec<String> = octet_engine::live::launch_args(&config)
        .iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    assert!(
        argv.starts_with(&octet.iter().map(String::as_str).collect::<Vec<_>>()),
        "gate {argv:?}\noctet {octet:?}"
    );
}

#[test]
fn an_approval_without_an_id_is_a_protocol_failure() {
    let root = octet_testkit::TempDir::new("octet-gate-no-id");
    std::fs::create_dir_all(root.path()).unwrap();
    let request = json!({"method":"item/commandExecution/requestApproval","params":{"command":"other","cwd":"/elsewhere"}});
    let (code, evidence) = probe_in(
        root.path(),
        "codex",
        "approval-interrupt",
        &wire_script(&codex_frames(vec![request])),
    );
    assert_eq!(code, Some(1), "a failed scenario, not a panic");
    let evidence = evidence.expect("evidence is printed");
    assert!(
        evidence["status"]
            .as_str()
            .unwrap()
            .starts_with("failed: protocol"),
        "{evidence}"
    );
}

#[test]
fn the_workspace_is_removed_on_failure() {
    let root = octet_testkit::TempDir::new("octet-gate-cleanup");
    std::fs::create_dir_all(root.path()).unwrap();
    let request = json!({"method":"item/commandExecution/requestApproval","params":{"command":"other","cwd":"/elsewhere"}});
    let _ = probe_in(
        root.path(),
        "codex",
        "approval-interrupt",
        &wire_script(&codex_frames(vec![request])),
    );
    let left: Vec<_> = std::fs::read_dir(root.path())
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .filter(|name| name.to_string_lossy().starts_with("octet-protocol-"))
        .collect();
    assert!(left.is_empty(), "{left:?}");
}
