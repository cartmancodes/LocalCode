//! Interactive vendor driver. A single owner correlates wire events while
//! cancellation and shutdown use independent watch channels.
use lc_proc::{Process, ProcessConfig};
use serde_json::{json, Value};
use std::{collections::HashMap, ffi::OsString, path::PathBuf, time::Duration};
use tokio::{
    sync::{mpsc, watch},
    time::{timeout, Instant},
};

pub const PROMPT_LIMIT: usize = 64 * 1024;
const EVENT_CAPACITY: usize = 128;
const EVENT_BYTES: usize = 32 * 1024;

/// Driver time limits. The defaults suit real vendors; tests shorten them.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Spawn to a usable session.
    pub connect: Duration,
    /// Longest silence from the vendor inside a turn. Not a cap on turn length.
    pub turn_idle: Duration,
    /// Interrupt request to the turn's terminal event.
    pub interrupt: Duration,
    /// Unanswered approval before it is denied.
    pub approval: Duration,
    /// Unanswered Claude mode switch before it is reported as unconfirmed.
    pub mode_confirm: Duration,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(30),
            turn_idle: Duration::from_secs(600),
            interrupt: Duration::from_secs(10),
            approval: Duration::from_secs(120),
            mode_confirm: Duration::from_secs(10),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Config {
    pub engine: String,
    pub binary: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub resume: Option<String>,
    pub mode: Mode,
}
/// Provider-neutral permission mode. The vendor mapping lives only in the
/// functions below; `Auto` delegates to each vendor's own reviewer and
/// LocalCode never answers a vendor approval by itself.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Mode {
    #[default]
    Ask,
    AcceptEdits,
    Auto,
    FullAccess,
}
impl Mode {
    pub const ALL: [Mode; 4] = [Mode::Ask, Mode::AcceptEdits, Mode::Auto, Mode::FullAccess];
    pub fn parse(value: &str) -> Option<Mode> {
        Self::ALL.into_iter().find(|mode| mode.label() == value)
    }
    pub fn label(self) -> &'static str {
        match self {
            Mode::Ask => "ask",
            Mode::AcceptEdits => "accept-edits",
            Mode::Auto => "auto",
            Mode::FullAccess => "full-access",
        }
    }
    /// Shift+Tab order. Full access is never reached by cycling.
    pub fn cycle(self) -> Mode {
        match self {
            Mode::Ask => Mode::AcceptEdits,
            Mode::AcceptEdits => Mode::Auto,
            Mode::Auto => Mode::Ask,
            Mode::FullAccess => Mode::FullAccess,
        }
    }
    pub fn describe(self, engine: &str) -> &'static str {
        match (engine, self) {
            ("claude", Mode::Ask) => "Claude asks before edits and commands (permission mode default)",
            ("claude", Mode::AcceptEdits) => "File edits proceed; other actions ask (acceptEdits)",
            ("claude", Mode::Auto) => "Claude's classifier approves or blocks each action (auto)",
            ("claude", Mode::FullAccess) => "No permission checks at all (bypassPermissions)",
            ("codex", Mode::Ask) => "Workspace sandbox; untrusted commands ask (untrusted)",
            ("codex", Mode::AcceptEdits) => "Workspace sandbox; asks only to escalate (on-request). Codex has no edits-only mode",
            ("codex", Mode::Auto) => "Workspace sandbox; Codex's auto-review agent decides escalations (auto_review)",
            ("codex", Mode::FullAccess) => "No sandbox; never asks (danger-full-access)",
            (_, Mode::Ask | Mode::AcceptEdits) => "Offline demo: /approval-demo shows the dialog",
            (_, Mode::Auto | Mode::FullAccess) => "Offline demo: /approval-demo is allowed without a dialog",
        }
    }
}
fn claude_mode(mode: Mode) -> &'static str {
    match mode {
        Mode::Ask => "default",
        Mode::AcceptEdits => "acceptEdits",
        Mode::Auto => "auto",
        Mode::FullAccess => "bypassPermissions",
    }
}
/// Claude refuses a live switch to bypassPermissions unless launched with this
/// allowance, so full access is only ever applied at launch.
pub fn claude_permission_args(mode: Mode) -> Vec<&'static str> {
    let mut args = vec!["--permission-mode", claude_mode(mode)];
    if mode == Mode::FullAccess {
        args.push("--allow-dangerously-skip-permissions");
    }
    args
}
pub fn codex_thread_params(mode: Mode) -> Value {
    let (sandbox, policy, reviewer) = match mode {
        Mode::Ask => ("workspace-write", "untrusted", "user"),
        Mode::AcceptEdits => ("workspace-write", "on-request", "user"),
        Mode::Auto => ("workspace-write", "on-request", "auto_review"),
        Mode::FullAccess => ("danger-full-access", "never", "user"),
    };
    json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer})
}
/// The mode Claude reports (`current_permission_mode`, or a switch's reply).
/// None means the value is not one of LocalCode's modes.
pub fn claude_reported_mode(raw: &str) -> Option<Mode> {
    Mode::ALL.into_iter().find(|mode| claude_mode(*mode) == raw)
}
/// The mode a Codex thread reply echoes. Outer None: the reply carries no
/// policy (older CLI). Inner None: a policy LocalCode does not map, with the
/// raw description for the user.
pub fn codex_reported(result: &Value) -> Option<(Option<Mode>, String)> {
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
pub fn codex_turn_overrides(mode: Mode) -> Value {
    let params = codex_thread_params(mode);
    json!({"approvalPolicy":params["approvalPolicy"],"approvalsReviewer":params["approvalsReviewer"]})
}
/// Provider-owned picker metadata; selection and resolved ID can differ.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelInfo {
    pub selection: String,
    pub id: Option<String>,
    pub name: String,
    pub description: String,
}

fn model_catalog(value: &Value, claude: bool) -> Vec<ModelInfo> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .take(256)
        .filter_map(|v| {
            let selection = v[if claude { "value" } else { "model" }].as_str()?;
            // Bound untrusted metadata without silently truncating model identifiers.
            if selection.is_empty()
                || selection.len() > 256
                || selection.chars().any(char::is_control)
            {
                return None;
            }
            let id = v[if claude { "resolvedModel" } else { "model" }]
                .as_str()
                .filter(|id| !id.is_empty() && id.len() <= 256 && !id.chars().any(char::is_control))
                .map(str::to_owned);
            Some(ModelInfo {
                selection: selection.into(),
                id,
                name: v["displayName"]
                    .as_str()
                    .filter(|s| s.len() <= 512)
                    .unwrap_or(selection)
                    .into(),
                description: v["description"]
                    .as_str()
                    .filter(|s| s.len() <= 2048)
                    .unwrap_or("")
                    .into(),
            })
        })
        .collect()
}

#[derive(Clone, Debug)]
pub enum Event {
    Ready { session: String },
    Models(Vec<ModelInfo>),
    ModelSelected(String),
    ModeChanged(Mode),
    User(String),
    Started,
    Text(String),
    Tool(String),
    Approval { id: u64, detail: String },
    ApprovalClosed(u64),
    Usage(String),
    Finished { outcome: String },
    Notice(String),
    Error(String),
    Stopped,
}
#[derive(Debug)]
pub enum Command {
    Prompt(String),
    PromptWithDisplay { wire: String, display: String },
    Answer { id: u64, allow: bool },
    SetMode(Mode),
}
fn prompt_parts(command: Command) -> (String, String) {
    match command {
        Command::Prompt(text) => (text.clone(), text),
        Command::PromptWithDisplay { wire, display } => (wire, display),
        Command::Answer { .. } | Command::SetMode(_) => {
            unreachable!("only prompt commands reach prompt_parts")
        }
    }
}
#[derive(Clone)]
pub struct Handle {
    commands: mpsc::Sender<Command>,
    interrupt: watch::Sender<u64>,
    stop: watch::Sender<bool>,
}
impl Handle {
    pub fn send(&self, command: Command) -> Result<(), String> {
        if matches!(&command, Command::Prompt(text) if text.len() > PROMPT_LIMIT)
            || matches!(&command, Command::PromptWithDisplay { wire, display } if wire.len()>PROMPT_LIMIT || display.len()>PROMPT_LIMIT)
        {
            return Err("Prompt exceeds the 64 KiB limit".into());
        }
        self.commands
            .try_send(command)
            .map_err(|_| "Session is busy or closed; try again".into())
    }
    pub fn interrupt(&self) {
        self.interrupt.send_modify(|n| *n = n.wrapping_add(1));
    }
    pub fn shutdown(&self) {
        let _ = self.stop.send(true);
    }
}
// Output is split before enqueueing. A stalled consumer fails the session rather
// than blocking the control path or silently dropping semantic output.
fn emit(tx: &mpsc::Sender<Event>, event: Event) -> Result<(), String> {
    let text = match event {
        Event::Text(text) => text,
        // Tool detail is a preview: megabytes of command output must not flood
        // the queue, so it is cut to one event.
        Event::Tool(text) => {
            return tx
                .try_send(Event::Tool(limited(&text)))
                .map_err(|_| "Output consumer overloaded; session stopped".into())
        }
        event => {
            return tx
                .try_send(event)
                .map_err(|_| "Output consumer overloaded; session stopped".into())
        }
    };
    let mut remaining = text.as_str();
    while !remaining.is_empty() {
        let mut end = remaining.len().min(EVENT_BYTES);
        while !remaining.is_char_boundary(end) {
            end -= 1;
        }
        let chunk = remaining[..end].to_owned();
        tx.try_send(Event::Text(chunk))
            .map_err(|_| "Output consumer overloaded; session stopped".to_string())?;
        remaining = &remaining[end..];
    }
    Ok(())
}

fn limited(text: &str) -> String {
    let mut end = text.len().min(EVENT_BYTES);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    if end == text.len() {
        text.to_owned()
    } else {
        format!("{}\n[detail exceeds preview limit]", &text[..end])
    }
}
async fn send(process: &Process, value: Value) -> Result<(), String> {
    timeout(Duration::from_secs(3), process.sender().send(&value))
        .await
        .map_err(|_| "Vendor stdin is unresponsive".to_string())?
        .map_err(|e| e.to_string())
}
pub fn spawn(config: Config) -> (Handle, mpsc::Receiver<Event>, tokio::task::JoinHandle<()>) {
    spawn_with_limits(config, Limits::default())
}
pub fn spawn_with_limits(
    config: Config,
    limits: Limits,
) -> (Handle, mpsc::Receiver<Event>, tokio::task::JoinHandle<()>) {
    let (commands, rx) = mpsc::channel(16);
    let (interrupt, cancel) = watch::channel(0);
    let (stop, stopping) = watch::channel(false);
    let (events, output) = mpsc::channel(EVENT_CAPACITY);
    let handle = Handle {
        commands,
        interrupt,
        stop,
    };
    let task = tokio::spawn(async move {
        let result = if config.engine == "demo" {
            demo(config.mode, rx, cancel, stopping, &events).await
        } else {
            vendor(config, limits, rx, cancel, stopping, &events).await
        };
        if let Err(error) = result {
            // After the driver stops, bounded waiting can deliver the final error.
            let _ = timeout(Duration::from_secs(2), events.send(Event::Error(error))).await;
        }
        let _ = timeout(Duration::from_secs(2), events.send(Event::Stopped)).await;
    });
    (handle, output, task)
}
struct Pending {
    wire: Value,
    deadline: Instant,
}
async fn vendor(
    config: Config,
    limits: Limits,
    mut commands: mpsc::Receiver<Command>,
    mut cancel: watch::Receiver<u64>,
    mut stopping: watch::Receiver<bool>,
    tx: &mpsc::Sender<Event>,
) -> Result<(), String> {
    let claude = config.engine == "claude";
    let mut args: Vec<OsString> = if claude {
        [
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
        .collect()
    } else {
        vec!["app-server".into()]
    };
    if claude {
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
    }
    let mut process = Process::spawn(ProcessConfig {
        executable: config.binary,
        args,
        cwd: Some(config.cwd.clone()),
        max_frame_bytes: 8 * 1024 * 1024,
        queue_bytes: 16 * 1024 * 1024,
        stderr_bytes: 4096,
        shutdown_grace: Duration::from_millis(150),
        term_grace: Duration::from_millis(250),
    })
    .await
    .map_err(|e| {
        format!(
            "Cannot start {}: {e}. Install the CLI and sign in first.",
            config.engine
        )
    })?;
    let result = async {
        if claude { send(&process, json!({"type":"control_request","request_id":"lc-init","request":{"subtype":"initialize"}})).await?; }
        else { send(&process, json!({"id":1,"method":"initialize","params":{"clientInfo":{"name":"localcode","version":"0.1.0"},"capabilities":{"experimentalApi":true}}})).await?; }
        let mut catalog = Vec::new();
        let mut catalog_pages = 0usize;
        let mut catalog_cursors = std::collections::HashSet::new();
        let mut selected_model = String::new();
        let mut initialized = false;
        let mut ready = false;
        let mut running = false;
        let mut session = String::new();
        let mut mode = config.mode;
        let mut mode_request: Option<(String, Mode, Instant)> = None;
        // A request that timed out; Claude may still confirm it, and the header
        // must never show a stricter mode than the vendor is really in.
        let mut late_modes: Vec<(String, Mode)> = Vec::new();
        let mut mode_seq = 0u64;
        let mut turn: Option<String> = None;
        let mut request_id = 10u64;
        let mut start_request = None;
        let mut interrupt_request = None;
        let mut interrupt_pending = false;
        let mut streamed = false;
        let mut text_items = std::collections::HashSet::new();
        let mut pending: HashMap<u64, Pending> = HashMap::new();
        let mut deadline = Instant::now() + limits.connect;
        loop {
            // The connect/turn deadline is stale when idle; counting it then would
            // make every idle timer fire immediately and spin.
            let next_deadline = pending.values().map(|p| p.deadline).chain(mode_request.as_ref().map(|r| r.2)).chain((!ready || running).then_some(deadline)).min().unwrap_or(deadline);
            tokio::select! {
                biased;
                _ = stopping.changed() => break,
                changed = cancel.changed() => {
                    if changed.is_err() { break; }
                    if !ready { return Err("Connection cancelled".into()); }
                    if running {
                        interrupt_pending = true;
                        if claude {
                            send(&process,json!({"type":"control_request","request_id":"lc-interrupt","request":{"subtype":"interrupt"}})).await?;
                        } else if let Some(id) = &turn {
                            request_id += 1;
                            interrupt_request = Some(request_id);
                            send(&process,json!({"id":request_id,"method":"turn/interrupt","params":{"threadId":session,"turnId":id}})).await?;
                        }
                        deadline = Instant::now() + limits.interrupt;
                        for (id,p) in pending.drain() { send(&process, answer(claude,&p.wire,false)).await?; emit(tx,Event::ApprovalClosed(id))?; }
                    }
                },
                _ = tokio::time::sleep_until(next_deadline), if !ready || running || !pending.is_empty() || mode_request.is_some() => {
                    // The connect/turn deadline only applies while connecting or in a turn;
                    // when idle it is stale and other timers can wake this branch.
                    if (!ready || running) && Instant::now() >= deadline {
                        return Err(if !ready { format!("Vendor did not finish connecting within {}; session stopped.",seconds(limits.connect)) }
                            else if interrupt_pending { format!("Vendor did not stop within {} of the interrupt; session stopped. Resume using the session ID.",seconds(limits.interrupt)) }
                            else { format!("Vendor sent nothing for {}; session stopped. Resume using the session ID.",seconds(limits.turn_idle)) });
                    }
                    let expired: Vec<u64> = pending.iter().filter(|(_,p)|p.deadline <= Instant::now()).map(|(id,_)|*id).collect();
                    for id in expired { let p=pending.remove(&id).unwrap(); send(&process,answer(claude,&p.wire,false)).await?; emit(tx,Event::ApprovalClosed(id))?; emit(tx,Event::Notice("Approval timed out and was denied".into()))?; }
                    if let Some((id,target,_))=mode_request.take_if(|r| r.2<=Instant::now()) {
                        late_modes.push((id,target));
                        emit(tx,Event::Notice(format!("Claude has not confirmed the switch to {}; the header shows {} until it does",target.label(),mode.label())))?;
                        emit(tx,Event::ModeChanged(mode))?;
                    }
                },
                command = commands.recv() => match command {
                    None => break,
                    Some(command @ (Command::Prompt(_) | Command::PromptWithDisplay { .. })) => {
                        let (text,display)=prompt_parts(command);
                        if !ready || running { emit(tx,Event::Notice("Wait for the current operation, or cancel it first".into()))?; continue; }
                        running=true; turn=None; streamed=false; text_items.clear(); interrupt_pending=false;
                        deadline=Instant::now()+limits.turn_idle;
                        emit(tx,Event::User(display))?;
                        emit(tx,Event::Started)?;
                        if claude { send(&process,json!({"type":"user","message":{"role":"user","content":[{"type":"text","text":text}]},"parent_tool_use_id":null})).await?; }
                        else {
                            request_id+=1; start_request=Some(request_id);
                            let mut params=json!({"threadId":session,"input":[{"type":"text","text":text}]});
                            if let Some(model)=&config.model { params["model"]=json!(model); }
                            if let (Some(target),Value::Object(extra))=(params.as_object_mut(),codex_turn_overrides(mode)) { target.extend(extra); }
                            send(&process,json!({"id":request_id,"method":"turn/start","params":params})).await?;
                        }
                    },
                    Some(Command::Answer{id,allow}) => {
                        if let Some(p)=pending.remove(&id) { send(&process,answer(claude,&p.wire,allow)).await?; emit(tx,Event::ApprovalClosed(id))?; }
                        else { emit(tx,Event::Notice("That approval is no longer active".into()))?; }
                    }
                    Some(Command::SetMode(target)) => {
                        // Refusals re-emit the current mode so the TUI clears its pending indicator.
                        if target==Mode::FullAccess || mode==Mode::FullAccess { emit(tx,Event::Notice("Full access is changed by reconnecting; use /mode".into()))?; emit(tx,Event::ModeChanged(mode))?; }
                        else if !ready { emit(tx,Event::Notice("Wait for the connection before changing modes".into()))?; emit(tx,Event::ModeChanged(mode))?; }
                        else if mode_request.is_some() { emit(tx,Event::Notice("A mode change is already pending".into()))?; }
                        else if target==mode { emit(tx,Event::ModeChanged(mode))?; }
                        else if claude {
                            mode_seq+=1;
                            let id=format!("lc-mode-{mode_seq}");
                            send(&process,json!({"type":"control_request","request_id":id,"request":{"subtype":"set_permission_mode","mode":claude_mode(target)}})).await?;
                            mode_request=Some((id,target,Instant::now()+limits.mode_confirm));
                        } else {
                            mode=target;
                            emit(tx,Event::ModeChanged(mode))?;
                            if running { emit(tx,Event::Notice("Mode applies from the next turn".into()))?; }
                        }
                    }
                },
                frame=process.next_frame() => {
                    let v=frame.map_err(|e|e.to_string())?.ok_or("Vendor disconnected. Check its login and installation.")?;
                    // The turn limit is a silence watchdog, not a cap on turn length.
                    if running && !interrupt_pending { deadline=Instant::now()+limits.turn_idle; }
                    if claude {
                        let kind=v["type"].as_str().unwrap_or("");
                        let response_id=v.pointer("/response/request_id").and_then(Value::as_str);
                        if let Some(index)=late_modes.iter().position(|(id,_)| kind=="control_response" && Some(id.as_str())==response_id) {
                            let (_,target)=late_modes.remove(index);
                            if v.pointer("/response/subtype").and_then(Value::as_str)==Some("success") {
                                mode=switch_reply_mode(tx,&v,target)?;
                                emit(tx,Event::Notice(format!("Claude confirmed the switch to {} late",mode.label())))?;
                                emit(tx,Event::ModeChanged(mode))?;
                            }
                            continue;
                        }
                        if kind=="control_response" && mode_request.as_ref().is_some_and(|(id,_,_)| Some(id.as_str())==response_id) {
                            let (_,target,_)=mode_request.take().unwrap();
                            if v.pointer("/response/subtype").and_then(Value::as_str)==Some("success") { mode=switch_reply_mode(tx,&v,target)?; }
                            else { emit(tx,Event::Notice(format!("Mode change refused by Claude: {}",limited(v.pointer("/response/error").and_then(Value::as_str).unwrap_or("unknown error")))))?; }
                            emit(tx,Event::ModeChanged(mode))?;
                            continue;
                        }
                        if kind=="control_response" && v.pointer("/response/request_id").and_then(Value::as_str)==Some("lc-init") {
                            if v.pointer("/response/subtype").and_then(Value::as_str)!=Some("success") { return Err("Claude initialization failed".into()); }
                            initialized=true; ready=true; emit(tx,Event::Ready{session:config.resume.clone().unwrap_or_default()})?;
                            let reported=v.pointer("/response/response/current_permission_mode").and_then(Value::as_str).map(|raw|(claude_reported_mode(raw),limited(raw)));
                            mode=confirm_mode(tx,"Claude",mode,reported)?; emit(tx,Event::ModeChanged(mode))?;
                            emit(tx,Event::Models(model_catalog(&v["response"]["response"]["models"],true)))?;
                        }
                        if let Some(id)=v["session_id"].as_str() { if session!=id { session=id.to_owned(); emit(tx,Event::Ready{session:session.clone()})?; } }
                        let actual = if kind=="system" && v["subtype"]=="init" {v["model"].as_str()}
                            else if kind=="assistant" {v["message"]["model"].as_str()}
                            else if kind=="stream_event" {v["event"]["message"]["model"].as_str()} else {None};
                        if let Some(model)=actual.filter(|m| !m.is_empty() && m.len()<=256 && !m.chars().any(char::is_control)) {
                            if selected_model!=model {selected_model=model.into();emit(tx,Event::ModelSelected(selected_model.clone()))?;}
                        }
                        if kind=="control_request" {
                            if v.pointer("/request/subtype").and_then(Value::as_str)==Some("can_use_tool") && running && !interrupt_pending {
                                if pending.len()>=8 { send(&process,answer(true,&v,false)).await?; continue; }
                                let detail=serde_json::to_string_pretty(&v["request"]).unwrap_or_default();
                                if detail.len()>EVENT_BYTES {send(&process,answer(true,&v,false)).await?;emit(tx,Event::Notice("Oversized approval denied: cannot show the complete request".into()))?;continue;}
                                request_id+=1;
                                emit(tx,Event::Approval{id:request_id,detail})?;
                                pending.insert(request_id,Pending{wire:v,deadline:Instant::now()+limits.approval});
                            } else if let Some(reply)=claude_stray_reply(&v) { send(&process,reply).await?; }
                            continue;
                        }
                        if kind=="control_cancel_request" {
                            let ids:Vec<_>=pending.iter().filter(|(_,p)|p.wire["request_id"]==v["request_id"]).map(|(id,_)|*id).collect();
                            for id in ids { pending.remove(&id); emit(tx,Event::ApprovalClosed(id))?; }
                        }
                        if !running { continue; }
                        if kind=="stream_event" {
                            if let Some(text)=v.pointer("/event/delta/text").and_then(Value::as_str) { streamed=true; emit(tx,Event::Text(text.into()))?; }
                        }
                        if kind=="assistant" {
                            if let Some(blocks)=v.pointer("/message/content").and_then(Value::as_array) {
                                for block in blocks {
                                    if !streamed && block["type"]=="text" { if let Some(text)=block["text"].as_str(){emit(tx,Event::Text(text.into()))?;} }
                                    if block["type"]=="tool_use" { emit(tx,Event::Tool(format!("{}\n{}",block["name"].as_str().unwrap_or("tool"),block["input"])))?; }
                                }
                            }
                            streamed=false;
                        }
                        if kind=="result" {
                            if v["is_error"].as_bool()!=Some(false) && !interrupt_pending { emit(tx,Event::Error(claude_result_error(&v)))?; }
                            if let Some(cost)=v["total_cost_usd"].as_f64() { emit(tx,Event::Usage(format!("${cost:.4} session cost")))?; }
                            running=false;
                            for (id,_) in pending.drain(){emit(tx,Event::ApprovalClosed(id))?;}
                            emit(tx,Event::Finished{outcome:if interrupt_pending {"interrupted"} else if v["is_error"]==true {"failed"} else {"completed"}.into()})?;
                        }
                    } else {
                        let method=v["method"].as_str().unwrap_or("");
                        if method.is_empty() && v["id"]==1 {
                            if v.get("error").is_some(){return Err("Codex initialization failed".into());}
                            initialized=true;
                            send(&process,json!({"method":"initialized","params":{}})).await?;
                            let mut params=codex_thread_params(mode);
                            params["cwd"]=json!(config.cwd);
                            let method=if let Some(id)=&config.resume {params["threadId"]=json!(id); "thread/resume"} else {"thread/start"};
                            if let Some(model)=&config.model{params["model"]=json!(model);}
                            send(&process,json!({"id":2,"method":method,"params":params})).await?;
                        } else if method.is_empty() && v["id"]==2 {
                            if let Some(error)=v.get("error") { return Err(format!("Codex could not open the session: {}",error_text(error))); }
                            session=v.pointer("/result/thread/id").and_then(Value::as_str).ok_or("Codex could not open the session")?.into();
                            ready=true; emit(tx,Event::Ready{session:session.clone()})?;
                            mode=confirm_mode(tx,"Codex",mode,codex_reported(&v["result"]))?; emit(tx,Event::ModeChanged(mode))?;
                            if let Some(model)=v["result"]["model"].as_str().filter(|m| !m.is_empty() && m.len()<=256 && !m.chars().any(char::is_control)) { emit(tx,Event::ModelSelected(model.into()))?; }
                            send(&process,json!({"id":3,"method":"model/list","params":{"limit":100,"includeHidden":false}})).await?;
                        } else if method.is_empty() && v["id"]==3 {
                            catalog_pages+=1;
                            if v.get("error").is_some() {
                                emit(tx,Event::Notice("Model catalog unavailable from this CLI; explicit model IDs remain supported".into()))?;
                            } else {
                                for model in model_catalog(&v["result"]["data"],false) {
                                    if catalog.len()<256 && !catalog.iter().any(|m: &ModelInfo|m.selection==model.selection) {catalog.push(model);}
                                }
                                if let Some(cursor)=v["result"]["nextCursor"].as_str().filter(|c| !c.is_empty()) {
                                    if catalog_pages<8 && catalog.len()<256 && cursor.len()<=4096 && catalog_cursors.insert(cursor.to_owned()) {
                                        send(&process,json!({"id":3,"method":"model/list","params":{"limit":100,"includeHidden":false,"cursor":cursor}})).await?;
                                    } else {emit(tx,Event::Notice("Model catalog exceeds discovery limits; showing partial results".into()))?;}
                                }
                                emit(tx,Event::Models(catalog.clone()))?;
                            }
                        } else if method.is_empty() && v["id"].as_u64()==start_request && start_request.is_some() {
                            start_request=None;
                            if v.get("error").is_some(){ running=false; emit(tx,Event::Error(error_text(&v["error"])))?; emit(tx,Event::Finished{outcome:"failed".into()})?; }
                        } else if method.is_empty() && v["id"].as_u64()==interrupt_request && interrupt_request.is_some() {
                            interrupt_request=None;
                            if v.get("error").is_some(){emit(tx,Event::Notice("Interrupt was rejected; waiting for terminal outcome".into()))?;}
                        }
                        if !method.is_empty() && v.get("id").is_some() {
                            if matches!(method,"item/commandExecution/requestApproval"|"item/fileChange/requestApproval") && running && !interrupt_pending && pending.len()<8 && v.pointer("/params/threadId").and_then(Value::as_str)==Some(session.as_str()) && turn.is_some() && v.pointer("/params/turnId").and_then(Value::as_str)==turn.as_deref() {
                                let detail=serde_json::to_string_pretty(&v["params"]).unwrap_or_default();
                                if detail.len()>EVENT_BYTES {send(&process,answer(false,&v,false)).await?;emit(tx,Event::Notice("Oversized approval denied: cannot show the complete request".into()))?;continue;}
                                request_id+=1;
                                emit(tx,Event::Approval{id:request_id,detail})?;
                                pending.insert(request_id,Pending{wire:v,deadline:Instant::now()+limits.approval});
                            } else if let Some(reply)=codex_stray_reply(&v){send(&process,reply).await?; emit(tx,Event::Notice(format!("Request declined: {method}")))?;}
                            continue;
                        }
                        if v.pointer("/params/threadId").and_then(Value::as_str)!=Some(session.as_str()){continue;}
                        if method=="turn/started" && running {
                            turn=v.pointer("/params/turn/id").and_then(Value::as_str).map(str::to_owned);
                            if interrupt_pending { request_id+=1; interrupt_request=Some(request_id); send(&process,json!({"id":request_id,"method":"turn/interrupt","params":{"threadId":session,"turnId":turn}})).await?; }
                        }
                        if method=="thread/tokenUsage/updated" { emit(tx,Event::Usage(format!("{} tokens",v.pointer("/params/tokenUsage/total/totalTokens").and_then(Value::as_u64).unwrap_or(0))))?; }
                        if !running {continue;}
                        if let Some(event_turn)=v.pointer("/params/turnId").and_then(Value::as_str){if Some(event_turn)!=turn.as_deref(){continue;}}
                        match method {
                            "item/agentMessage/delta" => {if let Some(text)=v.pointer("/params/delta").and_then(Value::as_str){if let Some(id)=v.pointer("/params/itemId").and_then(Value::as_str){if text_items.len()>=4096{return Err("Turn item limit reached; session stopped".into());}text_items.insert(id.to_owned());} emit(tx,Event::Text(text.into()))?;}},
                            "item/started" | "item/completed" => {
                                let item=&v["params"]["item"];
                                match item["type"].as_str().unwrap_or("") {
                                    "agentMessage" if method=="item/completed" => {if !text_items.contains(item["id"].as_str().unwrap_or("")){if let Some(text)=item["text"].as_str(){emit(tx,Event::Text(text.into()))?;}}},
                                    "commandExecution" | "fileChange" | "mcpToolCall" => {emit(tx,Event::Tool(format!("{} · {}\n{}",item["type"].as_str().unwrap_or("tool"),if method=="item/started"{"running"}else{"finished"},item)))?;},
                                    _=>{}
                                }
                            },
                            "turn/completed" => {
                                if turn.is_none() || v.pointer("/params/turn/id").and_then(Value::as_str)!=turn.as_deref(){continue;}
                                running=false;
                                for (id,_) in pending.drain(){emit(tx,Event::ApprovalClosed(id))?;}
                                let status=v.pointer("/params/turn/status").and_then(Value::as_str).unwrap_or("unknown");
                                if status=="failed"{emit(tx,Event::Error(error_text(&v["params"]["turn"]["error"])))?;}
                                emit(tx,Event::Finished{outcome:status.into()})?;
                            },
                            _=>{}
                        }
                    }
                    if !initialized && ready { return Err("Unexpected protocol initialization order".into()); }
                }
            }
        }
        Ok(())
    }.await;
    let report = process.shutdown().await;
    if !report.reaped || !report.descendants_stopped {
        return Err("Could not verify all vendor children stopped".into());
    }
    result.map_err(|error| with_stderr(error, &config.engine, &report.stderr_tail))
}
/// A failure plus what the vendor itself said on stderr, which usually names
/// the real cause (an unknown session ID, an expired login).
fn with_stderr(error: String, engine: &str, tail: &[u8]) -> String {
    let start = tail.len().saturating_sub(1024);
    let text = String::from_utf8_lossy(&tail[start..]);
    let text = text.trim();
    if text.is_empty() {
        error
    } else {
        format!("{error}\n{engine} stderr: {text}")
    }
}
fn seconds(limit: Duration) -> String {
    match limit.as_secs() {
        1 => "1 second".into(),
        n => format!("{n} seconds"),
    }
}
fn claude_result_error(result: &Value) -> String {
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
        None => "Claude returned an error".into(),
    }
}
/// Adopt the mode the vendor confirmed. An unmapped report keeps the requested
/// mode and says what the vendor actually uses; LocalCode never guesses.
fn confirm_mode(
    tx: &mpsc::Sender<Event>,
    vendor: &str,
    requested: Mode,
    reported: Option<(Option<Mode>, String)>,
) -> Result<Mode, String> {
    match reported {
        None => Ok(requested),
        Some((Some(actual), _)) if actual == requested => Ok(requested),
        Some((Some(actual), _)) => {
            emit(
                tx,
                Event::Notice(format!(
                    "{vendor} reports {}; you asked for {}",
                    actual.label(),
                    requested.label()
                )),
            )?;
            Ok(actual)
        }
        Some((None, raw)) => {
            emit(tx, Event::Notice(format!("{vendor} reports a permission setting LocalCode does not map ({raw}); showing the requested {}", requested.label())))?;
            Ok(requested)
        }
    }
}
/// The mode a successful Claude switch reply confirms, reported like a
/// connect-time mismatch; a reply without a mode confirms the target.
fn switch_reply_mode(
    tx: &mpsc::Sender<Event>,
    reply: &Value,
    target: Mode,
) -> Result<Mode, String> {
    let reported = reply
        .pointer("/response/response/mode")
        .and_then(Value::as_str)
        .map(|raw| (claude_reported_mode(raw), limited(raw)));
    confirm_mode(tx, "Claude", target, reported)
}
/// Reply to a Claude control request the driver will not put in front of the
/// user: a permission request outside an active turn is denied, anything else
/// is reported as unsupported. The deny text is shown to the model.
fn claude_stray_reply(value: &Value) -> Option<Value> {
    if value.get("type")?.as_str()? != "control_request" {
        return None;
    }
    let id = value.get("request_id")?;
    Some(
        if value.pointer("/request/subtype")?.as_str()? == "can_use_tool" {
            json!({"type":"control_response","response":{"subtype":"success","request_id":id,"response":{"behavior":"deny","message":"Denied by LocalCode: the turn was cancelled or too many approvals are waiting"}}})
        } else {
            json!({"type":"control_response","response":{"subtype":"error","request_id":id,"error":"LocalCode does not support this control request"}})
        },
    )
}
/// Codex counterpart of `claude_stray_reply`.
fn codex_stray_reply(value: &Value) -> Option<Value> {
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
fn error_text(error: &Value) -> String {
    let Some(message) = error.as_str().or(error["message"].as_str()) else {
        return limited(&error.to_string());
    };
    let inner = serde_json::from_str::<Value>(message).ok().and_then(|v| {
        v.pointer("/error/message")
            .and_then(Value::as_str)
            .map(str::to_owned)
    });
    let text = inner.unwrap_or_else(|| message.to_owned());
    match error["codexErrorInfo"].as_str() {
        Some(kind) if kind != "other" => limited(&format!("{text} ({kind})")),
        _ => limited(&text),
    }
}
fn answer(claude: bool, wire: &Value, allow: bool) -> Value {
    if claude {
        let response = if allow {
            json!({"behavior":"allow","updatedInput":wire["request"]["input"]})
        } else {
            json!({"behavior":"deny","message":"Denied by LocalCode user or timeout"})
        };
        json!({"type":"control_response","response":{"subtype":"success","request_id":wire["request_id"],"response":response}})
    } else {
        json!({"id":wire["id"],"result":{"decision":if allow{"accept"}else{"decline"}}})
    }
}
fn demo_set_mode(tx: &mpsc::Sender<Event>, mode: &mut Mode, target: Mode) -> Result<(), String> {
    if target == Mode::FullAccess || *mode == Mode::FullAccess {
        emit(
            tx,
            Event::Notice("Full access is changed by reconnecting; use /mode".into()),
        )?;
    } else {
        *mode = target;
    }
    emit(tx, Event::ModeChanged(*mode))
}
async fn demo(
    mut mode: Mode,
    mut commands: mpsc::Receiver<Command>,
    mut cancel: watch::Receiver<u64>,
    mut stop: watch::Receiver<bool>,
    tx: &mpsc::Sender<Event>,
) -> Result<(), String> {
    emit(
        tx,
        Event::Ready {
            session: "demo · offline".into(),
        },
    )?;
    emit(tx, Event::ModeChanged(mode))?;
    loop {
        tokio::select! {
            _=stop.changed()=>break,
            _=cancel.changed()=>{},
            command=commands.recv()=>match command{
                Some(command @ (Command::Prompt(_) | Command::PromptWithDisplay { .. }))=>{
                    let (text,display)=prompt_parts(command);
                    emit(tx,Event::User(display.clone()))?;
                    emit(tx,Event::Started)?;
                    let reply=if text.trim()=="/approval-demo" && mode!=Mode::Ask && mode!=Mode::AcceptEdits {
                        emit(tx,Event::Notice(format!("Allowed without a dialog by {} mode (demo only)",mode.label())))?;
                        "Approved automatically. In a live session, the vendor's own reviewer decides.".to_owned()
                    } else if text.trim()=="/approval-demo" {
                        emit(tx,Event::Approval{id:1,detail:"Demo only — no command will execute.\n\nWrite a greeting to hello.txt?".into()})?;
                        let expiry=tokio::time::sleep(Duration::from_secs(120));
                        tokio::pin!(expiry);
                        // A mode switch while the dialog is open applies and keeps waiting.
                        let allowed=loop{tokio::select!{_=stop.changed()=>return Ok(()),_=cancel.changed()=>break false,_=&mut expiry=>break false,c=commands.recv()=>match c{
                            Some(Command::SetMode(target))=>demo_set_mode(tx,&mut mode,target)?,
                            c=>break matches!(c,Some(Command::Answer{id:1,allow:true})),
                        }}};
                        emit(tx,Event::ApprovalClosed(1))?;
                        if allowed{"Approved. In a live session, the vendor would now continue.".to_owned()}else{"Denied. No action was performed.".to_owned()}
                    }else{format!("This is an offline demo. Your prompt was:\n\n{display}\n\nThe editor, streaming transcript, approval dialog, history, and cancellation are live. Start with --engine codex or --engine claude to work with a model.\n\nTry /approval-demo to preview a permission request.")};
                    let mut interrupted=false;
                    for word in reply.split_inclusive(' '){tokio::select!{_=stop.changed()=>return Ok(()),_=cancel.changed()=>{interrupted=true;break;},_=tokio::time::sleep(Duration::from_millis(18))=>{emit(tx,Event::Text(word.into()))?;}}}
                    emit(tx,Event::Finished{outcome:if interrupted{"interrupted"}else{"completed"}.into()})?;
                },
                Some(Command::SetMode(target))=>demo_set_mode(tx,&mut mode,target)?,
                None=>break,
                _=>{}
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod model_tests {
    use super::*;
    #[test]
    fn catalog_rejects_invalid_identifiers_and_bounds_metadata() {
        let models = model_catalog(
            &json!([
                {"value":"sonnet","displayName":"Sonnet"},
                {"value":"bad\u{1b}id"},
                {"value":"x".repeat(257)},
                {"value":"custom","resolvedModel":"full-id","description":"x".repeat(2049)}
            ]),
            true,
        );
        assert_eq!(models.len(), 2);
        assert_eq!(models[0].id, None);
        assert_eq!(models[1].id.as_deref(), Some("full-id"));
        assert!(models[1].description.is_empty());
        assert!(model_catalog(&Value::Null, false).is_empty());
    }
}

#[cfg(test)]
mod mode_tests {
    use super::*;
    #[test]
    fn mode_labels_parse_and_cycle() {
        for mode in Mode::ALL {
            assert_eq!(Mode::parse(mode.label()), Some(mode));
        }
        assert_eq!(Mode::default(), Mode::Ask);
        assert_eq!(Mode::parse("bypassPermissions"), None);
        assert_eq!(Mode::parse(""), None);
        assert_eq!(Mode::Ask.cycle(), Mode::AcceptEdits);
        assert_eq!(Mode::AcceptEdits.cycle(), Mode::Auto);
        assert_eq!(Mode::Auto.cycle(), Mode::Ask);
        assert_eq!(Mode::FullAccess.cycle(), Mode::FullAccess);
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
    fn every_mode_is_described_for_every_engine() {
        for engine in ["claude", "codex", "demo"] {
            for mode in Mode::ALL {
                assert!(!mode.describe(engine).is_empty());
            }
        }
    }
    #[test]
    fn reported_modes_map_back_or_stay_unmapped() {
        for mode in Mode::ALL {
            assert_eq!(claude_reported_mode(claude_mode(mode)), Some(mode));
            let mut echo = codex_thread_params(mode);
            echo["sandbox"] = match mode {
                Mode::FullAccess => json!({"type":"dangerFullAccess"}),
                _ => json!({"type":"workspaceWrite"}),
            };
            assert_eq!(codex_reported(&echo).map(|r| r.0), Some(Some(mode)));
        }
        assert_eq!(claude_reported_mode("plan"), None);
        let legacy = json!({"sandbox":{"type":"workspaceWrite"},"approvalPolicy":"on-request","approvalsReviewer":"guardian_subagent"});
        assert_eq!(codex_reported(&legacy).map(|r| r.0), Some(Some(Mode::Auto)));
        let read_only = json!({"sandbox":{"type":"readOnly"},"approvalPolicy":"on-request","approvalsReviewer":"user"});
        let (mode, raw) = codex_reported(&read_only).unwrap();
        assert_eq!(mode, None);
        assert!(raw.contains("readOnly"), "{raw}");
        assert_eq!(codex_reported(&json!({"thread":{}})), None);
    }
    #[test]
    fn stray_request_replies_use_production_wording() {
        let permission = json!({"type":"control_request","request_id":"r1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{}}});
        let unknown = json!({"type":"control_request","request_id":"r2","request":{"subtype":"hook_callback"}});
        let codex = json!({"id":7,"method":"item/tool/requestUserInput","params":{}});
        for reply in [
            claude_stray_reply(&permission).unwrap(),
            claude_stray_reply(&unknown).unwrap(),
            codex_stray_reply(&codex).unwrap(),
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
        assert_eq!(codex_stray_reply(&codex).unwrap()["error"]["code"], -32601);
        let approval = json!({"id":8,"method":"item/commandExecution/requestApproval","params":{}});
        assert_eq!(
            codex_stray_reply(&approval).unwrap()["result"]["decision"],
            "decline"
        );
    }
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
}
