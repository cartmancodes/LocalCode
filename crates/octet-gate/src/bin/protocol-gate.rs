//! `protocol-gate`: runs one scenario against a real vendor CLI and prints
//! the contract evidence as JSON.
use octet_gate::{
    claude_fixture_allow, claude_fixture_hook_response, claude_fixture_mcp_response,
    claude_fixture_mcp_tool_allow, codex_fixture_allow, codex_fixture_user_input, GateError,
    GateProcess,
};
use serde_json::{json, Value};
use std::{
    env,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::time::Instant;

struct Opt {
    engine: String,
    scenario: String,
    binary: PathBuf,
    workdir: Option<PathBuf>,
    output: Option<PathBuf>,
    timeout: Duration,
}
fn options() -> Result<Opt, &'static str> {
    let args: Vec<String> = env::args().skip(1).collect();
    let mut engine = None;
    let mut scenario = None;
    let mut binary = None;
    let mut workdir = None;
    let mut output = None;
    let mut seconds = 90;
    let mut i = 0;
    while i < args.len() {
        let val = args.get(i + 1).ok_or("flag requires value")?.clone();
        match args[i].as_str() {
            "--engine" => engine = Some(val),
            "--scenario" => scenario = Some(val),
            "--binary" => binary = Some(PathBuf::from(val)),
            "--workdir" => workdir = Some(PathBuf::from(val)),
            "--output" => output = Some(PathBuf::from(val)),
            "--timeout" => seconds = val.parse().map_err(|_| "invalid timeout")?,
            _ => return Err("unknown flag"),
        }
        i += 2;
    }
    let engine = engine.ok_or("missing --engine")?;
    if engine != "claude" && engine != "codex" {
        return Err("invalid engine");
    }
    let scenario = scenario.ok_or("missing --scenario")?;
    if scenario != "initialize"
        && scenario != "simple"
        && scenario != "interrupt"
        && scenario != "approval-deny"
        && scenario != "approval-interrupt"
        && scenario != "approval-allow"
        && scenario != "mcp"
        && scenario != "resume"
        && scenario != "fork"
        && scenario != "compact"
        && scenario != "hook"
        && scenario != "user-input"
        && scenario != "tool-only"
        && scenario != "failure"
    {
        return Err("unsupported scenario");
    }
    if !(1..=300).contains(&seconds) {
        return Err("timeout outside 1..300 seconds");
    }
    if engine == "codex" && (scenario == "mcp" || scenario == "hook")
        || engine == "claude"
            && (scenario == "user-input" || scenario == "tool-only" || scenario == "failure")
    {
        return Err("scenario is unsupported by selected engine");
    }
    let binary = binary.unwrap_or_else(|| PathBuf::from(&engine));
    Ok(Opt {
        engine,
        scenario,
        binary,
        workdir,
        output,
        timeout: Duration::from_secs(seconds),
    })
}
async fn version(binary: &Path) -> String {
    let mut command = tokio::process::Command::new(binary);
    command.arg("--version").kill_on_drop(true);
    tokio::time::timeout(Duration::from_secs(3), command.output())
        .await
        .ok()
        .and_then(Result::ok)
        .and_then(|out| String::from_utf8(out.stdout).ok())
        .and_then(|s| s.lines().next().map(|line| line.chars().take(80).collect()))
        .unwrap_or_else(|| "unknown".into())
}
async fn codex(gate: &mut GateProcess, scenario: &str, cwd: &Path) -> Result<(), GateError> {
    gate.send(&json!({"id":1,"method":"initialize","params":{"clientInfo":{"name":"octet_protocol_gate","title":"Octet Protocol Gate","version":"0.1.0"},"capabilities":{"experimentalApi":true}}})).await?;
    loop {
        let v = gate.receive().await?;
        if v.get("method").is_none() && v.get("id") == Some(&json!(1)) {
            if v.get("error").is_some() || !v.get("result").is_some_and(Value::is_object) {
                return Err(GateError::Protocol("initialize error"));
            }
            break;
        }
        if let Some(reply) = octet_engine::live::codex_stray_reply(&v) {
            gate.send(&reply).await?;
        }
    }
    gate.send(&json!({"method":"initialized","params":{}}))
        .await?;
    if scenario == "initialize" {
        return Ok(());
    }
    let approval = scenario.starts_with("approval-");
    let sandbox = if approval {
        "read-only"
    } else {
        "workspace-write"
    };
    let policy = if approval { "untrusted" } else { "on-request" };
    let instructions = if approval {
        "Protocol fixture: run exactly `printf READY > probe.out` in the current directory, then stop if approval is denied. Never inspect any other path."
    } else if scenario == "user-input" {
        "Protocol fixture: before answering, use request_user_input to ask whether to choose ALPHA or BETA."
    } else if scenario == "tool-only" {
        "Protocol fixture: use the terminal to read fixture.txt. Produce no assistant text before or after the tool call."
    } else {
        "Protocol fixture: do not use tools. Answer in plain text only."
    };
    gate.send(&json!({"id":2,"method":"thread/start","params":{"cwd":cwd,"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":"user","developerInstructions":instructions}})).await?;
    let (thread_id, model) = loop {
        let v = gate.receive().await?;
        if v.get("method").is_none() && v.get("id") == Some(&json!(2)) {
            if v.get("error").is_some() {
                return Err(GateError::Protocol("thread/start error"));
            }
            let thread_id = v
                .pointer("/result/thread/id")
                .and_then(Value::as_str)
                .ok_or(GateError::Protocol("missing thread id"))?
                .to_owned();
            let model = v
                .pointer("/result/model")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            break (thread_id, model);
        }
        if let Some(reply) = octet_engine::live::codex_stray_reply(&v) {
            gate.send(&reply).await?;
        }
    };
    let prompt = if approval {
        "Use the terminal to run `printf READY > probe.out` in this temporary workspace. If permission is denied, stop and say denied."
    } else if scenario == "user-input" {
        "Call request_user_input to ask me to choose ALPHA or BETA, then answer with the selected word."
    } else if scenario == "tool-only" {
        "Run `cat fixture.txt` using a terminal tool. Do not send any assistant messages."
    } else if scenario == "interrupt" {
        "Count from 1 to 1000, one number per line. Do not call tools."
    } else {
        "Reply with READY. Do not call tools."
    };
    let mut turn_params = json!({"threadId":thread_id,"input":[{"type":"text","text":prompt}]});
    if scenario == "user-input" {
        if model.is_empty() {
            return Err(GateError::Protocol("model missing for plan mode"));
        }
        turn_params["collaborationMode"] = json!({"mode":"plan","settings":{"model":model}});
    }
    if scenario == "failure" {
        turn_params["model"] = json!("octet-protocol-no-such-model");
    }
    gate.send(&json!({"id":3,"method":"turn/start","params":turn_params}))
        .await?;
    let mut turn_id = None;
    let mut interrupt_acked = false;
    let mut interrupt_sent = false;
    let mut pending_terminal = None;
    let mut ready_reply = false;
    loop {
        let mut v = gate.receive().await?;
        if v.get("method").is_none() && v.get("id") == Some(&json!(3)) {
            if v.get("error").is_some() {
                if scenario == "failure" {
                    gate.failure_kind = Some("turn_start_error");
                    return Ok(());
                }
                return Err(GateError::Protocol("turn/start error"));
            }
            turn_id = v
                .pointer("/result/turn/id")
                .and_then(Value::as_str)
                .map(str::to_owned);
        }
        if scenario == "interrupt"
            && v.get("method").and_then(Value::as_str) == Some("turn/started")
        {
            let id = turn_id
                .as_ref()
                .ok_or(GateError::Protocol("missing turn id"))?;
            if v.pointer("/params/turn/id").and_then(Value::as_str) != Some(id) {
                return Err(GateError::Protocol("turn/started id mismatch"));
            }
            interrupt_sent = true;
            gate.send(&json!({"id":4,"method":"turn/interrupt","params":{"threadId":thread_id,"turnId":id}})).await?;
        }
        if v.get("method").and_then(Value::as_str) == Some("item/completed")
            && v.pointer("/params/threadId").and_then(Value::as_str) == Some(thread_id.as_str())
            && turn_id.is_some()
            && v.pointer("/params/turnId").and_then(Value::as_str) == turn_id.as_deref()
            && v.pointer("/params/item/type").and_then(Value::as_str) == Some("agentMessage")
        {
            ready_reply = v
                .pointer("/params/item/text")
                .and_then(Value::as_str)
                .map(str::trim)
                == Some("READY");
        }
        if scenario == "tool-only"
            && !interrupt_sent
            && v.get("method").and_then(Value::as_str) == Some("item/completed")
            && v.pointer("/params/item/type").and_then(Value::as_str) == Some("commandExecution")
            && gate.agent_messages == 0
        {
            let id = turn_id
                .as_ref()
                .ok_or(GateError::Protocol("missing turn id"))?;
            interrupt_sent = true;
            gate.send(&json!({"id":4,"method":"turn/interrupt","params":{"threadId":thread_id,"turnId":id}})).await?;
        }
        if v.get("method").is_none() && v.get("id") == Some(&json!(4)) {
            if v.get("error").is_some() {
                return Err(GateError::Protocol("interrupt error"));
            }
            interrupt_acked = true;
            if let Some(terminal) = pending_terminal.take() {
                v = terminal;
            }
        }
        if v.get("method").and_then(Value::as_str) == Some("turn/completed") {
            if interrupt_sent && !interrupt_acked {
                pending_terminal = Some(v);
                continue;
            }
            if turn_id.is_none()
                || v.pointer("/params/turn/id").and_then(Value::as_str) != turn_id.as_deref()
                || v.pointer("/params/threadId").and_then(Value::as_str) != Some(thread_id.as_str())
            {
                return Err(GateError::Protocol("terminal thread or turn id mismatch"));
            }
            if scenario == "failure"
                && v.pointer("/params/turn/status").and_then(Value::as_str) != Some("failed")
            {
                return Err(GateError::Protocol("expected failed turn"));
            }
            if scenario == "failure"
                && v.pointer("/params/turn/status").and_then(Value::as_str) == Some("failed")
            {
                gate.failure_kind = Some("turn_failed");
                return Ok(());
            }
            let actual = v.pointer("/params/turn/id").and_then(Value::as_str);
            let expected_status = if scenario == "interrupt"
                || scenario == "approval-interrupt"
                || scenario == "tool-only"
            {
                "interrupted"
            } else {
                "completed"
            };
            if turn_id.as_deref() == actual
                && v.pointer("/params/turn/status").and_then(Value::as_str) == Some(expected_status)
                && (scenario != "interrupt"
                    && scenario != "approval-interrupt"
                    && scenario != "tool-only"
                    || interrupt_acked)
            {
                if scenario == "simple" && !ready_reply {
                    return Err(GateError::Protocol("unexpected simple reply"));
                }
                if approval && gate.approval_requests == 0 {
                    return Err(GateError::Protocol("no approval request observed"));
                }
                if scenario == "user-input" && gate.user_questions == 0 {
                    return Err(GateError::Protocol("no user question observed"));
                }
                if scenario == "tool-only" && gate.tool_only_turns == 0 {
                    return Err(GateError::Protocol("no tool-only turn observed"));
                }
                if scenario == "resume" || scenario == "fork" || scenario == "compact" {
                    let method = match scenario {
                        "resume" => "thread/resume",
                        "fork" => "thread/fork",
                        _ => "thread/compact/start",
                    };
                    gate.send(&json!({"id":5,"method":method,"params":{"threadId":thread_id}}))
                        .await?;
                    let mut ack = false;
                    let mut compacted = false;
                    let mut compact_turn_id: Option<String> = None;
                    let mut compact_terminal = false;
                    loop {
                        let next = gate.receive().await?;
                        if next.get("method").is_none() && next.get("id") == Some(&json!(5)) {
                            if next.get("error").is_some() {
                                return Err(GateError::Protocol("session operation error"));
                            }
                            if scenario == "resume"
                                && next.pointer("/result/thread/id").and_then(Value::as_str)
                                    != Some(&thread_id)
                            {
                                return Err(GateError::Protocol("resume id mismatch"));
                            }
                            if scenario == "fork"
                                && next
                                    .pointer("/result/thread/id")
                                    .and_then(Value::as_str)
                                    .is_none_or(|id| id == thread_id)
                            {
                                return Err(GateError::Protocol("fork id missing or unchanged"));
                            }
                            ack = true;
                        }
                        if next.get("id").is_none()
                            && next.pointer("/params/threadId").and_then(Value::as_str)
                                != Some(thread_id.as_str())
                        {
                            continue;
                        }
                        if next.get("method").and_then(Value::as_str) == Some("thread/compacted") {
                            compacted = true;
                        }
                        if next.get("method").and_then(Value::as_str) == Some("item/completed")
                            && next.pointer("/params/item/type").and_then(Value::as_str)
                                == Some("contextCompaction")
                        {
                            compacted = true;
                        }
                        if next.get("method").and_then(Value::as_str) == Some("turn/started") {
                            compact_turn_id = next
                                .pointer("/params/turn/id")
                                .and_then(Value::as_str)
                                .map(str::to_owned);
                        }
                        if next.get("method").and_then(Value::as_str) == Some("turn/completed")
                            && compact_turn_id.is_some()
                            && next.pointer("/params/threadId").and_then(Value::as_str)
                                == Some(thread_id.as_str())
                            && next.pointer("/params/turn/id").and_then(Value::as_str)
                                == compact_turn_id.as_deref()
                            && next.pointer("/params/turn/status").and_then(Value::as_str)
                                == Some("completed")
                        {
                            compact_terminal = true;
                        }
                        if ack && (scenario != "compact" || compacted && compact_terminal) {
                            return Ok(());
                        }
                        if let Some(reply) = octet_engine::live::codex_stray_reply(&next) {
                            gate.send(&reply).await?;
                        }
                    }
                }
                return Ok(());
            }
            return Err(GateError::Protocol(
                "turn completed with wrong id or status",
            ));
        }
        if scenario == "approval-interrupt"
            && v.get("method")
                .and_then(Value::as_str)
                .is_some_and(|m| m.ends_with("/requestApproval"))
        {
            if codex_fixture_allow(&v, cwd).is_none() {
                gate.send(&octet_engine::live::codex_stray_reply(&v).expect("server request"))
                    .await?;
                continue;
            }
            gate.approval_requests += 1;
            let id = turn_id
                .as_ref()
                .ok_or(GateError::Protocol("missing turn id"))?;
            interrupt_sent = true;
            gate.send(&json!({"id":4,"method":"turn/interrupt","params":{"threadId":thread_id,"turnId":id}})).await?;
            continue;
        }
        if scenario == "user-input" {
            if let Some(reply) = codex_fixture_user_input(&v) {
                gate.user_questions += 1;
                gate.send(&reply).await?;
                continue;
            }
        }
        if scenario == "approval-allow"
            && v.get("method")
                .and_then(Value::as_str)
                .is_some_and(|m| m.ends_with("/requestApproval"))
        {
            let allowed = codex_fixture_allow(&v, cwd);
            if allowed.is_some() {
                gate.approval_requests += 1;
            }
            let reply = allowed.unwrap_or_else(|| {
                octet_engine::live::codex_stray_reply(&v).expect("server request")
            });
            gate.send(&reply).await?;
            continue;
        }
        if let Some(reply) = octet_engine::live::codex_stray_reply(&v) {
            if scenario == "approval-deny"
                && codex_fixture_allow(&v, cwd).is_some()
                && v.get("method")
                    .and_then(Value::as_str)
                    .is_some_and(|m| m.ends_with("/requestApproval"))
            {
                gate.approval_requests += 1;
            }
            gate.send(&reply).await?;
        }
    }
}
async fn claude(gate: &mut GateProcess, scenario: &str, cwd: &Path) -> Result<(), GateError> {
    let hooks = if scenario == "hook" {
        json!({"PreToolUse":[{"matcher":null,"hookCallbackIds":["hook_0"]}]})
    } else {
        Value::Null
    };
    gate.send(&json!({"type":"control_request","request_id":"init-1","request":{"subtype":"initialize","hooks":hooks}})).await?;
    loop {
        let v = gate.receive().await?;
        if v.get("type").and_then(Value::as_str) == Some("control_response")
            && v.pointer("/response/request_id").and_then(Value::as_str) == Some("init-1")
        {
            if v.pointer("/response/subtype").and_then(Value::as_str) != Some("success") {
                return Err(GateError::Protocol("initialize error"));
            }
            break;
        }
        if scenario == "mcp" {
            if let Some((reply, _)) = claude_fixture_mcp_response(&v) {
                gate.send(&reply).await?;
                continue;
            }
        }
        if let Some(reply) = octet_engine::live::claude_stray_reply(&v) {
            gate.send(&reply).await?;
        }
    }
    if scenario == "initialize" {
        return Ok(());
    }
    let prompt = if scenario == "hook" {
        "Use Bash to run `cat fixture.txt` in this temporary workspace, then reply READY."
    } else if scenario == "mcp" {
        "Call the fixture_echo MCP tool exactly once, then reply with its returned word."
    } else if scenario.starts_with("approval-") {
        "Invoke Bash exactly once with command `printf READY > probe.out`. Use that relative path and no other shell commands or checks. If permission is denied, stop and say denied."
    } else if scenario == "interrupt" {
        "Count from 1 to 1000, one number per line. Do not call tools."
    } else {
        "Reply with READY. Do not call tools."
    };
    gate.send(&json!({"type":"user","message":{"role":"user","content":[{"type":"text","text":prompt}]},"parent_tool_use_id":null})).await?;
    if scenario == "interrupt" {
        gate.send(&json!({"type":"control_request","request_id":"interrupt-2","request":{"subtype":"interrupt"}})).await?;
    }
    let mut interrupt_acked = false;
    let mut compact_requested = false;
    let mut pending_result = None;
    loop {
        let mut v = gate.receive().await?;
        if (scenario == "interrupt" || scenario == "approval-interrupt")
            && v.get("type").and_then(Value::as_str) == Some("control_response")
            && v.pointer("/response/request_id").and_then(Value::as_str) == Some("interrupt-2")
        {
            if v.pointer("/response/subtype").and_then(Value::as_str) != Some("success") {
                return Err(GateError::Protocol("interrupt error"));
            }
            interrupt_acked = true;
            if let Some(result) = pending_result.take() {
                v = result;
            }
        }
        if v.get("type").and_then(Value::as_str) == Some("result") {
            if scenario == "interrupt" || scenario == "approval-interrupt" {
                if !interrupt_acked {
                    pending_result = Some(v);
                    continue;
                }
                return if interrupt_acked && (scenario == "interrupt" || gate.approval_requests > 0)
                {
                    Ok(())
                } else {
                    Err(GateError::Protocol("result before interrupt ack"))
                };
            }
            if v.get("is_error").and_then(Value::as_bool) != Some(false) {
                return Err(GateError::Protocol("result error"));
            }
            if scenario == "simple"
                && v.get("result").and_then(Value::as_str).map(str::trim) != Some("READY")
            {
                return Err(GateError::Protocol("unexpected simple reply"));
            }
            if scenario == "compact" && !compact_requested {
                gate.send(&json!({"type":"user","message":{"role":"user","content":[{"type":"text","text":"/compact"}]},"parent_tool_use_id":null})).await?;
                compact_requested = true;
                continue;
            }
            if scenario == "compact" && gate.compact_boundaries == 0 {
                return Err(GateError::Protocol("no compact boundary observed"));
            }
            if scenario.starts_with("approval-") && gate.approval_requests == 0 {
                return Err(GateError::Protocol("no approval request observed"));
            }
            if scenario == "mcp" && gate.mcp_calls == 0 {
                return Err(GateError::Protocol("no fixture MCP call observed"));
            }
            if scenario == "hook" && gate.hook_calls == 0 {
                return Err(GateError::Protocol("no hook callback observed"));
            }
            return Ok(());
        }
        if scenario == "hook" {
            if let Some(reply) = claude_fixture_hook_response(&v) {
                gate.hook_calls += 1;
                gate.send(&reply).await?;
                continue;
            }
        }
        if scenario == "mcp" {
            if let Some((reply, called)) = claude_fixture_mcp_response(&v) {
                if called {
                    gate.mcp_calls += 1;
                }
                gate.send(&reply).await?;
                continue;
            }
        }
        if let Some(reply) = octet_engine::live::claude_stray_reply(&v) {
            if v.pointer("/request/subtype").and_then(Value::as_str) == Some("can_use_tool") {
                let fixture_write_request = claude_fixture_allow(&v, cwd);
                if scenario.starts_with("approval-") && fixture_write_request.is_some() {
                    gate.approval_requests += 1;
                }
                if scenario == "approval-interrupt" {
                    if fixture_write_request.is_none() {
                        gate.send(&reply).await?;
                        continue;
                    }
                    gate.send(&json!({"type":"control_request","request_id":"interrupt-2","request":{"subtype":"interrupt"}})).await?;
                    continue;
                }
                if scenario == "approval-allow" {
                    let reply = fixture_write_request.unwrap_or(reply);
                    gate.send(&reply).await?;
                    continue;
                }
                if scenario == "mcp" {
                    if claude_fixture_mcp_tool_allow(&v).is_some() {
                        gate.approval_requests += 1;
                    }
                    let reply = claude_fixture_mcp_tool_allow(&v).unwrap_or(reply);
                    gate.send(&reply).await?;
                    continue;
                }
            }
            gate.send(&reply).await?;
        }
    }
}
#[tokio::main]
async fn main() {
    let opt = match options() {
        Ok(v) => v,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
    };
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the clock is after 1970")
        .as_nanos();
    let temporary = opt.workdir.is_none();
    let cwd = opt.workdir.clone().unwrap_or_else(|| {
        env::temp_dir().join(format!("octet-protocol-{}-{nonce}", std::process::id()))
    });
    if opt.workdir.is_some() && cwd.exists() {
        eprintln!("--workdir must be a new fixture directory");
        std::process::exit(2);
    }
    if fs::create_dir_all(&cwd).is_err() {
        eprintln!("cannot create fixture workspace");
        std::process::exit(2);
    }
    if fs::write(cwd.join("fixture.txt"), b"Octet protocol fixture\n").is_err() {
        eprintln!("cannot write fixture");
        std::process::exit(2);
    }
    let args: Vec<OsString> = if opt.engine == "claude" {
        let mut args: Vec<OsString> = [
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
            "--permission-prompt-tool",
            "stdio",
            "--permission-mode",
            "default",
            "--setting-sources=",
            "--strict-mcp-config",
            "--tools",
            if opt.scenario.starts_with("approval-") {
                "Bash"
            } else if opt.scenario == "hook" {
                "Bash,Read"
            } else if opt.scenario == "mcp" {
                "mcp__fixture__fixture_echo"
            } else {
                ""
            },
        ]
        .into_iter()
        .map(OsString::from)
        .collect();
        if opt.scenario == "mcp" {
            args.extend([
                OsString::from("--mcp-config"),
                OsString::from(r#"{"mcpServers":{"fixture":{"type":"sdk","name":"fixture"}}}"#),
            ]);
        }
        args
    } else {
        ["app-server"].into_iter().map(OsString::from).collect()
    };
    let started = Instant::now();
    let deadline = started + opt.timeout;
    let mut events = 0;
    let mut approvals = 0;
    let mut mcp_calls = 0;
    let mut hook_calls = 0;
    let mut compact_boundaries = 0;
    let mut agent_messages = 0;
    let mut late_usage_turns = 0;
    let mut tool_only_turns = 0;
    let mut user_questions = 0;
    let mut failure_kind = None;
    let mut cleaned = false;
    let mut status =
        match GateProcess::spawn(opt.binary.clone(), args.clone(), cwd.clone(), deadline) {
            Err(error) => format!("failed: {error}"),
            Ok(mut gate) => {
                let result = if opt.engine == "claude" {
                    claude(&mut gate, &opt.scenario, &cwd).await
                } else {
                    codex(&mut gate, &opt.scenario, &cwd).await
                };
                events = gate.event_count;
                approvals = gate.approval_requests;
                mcp_calls = gate.mcp_calls;
                hook_calls = gate.hook_calls;
                compact_boundaries = gate.compact_boundaries;
                agent_messages = gate.agent_messages;
                late_usage_turns = gate.late_usage_turns;
                tool_only_turns = gate.tool_only_turns;
                user_questions = gate.user_questions;
                failure_kind = gate.failure_kind;
                let first_session = gate.session_id.clone();
                let report = gate.shutdown().await;
                cleaned = report.reaped && report.descendants_stopped;
                let mut outcome = match result {
                    Ok(()) if cleaned => "passed".to_owned(),
                    Ok(()) => "failed: child cleanup".to_owned(),
                    Err(error) => format!("failed: {error}"),
                };
                if outcome == "passed"
                    && opt.engine == "claude"
                    && (opt.scenario == "resume" || opt.scenario == "fork")
                {
                    if let Some(id) = first_session {
                        let mut resumed_args = args.clone();
                        resumed_args.push(OsString::from(format!("--resume={id}")));
                        if opt.scenario == "fork" {
                            resumed_args.push(OsString::from("--fork-session"));
                        }
                        match GateProcess::spawn(
                            opt.binary.clone(),
                            resumed_args,
                            cwd.clone(),
                            deadline,
                        ) {
                            Err(error) => outcome = format!("failed: {error}"),
                            Ok(mut second) => {
                                let second_result = claude(&mut second, "simple", &cwd).await;
                                events += second.event_count;
                                approvals += second.approval_requests;
                                compact_boundaries += second.compact_boundaries;
                                let resumed_id = second.session_id.clone();
                                let second_report = second.shutdown().await;
                                cleaned &=
                                    second_report.reaped && second_report.descendants_stopped;
                                outcome = match second_result {
                                    Ok(()) if !cleaned => "failed: resumed child cleanup".into(),
                                    Ok(())
                                        if opt.scenario == "resume"
                                            && resumed_id.as_deref() == Some(&id) =>
                                    {
                                        "passed".into()
                                    }
                                    Ok(())
                                        if opt.scenario == "fork"
                                            && resumed_id
                                                .as_deref()
                                                .is_some_and(|new_id| new_id != id) =>
                                    {
                                        "passed".into()
                                    }
                                    Ok(()) => "failed: session id mismatch".into(),
                                    Err(error) => format!("failed: {error}"),
                                };
                            }
                        }
                    } else {
                        outcome = "failed: first session id missing".into();
                    }
                }
                outcome
            }
        };
    let fixture_write = fs::File::open(cwd.join("probe.out"))
        .and_then(|file| {
            use std::io::Read;
            let mut bytes = Vec::new();
            file.take(6).read_to_end(&mut bytes)?;
            Ok(bytes)
        })
        .ok()
        .is_some_and(|bytes| bytes == b"READY");
    if status == "passed" && opt.scenario == "approval-allow" && !fixture_write {
        status = "failed: approved fixture write not observed".into();
    }
    if status == "passed"
        && (opt.scenario == "approval-deny" || opt.scenario == "approval-interrupt")
        && cwd.join("probe.out").exists()
    {
        status = "failed: denied or interrupted fixture write occurred".into();
    }
    let evidence = json!({"engine":opt.engine,"scenario":opt.scenario,"version":version(&opt.binary).await,"host":env::consts::OS,"arch":env::consts::ARCH,"status":status,"events":events,"approval_requests":approvals,"mcp_calls":mcp_calls,"hook_calls":hook_calls,"compact_boundaries":compact_boundaries,"agent_messages":agent_messages,"late_usage_turns":late_usage_turns,"tool_only_turns":tool_only_turns,"user_questions":user_questions,"failure_kind":failure_kind,"fixture_write":fixture_write,"child_cleanup":cleaned,"elapsed_ms":started.elapsed().as_millis()});
    let serialized =
        serde_json::to_string_pretty(&evidence).expect("evidence is plain JSON values");
    if let Some(path) = opt.output {
        if fs::write(path, &serialized).is_err() {
            eprintln!("cannot write evidence");
            std::process::exit(2);
        }
    }
    println!("{serialized}");
    if temporary {
        let _ = fs::remove_dir_all(&cwd);
    }
    if !status.starts_with("passed") {
        std::process::exit(1);
    }
}
