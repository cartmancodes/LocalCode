//! Writing frames, and the frames both fake vendors build often.
use octet_testkit::scenario;
use serde_json::{Value, json};
use std::io::{self, Write};

/// Writes `value` as one JSON line and flushes it.
pub(crate) fn emit(value: &Value) {
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, value).unwrap();
    stdout.write_all(b"\n").unwrap();
    stdout.flush().unwrap();
}

/// The reply to a `scenario::GOAL` prompt: a first step, then completion.
pub(crate) fn goal_reply(text: &str) -> Option<&'static str> {
    if !scenario::is_goal_prompt(text, scenario::GOAL) {
        return None;
    }
    Some(if text.contains("Begin the objective.") {
        "First step verified; the final audit remains."
    } else {
        "Verified both steps and their tests.\n[[OCTET_GOAL_COMPLETE]]"
    })
}

/// Codex's notice that the fixture thread's turn `active` ended with `status`.
pub(crate) fn codex_turn_completed(active: &str, status: &str) -> Value {
    json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":status}}})
}

/// A Codex text delta for `thread`'s turn `turn`.
pub(crate) fn codex_delta(thread: &str, turn: &str, item: &str, delta: &str) -> Value {
    json!({"method":"item/agentMessage/delta","params":{"threadId":thread,"turnId":turn,"itemId":item,"delta":delta}})
}

/// Claude's session announcement at the start of a turn.
pub(crate) fn claude_init(sid: &str) -> Value {
    json!({"type":"system","subtype":"init","session_id":sid,"model":"claude-fixture-full-id"})
}

/// Claude's successful end of a turn with `result` as its text.
pub(crate) fn claude_result(sid: &str, result: &str) -> Value {
    json!({"type":"result","is_error":false,"result":result,"session_id":sid,"total_cost_usd":0.0})
}

/// A Claude streamed text delta.
pub(crate) fn claude_delta(text: &str) -> Value {
    json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":text}}})
}

/// A Claude permission request for `echo fixture`, numbered `id`.
pub(crate) fn claude_permission(id: &str) -> Value {
    json!({"type":"control_request","request_id":id,"request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"echo fixture"}}})
}

/// Claude's reply to the control request `request_id`.
pub(crate) fn claude_control_success(request_id: &Value, response: &Value) -> Value {
    json!({"type":"control_response","response":{"request_id":request_id,"subtype":"success","response":response}})
}

/// Claude's refusal of the control request `request_id`.
pub(crate) fn claude_control_error(request_id: &Value, error: &str) -> Value {
    json!({"type":"control_response","response":{"request_id":request_id,"subtype":"error","error":error}})
}
