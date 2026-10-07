//! The fake Claude Code (`--print` stream-json): control requests, answers
//! to its own permission requests, and user turns, with the scripts in
//! `scenario` chosen by the prompt's text or a launch argument.
use crate::wire::{
    claude_control_error, claude_control_success, claude_delta, claude_init, claude_permission,
    claude_result, emit, goal_reply,
};
use octet_testkit::scenario;
use serde_json::{Value, json};
use std::{
    env,
    io::{self, BufRead},
    process, thread,
    time::Duration,
};

/// One Claude session's state.
struct ClaudeFixture {
    /// The launch arguments; some scripts are chosen by them.
    argv: Vec<String>,
    /// The session ID it reports.
    sid: &'static str,
    /// Every permission mode asked for, for `MODES`.
    modes: Vec<String>,
    /// Mode switches held back until `FLUSH` (with `HANG_MODE`).
    held: Vec<Value>,
}

pub(crate) fn run() {
    let argv: Vec<String> = env::args().skip(1).collect();
    // A forked session gets a new ID, which Claude reports with the first turn.
    let sid = if argv.iter().any(|a| a == "--fork-session") {
        "claude-forked"
    } else {
        "claude-fixture"
    };
    let mut claude = ClaudeFixture {
        argv,
        sid,
        modes: Vec::new(),
        held: Vec::new(),
    };
    for line in io::stdin().lock().lines() {
        let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
        match v["type"].as_str() {
            Some("control_request") => claude.control_request(&v),
            Some("control_response") => claude.answered(&v),
            Some("user") => claude.user(&v),
            _ => {}
        }
    }
}

impl ClaudeFixture {
    fn launched_with(&self, flag: &str) -> bool {
        self.argv.iter().any(|a| a == flag)
    }

    fn control_request(&mut self, v: &Value) {
        match v["request"]["subtype"].as_str() {
            Some("initialize") => self.initialize(v),
            Some("interrupt") => emit(&claude_result(self.sid, "")),
            Some("set_permission_mode") => self.set_mode(v),
            _ => {}
        }
    }

    fn initialize(&self, v: &Value) {
        if self.launched_with(scenario::DIE_STDERR) {
            eprintln!("No conversation found with session ID: fixture");
            process::exit(1);
        }
        let requested = self
            .argv
            .iter()
            .position(|a| a == "--permission-mode")
            .and_then(|i| self.argv.get(i + 1))
            .map_or("default", String::as_str);
        let reported = if self.launched_with(scenario::REPORT_AUTO) {
            "auto"
        } else if self.launched_with(scenario::REPORT_PLAN) {
            "plan"
        } else {
            requested
        };
        emit(&claude_control_success(
            &v["request_id"],
            &json!({"current_permission_mode":reported,"models":[{"value":"sonnet","resolvedModel":"claude-fixture-full-id","displayName":"Fixture Sonnet","description":"Provider description"}]}),
        ));
    }

    fn set_mode(&mut self, v: &Value) {
        let mode = &v["request"]["mode"];
        self.modes
            .push(mode.as_str().unwrap_or("<missing>").to_owned());
        let id = &v["request_id"];
        if self.launched_with(scenario::HANG_MODE) {
            // Answer only when a `FLUSH` prompt arrives.
            self.held.push(id.clone());
        } else if self.launched_with(scenario::ODD_MODE) {
            emit(&claude_control_success(id, &json!({"mode":"plan"})));
        } else if self.launched_with(scenario::LATE_MODE) {
            // Confirm after the test's shortened mode deadline.
            let reply = claude_control_success(id, &json!({"mode":mode}));
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(2500));
                emit(&reply);
            });
        } else if self.launched_with(scenario::REJECT_MODE) {
            emit(&claude_control_error(
                id,
                "Cannot set permission mode: fixture refusal",
            ));
        } else {
            emit(&claude_control_success(id, &json!({"mode":mode})));
        }
    }

    /// Octet's answer to one of our permission requests.
    fn answered(&self, v: &Value) {
        let answer = &v["response"]["response"];
        match v.pointer("/response/request_id").and_then(Value::as_str) {
            // Octet answered a request Claude had cancelled.
            Some(scenario::CANCELLED_APPROVAL_ID) => {
                emit(&claude_delta("answered a cancelled request"));
            }
            // Each answer to an `APPROVALS_9` request, as "answered cap-N:behavior".
            Some(id) if id.starts_with(scenario::CAP_PREFIX) => {
                let reason = answer["message"]
                    .as_str()
                    .map(|reason| format!(":{reason}"))
                    .unwrap_or_default();
                let behavior = answer["behavior"].as_str().unwrap_or("?");
                emit(&claude_delta(&format!("answered {id}:{behavior}{reason}")));
            }
            // The `APPROVAL` script: echo what Octet sent, as
            // "allow:<command>" or "deny:<the reason Octet gave>".
            Some(scenario::APPROVAL_ID) => {
                let reply = format!(
                    "{}:{}",
                    answer["behavior"].as_str().unwrap_or("?"),
                    answer["updatedInput"]["command"]
                        .as_str()
                        .or(answer["message"].as_str())
                        .unwrap_or("")
                );
                emit(&claude_delta(&reply));
                emit(&claude_result(self.sid, &reply));
            }
            _ => {}
        }
    }

    fn user(&mut self, v: &Value) {
        let sid = self.sid;
        let text = v
            .pointer("/message/content/0/text")
            .and_then(Value::as_str)
            .unwrap_or("");
        match text {
            scenario::APPROVAL => {
                emit(&claude_init(sid));
                emit(&claude_permission(scenario::APPROVAL_ID));
            }
            scenario::APPROVAL_CANCEL => {
                emit(&claude_init(sid));
                emit(&claude_permission(scenario::CANCELLED_APPROVAL_ID));
                emit(
                    &json!({"type":"control_cancel_request","request_id":scenario::CANCELLED_APPROVAL_ID}),
                );
                // The turn stays open: an answer to it must never arrive.
            }
            scenario::APPROVALS_9 => {
                emit(&claude_init(sid));
                for n in 1..=9 {
                    emit(&claude_permission(&format!("{}{n}", scenario::CAP_PREFIX)));
                }
            }
            scenario::DELTA_BURST => {
                emit(&claude_init(sid));
                for _ in 0..200 {
                    emit(&claude_delta("x"));
                }
                emit(&claude_result(sid, ""));
            }
            scenario::DELTA_FLOOD => {
                emit(&claude_init(sid));
                let delta = "x".repeat(100);
                for _ in 0..3000 {
                    emit(&claude_delta(&delta));
                }
                emit(&claude_result(sid, ""));
            }
            scenario::SUBAGENT => {
                emit(&claude_init(sid));
                emit(
                    &json!({"type":"assistant","parent_tool_use_id":null,"message":{"model":"claude-main","content":[{"type":"text","text":"MAIN"}]}}),
                );
                emit(
                    &json!({"type":"stream_event","parent_tool_use_id":"toolu_1","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"SUBDELTA"}}}),
                );
                emit(
                    &json!({"type":"assistant","parent_tool_use_id":"toolu_1","message":{"model":"claude-sub","content":[{"type":"text","text":"SUBAGENT"},{"type":"tool_use","id":"toolu_2","name":"Read","input":{"file":"a"}}]}}),
                );
                emit(&claude_result(sid, ""));
            }
            scenario::ODD_SESSION => {
                emit(&claude_init(sid));
                emit(&claude_result("s\u{1b}[2Jx", ""));
            }
            // A result that omits is_error, as an older CLI might send.
            scenario::NO_IS_ERROR => {
                emit(&json!({"type":"result","result":"odd","session_id":sid}));
            }
            scenario::TOOL => {
                emit(&claude_init(sid));
                emit(
                    &json!({"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"Bash","input":{}}}}),
                );
                emit(
                    &json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"echo fixture"}}]}}),
                );
                emit(&claude_result(sid, ""));
            }
            scenario::ERRORS => fail(sid),
            _ if scenario::is_goal_prompt(text, scenario::GOAL_FAIL) => fail(sid),
            // Stay mid-turn until the driver interrupts.
            scenario::HOLD => emit(&claude_init(sid)),
            _ if scenario::is_goal_prompt(text, scenario::GOAL_HOLD) => emit(&claude_init(sid)),
            _ => {
                if text == scenario::FLUSH {
                    self.flush();
                }
                self.reply(text, v);
            }
        }
    }

    /// Answers the held mode switches: the first succeeds, the rest are refused.
    fn flush(&mut self) {
        for (index, id) in self.held.drain(..).enumerate() {
            emit(&if index == 0 {
                claude_control_success(&id, &json!({"mode":self.modes[0]}))
            } else {
                claude_control_error(&id, "fixture refusal")
            });
        }
    }

    /// A prompt with no script of its own: its reply, streamed and whole.
    fn reply(&self, text: &str, v: &Value) {
        // Image blocks are echoed as "image:<media type>:<base64 length>".
        let images: Vec<String> = v
            .pointer("/message/content")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter(|block| block["type"] == "image")
            .map(|block| {
                let source = &block["source"];
                let length = source["data"].as_str().map_or(0, str::len);
                format!(
                    "image:{}:{length}",
                    source["media_type"].as_str().unwrap_or("")
                )
            })
            .collect();
        let reply = if !images.is_empty() {
            images.join(",")
        } else if text == scenario::ARGV {
            self.argv.join(" ")
        } else if text == "/compact" {
            "Compacted".to_owned()
        } else if text == scenario::MODES {
            self.modes.join(",")
        } else if let Some(reply) = goal_reply(text) {
            reply.to_owned()
        } else {
            scenario::CLAUDE_REPLY.to_owned()
        };
        emit(&claude_init(self.sid));
        emit(&claude_delta(&reply));
        emit(&json!({"type":"assistant","message":{"content":[{"type":"text","text":reply}]}}));
        emit(&claude_result(self.sid, &reply));
    }
}

/// A failed result with error details.
fn fail(sid: &str) {
    emit(
        &json!({"type":"result","subtype":"error_during_execution","is_error":true,"errors":["Fixture failure detail"],"session_id":sid,"total_cost_usd":0.0}),
    );
}
