//! Claude Code stream-json protocol: launch arguments, control requests and
//! the frame handler.
use super::{
    driver::{Driver, ModeRequest},
    limited,
    mode::{claude_mode, claude_permission_args, claude_reported_mode, confirm_mode},
    model_catalog, valid_identifier, Config, DriverError, Event, Mode, Outcome,
};
use serde_json::{json, Value};
use std::ffi::OsString;
use tokio::{sync::mpsc, time::Instant};

pub(super) fn launch_args(config: &Config) -> Vec<OsString> {
    let mut args: Vec<OsString> = [
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-prompt-tool",
        "stdio",
        "--setting-sources=",
        "--strict-mcp-config",
    ]
    .into_iter()
    .map(Into::into)
    .collect();
    args.extend(
        claude_permission_args(config.mode)
            .into_iter()
            .map(OsString::from),
    );
    if let Some(model) = &config.model {
        args.extend(["--model".into(), model.into()]);
    }
    if let Some(session) = &config.resume {
        args.extend(["--resume".into(), session.into()]);
    }
    args
}

pub(super) fn answer(wire: &Value, allow: bool) -> Value {
    let response = if allow {
        json!({"behavior":"allow","updatedInput":wire["request"]["input"]})
    } else {
        json!({"behavior":"deny","message":"Denied by Octet user or timeout"})
    };
    json!({"type":"control_response","response":{"subtype":"success","request_id":wire["request_id"],"response":response}})
}

impl Driver<'_> {
    pub(super) async fn claude_initialize(&mut self) -> Result<(), DriverError> {
        self.send(json!({"type":"control_request","request_id":"octet-init","request":{"subtype":"initialize"}}))
            .await
    }

    pub(super) async fn claude_interrupt(&mut self) -> Result<(), DriverError> {
        self.send(json!({"type":"control_request","request_id":"octet-interrupt","request":{"subtype":"interrupt"}}))
            .await
    }

    pub(super) async fn claude_send_prompt(&mut self, text: &str) -> Result<(), DriverError> {
        self.send(json!({"type":"user","message":{"role":"user","content":[{"type":"text","text":text}]},"parent_tool_use_id":null}))
            .await
    }

    pub(super) async fn claude_request_mode(&mut self, target: Mode) -> Result<(), DriverError> {
        self.mode_seq += 1;
        let id = format!("octet-mode-{}", self.mode_seq);
        let request = json!({"subtype":"set_permission_mode","mode":claude_mode(target)});
        self.send(json!({"type":"control_request","request_id":id,"request":request}))
            .await?;
        self.mode_request = Some(ModeRequest {
            id,
            target,
            deadline: Instant::now() + self.limits.mode_confirm,
        });
        Ok(())
    }

    pub(super) async fn claude_frame(&mut self, v: Value) -> Result<(), DriverError> {
        let kind = v["type"].as_str().unwrap_or("");
        if kind == "control_response" && self.claude_control_response(&v)? {
            return Ok(());
        }
        if let Some(id) = v["session_id"].as_str() {
            if self.session != id {
                self.session = id.to_owned();
                self.emit(Event::Ready {
                    session: self.session.clone(),
                })?;
            }
        }
        let actual = match kind {
            "system" if v["subtype"] == "init" => v["model"].as_str(),
            "assistant" => v["message"]["model"].as_str(),
            "stream_event" => v["event"]["message"]["model"].as_str(),
            _ => None,
        };
        if let Some(model) = actual.filter(|model| valid_identifier(model)) {
            if self.selected_model != model {
                self.selected_model = model.into();
                self.emit(Event::ModelSelected(self.selected_model.clone()))?;
            }
        }
        if kind == "control_request" {
            let permission =
                v.pointer("/request/subtype").and_then(Value::as_str) == Some("can_use_tool");
            if permission && self.running && !self.interrupt_pending {
                if self.pending.len() >= 8 {
                    return self.send(answer(&v, false)).await;
                }
                let detail = serde_json::to_string_pretty(&v["request"]).unwrap_or_default();
                return self.queue_approval(v, detail).await;
            }
            if let Some(reply) = claude_stray_reply(&v) {
                self.send(reply).await?;
            }
            return Ok(());
        }
        if kind == "control_cancel_request" {
            let ids: Vec<u64> = self
                .pending
                .iter()
                .filter(|(_, pending)| pending.wire["request_id"] == v["request_id"])
                .map(|(id, _)| *id)
                .collect();
            for id in ids {
                self.pending.remove(&id);
                self.emit(Event::ApprovalClosed(id))?;
            }
            self.resume_watchdog();
        }
        if !self.running {
            return Ok(());
        }
        match kind {
            "stream_event" => {
                if let Some(text) = v.pointer("/event/delta/text").and_then(Value::as_str) {
                    self.streamed = true;
                    self.emit(Event::Text(text.into()))?;
                }
            }
            "assistant" => {
                for block in v
                    .pointer("/message/content")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if !self.streamed && block["type"] == "text" {
                        if let Some(text) = block["text"].as_str() {
                            self.emit(Event::Text(text.into()))?;
                        }
                    }
                    if block["type"] == "tool_use" {
                        let name = block["name"].as_str().unwrap_or("tool");
                        self.emit(Event::Tool(format!("{name}\n{}", block["input"])))?;
                    }
                }
                self.streamed = false;
            }
            "result" => self.claude_result(&v)?,
            _ => {}
        }
        Ok(())
    }

    /// Handles replies to Octet's own control requests. True when the
    /// frame is fully handled.
    fn claude_control_response(&mut self, v: &Value) -> Result<bool, DriverError> {
        let response_id = v.pointer("/response/request_id").and_then(Value::as_str);
        let succeeded = v.pointer("/response/subtype").and_then(Value::as_str) == Some("success");
        if let Some(index) = self
            .late_modes
            .iter()
            .position(|(id, _)| Some(id.as_str()) == response_id)
        {
            let (_, target) = self.late_modes.remove(index);
            if succeeded {
                self.mode = switch_reply_mode(self.tx, v, target)?;
                self.emit(Event::Notice(format!(
                    "Claude confirmed the switch to {} late",
                    self.mode.label()
                )))?;
                self.emit(Event::ModeChanged(self.mode))?;
            }
            return Ok(true);
        }
        if let Some(request) = self
            .mode_request
            .take_if(|request| Some(request.id.as_str()) == response_id)
        {
            if succeeded {
                self.mode = switch_reply_mode(self.tx, v, request.target)?;
            } else {
                let error = v.pointer("/response/error").and_then(Value::as_str);
                self.emit(Event::Notice(format!(
                    "Mode change refused by Claude: {}",
                    limited(error.unwrap_or("unknown error"))
                )))?;
            }
            self.emit(Event::ModeChanged(self.mode))?;
            return Ok(true);
        }
        if response_id == Some("octet-init") {
            if !succeeded {
                return Err("Claude initialization failed".into());
            }
            self.initialized = true;
            self.ready = true;
            self.emit(Event::Ready {
                session: self.config.resume.clone().unwrap_or_default(),
            })?;
            let reported = v
                .pointer("/response/response/current_permission_mode")
                .and_then(Value::as_str)
                .map(|raw| (claude_reported_mode(raw), limited(raw)));
            self.mode = confirm_mode(self.tx, "Claude", self.mode, reported)?;
            self.emit(Event::ModeChanged(self.mode))?;
            self.emit(Event::Models(model_catalog(
                &v["response"]["response"]["models"],
                true,
            )))?;
        }
        Ok(false)
    }

    fn claude_result(&mut self, v: &Value) -> Result<(), DriverError> {
        if v["is_error"].as_bool() != Some(false) && !self.interrupt_pending {
            self.emit(Event::Error(claude_result_error(v)))?;
        }
        if let Some(cost) = v["total_cost_usd"].as_f64() {
            self.emit(Event::Usage(format!("${cost:.4} session cost")))?;
        }
        self.running = false;
        self.close_all_pending()?;
        let outcome = if self.interrupt_pending {
            Outcome::Interrupted
        } else if v["is_error"] == true {
            Outcome::Failed
        } else {
            Outcome::Completed
        };
        self.emit(Event::Finished { outcome })
    }
}

pub(super) fn claude_result_error(result: &Value) -> String {
    let errors = result["errors"]
        .as_array()
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("; ")
        })
        .unwrap_or_default();
    match result["result"].as_str().filter(|text| !text.is_empty()) {
        Some(text) => limited(text),
        None if !errors.is_empty() => limited(&errors),
        None => match result["subtype"].as_str() {
            Some(subtype) => limited(&format!("Claude returned an error ({subtype})")),
            None => "Claude returned an error".into(),
        },
    }
}
/// The mode a successful Claude switch reply confirms, reported like a
/// connect-time mismatch; a reply without a mode confirms the target.
pub(super) fn switch_reply_mode(
    tx: &mpsc::Sender<Event>,
    reply: &Value,
    target: Mode,
) -> Result<Mode, DriverError> {
    let reported = reply
        .pointer("/response/response/mode")
        .and_then(Value::as_str)
        .map(|raw| (claude_reported_mode(raw), limited(raw)));
    confirm_mode(tx, "Claude", target, reported)
}
/// Reply to a Claude control request the driver will not put in front of the
/// user: a permission request outside an active turn is denied, anything else
/// is reported as unsupported. The deny text is shown to the model.
pub(super) fn claude_stray_reply(value: &Value) -> Option<Value> {
    if value.get("type")?.as_str()? != "control_request" {
        return None;
    }
    let id = value.get("request_id")?;
    Some(
        if value.pointer("/request/subtype")?.as_str()? == "can_use_tool" {
            json!({"type":"control_response","response":{"subtype":"success","request_id":id,"response":{"behavior":"deny","message":"Denied by Octet: no turn is waiting for this request"}}})
        } else {
            json!({"type":"control_response","response":{"subtype":"error","request_id":id,"error":"Octet does not support this control request"}})
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stray_permission_denial_says_why() {
        let permission = json!({"type":"control_request","request_id":"r1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{}}});
        let message = claude_stray_reply(&permission).unwrap()["response"]["response"]["message"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(message.contains("no turn is waiting"), "{message}");
    }
}
