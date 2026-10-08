//! Claude Code stream-json protocol: launch arguments, control requests and
//! the frame handler.
use super::{
    BoxFuture, Channels, Config, DriverError, Event, ImageAttachment, Limits, Mode, ModelInfo,
    Outcome, Provider, check_inline, deadline_after, limited,
    mode::confirm_mode,
    model_catalog_with,
    protocol::{Core, Phase, Protocol},
    push_bounded, valid_identifier,
};
use serde_json::{Value, json};
use std::{collections::VecDeque, ffi::OsString, time::Duration};
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
    inline_images: true,
    effort_live: false,
    efforts: &["low", "medium", "high", "xhigh", "max"],
    // It takes about 0.9 s to exit, saving its session; a kill could cut that.
    shutdown_grace: Duration::from_millis(1500),
    launch_args: <ClaudeProtocol as Protocol>::launch_args,
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
    if !allow {
        return deny_wire(wire, "Denied by Octet");
    }
    let response = json!({"behavior":"allow","updatedInput":wire["request"]["input"]});
    json!({
        "type": "control_response",
        "response": {"subtype": "success", "request_id": wire["request_id"], "response": response},
    })
}

/// Claude passes the message on to the model, which can then tell a refusal
/// from a timeout or the cap, and need not retry blindly.
fn deny_wire(wire: &Value, reason: &str) -> Value {
    let response = json!({"behavior":"deny","message":reason});
    json!({
        "type": "control_response",
        "response": {"subtype": "success", "request_id": wire["request_id"], "response": response},
    })
}

/// A Claude mode switch awaiting its `control_response`.
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
    late_modes: VecDeque<(String, Mode)>,
    mode_seq: u64,
    /// A session ID that failed `valid_identifier` was reported once.
    odd_session_noted: bool,
}

impl Protocol for ClaudeProtocol {
    fn launch_args(config: &Config) -> Vec<OsString> {
        launch_args(config)
    }

    fn answer(wire: &Value, allow: bool) -> Value {
        answer_wire(wire, allow)
    }

    fn deny(wire: &Value, reason: &str) -> Value {
        deny_wire(wire, reason)
    }

    fn mode_change_pending(&self) -> bool {
        self.mode_request.is_some()
    }

    fn deadline(&self) -> Option<Instant> {
        self.mode_request.as_ref().map(|request| request.deadline)
    }

    /// A switch Claude has not confirmed in time is reported, and kept in
    /// case Claude confirms it late. Nothing here waits, so the work is done
    /// at once and handed back as a ready future.
    fn on_deadline(
        &mut self,
        core: &mut Core,
    ) -> impl Future<Output = Result<(), DriverError>> + Send {
        std::future::ready(self.report_late_mode(core))
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
        let mut encoded = Vec::with_capacity(images.len());
        for image in images {
            // A file swapped for a FIFO, or on a stalled mount, must not hold
            // the session: cancel and stop wait while this runs.
            match tokio::time::timeout(core.limits.image_read, image.read_base64()).await {
                Ok(Ok(data)) => encoded.push((image, data)),
                Ok(Err(error)) => return fail_turn(core, error.to_string()),
                Err(_) => {
                    return fail_turn(
                        core,
                        format!(
                            "Reading {} took longer than {}; the prompt was not sent",
                            image.name,
                            super::driver::seconds(core.limits.image_read)
                        ),
                    );
                }
            }
        }
        // Files can change after they were attached; check what is sent.
        let sizes = encoded
            .iter()
            .map(|(image, data)| (image.name.as_str(), data.len() as u64));
        if let Err(error) = check_inline(PROVIDER.title, sizes) {
            return fail_turn(core, error.to_string());
        }
        for (image, data) in encoded {
            content.push(json!({
                "type": "image",
                "source": {"type": "base64", "media_type": image.media_type, "data": data},
            }));
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
            deadline: deadline_after(core.limits.mode_confirm),
        });
        Ok(())
    }

    async fn on_frame(&mut self, core: &mut Core, v: Value) -> Result<(), DriverError> {
        let kind = v["type"].as_str().unwrap_or("");
        if kind == "control_response" && self.control_response(core, &v)? {
            return Ok(());
        }
        if let Some(id) = v["session_id"].as_str()
            && core.session != id
        {
            // The ID goes into the journal and later `--resume` arguments.
            if valid_identifier(id) {
                core.session = id.to_owned();
                core.emit(Event::Ready {
                    session: core.session.clone(),
                })?;
            } else if !self.odd_session_noted {
                self.odd_session_noted = true;
                core.emit(Event::Notice(
                    "Claude reported a session ID Octet cannot use; it was ignored".into(),
                ))?;
            }
        }
        // A subagent's (Task tool) messages carry the tool use that started
        // it: its text is not the reply and its model is not the session's.
        let subagent = !v["parent_tool_use_id"].is_null();
        let actual = match kind {
            _ if subagent => None,
            "system" if v["subtype"] == "init" => v["model"].as_str(),
            "assistant" => v["message"]["model"].as_str(),
            "stream_event" => v["event"]["message"]["model"].as_str(),
            _ => None,
        };
        if let Some(model) = actual.filter(|model| valid_identifier(model))
            && self.selected_model != model
        {
            self.selected_model = model.into();
            core.emit(Event::ModelSelected(self.selected_model.clone()))?;
        }
        if kind == "control_request" {
            let permission =
                v.pointer("/request/subtype").and_then(Value::as_str) == Some("can_use_tool");
            if permission && core.phase == Phase::InTurn {
                let detail = serde_json::to_string_pretty(&v["request"]).unwrap_or_default();
                return core.queue_approval(v, detail).await;
            }
            if let Some(reply) = stray(&v) {
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
                if let Some(text) = v.pointer("/event/delta/text").and_then(Value::as_str)
                    && !subagent
                {
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
                    if !self.streamed
                        && !subagent
                        && block["type"] == "text"
                        && let Some(text) = block["text"].as_str()
                    {
                        core.emit(Event::Text(text.into()))?;
                    }
                    if block["type"] == "tool_use" {
                        let name = block["name"].as_str().unwrap_or("tool");
                        core.emit(Event::Tool(format!("{name}\n{}", block["input"])))?;
                    }
                }
                if !subagent {
                    self.streamed = false;
                }
            }
            "result" => Self::result(core, &v)?,
            _ => {}
        }
        Ok(())
    }
}

impl ClaudeProtocol {
    /// Reports a mode switch Claude did not confirm in time.
    fn report_late_mode(&mut self, core: &Core) -> Result<(), DriverError> {
        if let Some(request) = self
            .mode_request
            .take_if(|request| request.deadline <= Instant::now())
        {
            let notice = format!(
                "Claude has not confirmed the switch to {}; the header shows {} until it does",
                request.target.label(),
                core.mode.label()
            );
            // Kept bounded: a vendor that never answers cannot grow it.
            push_bounded(&mut self.late_modes, (request.id, request.target), 8);
            core.emit(Event::Notice(notice))?;
            core.emit(Event::ModeChanged(core.mode))?;
        }
        Ok(())
    }
    /// Handles replies to Octet's own control requests. True when the
    /// frame is fully handled.
    fn control_response(&mut self, core: &mut Core, v: &Value) -> Result<bool, DriverError> {
        let response_id = v.pointer("/response/request_id").and_then(Value::as_str);
        let succeeded = v.pointer("/response/subtype").and_then(Value::as_str) == Some("success");
        if let Some((_, target)) = self
            .late_modes
            .iter()
            .position(|(id, _)| Some(id.as_str()) == response_id)
            .and_then(|index| self.late_modes.remove(index))
        {
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
            // A fork is a new session that Claude names with its first turn;
            // until then there is none to resume.
            let session = if core.config.fork {
                String::new()
            } else {
                core.config.resume.clone().unwrap_or_default()
            };
            core.emit(Event::Ready { session })?;
            let reported = v
                .pointer("/response/response/current_permission_mode")
                .and_then(Value::as_str)
                .map(|raw| (claude_reported_mode(raw), limited(raw)));
            core.adopt_mode(reported)?;
            core.emit(Event::Models(catalog(&v["response"]["response"]["models"])))?;
        }
        Ok(false)
    }

    /// The turn's end. A result that does not say `is_error: false` failed.
    fn result(core: &mut Core, v: &Value) -> Result<(), DriverError> {
        if let Some(cost) = v["total_cost_usd"].as_f64() {
            core.emit(Event::Usage(format!("${cost:.4} session cost")))?;
        }
        let failed = v["is_error"].as_bool() != Some(false);
        if core.phase == Phase::Interrupting {
            core.finish_turn(Outcome::Interrupted, None)
        } else if failed {
            core.finish_turn(Outcome::Failed, Some(claude_result_error(v)))
        } else {
            core.finish_turn(Outcome::Completed, None)
        }
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
    stray(value)
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

/// Ends a turn that could not be sent, keeping the session.
fn fail_turn(core: &mut Core, message: String) -> Result<(), DriverError> {
    core.finish_turn(Outcome::Failed, Some(message))
}

pub(super) fn claude_mode(mode: Mode) -> &'static str {
    match mode {
        Mode::Ask => "default",
        Mode::AcceptEdits => "acceptEdits",
        Mode::Auto => "auto",
        Mode::FullAccess => "bypassPermissions",
    }
}
/// Claude refuses a live switch to bypassPermissions unless launched with this
/// allowance, so full access is only ever applied at launch.
pub(crate) fn claude_permission_args(mode: Mode) -> Vec<&'static str> {
    let mut args = vec!["--permission-mode", claude_mode(mode)];
    if mode == Mode::FullAccess {
        args.push("--allow-dangerously-skip-permissions");
    }
    args
}
/// The mode Claude reports (`current_permission_mode`, or a switch's reply).
/// None means the value is not one of Octet's modes.
pub(crate) fn claude_reported_mode(raw: &str) -> Option<Mode> {
    Mode::ALL.into_iter().find(|mode| claude_mode(*mode) == raw)
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
    #[test]
    fn claude_mapping_matches_spec_table() {
        assert_eq!(
            claude_permission_args(Mode::Ask),
            ["--permission-mode", "default"]
        );
        assert_eq!(
            claude_permission_args(Mode::AcceptEdits),
            ["--permission-mode", "acceptEdits"]
        );
        assert_eq!(
            claude_permission_args(Mode::Auto),
            ["--permission-mode", "auto"]
        );
        assert_eq!(
            claude_permission_args(Mode::FullAccess),
            [
                "--permission-mode",
                "bypassPermissions",
                "--allow-dangerously-skip-permissions"
            ]
        );
    }
    #[test]
    fn reported_modes_map_back_or_stay_unmapped() {
        for mode in Mode::ALL {
            assert_eq!(claude_reported_mode(claude_mode(mode)), Some(mode));
        }
        assert_eq!(claude_reported_mode("plan"), None);
    }
    #[test]
    fn stray_request_replies_use_production_wording() {
        let request = json!({"subtype": "can_use_tool", "tool_name": "Bash", "input": {}});
        let permission = json!({"type": "control_request", "request_id": "r1", "request": request});
        let unknown = json!({"type":"control_request","request_id":"r2","request":{"subtype":"hook_callback"}});
        for reply in [
            claude_stray_reply(&permission).unwrap(),
            claude_stray_reply(&unknown).unwrap(),
        ] {
            assert!(!reply.to_string().contains("fixture"), "{reply}");
        }
        assert_eq!(
            claude_stray_reply(&permission).unwrap()["response"]["response"]["behavior"],
            "deny"
        );
        assert_eq!(
            claude_stray_reply(&unknown).unwrap()["response"]["subtype"],
            "error"
        );
    }
    #[test]
    fn a_result_error_names_its_subtype_when_it_has_no_text() {
        assert_eq!(
            claude_result_error(&json!({"is_error":true,"subtype":"error_max_turns"})),
            "Claude returned an error (error_max_turns)"
        );
    }
}
