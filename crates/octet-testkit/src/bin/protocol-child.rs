//! A fake Codex/Claude vendor for tests: it speaks just enough of each
//! protocol to drive Octet through scripted scenarios.
// Test fixture: a panic here fails the test that started it.
#![allow(clippy::unwrap_used)]
use octet_testkit::scenario;
use serde_json::{Value, json};
use std::{
    env,
    io::{self, BufRead, Write},
    process::{self, Command, Stdio},
    thread,
    time::Duration,
};

fn emit(value: &Value) {
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, value).unwrap();
    stdout.write_all(b"\n").unwrap();
    stdout.flush().unwrap();
}

/// Codex's notice that a turn ended with `status`.
fn codex_turn_completed(active: &str, status: &str) -> Value {
    json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":status}}})
}

/// Claude's session announcement at the start of a turn.
fn claude_init(sid: &str) -> Value {
    json!({"type":"system","subtype":"init","session_id":sid,"model":"claude-fixture-full-id"})
}

/// Claude's successful end of a turn with `result` as its text.
fn claude_result(sid: &str, result: &str) -> Value {
    json!({"type":"result","is_error":false,"result":result,"session_id":sid,"total_cost_usd":0.0})
}

fn goal_reply(text: &str) -> Option<&'static str> {
    if !text.starts_with("Octet active goal: fixture-goal\n") {
        return None;
    }
    Some(if text.contains("Begin the objective.") {
        "First step verified; the final audit remains."
    } else {
        "Verified both steps and their tests.\n[[OCTET_GOAL_COMPLETE]]"
    })
}

fn main() {
    let mode = env::args().nth(1).expect("mode");
    match mode.as_str() {
        "app-server" => interactive_codex(),
        "--print" => interactive_claude(),
        "split" => {
            let mut stdout = io::stdout().lock();
            stdout.write_all(b"{\"part\":").unwrap();
            stdout.flush().unwrap();
            thread::sleep(Duration::from_millis(25));
            stdout.write_all(b"\"complete\"}\n").unwrap();
        }
        "oversize" => {
            let mut stdout = io::stdout().lock();
            stdout.write_all(b"{\"x\":\"").unwrap();
            stdout.write_all(&vec![b'x'; 8192]).unwrap();
            stdout.write_all(b"\"}\n").unwrap();
        }
        "malformed" => println!("{{invalid\n{{\"valid\":true}}"),
        "partial" => {
            io::stdout().write_all(b"{\"unfinished\":").unwrap();
        }
        "stderr" => {
            let mut stderr = io::stderr().lock();
            stderr.write_all(&vec![b'z'; 65_536]).unwrap();
            stderr.write_all(b"END-OF-STDERR\n").unwrap();
            stderr.flush().unwrap();
            emit(&json!({"ready":true}));
            thread::sleep(Duration::from_secs(5));
        }
        "stderr-exit" => {
            // Explain on stderr and exit at once, like a CLI rejecting its arguments.
            let mut stderr = io::stderr().lock();
            stderr.write_all(&vec![b'z'; 300_000]).unwrap();
            stderr.write_all(b"FINAL-REASON\n").unwrap();
        }
        "grandchild" => {
            // This fixture intentionally exits before reaping its child so the
            // supervisor must clean up a process group whose leader is gone.
            #[allow(clippy::zombie_processes)]
            let child = Command::new(env::current_exe().unwrap())
                .arg("sleeper")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            emit(&json!({"pid":child.id()}));
        }
        "sleeper" => loop {
            thread::sleep(Duration::from_secs(10));
        },
        "echo" | "flood" => {
            let stdin = io::stdin();
            for line in stdin.lock().lines() {
                let value: Value = serde_json::from_str(&line.unwrap()).unwrap();
                match value["op"].as_str() {
                    Some("exit") => process::exit(0),
                    Some("echo") => emit(&value),
                    Some("flood") if mode == "flood" => {
                        thread::spawn(|| {
                            for i in 0..200 {
                                emit(&json!({"data":i}));
                            }
                        });
                    }
                    Some("interrupt") => emit(&json!({"ack":"interrupt"})),
                    _ => {}
                }
            }
        }
        _ => panic!("invalid mode"),
    }
}

fn interactive_codex() {
    let mut turn = 0u64;
    let mut active = String::new();
    // "die-on-interrupt" holds its turn and exits when asked to stop it, as
    // a vendor that ignores the interrupt is eventually stopped.
    let mut dies_on_interrupt = false;
    let mut thread_params = Value::Null;
    for line in io::stdin().lock().lines() {
        let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
        match v["method"].as_str() {
            Some("model/list") => {
                if v["params"]["cursor"].as_str() == Some("page-two") {
                    emit(
                        &json!({"id":v["id"],"result":{"data":[{"id":"picker-other","model":"other-full-id","displayName":"Other Model","description":"Second page"}],"nextCursor":null}}),
                    );
                } else {
                    emit(
                        &json!({"id":v["id"],"result":{"data":[{"id":"picker-fixture","model":"fixture","displayName":"Fixture Model","description":"Runtime metadata"}],"nextCursor":"page-two"}}),
                    );
                }
            }
            Some("initialize") => emit(&json!({"id":v["id"],"result":{}})),
            Some("thread/start" | "thread/resume" | "thread/fork")
                if v["params"]["model"] == scenario::REFUSE_THREAD =>
            {
                emit(
                    &json!({"id":v["id"],"error":{"code":-32600,"message":"no rollout found for thread id fixture"}}),
                );
            }
            Some("thread/start" | "thread/resume" | "thread/fork") => {
                thread_params = v["params"].clone();
                thread_params["method"] = v["method"].clone();
                // Echo the policy like Codex does; "report-stricter" simulates a
                // managed requirement that overrides what the client asked for.
                let p = &v["params"];
                let (sandbox, policy, reviewer) = if p["model"] == scenario::REPORT_STRICTER {
                    (json!("workspace-write"), json!("untrusted"), json!("user"))
                } else {
                    (
                        p["sandbox"].clone(),
                        p["approvalPolicy"].clone(),
                        p["approvalsReviewer"].clone(),
                    )
                };
                let sandbox = match sandbox.as_str() {
                    Some("workspace-write") => json!({"type":"workspaceWrite"}),
                    Some("danger-full-access") => json!({"type":"dangerFullAccess"}),
                    Some("read-only") => json!({"type":"readOnly"}),
                    _ => sandbox,
                };
                // A fork opens a new thread; the turns that follow still run
                // on the fixture thread, which is all the fork tests need.
                let thread = if v["method"] == "thread/fork" {
                    "forked-thread"
                } else {
                    "fixture-thread"
                };
                emit(
                    &json!({"id":v["id"],"result":{"thread":{"id":thread},"model":"fixture","sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer}}),
                )
            }
            Some("thread/compact/start") => {
                // Codex runs compaction as a turn on the thread.
                turn += 1;
                active = format!("turn-{turn}");
                emit(&json!({"id":v["id"],"result":{}}));
                emit(
                    &json!({"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":active}}}),
                );
                emit(
                    &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"compact","type":"contextCompaction"}}}),
                );
                emit(&codex_turn_completed(&active, "completed"));
            }
            Some("turn/start") => {
                turn += 1;
                active = format!("turn-{turn}");
                let first = v
                    .pointer("/params/input/0/text")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if first == scenario::REFUSE_START {
                    // Refuse the turn after a pause, so Octet can hold a steer.
                    thread::sleep(Duration::from_millis(400));
                    emit(&json!({"id":v["id"],"error":{"code":-32600,"message":"turn refused"}}));
                    continue;
                }
                emit(&json!({"id":v["id"],"result":{"turn":{"id":active}}}));
                if first == scenario::LATE_START {
                    // Name the turn late, so Octet holds steers and cancels.
                    thread::sleep(Duration::from_millis(400));
                }
                emit(
                    &json!({"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":active}}}),
                );
                let text = v
                    .pointer("/params/input/0/text")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if text == scenario::HOLD_MARKED {
                    emit(
                        &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"marker","delta":"named"}}),
                    );
                    continue;
                }
                if text == scenario::HOLD
                    || text == scenario::LATE_START
                    || text.starts_with("Octet active goal: fixture-hold\n")
                {
                    continue;
                }
                if text == scenario::DIE_ON_INTERRUPT {
                    dies_on_interrupt = true;
                    continue;
                }
                if text == scenario::APPROVALS_9 {
                    for n in 1..=9 {
                        emit(
                            &json!({"id":format!("cap-{n}"),"method":"item/commandExecution/requestApproval","params":{"threadId":"fixture-thread","turnId":active,"command":"echo fixture"}}),
                        );
                    }
                    continue;
                }
                if text == scenario::APPROVAL {
                    emit(
                        &json!({"id":"permission","method":"item/commandExecution/requestApproval","params":{"threadId":"fixture-thread","turnId":active,"command":"echo fixture"}}),
                    );
                    continue;
                }
                if text == scenario::BIGTOOL {
                    // One completed command whose output is far larger than the event queue.
                    let output = "x".repeat(6 * 1024 * 1024);
                    emit(
                        &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"cmd","type":"commandExecution","command":"cat big","status":"completed","exitCode":0,"aggregatedOutput":output}}}),
                    );
                    emit(&codex_turn_completed(&active, "completed"));
                    continue;
                }
                if text == scenario::CHATTER {
                    // The turn goes silent while another thread keeps talking.
                    for _ in 0..8 {
                        thread::sleep(Duration::from_millis(300));
                        emit(
                            &json!({"method":"item/agentMessage/delta","params":{"threadId":"other-thread","turnId":"other","itemId":"x","delta":"."}}),
                        );
                    }
                    continue;
                }
                if text == scenario::FAIL || text.starts_with("Octet active goal: fixture-fail\n") {
                    emit(
                        &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"failed","error":{"additionalDetails":null,"codexErrorInfo":"usageLimitExceeded","message":"You've hit your usage limit."}}}}),
                    );
                    continue;
                }
                if text == scenario::FAIL_NESTED {
                    emit(
                        &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"failed","error":{"codexErrorInfo":"other","message":"{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The model is not supported.\"}}"}}}}),
                    );
                    continue;
                }
                if text == scenario::SLOW {
                    // Stays active longer than a short idle limit, never silent for long.
                    for _ in 0..8 {
                        thread::sleep(Duration::from_millis(300));
                        emit(
                            &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"slow","delta":"."}}),
                        );
                    }
                    emit(&codex_turn_completed(&active, "completed"));
                    continue;
                }
                if text == scenario::PARAMS {
                    let echo = json!({"thread":thread_params,"turn":v["params"]}).to_string();
                    emit(
                        &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"params","delta":echo}}),
                    );
                    emit(&codex_turn_completed(&active, "completed"));
                    continue;
                }
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"other-thread","turnId":active,"itemId":"wrong","delta":"MUST NOT DISPLAY"}}),
                );
                // Image inputs are echoed so tests can check what was sent.
                let images: Vec<&str> = v
                    .pointer("/params/input")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter(|item| item["type"] == "localImage")
                    .filter_map(|item| item["path"].as_str())
                    .collect();
                let reply = if images.is_empty() {
                    goal_reply(text).unwrap_or("Hello fixture").to_owned()
                } else {
                    format!("images:{}", images.join(","))
                };
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"msg","delta":reply}}),
                );
                emit(
                    &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"msg","type":"agentMessage","text":reply}}}),
                );
                emit(
                    &json!({"method":"thread/tokenUsage/updated","params":{"threadId":"fixture-thread","tokenUsage":{"total":{"totalTokens":42}}}}),
                );
                emit(&codex_turn_completed(&active, "completed"));
            }
            Some("turn/steer") => {
                // Steering adds to the running turn; a stale turn ID, or the
                // text "reject", is refused.
                let refused = v.pointer("/params/input/0/text") == Some(&json!("reject"));
                if v["params"]["expectedTurnId"] == active.as_str() && !refused {
                    let text = v
                        .pointer("/params/input/0/text")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    emit(
                        &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"steer","delta":format!("steered:{text}")}}),
                    );
                    emit(&json!({"id":v["id"],"result":{"turnId":active}}));
                } else {
                    emit(
                        &json!({"id":v["id"],"error":{"code":-32600,"message":"no active turn to steer"}}),
                    );
                }
            }
            Some("turn/interrupt") => {
                if dies_on_interrupt {
                    std::process::exit(3);
                }
                emit(&codex_turn_completed(&active, "interrupted"));
                emit(&json!({"id":v["id"],"result":{}}));
            }
            None if v["id"].as_str().is_some_and(|id| id.starts_with("cap-")) => {
                // Each answer to an "approvals-9" request, as "answered cap-N:decision".
                let reply = format!(
                    "answered {}:{}",
                    v["id"].as_str().unwrap_or(""),
                    v["result"]["decision"].as_str().unwrap_or("?")
                );
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"cap","delta":reply}}),
                );
            }
            None if v["id"] == "permission" => {
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"answer","delta":v["result"]["decision"].as_str().unwrap_or("bad response")}}),
                );
                emit(&codex_turn_completed(&active, "completed"));
            }
            _ => {}
        }
    }
}

fn interactive_claude() {
    let argv: Vec<String> = env::args().skip(1).collect();
    // A forked session gets a new ID, which Claude reports with the first turn.
    let sid = if argv.iter().any(|a| a == "--fork-session") {
        "claude-forked"
    } else {
        "claude-fixture"
    };
    let mut modes: Vec<String> = Vec::new();
    let mut held: Vec<Value> = Vec::new();
    for line in io::stdin().lock().lines() {
        let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if v["type"] == "control_request"
            && v["request"]["subtype"] == "initialize"
            && argv.iter().any(|a| a == scenario::DIE_STDERR)
        {
            eprintln!("No conversation found with session ID: fixture");
            process::exit(1);
        }
        if v["type"] == "control_request" && v["request"]["subtype"] == "initialize" {
            let requested = argv
                .iter()
                .position(|a| a == "--permission-mode")
                .and_then(|i| argv.get(i + 1))
                .map(String::as_str)
                .unwrap_or("default");
            let reported = if argv.iter().any(|a| a == scenario::REPORT_AUTO) {
                "auto"
            } else if argv.iter().any(|a| a == scenario::REPORT_PLAN) {
                "plan"
            } else {
                requested
            };
            emit(
                &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"current_permission_mode":reported,"models":[{"value":"sonnet","resolvedModel":"claude-fixture-full-id","displayName":"Fixture Sonnet","description":"Provider description"}]}}}),
            );
        } else if v["type"] == "control_request" && v["request"]["subtype"] == "interrupt" {
            emit(&claude_result(sid, ""));
        } else if v["type"] == "control_request" && v["request"]["subtype"] == "set_permission_mode"
        {
            modes.push(
                v["request"]["mode"]
                    .as_str()
                    .unwrap_or("<missing>")
                    .to_owned(),
            );
            if argv.iter().any(|a| a == scenario::HANG_MODE) {
                // Answer only when a "flush" prompt arrives.
                held.push(v["request_id"].clone());
            } else if argv.iter().any(|a| a == scenario::ODD_MODE) {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":"plan"}}}),
                );
            } else if argv.iter().any(|a| a == scenario::LATE_MODE) {
                // Confirm after the test's shortened mode deadline.
                let reply = json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":v["request"]["mode"]}}});
                thread::spawn(move || {
                    thread::sleep(Duration::from_millis(2500));
                    emit(&reply);
                });
            } else if argv.iter().any(|a| a == scenario::REJECT_MODE) {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"error","error":"Cannot set permission mode: fixture refusal"}}),
                );
            } else {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":v["request"]["mode"]}}}),
                );
            }
        } else if v["type"] == "control_response"
            && v.pointer("/response/request_id") == Some(&json!("perm-2"))
        {
            // Octet answered a request Claude had cancelled.
            emit(
                &json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"answered a cancelled request"}}}),
            );
        } else if v["type"] == "control_response"
            && v.pointer("/response/request_id")
                .and_then(Value::as_str)
                .is_some_and(|id| id.starts_with("cap-"))
        {
            // Each answer to an "approvals-9" request, as "answered cap-N:behavior".
            let answer = &v["response"]["response"];
            let reply = format!(
                "answered {}:{}{}",
                v["response"]["request_id"].as_str().unwrap_or(""),
                answer["behavior"].as_str().unwrap_or("?"),
                answer["message"]
                    .as_str()
                    .map(|reason| format!(":{reason}"))
                    .unwrap_or_default()
            );
            emit(
                &json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":reply}}}),
            );
        } else if v["type"] == "control_response"
            && v.pointer("/response/request_id") == Some(&json!("perm-1"))
        {
            // The answer to the "approval" script: echo what Octet sent.
            // "allow:<command>" or "deny:<the reason Octet gave>".
            let answer = &v["response"]["response"];
            let reply = format!(
                "{}:{}",
                answer["behavior"].as_str().unwrap_or("?"),
                answer["updatedInput"]["command"]
                    .as_str()
                    .or(answer["message"].as_str())
                    .unwrap_or("")
            );
            emit(
                &json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":reply}}}),
            );
            emit(&claude_result(sid, &reply));
        } else if v["type"] == "user" {
            let text = v
                .pointer("/message/content/0/text")
                .and_then(Value::as_str)
                .unwrap_or("");
            let permission = |id: &str| json!({"type":"control_request","request_id":id,"request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"echo fixture"}}});
            if text == scenario::APPROVAL {
                emit(&claude_init(sid));
                emit(&permission("perm-1"));
                continue;
            }
            if text == scenario::APPROVAL_CANCEL {
                emit(&claude_init(sid));
                emit(&permission("perm-2"));
                emit(&json!({"type":"control_cancel_request","request_id":"perm-2"}));
                // The turn stays open: an answer to perm-2 must never arrive.
                continue;
            }
            if text == scenario::APPROVALS_9 {
                emit(&claude_init(sid));
                for n in 1..=9 {
                    emit(&permission(&format!("cap-{n}")));
                }
                continue;
            }
            if text == scenario::NO_IS_ERROR {
                // A result that omits is_error, as an older CLI might send.
                emit(&json!({"type":"result","result":"odd","session_id":sid}));
                continue;
            }
            if text == scenario::FLUSH {
                // First held request succeeds, every later one is refused.
                for (index, id) in held.drain(..).enumerate() {
                    emit(&if index == 0 {
                        json!({"type":"control_response","response":{"request_id":id,"subtype":"success","response":{"mode":modes[0]}}})
                    } else {
                        json!({"type":"control_response","response":{"request_id":id,"subtype":"error","error":"fixture refusal"}})
                    });
                }
            }
            if text == scenario::TOOL {
                emit(&claude_init(sid));
                emit(
                    &json!({"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"Bash","input":{}}}}),
                );
                emit(
                    &json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"echo fixture"}}]}}),
                );
                emit(&claude_result(sid, ""));
                continue;
            }
            if text == scenario::ERRORS || text.starts_with("Octet active goal: fixture-fail\n") {
                emit(
                    &json!({"type":"result","subtype":"error_during_execution","is_error":true,"errors":["Fixture failure detail"],"session_id":sid,"total_cost_usd":0.0}),
                );
                continue;
            }
            if text == scenario::HOLD || text.starts_with("Octet active goal: fixture-hold\n") {
                // Stay mid-turn until the driver interrupts.
                emit(&claude_init(sid));
                continue;
            }
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
                argv.join(" ")
            } else if text == "/compact" {
                "Compacted".to_owned()
            } else if text == scenario::MODES {
                modes.join(",")
            } else if let Some(reply) = goal_reply(text) {
                reply.to_owned()
            } else {
                "Hello Claude".to_owned()
            };
            emit(&claude_init(sid));
            emit(
                &json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":reply}}}),
            );
            emit(&json!({"type":"assistant","message":{"content":[{"type":"text","text":reply}]}}));
            emit(&claude_result(sid, &reply));
        }
    }
}
