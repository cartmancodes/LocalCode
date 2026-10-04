use serde_json::{json, Value};
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
            Some("thread/start" | "thread/resume") => {
                thread_params = v["params"].clone();
                thread_params["method"] = v["method"].clone();
                // Echo the policy like Codex does; "report-stricter" simulates a
                // managed requirement that overrides what the client asked for.
                let p = &v["params"];
                let (sandbox, policy, reviewer) = if p["model"] == "report-stricter" {
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
                emit(
                    &json!({"id":v["id"],"result":{"thread":{"id":"fixture-thread"},"model":"fixture","sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer}}),
                )
            }
            Some("turn/start") => {
                turn += 1;
                active = format!("turn-{turn}");
                emit(&json!({"id":v["id"],"result":{"turn":{"id":active}}}));
                emit(
                    &json!({"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":active}}}),
                );
                let text = v
                    .pointer("/params/input/0/text")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if text == "hold" {
                    continue;
                }
                if text == "approval" {
                    emit(
                        &json!({"id":"permission","method":"item/commandExecution/requestApproval","params":{"threadId":"fixture-thread","turnId":active,"command":"echo fixture"}}),
                    );
                    continue;
                }
                if text == "params" {
                    let echo = json!({"thread":thread_params,"turn":v["params"]}).to_string();
                    emit(
                        &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"params","delta":echo}}),
                    );
                    emit(
                        &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"completed"}}}),
                    );
                    continue;
                }
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"other-thread","turnId":active,"itemId":"wrong","delta":"MUST NOT DISPLAY"}}),
                );
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"msg","delta":"Hello fixture"}}),
                );
                emit(
                    &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"msg","type":"agentMessage","text":"Hello fixture"}}}),
                );
                emit(
                    &json!({"method":"thread/tokenUsage/updated","params":{"threadId":"fixture-thread","tokenUsage":{"total":{"totalTokens":42}}}}),
                );
                emit(
                    &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"completed"}}}),
                );
            }
            Some("turn/interrupt") => {
                emit(
                    &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"interrupted"}}}),
                );
                emit(&json!({"id":v["id"],"result":{}}));
            }
            None if v["id"] == "permission" => {
                emit(
                    &json!({"method":"item/agentMessage/delta","params":{"threadId":"fixture-thread","turnId":active,"itemId":"answer","delta":v["result"]["decision"].as_str().unwrap_or("bad response")}}),
                );
                emit(
                    &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"completed"}}}),
                );
            }
            _ => {}
        }
    }
}

fn interactive_claude() {
    let argv: Vec<String> = env::args().skip(1).collect();
    let mut modes: Vec<String> = Vec::new();
    let mut held: Vec<Value> = Vec::new();
    for line in io::stdin().lock().lines() {
        let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if v["type"] == "control_request" && v["request"]["subtype"] == "initialize" {
            let requested = argv
                .iter()
                .position(|a| a == "--permission-mode")
                .and_then(|i| argv.get(i + 1))
                .map(String::as_str)
                .unwrap_or("default");
            let reported = if argv.iter().any(|a| a == "report-auto") {
                "auto"
            } else if argv.iter().any(|a| a == "report-plan") {
                "plan"
            } else {
                requested
            };
            emit(
                &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"current_permission_mode":reported,"models":[{"value":"sonnet","resolvedModel":"claude-fixture-full-id","displayName":"Fixture Sonnet","description":"Provider description"}]}}}),
            );
        } else if v["type"] == "control_request" && v["request"]["subtype"] == "interrupt" {
            emit(
                &json!({"type":"result","is_error":false,"result":"","session_id":"claude-fixture","total_cost_usd":0.0}),
            );
        } else if v["type"] == "control_request" && v["request"]["subtype"] == "set_permission_mode"
        {
            modes.push(
                v["request"]["mode"]
                    .as_str()
                    .unwrap_or("<missing>")
                    .to_owned(),
            );
            if argv.iter().any(|a| a == "hang-mode") {
                // Answer only when a "flush" prompt arrives.
                held.push(v["request_id"].clone());
            } else if argv.iter().any(|a| a == "odd-mode") {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":"plan"}}}),
                );
            } else if argv.iter().any(|a| a == "late-mode") {
                // Confirm after the driver's 10-second mode deadline.
                let reply = json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":v["request"]["mode"]}}});
                thread::spawn(move || {
                    thread::sleep(Duration::from_secs(11));
                    emit(&reply);
                });
            } else if argv.iter().any(|a| a == "reject-mode") {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"error","error":"Cannot set permission mode: fixture refusal"}}),
                );
            } else {
                emit(
                    &json!({"type":"control_response","response":{"request_id":v["request_id"],"subtype":"success","response":{"mode":v["request"]["mode"]}}}),
                );
            }
        } else if v["type"] == "user" {
            let text = v
                .pointer("/message/content/0/text")
                .and_then(Value::as_str)
                .unwrap_or("");
            if text == "flush" {
                // First held request succeeds, every later one is refused.
                for (index, id) in held.drain(..).enumerate() {
                    emit(&if index == 0 {
                        json!({"type":"control_response","response":{"request_id":id,"subtype":"success","response":{"mode":modes[0]}}})
                    } else {
                        json!({"type":"control_response","response":{"request_id":id,"subtype":"error","error":"fixture refusal"}})
                    });
                }
            }
            if text == "hold" {
                // Stay mid-turn until the driver interrupts.
                emit(
                    &json!({"type":"system","subtype":"init","session_id":"claude-fixture","model":"claude-fixture-full-id"}),
                );
                continue;
            }
            let reply = if text == "argv" {
                argv.join(" ")
            } else if text == "modes" {
                modes.join(",")
            } else {
                "Hello Claude".to_owned()
            };
            emit(
                &json!({"type":"system","subtype":"init","session_id":"claude-fixture","model":"claude-fixture-full-id"}),
            );
            emit(
                &json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":reply}}}),
            );
            emit(&json!({"type":"assistant","message":{"content":[{"type":"text","text":reply}]}}));
            emit(
                &json!({"type":"result","is_error":false,"result":reply,"session_id":"claude-fixture","total_cost_usd":0.0}),
            );
        }
    }
}
