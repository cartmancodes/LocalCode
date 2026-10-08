//! The protocol gate: drives a real vendor CLI through fixed scenarios and
//! records contract evidence. Default replies are Octet's own
//! (`octet_engine::live::{codex_stray_reply, claude_stray_reply}`).
#![forbid(unsafe_code)]

use octet_proc::{Process, ProcessError, ProcessSender, ShutdownReport};
use serde_json::{Value, json};
use std::{ffi::OsString, path::PathBuf};
use thiserror::Error;
use tokio::time::{Instant, timeout};

#[derive(Debug, Error)]
/// Why a gate scenario could not finish.
pub enum GateError {
    /// The transport failed.
    #[error("transport: {0}")]
    Transport(#[from] ProcessError),
    /// The scenario ran past its deadline.
    #[error("deadline exceeded")]
    Deadline,
    /// The vendor exited before the event the scenario waits for.
    #[error("child closed before the expected protocol event")]
    Eof,
    /// The vendor broke the expected protocol, as described.
    #[error("protocol: {0}")]
    Protocol(&'static str),
}

/// The contract evidence one run collects.
#[derive(Clone, Debug, Default)]
pub struct Evidence {
    /// Frames received.
    pub events: u64,
    /// Approval requests the vendor sent.
    pub approval_requests: u64,
    /// MCP tool calls answered.
    pub mcp_calls: u64,
    /// Hook callbacks answered.
    pub hook_calls: u64,
    /// Questions the vendor asked the user.
    pub user_questions: u64,
    /// The vendor's failure kind, when a turn failed.
    pub failure_kind: Option<&'static str>,
    /// The vendor session ID, once reported.
    pub session_id: Option<String>,
    /// Context compactions the vendor reported.
    pub compact_boundaries: u64,
    /// Completed assistant messages.
    pub agent_messages: u64,
    /// Turns whose usage arrived after their last message.
    pub late_usage_turns: u64,
    /// Turns with tool items but no assistant message.
    pub tool_only_turns: u64,
}

impl Evidence {
    /// Adds what a resumed (second) process saw to the first run's counts.
    pub fn add_resumed(&mut self, resumed: &Evidence) {
        self.events += resumed.events;
        self.approval_requests += resumed.approval_requests;
        self.compact_boundaries += resumed.compact_boundaries;
    }
}

/// Per-turn tallies behind `late_usage_turns` and `tool_only_turns`.
#[derive(Debug, Default)]
struct TurnTally {
    agent_messages: u64,
    tool_items: u64,
    last_message_seq: u64,
    last_usage_seq: u64,
}

/// A vendor process under a scenario, counting the contract evidence.
pub struct GateProcess {
    process: Process,
    sender: ProcessSender,
    /// When the whole scenario must be done.
    deadline: Instant,
    /// `OCTET_GATE_TRACE` was set: each frame is summarized on stderr.
    trace: bool,
    evidence: Evidence,
    turn: TurnTally,
}

impl std::fmt::Debug for GateProcess {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GateProcess")
            .field("process", &self.process)
            .field("evidence", &self.evidence)
            .finish_non_exhaustive()
    }
}

impl GateProcess {
    /// Starts `executable` with `args` in `cwd`, with the limits Octet's
    /// driver uses; the scenario must end by `deadline`.
    ///
    /// # Errors
    ///
    /// Fails if the vendor cannot start.
    pub fn spawn(
        engine: octet_engine::live::Engine,
        executable: PathBuf,
        args: Vec<OsString>,
        cwd: PathBuf,
        deadline: Instant,
    ) -> Result<Self, GateError> {
        let process = Process::spawn(&octet_engine::live::vendor_process(
            engine, executable, args, cwd,
        ))?;
        let sender = process.sender();
        Ok(Self {
            process,
            sender,
            deadline,
            trace: std::env::var_os("OCTET_GATE_TRACE").is_some(),
            evidence: Evidence::default(),
            turn: TurnTally::default(),
        })
    }

    /// What this run has seen so far.
    pub fn evidence(&self) -> &Evidence {
        &self.evidence
    }
    /// Counts an approval request the scenario expected.
    pub fn record_approval(&mut self) {
        self.evidence.approval_requests += 1;
    }
    /// Counts an answered MCP tool call.
    pub fn record_mcp_call(&mut self) {
        self.evidence.mcp_calls += 1;
    }
    /// Counts an answered hook callback.
    pub fn record_hook_call(&mut self) {
        self.evidence.hook_calls += 1;
    }
    /// Counts a question the vendor asked the user.
    pub fn record_user_question(&mut self) {
        self.evidence.user_questions += 1;
    }
    /// Records how the vendor failed the turn.
    pub fn record_failure(&mut self, kind: &'static str) {
        self.evidence.failure_kind = Some(kind);
    }

    /// Sends one frame before the deadline.
    ///
    /// # Errors
    ///
    /// `Deadline` if time runs out, or `Transport` if the write fails.
    pub async fn send(&self, value: &Value) -> Result<(), GateError> {
        timeout(
            self.deadline.saturating_duration_since(Instant::now()),
            self.sender.send(value),
        )
        .await
        .map_err(|_| GateError::Deadline)??;
        Ok(())
    }

    /// The next frame, counted into the evidence.
    ///
    /// # Errors
    ///
    /// `Deadline` if time runs out, `Eof` if the vendor exits, or
    /// `Transport` for a bad frame.
    pub async fn receive(&mut self) -> Result<Value, GateError> {
        let value = timeout(
            self.deadline.saturating_duration_since(Instant::now()),
            self.process.next_frame(),
        )
        .await
        .map_err(|_| GateError::Deadline)??
        .ok_or(GateError::Eof)?;
        self.count(&value);
        if self.trace {
            eprintln!(
                "event id={:?} method={:?} type={:?} control={:?} status={:?}",
                value.get("id"),
                value.get("method").and_then(Value::as_str),
                value.get("type").and_then(Value::as_str),
                value.pointer("/request/subtype").and_then(Value::as_str),
                value.pointer("/params/turn/status").and_then(Value::as_str)
            );
        }
        Ok(value)
    }

    /// Counts `value` into the evidence.
    fn count(&mut self, value: &Value) {
        let evidence = &mut self.evidence;
        let turn = &mut self.turn;
        evidence.events += 1;
        match value.get("method").and_then(Value::as_str) {
            Some("turn/started") => *turn = TurnTally::default(),
            Some("item/completed") => {
                match value.pointer("/params/item/type").and_then(Value::as_str) {
                    Some("agentMessage") => {
                        evidence.agent_messages += 1;
                        turn.agent_messages += 1;
                        turn.last_message_seq = evidence.events;
                    }
                    Some("commandExecution") => turn.tool_items += 1,
                    Some("contextCompaction") => evidence.compact_boundaries += 1,
                    _ => {}
                }
            }
            Some("thread/tokenUsage/updated") => turn.last_usage_seq = evidence.events,
            Some("turn/completed") => {
                if turn.last_message_seq > 0 && turn.last_usage_seq > turn.last_message_seq {
                    evidence.late_usage_turns += 1;
                }
                if turn.tool_items > 0 && turn.agent_messages == 0 {
                    evidence.tool_only_turns += 1;
                }
            }
            _ => {}
        }
        if let Some(id) = value.get("session_id").and_then(Value::as_str) {
            evidence.session_id = Some(id.to_owned());
        }
        if value.get("type").and_then(Value::as_str) == Some("system")
            && value.get("subtype").and_then(Value::as_str) == Some("compact_boundary")
        {
            evidence.compact_boundaries += 1;
        }
    }

    /// Stops the vendor and its process group.
    pub async fn shutdown(&mut self) -> ShutdownReport {
        self.process.shutdown().await
    }
}

/// Allow only the single fixture write used by the live approval proof.
pub fn codex_fixture_allow(value: &Value, expected_cwd: &std::path::Path) -> Option<Value> {
    if value.get("method")?.as_str()? != "item/commandExecution/requestApproval" {
        return None;
    }
    let command = value.pointer("/params/command")?.as_str()?;
    if let Some(cwd) = value.pointer("/params/cwd").and_then(Value::as_str)
        && std::path::Path::new(cwd) != expected_cwd
    {
        return None;
    }
    if command != "printf READY > probe.out"
        && command != "printf 'READY' > probe.out"
        && command != "/bin/zsh -lc 'printf READY > probe.out'"
    {
        return None;
    }
    Some(json!({"id":value.get("id")?,"result":{"decision":"accept"}}))
}

/// Answers a Codex question with each question's first option.
pub fn codex_fixture_user_input(value: &Value) -> Option<Value> {
    if value.get("method")?.as_str()? != "item/tool/requestUserInput" {
        return None;
    }
    let id = value.get("id")?;
    let questions = value.pointer("/params/questions")?.as_array()?;
    let mut answers = serde_json::Map::new();
    for question in questions {
        let question_id = question.get("id")?.as_str()?;
        let answer = question
            .pointer("/options/0/label")
            .and_then(Value::as_str)
            .unwrap_or("ALPHA");
        answers.insert(question_id.to_owned(), json!({"answers":[answer]}));
    }
    Some(json!({"id":id,"result":{"answers":answers}}))
}

/// Claude permission replies must carry the original tool input on allow.
pub fn claude_fixture_allow(value: &Value, expected_cwd: &std::path::Path) -> Option<Value> {
    if value.pointer("/request/subtype")?.as_str()? != "can_use_tool"
        || value.pointer("/request/tool_name")?.as_str()? != "Bash"
    {
        return None;
    }
    let input = value.pointer("/request/input")?;
    let command = input.get("command")?.as_str()?;
    let canonical = std::fs::canonicalize(expected_cwd).ok()?;
    let target = canonical.join("probe.out");
    let target = target.to_str()?;
    // Unquoted absolute paths are safe only when every byte is shell-literal.
    let safe_absolute = target
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"/_-.".contains(&b));
    if command != "printf READY > probe.out"
        && command != "printf 'READY' > probe.out"
        && !(safe_absolute
            && (command == format!("printf READY > {target}")
                || command == format!("printf READY > {target} && ls -l {target}")))
    {
        return None;
    }
    Some(
        json!({"type":"control_response","response":{"subtype":"success","request_id":value.get("request_id")?,"response":{"behavior":"allow","updatedInput":input}}}),
    )
}

/// Answers the fixture MCP server's messages; the flag is true for a tool
/// call.
pub fn claude_fixture_mcp_response(value: &Value) -> Option<(Value, bool)> {
    if value.get("type")?.as_str()? != "control_request"
        || value.pointer("/request/subtype")?.as_str()? != "mcp_message"
    {
        return None;
    }
    let id = value.get("request_id")?;
    let request = value.get("request")?;
    let message = request.get("message")?;
    let jsonrpc_id = message.get("id");
    if jsonrpc_id.is_none() {
        return Some((
            json!({"type":"control_response","response":{"subtype":"success","request_id":id,"response":{"mcp_response":{"jsonrpc":"2.0","result":{}}}}}),
            false,
        ));
    }
    let method = message.get("method").and_then(Value::as_str);
    let (result, called) = if request.get("server_name")?.as_str()? == "fixture" {
        match method {
            Some("initialize") => (
                json!({"jsonrpc":"2.0","id":jsonrpc_id,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"0.1.0"}}}),
                false,
            ),
            Some("tools/list") => (
                json!({"jsonrpc":"2.0","id":jsonrpc_id,"result":{"tools":[{"name":"fixture_echo","description":"Returns READY from the protocol fixture","inputSchema":{"type":"object","properties":{}}}]}}),
                false,
            ),
            Some("tools/call")
                if message.pointer("/params/name").and_then(Value::as_str)
                    == Some("fixture_echo") =>
            {
                (
                    json!({"jsonrpc":"2.0","id":jsonrpc_id,"result":{"content":[{"type":"text","text":"READY"}],"isError":false}}),
                    true,
                )
            }
            Some(_) => (
                json!({"jsonrpc":"2.0","id":jsonrpc_id,"error":{"code":-32601,"message":"unknown fixture method"}}),
                false,
            ),
            None => (json!({"jsonrpc":"2.0","result":{}}), false),
        }
    } else {
        (
            json!({"jsonrpc":"2.0","id":jsonrpc_id,"error":{"code":-32601,"message":"unknown fixture server"}}),
            false,
        )
    };
    Some((
        json!({"type":"control_response","response":{"subtype":"success","request_id":id,"response":{"mcp_response":result}}}),
        called,
    ))
}

/// Allows only the fixture MCP tool.
pub fn claude_fixture_mcp_tool_allow(value: &Value) -> Option<Value> {
    if value.pointer("/request/subtype")?.as_str()? != "can_use_tool"
        || value.pointer("/request/tool_name")?.as_str()? != "mcp__fixture__fixture_echo"
    {
        return None;
    }
    let input = value.pointer("/request/input")?;
    Some(
        json!({"type":"control_response","response":{"subtype":"success","request_id":value.get("request_id")?,"response":{"behavior":"allow","updatedInput":input}}}),
    )
}

/// Continues the fixture hook callback, and nothing else.
pub fn claude_fixture_hook_response(value: &Value) -> Option<Value> {
    if value.pointer("/request/subtype")?.as_str()? != "hook_callback"
        || value.pointer("/request/callback_id")?.as_str()? != "hook_0"
    {
        return None;
    }
    Some(
        json!({"type":"control_response","response":{"subtype":"success","request_id":value.get("request_id")?,"response":{"continue":true}}}),
    )
}
