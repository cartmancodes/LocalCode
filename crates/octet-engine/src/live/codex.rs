//! Codex app-server JSON-RPC protocol: session setup, the model catalog,
//! server requests and turn notifications.
use super::{
    limited, model_catalog_with,
    protocol::{Core, Phase, Protocol},
    valid_identifier, BoxFuture, Channels, Config, DriverError, Event, ImageAttachment, Limits,
    Mode, ModelInfo, Outcome, Provider, EVENT_BYTES,
};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    ffi::OsString,
};

/// Codex's row in the provider table.
pub(super) const PROVIDER: Provider = Provider {
    name: "codex",
    title: "Codex",
    default_binary: "codex",
    offline: false,
    modes: [
        "Workspace sandbox; untrusted commands ask (untrusted)",
        "Workspace sandbox; asks only to escalate (on-request). Codex has no edits-only mode",
        "Workspace sandbox; Codex's auto-review agent decides escalations (auto_review)",
        "No sandbox; never asks (danger-full-access)",
    ],
    steer: true,
    inline_images: false,
    effort_live: true,
    efforts: &[],
    start,
};

fn start(config: Config, limits: Limits, channels: Channels) -> BoxFuture<Result<(), String>> {
    Box::pin(async move { super::driver::run::<CodexProtocol>(config, limits, channels).await })
}

fn answer_wire(wire: &Value, allow: bool) -> Value {
    json!({"id":wire["id"],"result":{"decision":if allow {"accept"} else {"decline"}}})
}

/// What a reply from Codex answers: one of Octet's own requests.
enum Outstanding {
    Initialize,
    OpenThread,
    ModelList,
    /// `turn/start` or `thread/compact/start`: an error fails the turn.
    Start,
    Interrupt,
    /// A steer, kept so a refusal can say which text was lost.
    Steer(String),
}

/// Codex's per-connection state: the turn in flight, Octet's own requests
/// awaiting replies and the model catalog being paged in.
#[derive(Default)]
pub(super) struct CodexProtocol {
    turn: Option<String>,
    /// Octet's last wire request ID.
    next_id: u64,
    requests: HashMap<u64, Outstanding>,
    text_items: HashSet<String>,
    catalog: Vec<ModelInfo>,
    catalog_pages: usize,
    catalog_cursors: HashSet<String>,
    /// Steering sent before Codex named the turn; sent once it does.
    pending_steer: Vec<String>,
}

impl Protocol for CodexProtocol {
    fn launch_args(_config: &Config) -> Vec<OsString> {
        vec!["app-server".into()]
    }

    fn answer(wire: &Value, allow: bool) -> Value {
        answer_wire(wire, allow)
    }

    fn stray_reply(request: &Value) -> Option<Value> {
        stray(request)
    }

    /// Only frames for this session's thread count: another thread's
    /// chatter is not progress.
    fn is_progress(&self, core: &Core, frame: &Value) -> bool {
        frame
            .pointer("/params/threadId")
            .and_then(Value::as_str)
            .is_none_or(|id| id == core.session)
    }

    fn turn_started(&mut self) {
        self.turn = None;
        self.text_items.clear();
        self.pending_steer.clear();
    }

    /// Steers the turn, or holds the text until Codex names the turn.
    async fn steer(&mut self, core: &mut Core, text: &str) -> Result<bool, DriverError> {
        if self.turn.is_none() {
            self.pending_steer.push(text.to_owned());
            return Ok(true);
        }
        self.send_steer(core, text).await?;
        Ok(true)
    }

    async fn initialize(&mut self, core: &mut Core) -> Result<(), DriverError> {
        let client = json!({"name":"octet","version":env!("CARGO_PKG_VERSION")});
        let params = json!({"clientInfo": client, "capabilities": {"experimentalApi": true}});
        self.request(core, Outstanding::Initialize, "initialize", params)
            .await
    }

    /// Interrupts the running turn once Codex has said which turn it is.
    async fn interrupt(&mut self, core: &mut Core) -> Result<(), DriverError> {
        if self.turn.is_some() {
            self.request_interrupt(core).await?;
        }
        Ok(())
    }

    /// Images go as local paths after the text; Codex reads them itself.
    async fn send_prompt(
        &mut self,
        core: &mut Core,
        text: &str,
        images: &[ImageAttachment],
    ) -> Result<(), DriverError> {
        let input: Vec<Value> = std::iter::once(json!({"type":"text","text":text}))
            .chain(
                images
                    .iter()
                    .map(|image| json!({"type":"localImage","path":image.path})),
            )
            .collect();
        let mut params = json!({"threadId":core.session,"input":input});
        if let Some(model) = &core.config.model {
            params["model"] = json!(model);
        }
        if let Some(effort) = &core.config.effort {
            params["effort"] = json!(effort);
        }
        if let (Some(target), Value::Object(extra)) =
            (params.as_object_mut(), codex_turn_overrides(core.mode))
        {
            target.extend(extra);
        }
        self.request(core, Outstanding::Start, "turn/start", params)
            .await
    }

    /// Compaction is a turn on the thread; an error reply fails it as a
    /// rejected `turn/start` does.
    async fn compact(&mut self, core: &mut Core) -> Result<(), DriverError> {
        let params = json!({"threadId": core.session});
        self.request(core, Outstanding::Start, "thread/compact/start", params)
            .await
    }

    /// Codex applies a mode with the next turn's overrides, so no request is sent.
    async fn set_mode(&mut self, core: &mut Core, target: Mode) -> Result<(), DriverError> {
        core.mode = target;
        core.emit(Event::ModeChanged(core.mode))?;
        if core.phase.is_running() {
            core.emit(Event::Notice("Mode applies from the next turn".into()))?;
        }
        Ok(())
    }

    async fn on_frame(&mut self, core: &mut Core, v: Value) -> Result<(), DriverError> {
        let method = v["method"].as_str().unwrap_or("");
        if method.is_empty() {
            return self.response(core, &v).await;
        }
        if v.get("id").is_some() {
            return self.server_request(core, v).await;
        }
        if v.pointer("/params/threadId").and_then(Value::as_str) != Some(core.session.as_str()) {
            return Ok(());
        }
        if method == "turn/started" && core.phase.is_running() {
            self.turn = v
                .pointer("/params/turn/id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if core.phase == Phase::Interrupting {
                self.drop_held_steers(core)?;
                self.request_interrupt(core).await?;
            } else {
                for text in std::mem::take(&mut self.pending_steer) {
                    self.send_steer(core, &text).await?;
                }
            }
        }
        if method == "thread/tokenUsage/updated" {
            let total = v
                .pointer("/params/tokenUsage/total/totalTokens")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            core.emit(Event::Usage(format!("{total} tokens")))?;
        }
        if !core.phase.is_running() {
            return Ok(());
        }
        if let Some(event_turn) = v.pointer("/params/turnId").and_then(Value::as_str) {
            if Some(event_turn) != self.turn.as_deref() {
                return Ok(());
            }
        }
        match method {
            "item/agentMessage/delta" => {
                if let Some(text) = v.pointer("/params/delta").and_then(Value::as_str) {
                    if let Some(id) = v.pointer("/params/itemId").and_then(Value::as_str) {
                        if self.text_items.len() >= 4096 {
                            return Err(DriverError::TurnItemLimit);
                        }
                        self.text_items.insert(id.to_owned());
                    }
                    core.emit(Event::Text(text.into()))?;
                }
            }
            "item/started" | "item/completed" => {
                let item = &v["params"]["item"];
                match item["type"].as_str().unwrap_or("") {
                    "agentMessage" if method == "item/completed" => {
                        if !self.text_items.contains(item["id"].as_str().unwrap_or("")) {
                            if let Some(text) = item["text"].as_str() {
                                core.emit(Event::Text(text.into()))?;
                            }
                        }
                    }
                    "commandExecution" | "fileChange" | "mcpToolCall" => {
                        let phase = if method == "item/started" {
                            "running"
                        } else {
                            "finished"
                        };
                        core.emit(Event::Tool(codex_tool_detail(item, phase)))?;
                    }
                    _ => {}
                }
            }
            "turn/completed" => {
                let completed = v.pointer("/params/turn/id").and_then(Value::as_str);
                if self.turn.is_none() || completed != self.turn.as_deref() {
                    return Ok(());
                }
                let status = v
                    .pointer("/params/turn/status")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown");
                let error = (status == "failed").then(|| error_text(&v["params"]["turn"]["error"]));
                core.finish_turn(Outcome::from_vendor(status), error)?;
            }
            _ => {}
        }
        Ok(())
    }
}

impl CodexProtocol {
    /// Sends one of Octet's requests and records what its reply answers.
    async fn request(
        &mut self,
        core: &Core,
        kind: Outstanding,
        method: &str,
        params: Value,
    ) -> Result<(), DriverError> {
        self.next_id += 1;
        self.requests.insert(self.next_id, kind);
        core.send(json!({"id": self.next_id, "method": method, "params": params}))
            .await
    }

    async fn send_steer(&mut self, core: &Core, text: &str) -> Result<(), DriverError> {
        let params = json!({
            "threadId": core.session,
            "expectedTurnId": self.turn,
            "input": [{"type": "text", "text": text}],
        });
        self.request(
            core,
            Outstanding::Steer(text.to_owned()),
            "turn/steer",
            params,
        )
        .await
    }

    async fn request_interrupt(&mut self, core: &Core) -> Result<(), DriverError> {
        let params = json!({"threadId":core.session,"turnId":self.turn});
        self.request(core, Outstanding::Interrupt, "turn/interrupt", params)
            .await
    }

    /// Steers held for a turn Codex never named are reported, not dropped.
    fn drop_held_steers(&mut self, core: &Core) -> Result<(), DriverError> {
        for text in std::mem::take(&mut self.pending_steer) {
            core.emit(Event::Notice(format!("This steer was not sent: {text}")))?;
        }
        Ok(())
    }

    /// Replies to Octet's own requests, matched by the request they answer.
    async fn response(&mut self, core: &mut Core, v: &Value) -> Result<(), DriverError> {
        let id = v["id"].as_u64();
        let Some(kind) = id.and_then(|id| self.requests.remove(&id)) else {
            // A reply to a request not yet sent, while connecting, means the
            // handshake is out of order; a repeated reply is ignored.
            if !core.phase.is_ready() && id.is_some_and(|id| id > self.next_id) {
                return Err("Unexpected protocol initialization order".into());
            }
            return Ok(());
        };
        match kind {
            Outstanding::Initialize => self.handshaken(core, v).await,
            Outstanding::OpenThread => self.opened(core, v).await,
            Outstanding::ModelList => self.catalog_page(core, v).await,
            Outstanding::Start => {
                if v.get("error").is_some() {
                    self.drop_held_steers(core)?;
                    core.finish_turn(Outcome::Failed, Some(error_text(&v["error"])))?;
                }
                Ok(())
            }
            Outstanding::Interrupt => match v.get("error") {
                Some(_) => core.emit(Event::Notice(
                    "Interrupt was rejected; waiting for terminal outcome".into(),
                )),
                None => Ok(()),
            },
            Outstanding::Steer(text) => match v.get("error") {
                Some(error) => core.emit(Event::Notice(format!(
                    "Codex did not take the steer ({}). Not sent: {text}",
                    error_text(error)
                ))),
                None => Ok(()),
            },
        }
    }

    /// The handshake's reply: open the session.
    async fn handshaken(&mut self, core: &mut Core, v: &Value) -> Result<(), DriverError> {
        if v.get("error").is_some() {
            return Err("Codex initialization failed".into());
        }
        core.phase = Phase::Handshaken;
        core.send(json!({"method":"initialized","params":{}}))
            .await?;
        let mut params = codex_thread_params(core.mode);
        params["cwd"] = json!(core.config.cwd);
        let method = match &core.config.resume {
            Some(id) => {
                params["threadId"] = json!(id);
                if core.config.fork {
                    "thread/fork"
                } else {
                    "thread/resume"
                }
            }
            None => "thread/start",
        };
        if let Some(model) = &core.config.model {
            params["model"] = json!(model);
        }
        self.request(core, Outstanding::OpenThread, method, params)
            .await
    }

    /// The session is open: ready, its mode confirmed, its catalog asked for.
    async fn opened(&mut self, core: &mut Core, v: &Value) -> Result<(), DriverError> {
        if let Some(error) = v.get("error") {
            return Err(format!("Codex could not open the session: {}", error_text(error)).into());
        }
        core.session = v
            .pointer("/result/thread/id")
            .and_then(Value::as_str)
            .ok_or("Codex could not open the session")?
            .into();
        core.phase = Phase::Idle;
        core.emit(Event::Ready {
            session: core.session.clone(),
        })?;
        core.adopt_mode(codex_reported(&v["result"]))?;
        if let Some(model) = v["result"]["model"]
            .as_str()
            .filter(|m| valid_identifier(m))
        {
            core.emit(Event::ModelSelected(model.into()))?;
        }
        self.request_models(core, None).await
    }

    /// Asks for a page of the model catalog.
    async fn request_models(
        &mut self,
        core: &Core,
        cursor: Option<&str>,
    ) -> Result<(), DriverError> {
        let mut params = json!({"limit":100,"includeHidden":false});
        if let Some(cursor) = cursor {
            params["cursor"] = json!(cursor);
        }
        self.request(core, Outstanding::ModelList, "model/list", params)
            .await
    }

    async fn catalog_page(&mut self, core: &Core, v: &Value) -> Result<(), DriverError> {
        self.catalog_pages += 1;
        if v.get("error").is_some() {
            return core.emit(Event::Notice(
                "Model catalog unavailable from this CLI; explicit model IDs remain supported"
                    .into(),
            ));
        }
        for model in catalog(&v["result"]["data"]) {
            if self.catalog.len() < 256
                && !self.catalog.iter().any(|m| m.selection == model.selection)
            {
                self.catalog.push(model);
            }
        }
        if let Some(cursor) = v["result"]["nextCursor"].as_str().filter(|c| !c.is_empty()) {
            if self.catalog_pages < 8
                && self.catalog.len() < 256
                && cursor.len() <= 4096
                && self.catalog_cursors.insert(cursor.to_owned())
            {
                self.request_models(core, Some(cursor)).await?;
            } else {
                core.emit(Event::Notice(
                    "Model catalog exceeds discovery limits; showing partial results".into(),
                ))?;
            }
        }
        core.emit(Event::Models(self.catalog.clone()))
    }

    /// A request from Codex: an approval for the running turn is shown to the
    /// user; anything else gets a stray reply.
    async fn server_request(&self, core: &mut Core, v: Value) -> Result<(), DriverError> {
        let method = v["method"].as_str().unwrap_or("");
        let approval = matches!(
            method,
            "item/commandExecution/requestApproval" | "item/fileChange/requestApproval"
        );
        let this_turn = v.pointer("/params/threadId").and_then(Value::as_str)
            == Some(core.session.as_str())
            && self.turn.is_some()
            && v.pointer("/params/turnId").and_then(Value::as_str) == self.turn.as_deref();
        if approval && core.phase == Phase::InTurn && this_turn {
            let detail = serde_json::to_string_pretty(&v["params"]).unwrap_or_default();
            return core.queue_approval(v, detail).await;
        }
        if let Some(reply) = Self::stray_reply(&v) {
            let notice = format!("Request declined: {method}");
            core.send(reply).await?;
            core.emit(Event::Notice(notice))?;
        }
        Ok(())
    }
}

/// The Codex catalog: `model` is both the selection and the resolved ID.
fn catalog(value: &Value) -> Vec<ModelInfo> {
    model_catalog_with(value, "model", "model")
}

/// Codex counterpart of `claude_stray_reply`: the reply to a request Octet
/// will not put in front of the user.
pub fn codex_stray_reply(value: &Value) -> Option<Value> {
    CodexProtocol::stray_reply(value)
}

/// A Codex item as a transcript preview. A command leads with what ran and how
/// it ended; long output keeps its end, where failures are reported.
pub(super) fn codex_tool_detail(item: &Value, phase: &str) -> String {
    let kind = item["type"].as_str().unwrap_or("tool");
    let mut text = format!("{kind} · {phase}");
    if kind != "commandExecution" {
        text.push('\n');
        text.push_str(&item.to_string());
        return limited(&text);
    }
    match &item["command"] {
        Value::Null => {}
        Value::String(command) => text.push_str(&format!("\n$ {command}")),
        other => text.push_str(&format!("\n$ {other}")),
    }
    match (item["status"].as_str(), item["exitCode"].as_i64()) {
        (Some(status), Some(code)) => text.push_str(&format!("\n{status} · exit {code}")),
        (Some(status), None) => text.push_str(&format!("\n{status}")),
        (None, Some(code)) => text.push_str(&format!("\nexit {code}")),
        (None, None) => {}
    }
    if let Some(output) = item["aggregatedOutput"].as_str().filter(|o| !o.is_empty()) {
        const CUT: &str = "[earlier output cut]\n";
        let room = EVENT_BYTES.saturating_sub(text.len() + 1 + CUT.len());
        text.push('\n');
        if output.len() > room {
            let start = output.ceil_char_boundary(output.len() - room);
            text.push_str(CUT);
            text.push_str(&output[start..]);
        } else {
            text.push_str(output);
        }
    }
    limited(&text)
}
/// The reply to a Codex request Octet will not put in front of the user:
/// approvals outside a turn are declined, anything else is unsupported.
fn stray(value: &Value) -> Option<Value> {
    let id = value.get("id")?;
    Some(match value.get("method")?.as_str()? {
        "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
            json!({"id":id,"result":{"decision":"decline"}})
        }
        "item/permissions/requestApproval" => {
            json!({"id":id,"result":{"permissions":{},"scope":"turn"}})
        }
        _ => {
            json!({"id":id,"error":{"code":-32601,"message":"Octet does not support this request"}})
        }
    })
}
/// A vendor error as a person should read it: the message, including one
/// nested as JSON text, with Codex's error kind when it says more than "other".
pub(super) fn error_text(error: &Value) -> String {
    if error.is_null() {
        return "The vendor reported an error without details".into();
    }
    let Some(message) = error.as_str().or(error["message"].as_str()) else {
        return limited(&error.to_string());
    };
    let inner = serde_json::from_str::<Value>(message).ok().and_then(|v| {
        v.pointer("/error/message")
            .and_then(Value::as_str)
            .map(str::to_owned)
    });
    let text = inner.unwrap_or_else(|| message.to_owned());
    match &error["codexErrorInfo"] {
        Value::String(kind) if kind != "other" => limited(&format!("{text} ({kind})")),
        kind @ Value::Object(_) => limited(&format!("{text} ({kind})")),
        _ => limited(&text),
    }
}

pub(crate) fn codex_thread_params(mode: Mode) -> Value {
    let (sandbox, policy, reviewer) = match mode {
        Mode::Ask => ("workspace-write", "untrusted", "user"),
        Mode::AcceptEdits => ("workspace-write", "on-request", "user"),
        Mode::Auto => ("workspace-write", "on-request", "auto_review"),
        Mode::FullAccess => ("danger-full-access", "never", "user"),
    };
    json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer})
}
/// The mode a Codex thread reply echoes. Outer None: the reply carries no
/// policy (older CLI). Inner None: a policy Octet does not map, with the
/// raw description for the user.
pub(crate) fn codex_reported(result: &Value) -> Option<(Option<Mode>, String)> {
    let policy = result.get("approvalPolicy")?;
    let sandbox = match result["sandbox"]["type"]
        .as_str()
        .or(result["sandbox"].as_str())
    {
        Some("workspaceWrite" | "workspace-write") => json!("workspace-write"),
        Some("dangerFullAccess" | "danger-full-access") => json!("danger-full-access"),
        _ => result["sandbox"].clone(),
    };
    let reviewer = match result["approvalsReviewer"].as_str() {
        Some("guardian_subagent") => json!("auto_review"),
        Some(reviewer) => json!(reviewer),
        None => json!("user"),
    };
    let echo = json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer});
    let mode = Mode::ALL
        .into_iter()
        .find(|mode| codex_thread_params(*mode) == echo);
    let raw = format!(
        "sandbox {}, approval {}, reviewer {}",
        result["sandbox"], policy, result["approvalsReviewer"]
    );
    Some((mode, limited(&raw)))
}
/// Codex applies these on the turn and every later turn. Live modes all share
/// the workspace-write sandbox, so no sandboxPolicy override is sent.
pub(crate) fn codex_turn_overrides(mode: Mode) -> Value {
    let params = codex_thread_params(mode);
    json!({"approvalPolicy":params["approvalPolicy"],"approvalsReviewer":params["approvalsReviewer"]})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn error_text_prefers_the_vendor_message() {
        assert_eq!(
            error_text(&json!({"message":"plain","codexErrorInfo":"other"})),
            "plain"
        );
        assert_eq!(
            error_text(&json!({"message":"limit","codexErrorInfo":"usageLimitExceeded"})),
            "limit (usageLimitExceeded)"
        );
        assert_eq!(
            error_text(&json!({"message":"{\"error\":{\"message\":\"inner\"}}"})),
            "inner"
        );
        assert_eq!(error_text(&json!({"code":-1})), "{\"code\":-1}");
        assert_eq!(error_text(&json!("text")), "text");
    }
    #[test]
    fn command_preview_leads_with_the_command_and_its_outcome() {
        let item = json!({
            "type": "commandExecution",
            "command": "cargo test",
            "status": "failed",
            "exitCode": 101,
            "aggregatedOutput": "line 1\nline 2",
        });
        assert_eq!(
            codex_tool_detail(&item, "finished"),
            "commandExecution · finished\n$ cargo test\nfailed · exit 101\nline 1\nline 2"
        );
        let running = json!({"type":"commandExecution","command":"cargo test"});
        assert_eq!(
            codex_tool_detail(&running, "running"),
            "commandExecution · running\n$ cargo test"
        );
        let other = json!({"type":"fileChange","changes":[]});
        assert!(codex_tool_detail(&other, "finished").starts_with("fileChange · finished\n{"));
        // Long output keeps its end, where failures are reported.
        let output = format!("{}END", "y".repeat(EVENT_BYTES * 2));
        let long = json!({"type": "commandExecution", "command": "x", "aggregatedOutput": output});
        let detail = codex_tool_detail(&long, "finished");
        assert!(
            detail.len() <= EVENT_BYTES
                && detail.ends_with("END")
                && detail.contains("[earlier output cut]"),
            "{}",
            detail.len()
        );
    }
    #[test]
    fn codex_mapping_matches_spec_table() {
        let expected = [
            (Mode::Ask, "workspace-write", "untrusted", "user"),
            (Mode::AcceptEdits, "workspace-write", "on-request", "user"),
            (Mode::Auto, "workspace-write", "on-request", "auto_review"),
            (Mode::FullAccess, "danger-full-access", "never", "user"),
        ];
        for (mode, sandbox, policy, reviewer) in expected {
            assert_eq!(
                codex_thread_params(mode),
                json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer})
            );
            assert_eq!(
                codex_turn_overrides(mode),
                json!({"approvalPolicy":policy,"approvalsReviewer":reviewer})
            );
        }
    }
    #[test]
    fn reported_modes_map_back_or_stay_unmapped() {
        for mode in Mode::ALL {
            let mut echo = codex_thread_params(mode);
            echo["sandbox"] = match mode {
                Mode::FullAccess => json!({"type":"dangerFullAccess"}),
                _ => json!({"type":"workspaceWrite"}),
            };
            assert_eq!(codex_reported(&echo).map(|r| r.0), Some(Some(mode)));
        }
        let legacy = json!({
            "sandbox": {"type": "workspaceWrite"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "guardian_subagent",
        });
        assert_eq!(codex_reported(&legacy).map(|r| r.0), Some(Some(Mode::Auto)));
        let read_only = json!({
            "sandbox": {"type": "readOnly"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        });
        let (mode, raw) = codex_reported(&read_only).unwrap();
        assert_eq!(mode, None);
        assert!(raw.contains("readOnly"), "{raw}");
        assert_eq!(codex_reported(&json!({"thread":{}})), None);
    }
}
