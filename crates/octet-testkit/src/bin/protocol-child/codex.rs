//! The fake Codex app server: `thread/*`, `turn/*` and `model/list`, with
//! the scripts in `scenario` chosen by the turn's text or the thread's model.
use crate::wire::{codex_delta, codex_turn_completed, emit, goal_reply};
use octet_testkit::scenario;
use serde_json::{Value, json};
use std::{
    io::{self, BufRead},
    process, thread,
    time::Duration,
};

/// One Codex session's state.
#[derive(Default)]
struct CodexFixture {
    /// Turns started so far, naming each `turn-N`.
    turn: u64,
    /// The running (or last) turn.
    active: String,
    /// `DIE_ON_INTERRUPT`: exit when asked to stop the turn, as a vendor
    /// that ignores the interrupt is eventually stopped.
    dies_on_interrupt: bool,
    /// `IGNORE_INTERRUPT`: acknowledge interrupts but never end the turn.
    ignores_interrupt: bool,
    /// What the last `thread/*` request asked for, for `PARAMS`.
    thread_params: Value,
}

pub(crate) fn run() {
    let mut codex = CodexFixture::default();
    for line in io::stdin().lock().lines() {
        let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
        match v["method"].as_str() {
            Some("model/list") => model_list(&v),
            Some("initialize") => emit(&json!({"id":v["id"],"result":{}})),
            Some("thread/start" | "thread/resume" | "thread/fork") => codex.open_thread(&v),
            Some("thread/compact/start") => codex.compact(&v),
            Some("turn/start") => codex.start_turn(&v),
            Some("turn/steer") => codex.steer(&v),
            Some("turn/interrupt") => codex.interrupt(&v),
            None => codex.answer(&v),
            _ => {}
        }
    }
}

/// Two catalog pages: `picker-fixture`, then `picker-other`.
fn model_list(v: &Value) {
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

impl CodexFixture {
    /// Opens, resumes or forks the thread, echoing the policy as Codex does.
    fn open_thread(&mut self, v: &Value) {
        let p = &v["params"];
        if p["model"] == scenario::REFUSE_THREAD {
            emit(
                &json!({"id":v["id"],"error":{"code":-32600,"message":"no rollout found for thread id fixture"}}),
            );
            return;
        }
        self.thread_params = p.clone();
        self.thread_params["method"] = v["method"].clone();
        // "report-stricter" simulates a managed requirement that overrides
        // what the client asked for.
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
        // A fork opens a new thread; the turns that follow still run on the
        // fixture thread, which is all the fork tests need.
        let thread = if v["method"] == "thread/fork" {
            "forked-thread"
        } else {
            "fixture-thread"
        };
        emit(
            &json!({"id":v["id"],"result":{"thread":{"id":thread},"model":"fixture","sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer}}),
        );
    }

    fn next_turn(&mut self) {
        self.turn += 1;
        self.active = format!("turn-{}", self.turn);
    }

    /// Codex runs compaction as a turn on the thread.
    fn compact(&mut self, v: &Value) {
        self.next_turn();
        let active = &self.active;
        emit(&json!({"id":v["id"],"result":{}}));
        emit(
            &json!({"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":active}}}),
        );
        emit(
            &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"compact","type":"contextCompaction"}}}),
        );
        emit(&codex_turn_completed(active, "completed"));
    }

    fn start_turn(&mut self, v: &Value) {
        self.next_turn();
        let text = v
            .pointer("/params/input/0/text")
            .and_then(Value::as_str)
            .unwrap_or("");
        if text == scenario::REFUSE_START {
            // Refuse the turn after a pause, so Octet can hold a steer.
            thread::sleep(Duration::from_millis(400));
            emit(&json!({"id":v["id"],"error":{"code":-32600,"message":"turn refused"}}));
            return;
        }
        emit(&json!({"id":v["id"],"result":{"turn":{"id":self.active}}}));
        if text == scenario::LATE_START {
            // Name the turn late, so Octet holds steers and cancels.
            thread::sleep(Duration::from_millis(400));
        }
        emit(
            &json!({"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":self.active}}}),
        );
        self.turn_script(text, v);
    }

    /// What the turn does, chosen by its text.
    fn turn_script(&mut self, text: &str, v: &Value) {
        let active = self.active.clone();
        let done = || emit(&codex_turn_completed(&active, "completed"));
        match text {
            scenario::HOLD_MARKED => {
                emit(&codex_delta("fixture-thread", &active, "marker", "named"))
            }
            scenario::HOLD | scenario::LATE_START => {}
            _ if scenario::is_goal_prompt(text, scenario::GOAL_HOLD) => {}
            scenario::DIE_ON_INTERRUPT => self.dies_on_interrupt = true,
            scenario::IGNORE_INTERRUPT => self.ignores_interrupt = true,
            scenario::ODD_STRINGS => {
                let method = format!("x/{}", "y".repeat(100_000));
                emit(&json!({"id":"srv-1","method":method,"params":{}}));
                emit(&codex_turn_completed(&active, &"z".repeat(100_000)));
            }
            scenario::APPROVALS_9 => {
                for n in 1..=9 {
                    emit(
                        &json!({"id":format!("{}{n}", scenario::CAP_PREFIX),"method":"item/commandExecution/requestApproval","params":{"threadId":"fixture-thread","turnId":active,"command":"echo fixture"}}),
                    );
                }
            }
            scenario::APPROVAL => emit(
                &json!({"id":"permission","method":"item/commandExecution/requestApproval","params":{"threadId":"fixture-thread","turnId":active,"command":"echo fixture"}}),
            ),
            scenario::BIGTOOL => {
                // One completed command whose output is far larger than the event queue.
                let output = "x".repeat(6 * 1024 * 1024);
                emit(
                    &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"cmd","type":"commandExecution","command":"cat big","status":"completed","exitCode":0,"aggregatedOutput":output}}}),
                );
                done();
            }
            scenario::CHATTER => {
                // The turn goes silent while another thread keeps talking.
                for _ in 0..8 {
                    thread::sleep(Duration::from_millis(300));
                    emit(&codex_delta("other-thread", "other", "x", "."));
                }
            }
            scenario::FAIL => fail(&active),
            _ if scenario::is_goal_prompt(text, scenario::GOAL_FAIL) => fail(&active),
            scenario::FAIL_NESTED => emit(
                &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"failed","error":{"codexErrorInfo":"other","message":"{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The model is not supported.\"}}"}}}}),
            ),
            scenario::SLOW => {
                // Stays active longer than a short idle limit, never silent for long.
                for _ in 0..8 {
                    thread::sleep(Duration::from_millis(300));
                    emit(&codex_delta("fixture-thread", &active, "slow", "."));
                }
                done();
            }
            scenario::PARAMS => {
                let echo = json!({"thread":self.thread_params,"turn":v["params"]}).to_string();
                emit(&codex_delta("fixture-thread", &active, "params", &echo));
                done();
            }
            _ => reply(&active, text, v),
        }
    }

    /// Steering adds to the running turn; a stale turn ID, or the text
    /// "reject", is refused.
    fn steer(&self, v: &Value) {
        let text = v
            .pointer("/params/input/0/text")
            .and_then(Value::as_str)
            .unwrap_or("");
        if v["params"]["expectedTurnId"] == self.active.as_str() && text != "reject" {
            emit(&codex_delta(
                "fixture-thread",
                &self.active,
                "steer",
                &format!("steered:{text}"),
            ));
            emit(&json!({"id":v["id"],"result":{"turnId":self.active}}));
        } else {
            emit(
                &json!({"id":v["id"],"error":{"code":-32600,"message":"no active turn to steer"}}),
            );
        }
    }

    fn interrupt(&self, v: &Value) {
        if self.dies_on_interrupt {
            process::exit(3);
        }
        if !self.ignores_interrupt {
            emit(&codex_turn_completed(&self.active, "interrupted"));
        }
        emit(&json!({"id":v["id"],"result":{}}));
    }

    /// Octet's answer to one of our approval requests, echoed as text.
    fn answer(&self, v: &Value) {
        let decision = v["result"]["decision"].as_str();
        match v["id"].as_str() {
            // Each answer to an `APPROVALS_9` request, as "answered cap-N:decision".
            Some(id) if id.starts_with(scenario::CAP_PREFIX) => {
                let reply = format!("answered {id}:{}", decision.unwrap_or("?"));
                emit(&codex_delta("fixture-thread", &self.active, "cap", &reply));
            }
            Some("permission") => {
                let reply = decision.unwrap_or("bad response");
                emit(&codex_delta(
                    "fixture-thread",
                    &self.active,
                    "answer",
                    reply,
                ));
                emit(&codex_turn_completed(&self.active, "completed"));
            }
            _ => {}
        }
    }
}

/// A failed turn: the usage limit.
fn fail(active: &str) {
    emit(
        &json!({"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":active,"status":"failed","error":{"additionalDetails":null,"codexErrorInfo":"usageLimitExceeded","message":"You've hit your usage limit."}}}}),
    );
}

/// A prompt with no script: chatter for another thread (which must not
/// show), then the reply, its completion, usage and the turn's end.
fn reply(active: &str, text: &str, v: &Value) {
    emit(&codex_delta(
        "other-thread",
        active,
        "wrong",
        "MUST NOT DISPLAY",
    ));
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
        goal_reply(text).unwrap_or(scenario::CODEX_REPLY).to_owned()
    } else {
        format!("images:{}", images.join(","))
    };
    emit(&codex_delta("fixture-thread", active, "msg", &reply));
    emit(
        &json!({"method":"item/completed","params":{"threadId":"fixture-thread","turnId":active,"item":{"id":"msg","type":"agentMessage","text":reply}}}),
    );
    emit(
        &json!({"method":"thread/tokenUsage/updated","params":{"threadId":"fixture-thread","tokenUsage":{"total":{"totalTokens":42}}}}),
    );
    emit(&codex_turn_completed(active, "completed"));
}
