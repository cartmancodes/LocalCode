//! Claude Code stream-json protocol: launch arguments, control requests and
//! the frame handler.
use super::{
    limited,
    mode::{claude_mode, claude_permission_args, claude_reported_mode, confirm_mode},
    model_catalog_with,
    protocol::{Core, Phase, Protocol},
    valid_identifier, BoxFuture, Channels, Config, DriverError, Event, ImageAttachment, Limits,
    Mode, ModelInfo, Outcome, Provider,
};
use serde_json::{json, Value};
use std::ffi::OsString;
use tokio::{sync::mpsc, time::Instant};

/// Claude Code's row in the provider table.
pub(super) const PROVIDER: Provider = Provider {
    name: "claude",
    title: "Claude",
    default_binary: "claude",
    offline: false,
    modes: [
        "Claude asks before edits and commands (permission mode default)",
        "File edits proceed; other actions ask (acceptEdits)",
        "Claude's classifier approves or blocks each action (auto)",
        "No permission checks at all (bypassPermissions)",
    ],
    steer: false,
    effort_live: false,
    start,
};

fn start(config: Config, limits: Limits, channels: Channels) -> BoxFuture<Result<(), String>> {
    Box::pin(async move { super::driver::run::<ClaudeProtocol>(config, limits, channels).await })
}

fn launch_args(config: &Config) -> Vec<OsString> {
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
        if config.fork {
            args.push("--fork-session".into());
        }
    }
    if let Some(effort) = &config.effort {
        args.extend(["--effort".into(), effort.into()]);
    }
    args
}

fn answer_wire(wire: &Value, allow: bool) -> Value {
    let response = if allow {
        json!({"behavior":"allow","updatedInput":wire["request"]["input"]})
    } else {
        json!({"behavior":"deny","message":"Denied by Octet user or timeout"})
    };
    json!({
        "type": "control_response",
        "response": {"subtype": "success", "request_id": wire["request_id"], "response": response},
    })
}

/// A Claude mode switch awaiting its control_response.
struct ModeRequest {
    id: String,
    target: Mode,
    deadline: Instant,
}

/// Claude's per-connection state: the model it reports, whether this
/// turn's text is streaming, and mode switches awaiting confirmation.
#[derive(Default)]
pub(super) struct ClaudeProtocol {
    selected_model: String,
    streamed: bool,
    mode_request: Option<ModeRequest>,
    /// Timed-out switches Claude may still confirm; the header must never show
    /// a stricter mode than the vendor is really in.
    late_modes: Vec<(String, Mode)>,
    mode_seq: u64,
}

impl Protocol for ClaudeProtocol {
    fn launch_args(config: &Config) -> Vec<OsString> {
        launch_args(config)
    }

    fn answer(wire: &Value, allow: bool) -> Value {
        answer_wire(wire, allow)
    }

    fn stray_reply(request: &Value) -> Option<Value> {
        stray(request)
    }

    fn mode_change_pending(&self) -> bool {
        self.mode_request.is_some()
    }

    fn deadline(&self) -> Option<Instant> {
        self.mode_request.as_ref().map(|request| request.deadline)
    }

    /// A switch Claude has not confirmed in time is reported, and kept in
    /// case Claude confirms it late.
    async fn on_deadline(&mut self, core: &mut Core) -> Result<(), DriverError> {
        if let Some(request) = self
            .mode_request
            .take_if(|request| request.deadline <= Instant::now())
        {
            let notice = format!(
                "Claude has not confirmed the switch to {}; the header shows {} until it does",
                request.target.label(),
                core.mode.label()
            );
            self.late_modes.push((request.id, request.target));
            core.emit(Event::Notice(notice))?;
            core.emit(Event::ModeChanged(core.mode))?;
        }
        Ok(())
    }

    fn turn_started(&mut self) {
        self.streamed = false;
    }

    async fn initialize(&mut self, core: &mut Core) -> Result<(), DriverError> {
        core.send(json!({"type":"control_request","request_id":"octet-init","request":{"subtype":"initialize"}}))
            .await
    }

    async fn interrupt(&mut self, core: &mut Core) -> Result<(), DriverError> {
        core.send(json!({"type":"control_request","request_id":"octet-interrupt","request":{"subtype":"interrupt"}}))
            .await
    }

    /// Images go inline as base64 blocks, read now; an image that cannot
    /// be read fails the turn.
    async fn send_prompt(
        &mut self,
        core: &mut Core,
        text: &str,
        images: &[ImageAttachment],
    ) -> Result<(), DriverError> {
        let mut content = vec![json!({"type": "text", "text": text})];
        for image in images {
            match image.read_base64().await {
                Ok(data) => content.push(json!({
                    "type": "image",
                    "source": {"type": "base64", "media_type": image.media_type, "data": data},
                })),
                Err(error) => {
                    core.phase = Phase::Idle;
                    core.emit(Event::Error(error.to_string()))?;
                    return core.emit(Event::Finished {
                        outcome: Outcome::Failed,
                    });
                }
            }
        }
        core.send(json!({
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": null,
        }))
        .await
    }

    /// Claude compacts when the user message is `/compact`.
    async fn compact(&mut self, core: &mut Core) -> Result<(), DriverError> {
        self.send_prompt(core, "/compact", &[]).await
    }

    async fn set_mode(&mut self, core: &mut Core, target: Mode) -> Result<(), DriverError> {
        self.mode_seq += 1;
        let id = format!("octet-mode-{}", self.mode_seq);
        let request = json!({"subtype":"set_permission_mode","mode":claude_mode(target)});
        core.send(json!({"type":"control_request","request_id":id,"request":request}))
            .await?;
        self.mode_request = Some(ModeRequest {
            id,
            target,
            deadline: Instant::now() + core.limits.mode_confirm,
        });
        Ok(())
    }

    async fn on_frame(&mut self, core: &mut Core, v: Value) -> Result<(), DriverError> {
        let kind = v["type"].as_str().unwrap_or("");
        if kind == "control_response" && self.control_response(core, &v)? {
            return Ok(());
        }
        if let Some(id) = v["session_id"].as_str() {
            if core.session != id {
                core.session = id.to_owned();
                core.emit(Event::Ready {
                    session: core.session.clone(),
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
                core.emit(Event::ModelSelected(self.selected_model.clone()))?;
            }
        }
        if kind == "control_request" {
            let permission =
                v.pointer("/request/subtype").and_then(Value::as_str) == Some("can_use_tool");
            if permission && core.phase == Phase::InTurn {
                if core.pending.len() >= 8 {
                    return core.send(Self::answer(&v, false)).await;
                }
                let detail = serde_json::to_string_pretty(&v["request"]).unwrap_or_default();
                return core.queue_approval(v, detail).await;
            }
            if let Some(reply) = Self::stray_reply(&v) {
                core.send(reply).await?;
            }
            return Ok(());
        }
        if kind == "control_cancel_request" {
            let ids: Vec<u64> = core
                .pending
                .iter()
                .filter(|(_, pending)| pending.wire["request_id"] == v["request_id"])
                .map(|(id, _)| *id)
                .collect();
            for id in ids {
                core.pending.remove(&id);
                core.emit(Event::ApprovalClosed(id))?;
            }
            core.resume_watchdog();
        }
        if !core.phase.is_running() {
            return Ok(());
        }
        match kind {
            "stream_event" => {
                if let Some(text) = v.pointer("/event/delta/text").and_then(Value::as_str) {
                    self.streamed = true;
                    core.emit(Event::Text(text.into()))?;
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
                            core.emit(Event::Text(text.into()))?;
                        }
                    }
                    if block["type"] == "tool_use" {
                        let name = block["name"].as_str().unwrap_or("tool");
                        core.emit(Event::Tool(format!("{name}\n{}", block["input"])))?;
                    }
                }
                self.streamed = false;
            }
            "result" => self.result(core, &v)?,
            _ => {}
        }
        Ok(())
    }
}

impl ClaudeProtocol {
    /// Handles replies to Octet's own control requests. True when the
    /// frame is fully handled.
    fn control_response(&mut self, core: &mut Core, v: &Value) -> Result<bool, DriverError> {
        let response_id = v.pointer("/response/request_id").and_then(Value::as_str);
        let succeeded = v.pointer("/response/subtype").and_then(Value::as_str) == Some("success");
        if let Some(index) = self
            .late_modes
            .iter()
            .position(|(id, _)| Some(id.as_str()) == response_id)
        {
            let (_, target) = self.late_modes.remove(index);
            if succeeded {
                core.mode = switch_reply_mode(&core.tx, v, target)?;
                core.emit(Event::Notice(format!(
                    "Claude confirmed the switch to {} late",
                    core.mode.label()
                )))?;
                core.emit(Event::ModeChanged(core.mode))?;
            }
            return Ok(true);
        }
        if let Some(request) = self
            .mode_request
            .take_if(|request| Some(request.id.as_str()) == response_id)
        {
            if succeeded {
                core.mode = switch_reply_mode(&core.tx, v, request.target)?;
            } else {
                let error = v.pointer("/response/error").and_then(Value::as_str);
                core.emit(Event::Notice(format!(
                    "Mode change refused by Claude: {}",
                    limited(error.unwrap_or("unknown error"))
                )))?;
            }
            core.emit(Event::ModeChanged(core.mode))?;
            return Ok(true);
        }
        if response_id == Some("octet-init") {
            if !succeeded {
                return Err("Claude initialization failed".into());
            }
            // The phase only moves forward: a repeated reply mid-turn must
            // not end the turn.
            if !core.phase.is_ready() {
                core.phase = Phase::Idle;
            }
            core.emit(Event::Ready {
                session: core.config.resume.clone().unwrap_or_default(),
            })?;
            let reported = v
                .pointer("/response/response/current_permission_mode")
                .and_then(Value::as_str)
                .map(|raw| (claude_reported_mode(raw), limited(raw)));
            core.mode = confirm_mode(&core.tx, PROVIDER.title, core.mode, reported)?;
            core.emit(Event::ModeChanged(core.mode))?;
            core.emit(Event::Models(catalog(&v["response"]["response"]["models"])))?;
        }
        Ok(false)
    }

    fn result(&self, core: &mut Core, v: &Value) -> Result<(), DriverError> {
        let interrupted = core.phase == Phase::Interrupting;
        if v["is_error"].as_bool() != Some(false) && !interrupted {
            core.emit(Event::Error(claude_result_error(v)))?;
        }
        if let Some(cost) = v["total_cost_usd"].as_f64() {
            core.emit(Event::Usage(format!("${cost:.4} session cost")))?;
        }
        core.phase = Phase::Idle;
        core.close_all_pending()?;
        let outcome = if interrupted {
            Outcome::Interrupted
        } else if v["is_error"] == true {
            Outcome::Failed
        } else {
            Outcome::Completed
        };
        core.emit(Event::Finished { outcome })
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
    confirm_mode(tx, PROVIDER.title, target, reported)
}
/// The Claude catalog: `value` selects, `resolvedModel` is the full ID.
fn catalog(value: &Value) -> Vec<ModelInfo> {
    model_catalog_with(value, "value", "resolvedModel")
}

/// Reply to a Claude control request the driver will not put in front of the
/// user; see `stray`.
pub fn claude_stray_reply(value: &Value) -> Option<Value> {
    ClaudeProtocol::stray_reply(value)
}

/// Reply to a Claude control request the driver will not put in front of the
/// user: a permission request outside an active turn is denied, anything else
/// is reported as unsupported. The deny text is shown to the model.
fn stray(value: &Value) -> Option<Value> {
    if value.get("type")?.as_str()? != "control_request" {
        return None;
    }
    let id = value.get("request_id")?;
    let response = if value.pointer("/request/subtype")?.as_str()? == "can_use_tool" {
        let deny = json!({
            "behavior": "deny",
            "message": "Denied by Octet: no turn is waiting for this request",
        });
        json!({"subtype": "success", "request_id": id, "response": deny})
    } else {
        json!({
            "subtype": "error",
            "request_id": id,
            "error": "Octet does not support this control request",
        })
    };
    Some(json!({"type": "control_response", "response": response}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stray_permission_denial_says_why() {
        let request = json!({"subtype": "can_use_tool", "tool_name": "Bash", "input": {}});
        let permission = json!({"type": "control_request", "request_id": "r1", "request": request});
        let message = claude_stray_reply(&permission).unwrap()["response"]["response"]["message"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(message.contains("no turn is waiting"), "{message}");
    }
}
