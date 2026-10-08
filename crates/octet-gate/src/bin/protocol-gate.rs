//! `protocol-gate`: runs one scenario against a real vendor CLI and prints
//! the contract evidence as JSON. It launches the vendor exactly as Octet
//! does (`octet_engine::live::launch_args`), adding only what a scenario
//! needs, so it checks the contract Octet ships.
#![forbid(unsafe_code)]
use octet_engine::live::{Config, Engine, claude_stray_reply, codex_stray_reply, launch_args};
use octet_gate::{
    Evidence, GateError, GateProcess, claude_fixture_allow, claude_fixture_hook_response,
    claude_fixture_mcp_response, claude_fixture_mcp_tool_allow, codex_fixture_allow,
    codex_fixture_user_input,
};
use serde_json::{Value, json};
use std::{
    env,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::time::Instant;

/// Every scenario the gate runs.
const SCENARIOS: [&str; 14] = [
    "initialize",
    "simple",
    "interrupt",
    "approval-deny",
    "approval-interrupt",
    "approval-allow",
    "mcp",
    "resume",
    "fork",
    "compact",
    "hook",
    "user-input",
    "tool-only",
    "failure",
];

struct Opt {
    engine: Engine,
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
    let name = engine.ok_or("missing --engine")?;
    let engine = Engine::parse(&name)
        .filter(|engine| engine.is_vendor())
        .ok_or("invalid engine")?;
    let scenario = scenario.ok_or("missing --scenario")?;
    if !SCENARIOS.contains(&scenario.as_str()) {
        return Err("unsupported scenario");
    }
    if !(1..=300).contains(&seconds) {
        return Err("timeout outside 1..300 seconds");
    }
    let codex_only = ["user-input", "tool-only", "failure"];
    let claude_only = ["mcp", "hook"];
    if engine == Engine::CODEX && claude_only.contains(&scenario.as_str())
        || engine == Engine::CLAUDE && codex_only.contains(&scenario.as_str())
    {
        return Err("scenario is unsupported by selected engine");
    }
    let binary = binary.unwrap_or_else(|| PathBuf::from(&name));
    Ok(Opt {
        engine,
        scenario,
        binary,
        workdir,
        output,
        timeout: Duration::from_secs(seconds),
    })
}

/// The scenario's workspace. A temporary one is removed when this drops, on
/// every exit path; a `--workdir` is kept for inspection.
struct Workspace {
    path: PathBuf,
    temporary: bool,
}
impl Workspace {
    fn create(workdir: Option<PathBuf>) -> Result<Self, &'static str> {
        let temporary = workdir.is_none();
        let path = workdir.unwrap_or_else(|| {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |since| since.as_nanos());
            env::temp_dir().join(format!("octet-protocol-{}-{nonce}", std::process::id()))
        });
        if !temporary && path.exists() {
            return Err("--workdir must be a new fixture directory");
        }
        fs::create_dir_all(&path).map_err(|_| "cannot create fixture workspace")?;
        let workspace = Self { path, temporary };
        fs::write(
            workspace.path.join("fixture.txt"),
            b"Octet protocol fixture\n",
        )
        .map_err(|_| "cannot write fixture")?;
        Ok(workspace)
    }
}
impl Drop for Workspace {
    fn drop(&mut self) {
        if self.temporary {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
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

/// Octet's own arguments for this engine, plus what the scenario needs:
/// Claude's tool list and, for `mcp`, the fixture server.
fn vendor_args(opt: &Opt, cwd: &Path, resume: Option<&str>) -> Vec<OsString> {
    let mut config = Config::new(opt.engine, &opt.binary, cwd);
    if let Some(session) = resume {
        config.resume = Some(session.to_owned());
        config.fork = opt.scenario == "fork";
    }
    let mut args = launch_args(&config);
    if opt.engine == Engine::CLAUDE {
        let tools = match opt.scenario.as_str() {
            s if s.starts_with("approval-") => "Bash",
            "hook" => "Bash,Read",
            "mcp" => "mcp__fixture__fixture_echo",
            _ => "",
        };
        args.extend(["--tools".into(), tools.into()]);
        if opt.scenario == "mcp" {
            args.extend([
                OsString::from("--mcp-config"),
                OsString::from(r#"{"mcpServers":{"fixture":{"type":"sdk","name":"fixture"}}}"#),
            ]);
        }
    }
    args
}

/// Answers a request the scenario does not handle, as Octet would.
async fn codex_default(gate: &GateProcess, v: &Value) -> Result<(), GateError> {
    if let Some(reply) = codex_stray_reply(v) {
        gate.send(&reply).await?;
    }
    Ok(())
}
fn is_reply_to(v: &Value, id: u64) -> bool {
    v.get("method").is_none() && v.get("id") == Some(&json!(id))
}
fn method(v: &Value) -> Option<&str> {
    v.get("method").and_then(Value::as_str)
}
fn is_approval_request(v: &Value) -> bool {
    method(v).is_some_and(|m| m.ends_with("/requestApproval"))
}

async fn codex(gate: &mut GateProcess, scenario: &str, cwd: &Path) -> Result<(), GateError> {
    codex_handshake(gate).await?;
    if scenario == "initialize" {
        return Ok(());
    }
    let (thread_id, model) = codex_open_thread(gate, scenario, cwd).await?;
    let ended = codex_run_turn(gate, scenario, cwd, &thread_id, &model).await?;
    if ended && matches!(scenario, "resume" | "fork" | "compact") {
        codex_session_op(gate, scenario, &thread_id).await?;
    }
    Ok(())
}

async fn codex_handshake(gate: &mut GateProcess) -> Result<(), GateError> {
    gate.send(&json!({"id":1,"method":"initialize","params":{"clientInfo":{"name":"octet_protocol_gate","title":"Octet Protocol Gate","version":"0.1.0"},"capabilities":{"experimentalApi":true}}})).await?;
    loop {
        let v = gate.receive().await?;
        if is_reply_to(&v, 1) {
            if v.get("error").is_some() || !v.get("result").is_some_and(Value::is_object) {
                return Err(GateError::Protocol("initialize error"));
            }
            break;
        }
        codex_default(gate, &v).await?;
    }
    gate.send(&json!({"method":"initialized","params":{}}))
        .await
}

/// Starts the thread; its ID and the model Codex chose.
async fn codex_open_thread(
    gate: &mut GateProcess,
    scenario: &str,
    cwd: &Path,
) -> Result<(String, String), GateError> {
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
    loop {
        let v = gate.receive().await?;
        if is_reply_to(&v, 2) {
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
            return Ok((thread_id, model));
        }
        codex_default(gate, &v).await?;
    }
}

fn codex_prompt(scenario: &str) -> &'static str {
    match scenario {
        s if s.starts_with("approval-") => {
            "Use the terminal to run `printf READY > probe.out` in this temporary workspace. If permission is denied, stop and say denied."
        }
        "user-input" => {
            "Call request_user_input to ask me to choose ALPHA or BETA, then answer with the selected word."
        }
        "tool-only" => {
            "Run `cat fixture.txt` using a terminal tool. Do not send any assistant messages."
        }
        "interrupt" => "Count from 1 to 1000, one number per line. Do not call tools.",
        _ => "Reply with READY. Do not call tools.",
    }
}

/// The state of the one turn a Codex scenario runs.
#[derive(Default)]
struct CodexTurn {
    id: Option<String>,
    interrupt_sent: bool,
    interrupt_acked: bool,
    /// A terminal event that arrived before the interrupt's acknowledgement.
    pending_terminal: Option<Value>,
    ready_reply: bool,
}

impl CodexTurn {
    async fn interrupt(&mut self, gate: &GateProcess, thread_id: &str) -> Result<(), GateError> {
        let id = self
            .id
            .as_ref()
            .ok_or(GateError::Protocol("missing turn id"))?;
        self.interrupt_sent = true;
        gate.send(
            &json!({"id":4,"method":"turn/interrupt","params":{"threadId":thread_id,"turnId":id}}),
        )
        .await
    }
}

/// Runs the scenario's turn. True when it ended as expected and a session
/// operation may follow; false when a `failure` scenario ended early.
async fn codex_run_turn(
    gate: &mut GateProcess,
    scenario: &str,
    cwd: &Path,
    thread_id: &str,
    model: &str,
) -> Result<bool, GateError> {
    let mut params =
        json!({"threadId":thread_id,"input":[{"type":"text","text":codex_prompt(scenario)}]});
    if scenario == "user-input" {
        if model.is_empty() {
            return Err(GateError::Protocol("model missing for plan mode"));
        }
        params["collaborationMode"] = json!({"mode":"plan","settings":{"model":model}});
    }
    if scenario == "failure" {
        params["model"] = json!("octet-protocol-no-such-model");
    }
    gate.send(&json!({"id":3,"method":"turn/start","params":params}))
        .await?;
    let mut turn = CodexTurn::default();
    loop {
        let mut v = gate.receive().await?;
        if is_reply_to(&v, 3) {
            if v.get("error").is_some() {
                if scenario == "failure" {
                    gate.record_failure("turn_start_error");
                    return Ok(false);
                }
                return Err(GateError::Protocol("turn/start error"));
            }
            turn.id = v
                .pointer("/result/turn/id")
                .and_then(Value::as_str)
                .map(str::to_owned);
        }
        if scenario == "interrupt" && method(&v) == Some("turn/started") {
            let id = turn
                .id
                .as_deref()
                .ok_or(GateError::Protocol("missing turn id"))?;
            if v.pointer("/params/turn/id").and_then(Value::as_str) != Some(id) {
                return Err(GateError::Protocol("turn/started id mismatch"));
            }
            turn.interrupt(gate, thread_id).await?;
        }
        let this_turn = v.pointer("/params/threadId").and_then(Value::as_str) == Some(thread_id)
            && turn.id.is_some()
            && v.pointer("/params/turnId").and_then(Value::as_str) == turn.id.as_deref();
        let item = v.pointer("/params/item/type").and_then(Value::as_str);
        if method(&v) == Some("item/completed") && this_turn && item == Some("agentMessage") {
            turn.ready_reply = v
                .pointer("/params/item/text")
                .and_then(Value::as_str)
                .map(str::trim)
                == Some("READY");
        }
        if scenario == "tool-only"
            && !turn.interrupt_sent
            && method(&v) == Some("item/completed")
            && item == Some("commandExecution")
            && gate.evidence().agent_messages == 0
        {
            turn.interrupt(gate, thread_id).await?;
        }
        if is_reply_to(&v, 4) {
            if v.get("error").is_some() {
                return Err(GateError::Protocol("interrupt error"));
            }
            turn.interrupt_acked = true;
            if let Some(terminal) = turn.pending_terminal.take() {
                v = terminal;
            }
        }
        if method(&v) == Some("turn/completed") {
            if turn.interrupt_sent && !turn.interrupt_acked {
                turn.pending_terminal = Some(v);
                continue;
            }
            return codex_turn_ended(gate, scenario, thread_id, &turn, &v);
        }
        codex_turn_request(gate, scenario, cwd, thread_id, &mut turn, &v).await?;
    }
}

/// Checks the turn's terminal event against what the scenario expects.
fn codex_turn_ended(
    gate: &mut GateProcess,
    scenario: &str,
    thread_id: &str,
    turn: &CodexTurn,
    v: &Value,
) -> Result<bool, GateError> {
    let actual = v.pointer("/params/turn/id").and_then(Value::as_str);
    let status = v.pointer("/params/turn/status").and_then(Value::as_str);
    if turn.id.is_none()
        || actual != turn.id.as_deref()
        || v.pointer("/params/threadId").and_then(Value::as_str) != Some(thread_id)
    {
        return Err(GateError::Protocol("terminal thread or turn id mismatch"));
    }
    if scenario == "failure" {
        if status != Some("failed") {
            return Err(GateError::Protocol("expected failed turn"));
        }
        gate.record_failure("turn_failed");
        return Ok(false);
    }
    let interrupting = matches!(scenario, "interrupt" | "approval-interrupt" | "tool-only");
    let expected = if interrupting {
        "interrupted"
    } else {
        "completed"
    };
    if status != Some(expected) || interrupting && !turn.interrupt_acked {
        return Err(GateError::Protocol(
            "turn completed with wrong id or status",
        ));
    }
    let evidence = gate.evidence();
    if scenario == "simple" && !turn.ready_reply {
        return Err(GateError::Protocol("unexpected simple reply"));
    }
    if scenario.starts_with("approval-") && evidence.approval_requests == 0 {
        return Err(GateError::Protocol("no approval request observed"));
    }
    if scenario == "user-input" && evidence.user_questions == 0 {
        return Err(GateError::Protocol("no user question observed"));
    }
    if scenario == "tool-only" && evidence.tool_only_turns == 0 {
        return Err(GateError::Protocol("no tool-only turn observed"));
    }
    Ok(true)
}

/// Answers a server request during the turn as the scenario says.
async fn codex_turn_request(
    gate: &mut GateProcess,
    scenario: &str,
    cwd: &Path,
    thread_id: &str,
    turn: &mut CodexTurn,
    v: &Value,
) -> Result<(), GateError> {
    let stray = || codex_stray_reply(v).ok_or(GateError::Protocol("approval request without id"));
    if scenario == "approval-interrupt" && is_approval_request(v) {
        if codex_fixture_allow(v, cwd).is_none() {
            return gate.send(&stray()?).await;
        }
        gate.record_approval();
        return turn.interrupt(gate, thread_id).await;
    }
    if scenario == "user-input"
        && let Some(reply) = codex_fixture_user_input(v)
    {
        gate.record_user_question();
        return gate.send(&reply).await;
    }
    if scenario == "approval-allow" && is_approval_request(v) {
        let allowed = codex_fixture_allow(v, cwd);
        if allowed.is_some() {
            gate.record_approval();
        }
        let reply = match allowed {
            Some(reply) => reply,
            None => stray()?,
        };
        return gate.send(&reply).await;
    }
    if let Some(reply) = codex_stray_reply(v) {
        if scenario == "approval-deny"
            && codex_fixture_allow(v, cwd).is_some()
            && is_approval_request(v)
        {
            gate.record_approval();
        }
        gate.send(&reply).await?;
    }
    Ok(())
}

/// After the turn: resume, fork or compact the thread and check the result.
async fn codex_session_op(
    gate: &mut GateProcess,
    scenario: &str,
    thread_id: &str,
) -> Result<(), GateError> {
    let op = match scenario {
        "resume" => "thread/resume",
        "fork" => "thread/fork",
        _ => "thread/compact/start",
    };
    gate.send(&json!({"id":5,"method":op,"params":{"threadId":thread_id}}))
        .await?;
    let mut ack = false;
    let mut compacted = false;
    let mut compact_turn_id: Option<String> = None;
    let mut compact_terminal = false;
    loop {
        let next = gate.receive().await?;
        if is_reply_to(&next, 5) {
            if next.get("error").is_some() {
                return Err(GateError::Protocol("session operation error"));
            }
            let id = next.pointer("/result/thread/id").and_then(Value::as_str);
            if scenario == "resume" && id != Some(thread_id) {
                return Err(GateError::Protocol("resume id mismatch"));
            }
            if scenario == "fork" && id.is_none_or(|id| id == thread_id) {
                return Err(GateError::Protocol("fork id missing or unchanged"));
            }
            ack = true;
        }
        if next.get("id").is_none()
            && next.pointer("/params/threadId").and_then(Value::as_str) != Some(thread_id)
        {
            continue;
        }
        let item = next.pointer("/params/item/type").and_then(Value::as_str);
        match method(&next) {
            Some("thread/compacted") => compacted = true,
            Some("item/completed") if item == Some("contextCompaction") => compacted = true,
            Some("turn/started") => {
                compact_turn_id = next
                    .pointer("/params/turn/id")
                    .and_then(Value::as_str)
                    .map(str::to_owned);
            }
            Some("turn/completed")
                if compact_turn_id.is_some()
                    && next.pointer("/params/turn/id").and_then(Value::as_str)
                        == compact_turn_id.as_deref()
                    && next.pointer("/params/turn/status").and_then(Value::as_str)
                        == Some("completed") =>
            {
                compact_terminal = true;
            }
            _ => {}
        }
        if ack && (scenario != "compact" || compacted && compact_terminal) {
            return Ok(());
        }
        codex_default(gate, &next).await?;
    }
}

async fn claude(gate: &mut GateProcess, scenario: &str, cwd: &Path) -> Result<(), GateError> {
    claude_handshake(gate, scenario).await?;
    if scenario == "initialize" {
        return Ok(());
    }
    gate.send(&json!({"type":"user","message":{"role":"user","content":[{"type":"text","text":claude_prompt(scenario)}]},"parent_tool_use_id":null})).await?;
    if scenario == "interrupt" {
        gate.send(&json!({"type":"control_request","request_id":"interrupt-2","request":{"subtype":"interrupt"}})).await?;
    }
    claude_turn(gate, scenario, cwd).await
}

async fn claude_handshake(gate: &mut GateProcess, scenario: &str) -> Result<(), GateError> {
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
            return Ok(());
        }
        if scenario == "mcp"
            && let Some((reply, _)) = claude_fixture_mcp_response(&v)
        {
            gate.send(&reply).await?;
            continue;
        }
        if let Some(reply) = claude_stray_reply(&v) {
            gate.send(&reply).await?;
        }
    }
}

fn claude_prompt(scenario: &str) -> &'static str {
    match scenario {
        "hook" => {
            "Use Bash to run `cat fixture.txt` in this temporary workspace, then reply READY."
        }
        "mcp" => "Call the fixture_echo MCP tool exactly once, then reply with its returned word.",
        s if s.starts_with("approval-") => {
            "Invoke Bash exactly once with command `printf READY > probe.out`. Use that relative path and no other shell commands or checks. If permission is denied, stop and say denied."
        }
        "interrupt" => "Count from 1 to 1000, one number per line. Do not call tools.",
        _ => "Reply with READY. Do not call tools.",
    }
}

async fn claude_turn(gate: &mut GateProcess, scenario: &str, cwd: &Path) -> Result<(), GateError> {
    let interrupting = scenario == "interrupt" || scenario == "approval-interrupt";
    let mut interrupt_acked = false;
    let mut compact_requested = false;
    let mut pending_result = None;
    loop {
        let mut v = gate.receive().await?;
        if interrupting
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
            if interrupting {
                if !interrupt_acked {
                    pending_result = Some(v);
                    continue;
                }
                return if scenario == "interrupt" || gate.evidence().approval_requests > 0 {
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
            return claude_result_checks(gate.evidence(), scenario);
        }
        if scenario == "hook"
            && let Some(reply) = claude_fixture_hook_response(&v)
        {
            gate.record_hook_call();
            gate.send(&reply).await?;
            continue;
        }
        if scenario == "mcp"
            && let Some((reply, called)) = claude_fixture_mcp_response(&v)
        {
            if called {
                gate.record_mcp_call();
            }
            gate.send(&reply).await?;
            continue;
        }
        if let Some(reply) = claude_stray_reply(&v) {
            claude_request(gate, scenario, cwd, &v, reply).await?;
        }
    }
}

/// What a successful result must have been preceded by.
fn claude_result_checks(evidence: &Evidence, scenario: &str) -> Result<(), GateError> {
    if scenario == "compact" && evidence.compact_boundaries == 0 {
        return Err(GateError::Protocol("no compact boundary observed"));
    }
    if scenario.starts_with("approval-") && evidence.approval_requests == 0 {
        return Err(GateError::Protocol("no approval request observed"));
    }
    if scenario == "mcp" && evidence.mcp_calls == 0 {
        return Err(GateError::Protocol("no fixture MCP call observed"));
    }
    if scenario == "hook" && evidence.hook_calls == 0 {
        return Err(GateError::Protocol("no hook callback observed"));
    }
    Ok(())
}

/// Answers a control request: the scenario's fixture permission, or
/// Octet's own `reply`.
async fn claude_request(
    gate: &mut GateProcess,
    scenario: &str,
    cwd: &Path,
    v: &Value,
    reply: Value,
) -> Result<(), GateError> {
    if v.pointer("/request/subtype").and_then(Value::as_str) == Some("can_use_tool") {
        let fixture_write = claude_fixture_allow(v, cwd);
        if scenario.starts_with("approval-") && fixture_write.is_some() {
            gate.record_approval();
        }
        match scenario {
            "approval-interrupt" if fixture_write.is_some() => {
                return gate.send(&json!({"type":"control_request","request_id":"interrupt-2","request":{"subtype":"interrupt"}})).await;
            }
            "approval-allow" => return gate.send(&fixture_write.unwrap_or(reply)).await,
            "mcp" => {
                let allowed = claude_fixture_mcp_tool_allow(v);
                if allowed.is_some() {
                    gate.record_approval();
                }
                return gate.send(&allowed.unwrap_or(reply)).await;
            }
            _ => {}
        }
    }
    gate.send(&reply).await
}

/// Runs the scenario once, and for Claude's `resume` and `fork` again in a
/// second process. The status, the evidence and whether every child stopped.
async fn run_scenario(opt: &Opt, cwd: &Path, deadline: Instant) -> (String, Evidence, bool) {
    let mut gate = match GateProcess::spawn(
        opt.engine,
        opt.binary.clone(),
        vendor_args(opt, cwd, None),
        cwd.to_owned(),
        deadline,
    ) {
        Ok(gate) => gate,
        Err(error) => return (format!("failed: {error}"), Evidence::default(), false),
    };
    let result = if opt.engine == Engine::CLAUDE {
        claude(&mut gate, &opt.scenario, cwd).await
    } else {
        codex(&mut gate, &opt.scenario, cwd).await
    };
    let mut evidence = gate.evidence().clone();
    let report = gate.shutdown().await;
    let mut cleaned = report.reaped && report.descendants_stopped;
    let outcome = match result {
        Ok(()) if cleaned => "passed".to_owned(),
        Ok(()) => "failed: child cleanup".to_owned(),
        Err(error) => format!("failed: {error}"),
    };
    let second_run = outcome == "passed"
        && opt.engine == Engine::CLAUDE
        && matches!(opt.scenario.as_str(), "resume" | "fork");
    if !second_run {
        return (outcome, evidence, cleaned);
    }
    let Some(first) = evidence.session_id.clone() else {
        return ("failed: first session id missing".into(), evidence, cleaned);
    };
    let mut second = match GateProcess::spawn(
        opt.engine,
        opt.binary.clone(),
        vendor_args(opt, cwd, Some(&first)),
        cwd.to_owned(),
        deadline,
    ) {
        Ok(second) => second,
        Err(error) => return (format!("failed: {error}"), evidence, cleaned),
    };
    let second_result = claude(&mut second, "simple", cwd).await;
    evidence.add_resumed(second.evidence());
    let resumed = second.evidence().session_id.clone();
    let second_report = second.shutdown().await;
    cleaned &= second_report.reaped && second_report.descendants_stopped;
    let outcome = match second_result {
        Ok(()) if !cleaned => "failed: resumed child cleanup".into(),
        Ok(()) if opt.scenario == "resume" && resumed.as_deref() == Some(first.as_str()) => {
            "passed".into()
        }
        Ok(()) if opt.scenario == "fork" && resumed.as_deref().is_some_and(|id| id != first) => {
            "passed".into()
        }
        Ok(()) => "failed: session id mismatch".into(),
        Err(error) => format!("failed: {error}"),
    };
    (outcome, evidence, cleaned)
}

/// Whether the fixture write landed: exactly `READY` in `probe.out`.
fn fixture_written(cwd: &Path) -> bool {
    fs::File::open(cwd.join("probe.out"))
        .and_then(|file| {
            use std::io::Read;
            let mut bytes = Vec::new();
            file.take(6).read_to_end(&mut bytes)?;
            Ok(bytes)
        })
        .is_ok_and(|bytes| bytes == b"READY")
}

/// Runs the gate; the process's exit code. Everything it owns, the
/// workspace above all, is dropped before the process exits.
async fn run() -> i32 {
    let opt = match options() {
        Ok(opt) => opt,
        Err(message) => {
            eprintln!("{message}");
            return 2;
        }
    };
    let workspace = match Workspace::create(opt.workdir.clone()) {
        Ok(workspace) => workspace,
        Err(message) => {
            eprintln!("{message}");
            return 2;
        }
    };
    let cwd = workspace.path.clone();
    let started = Instant::now();
    let (mut status, evidence, cleaned) = run_scenario(&opt, &cwd, started + opt.timeout).await;
    let fixture_write = fixture_written(&cwd);
    if status == "passed" && opt.scenario == "approval-allow" && !fixture_write {
        status = "failed: approved fixture write not observed".into();
    }
    if status == "passed"
        && (opt.scenario == "approval-deny" || opt.scenario == "approval-interrupt")
        && cwd.join("probe.out").exists()
    {
        status = "failed: denied or interrupted fixture write occurred".into();
    }
    let record = json!({"engine":opt.engine.as_str(),"scenario":opt.scenario,"version":version(&opt.binary).await,"host":env::consts::OS,"arch":env::consts::ARCH,"status":status,"events":evidence.events,"approval_requests":evidence.approval_requests,"mcp_calls":evidence.mcp_calls,"hook_calls":evidence.hook_calls,"compact_boundaries":evidence.compact_boundaries,"agent_messages":evidence.agent_messages,"late_usage_turns":evidence.late_usage_turns,"tool_only_turns":evidence.tool_only_turns,"user_questions":evidence.user_questions,"failure_kind":evidence.failure_kind,"fixture_write":fixture_write,"child_cleanup":cleaned,"elapsed_ms":started.elapsed().as_millis()});
    let serialized = record.to_string();
    let serialized = serde_json::to_string_pretty(&record).unwrap_or(serialized);
    if let Some(path) = &opt.output
        && fs::write(path, &serialized).is_err()
    {
        eprintln!("cannot write evidence");
        return 2;
    }
    println!("{serialized}");
    i32::from(!status.starts_with("passed"))
}

#[tokio::main]
async fn main() {
    let code = run().await;
    std::process::exit(code);
}
