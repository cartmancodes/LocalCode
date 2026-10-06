//! Codex app-server JSON-RPC protocol: session setup, the model catalog,
//! server requests and turn notifications.
use super::{
    limited,
    mode::{codex_reported, codex_thread_params, codex_turn_overrides, confirm_mode},
    model_catalog_with,
    protocol::{Core, Protocol},
    valid_identifier, BoxFuture, Channels, Config, DriverError, Event, Limits, Mode, ModelInfo,
    Outcome, Provider, EVENT_BYTES,
};
use serde_json::{json, Value};
use std::{collections::HashSet, ffi::OsString};

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
    start,
};

fn start(config: Config, limits: Limits, channels: Channels) -> BoxFuture<Result<(), String>> {
    Box::pin(async move { super::driver::run::<CodexProtocol>(config, limits, channels).await })
}

fn answer_wire(wire: &Value, allow: bool) -> Value {
    json!({"id":wire["id"],"result":{"decision":if allow {"accept"} else {"decline"}}})
}

/// Codex's per-connection state: the turn in flight, Octet's own pending
/// requests and the model catalog being paged in.
#[derive(Default)]
pub(super) struct CodexProtocol {
    turn: Option<String>,
    start_request: Option<u64>,
    interrupt_request: Option<u64>,
    text_items: HashSet<String>,
    catalog: Vec<ModelInfo>,
    catalog_pages: usize,
    catalog_cursors: HashSet<String>,
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
    }

    async fn initialize(&mut self, core: &mut Core) -> Result<(), DriverError> {
        let client = json!({"name":"octet","version":"0.1.0"});
        let params = json!({"clientInfo": client, "capabilities": {"experimentalApi": true}});
        core.send(json!({"id": 1, "method": "initialize", "params": params}))
            .await
    }

    /// Interrupts the running turn once Codex has said which turn it is.
    async fn interrupt(&mut self, core: &mut Core) -> Result<(), DriverError> {
        if self.turn.is_some() {
            self.request_interrupt(core).await?;
        }
        Ok(())
    }

    async fn send_prompt(&mut self, core: &mut Core, text: &str) -> Result<(), DriverError> {
        core.request_id += 1;
        self.start_request = Some(core.request_id);
        let mut params = json!({"threadId":core.session,"input":[{"type":"text","text":text}]});
        if let Some(model) = &core.config.model {
            params["model"] = json!(model);
        }
        if let (Some(target), Value::Object(extra)) =
            (params.as_object_mut(), codex_turn_overrides(core.mode))
        {
            target.extend(extra);
        }
        core.send(json!({"id":core.request_id,"method":"turn/start","params":params}))
            .await
    }

    /// Codex applies a mode with the next turn's overrides, so no request is sent.
    async fn set_mode(&mut self, core: &mut Core, target: Mode) -> Result<(), DriverError> {
        core.mode = target;
        core.emit(Event::ModeChanged(core.mode))?;
        if core.running {
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
        if method == "turn/started" && core.running {
            self.turn = v
                .pointer("/params/turn/id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if core.interrupt_pending {
                self.request_interrupt(core).await?;
            }
        }
        if method == "thread/tokenUsage/updated" {
            let total = v
                .pointer("/params/tokenUsage/total/totalTokens")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            core.emit(Event::Usage(format!("{total} tokens")))?;
        }
        if !core.running {
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
                core.running = false;
                core.close_all_pending()?;
                let status = v
                    .pointer("/params/turn/status")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown");
                if status == "failed" {
                    core.emit(Event::Error(error_text(&v["params"]["turn"]["error"])))?;
                }
                core.emit(Event::Finished {
                    outcome: Outcome::from_vendor(status),
                })?;
            }
            _ => {}
        }
        Ok(())
    }
}

impl CodexProtocol {
    async fn request_interrupt(&mut self, core: &mut Core) -> Result<(), DriverError> {
        core.request_id += 1;
        self.interrupt_request = Some(core.request_id);
        let params = json!({"threadId":core.session,"turnId":self.turn});
        core.send(json!({"id":core.request_id,"method":"turn/interrupt","params":params}))
            .await
    }

    /// Replies to Octet's own requests, matched by their fixed or
    /// recorded ids.
    async fn response(&mut self, core: &mut Core, v: &Value) -> Result<(), DriverError> {
        if v["id"] == 1 {
            if v.get("error").is_some() {
                return Err("Codex initialization failed".into());
            }
            core.initialized = true;
            core.send(json!({"method":"initialized","params":{}}))
                .await?;
            let mut params = codex_thread_params(core.mode);
            params["cwd"] = json!(core.config.cwd);
            let method = match &core.config.resume {
                Some(id) => {
                    params["threadId"] = json!(id);
                    "thread/resume"
                }
                None => "thread/start",
            };
            if let Some(model) = &core.config.model {
                params["model"] = json!(model);
            }
            core.send(json!({"id":2,"method":method,"params":params}))
                .await
        } else if v["id"] == 2 {
            if let Some(error) = v.get("error") {
                return Err(
                    format!("Codex could not open the session: {}", error_text(error)).into(),
                );
            }
            core.session = v
                .pointer("/result/thread/id")
                .and_then(Value::as_str)
                .ok_or("Codex could not open the session")?
                .into();
            core.ready = true;
            core.emit(Event::Ready {
                session: core.session.clone(),
            })?;
            core.mode = confirm_mode(
                &core.tx,
                super::Engine::CODEX.title(),
                core.mode,
                codex_reported(&v["result"]),
            )?;
            core.emit(Event::ModeChanged(core.mode))?;
            if let Some(model) = v["result"]["model"]
                .as_str()
                .filter(|m| valid_identifier(m))
            {
                core.emit(Event::ModelSelected(model.into()))?;
            }
            core.send(
                json!({"id":3,"method":"model/list","params":{"limit":100,"includeHidden":false}}),
            )
            .await
        } else if v["id"] == 3 {
            self.catalog_page(core, v).await
        } else if self.start_request.is_some() && v["id"].as_u64() == self.start_request {
            self.start_request = None;
            if v.get("error").is_some() {
                core.running = false;
                core.emit(Event::Error(error_text(&v["error"])))?;
                core.emit(Event::Finished {
                    outcome: Outcome::Failed,
                })?;
            }
            Ok(())
        } else if self.interrupt_request.is_some() && v["id"].as_u64() == self.interrupt_request {
            self.interrupt_request = None;
            if v.get("error").is_some() {
                core.emit(Event::Notice(
                    "Interrupt was rejected; waiting for terminal outcome".into(),
                ))?;
            }
            Ok(())
        } else {
            Ok(())
        }
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
                let params = json!({"limit":100,"includeHidden":false,"cursor":cursor});
                core.send(json!({"id":3,"method":"model/list","params":params}))
                    .await?;
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
        if approval
            && core.running
            && !core.interrupt_pending
            && core.pending.len() < 8
            && this_turn
        {
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
}
