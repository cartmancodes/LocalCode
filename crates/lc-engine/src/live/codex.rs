//! Codex app-server JSON-RPC protocol: session setup, the model catalog,
//! server requests and turn notifications.
use super::{
    driver::Driver,
    limited,
    mode::{codex_reported, codex_thread_params, codex_turn_overrides, confirm_mode},
    model_catalog, valid_identifier, DriverError, Event, Mode, Outcome, EVENT_BYTES,
};
use serde_json::{json, Value};

pub(super) fn answer(wire: &Value, allow: bool) -> Value {
    json!({"id":wire["id"],"result":{"decision":if allow {"accept"} else {"decline"}}})
}

impl Driver<'_> {
    pub(super) async fn codex_initialize(&mut self) -> Result<(), DriverError> {
        let client = json!({"name":"localcode","version":"0.1.0"});
        self.send(json!({"id":1,"method":"initialize","params":{"clientInfo":client,"capabilities":{"experimentalApi":true}}}))
            .await
    }

    /// Interrupts the running turn once Codex has said which turn it is.
    pub(super) async fn codex_interrupt(&mut self) -> Result<(), DriverError> {
        if self.turn.is_some() {
            self.codex_request_interrupt().await?;
        }
        Ok(())
    }

    async fn codex_request_interrupt(&mut self) -> Result<(), DriverError> {
        self.request_id += 1;
        self.interrupt_request = Some(self.request_id);
        let params = json!({"threadId":self.session,"turnId":self.turn});
        self.send(json!({"id":self.request_id,"method":"turn/interrupt","params":params}))
            .await
    }

    pub(super) async fn codex_send_prompt(&mut self, text: &str) -> Result<(), DriverError> {
        self.request_id += 1;
        self.start_request = Some(self.request_id);
        let mut params = json!({"threadId":self.session,"input":[{"type":"text","text":text}]});
        if let Some(model) = &self.config.model {
            params["model"] = json!(model);
        }
        if let (Some(target), Value::Object(extra)) =
            (params.as_object_mut(), codex_turn_overrides(self.mode))
        {
            target.extend(extra);
        }
        self.send(json!({"id":self.request_id,"method":"turn/start","params":params}))
            .await
    }

    /// Codex applies a mode with the next turn's overrides, so no request is sent.
    pub(super) fn codex_set_mode(&mut self, target: Mode) -> Result<(), DriverError> {
        self.mode = target;
        self.emit(Event::ModeChanged(self.mode))?;
        if self.running {
            self.emit(Event::Notice("Mode applies from the next turn".into()))?;
        }
        Ok(())
    }

    pub(super) async fn codex_frame(&mut self, v: Value) -> Result<(), DriverError> {
        let method = v["method"].as_str().unwrap_or("");
        if method.is_empty() {
            return self.codex_response(&v).await;
        }
        if v.get("id").is_some() {
            return self.codex_server_request(v).await;
        }
        if v.pointer("/params/threadId").and_then(Value::as_str) != Some(self.session.as_str()) {
            return Ok(());
        }
        if method == "turn/started" && self.running {
            self.turn = v
                .pointer("/params/turn/id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if self.interrupt_pending {
                self.codex_request_interrupt().await?;
            }
        }
        if method == "thread/tokenUsage/updated" {
            let total = v
                .pointer("/params/tokenUsage/total/totalTokens")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            self.emit(Event::Usage(format!("{total} tokens")))?;
        }
        if !self.running {
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
                    self.emit(Event::Text(text.into()))?;
                }
            }
            "item/started" | "item/completed" => {
                let item = &v["params"]["item"];
                match item["type"].as_str().unwrap_or("") {
                    "agentMessage" if method == "item/completed" => {
                        if !self.text_items.contains(item["id"].as_str().unwrap_or("")) {
                            if let Some(text) = item["text"].as_str() {
                                self.emit(Event::Text(text.into()))?;
                            }
                        }
                    }
                    "commandExecution" | "fileChange" | "mcpToolCall" => {
                        let phase = if method == "item/started" {
                            "running"
                        } else {
                            "finished"
                        };
                        self.emit(Event::Tool(codex_tool_detail(item, phase)))?;
                    }
                    _ => {}
                }
            }
            "turn/completed" => {
                let completed = v.pointer("/params/turn/id").and_then(Value::as_str);
                if self.turn.is_none() || completed != self.turn.as_deref() {
                    return Ok(());
                }
                self.running = false;
                self.close_all_pending()?;
                let status = v
                    .pointer("/params/turn/status")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown");
                if status == "failed" {
                    self.emit(Event::Error(error_text(&v["params"]["turn"]["error"])))?;
                }
                self.emit(Event::Finished {
                    outcome: Outcome::from_vendor(status),
                })?;
            }
            _ => {}
        }
        Ok(())
    }

    /// Replies to LocalCode's own requests, matched by their fixed or
    /// recorded ids.
    async fn codex_response(&mut self, v: &Value) -> Result<(), DriverError> {
        if v["id"] == 1 {
            if v.get("error").is_some() {
                return Err("Codex initialization failed".into());
            }
            self.initialized = true;
            self.send(json!({"method":"initialized","params":{}}))
                .await?;
            let mut params = codex_thread_params(self.mode);
            params["cwd"] = json!(self.config.cwd);
            let method = match &self.config.resume {
                Some(id) => {
                    params["threadId"] = json!(id);
                    "thread/resume"
                }
                None => "thread/start",
            };
            if let Some(model) = &self.config.model {
                params["model"] = json!(model);
            }
            self.send(json!({"id":2,"method":method,"params":params}))
                .await
        } else if v["id"] == 2 {
            if let Some(error) = v.get("error") {
                return Err(
                    format!("Codex could not open the session: {}", error_text(error)).into(),
                );
            }
            self.session = v
                .pointer("/result/thread/id")
                .and_then(Value::as_str)
                .ok_or("Codex could not open the session")?
                .into();
            self.ready = true;
            self.emit(Event::Ready {
                session: self.session.clone(),
            })?;
            self.mode = confirm_mode(self.tx, "Codex", self.mode, codex_reported(&v["result"]))?;
            self.emit(Event::ModeChanged(self.mode))?;
            if let Some(model) = v["result"]["model"]
                .as_str()
                .filter(|m| valid_identifier(m))
            {
                self.emit(Event::ModelSelected(model.into()))?;
            }
            self.send(
                json!({"id":3,"method":"model/list","params":{"limit":100,"includeHidden":false}}),
            )
            .await
        } else if v["id"] == 3 {
            self.codex_catalog_page(v).await
        } else if self.start_request.is_some() && v["id"].as_u64() == self.start_request {
            self.start_request = None;
            if v.get("error").is_some() {
                self.running = false;
                self.emit(Event::Error(error_text(&v["error"])))?;
                self.emit(Event::Finished {
                    outcome: Outcome::Failed,
                })?;
            }
            Ok(())
        } else if self.interrupt_request.is_some() && v["id"].as_u64() == self.interrupt_request {
            self.interrupt_request = None;
            if v.get("error").is_some() {
                self.emit(Event::Notice(
                    "Interrupt was rejected; waiting for terminal outcome".into(),
                ))?;
            }
            Ok(())
        } else {
            Ok(())
        }
    }

    async fn codex_catalog_page(&mut self, v: &Value) -> Result<(), DriverError> {
        self.catalog_pages += 1;
        if v.get("error").is_some() {
            return self.emit(Event::Notice(
                "Model catalog unavailable from this CLI; explicit model IDs remain supported"
                    .into(),
            ));
        }
        for model in model_catalog(&v["result"]["data"], false) {
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
                self.send(json!({"id":3,"method":"model/list","params":params}))
                    .await?;
            } else {
                self.emit(Event::Notice(
                    "Model catalog exceeds discovery limits; showing partial results".into(),
                ))?;
            }
        }
        self.emit(Event::Models(self.catalog.clone()))
    }

    /// A request from Codex: an approval for the running turn is shown to the
    /// user; anything else gets a stray reply.
    async fn codex_server_request(&mut self, v: Value) -> Result<(), DriverError> {
        let method = v["method"].as_str().unwrap_or("");
        let approval = matches!(
            method,
            "item/commandExecution/requestApproval" | "item/fileChange/requestApproval"
        );
        let this_turn = v.pointer("/params/threadId").and_then(Value::as_str)
            == Some(self.session.as_str())
            && self.turn.is_some()
            && v.pointer("/params/turnId").and_then(Value::as_str) == self.turn.as_deref();
        if approval
            && self.running
            && !self.interrupt_pending
            && self.pending.len() < 8
            && this_turn
        {
            let detail = serde_json::to_string_pretty(&v["params"]).unwrap_or_default();
            return self.queue_approval(v, detail).await;
        }
        if let Some(reply) = codex_stray_reply(&v) {
            let notice = format!("Request declined: {method}");
            self.send(reply).await?;
            self.emit(Event::Notice(notice))?;
        }
        Ok(())
    }
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
/// Codex counterpart of `claude_stray_reply`.
pub(super) fn codex_stray_reply(value: &Value) -> Option<Value> {
    let id = value.get("id")?;
    Some(match value.get("method")?.as_str()? {
        "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
            json!({"id":id,"result":{"decision":"decline"}})
        }
        "item/permissions/requestApproval" => {
            json!({"id":id,"result":{"permissions":{},"scope":"turn"}})
        }
        _ => {
            json!({"id":id,"error":{"code":-32601,"message":"LocalCode does not support this request"}})
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
        let item = json!({"type":"commandExecution","command":"cargo test","status":"failed","exitCode":101,"aggregatedOutput":"line 1\nline 2"});
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
        let long = json!({"type":"commandExecution","command":"x","aggregatedOutput":format!("{}END", "y".repeat(EVENT_BYTES * 2))});
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
