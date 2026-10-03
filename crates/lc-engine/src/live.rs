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

#[derive(Clone, Debug)]
pub struct Config {
    pub engine: String,
    pub binary: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub resume: Option<String>,
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
    Answer { id: u64, allow: bool },
}
#[derive(Clone)]
pub struct Handle {
    commands: mpsc::Sender<Command>,
    interrupt: watch::Sender<u64>,
    stop: watch::Sender<bool>,
}
impl Handle {
    pub fn send(&self, command: Command) -> Result<(), String> {
        if matches!(&command, Command::Prompt(text) if text.len() > PROMPT_LIMIT) {
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
    let (text, tool) = match event {
        Event::Text(text) => (text, false),
        Event::Tool(text) => (text, true),
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
        tx.try_send(if tool {
            Event::Tool(chunk)
        } else {
            Event::Text(chunk)
        })
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
            demo(rx, cancel, stopping, &events).await
        } else {
            vendor(config, rx, cancel, stopping, &events).await
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
            "--permission-mode",
            "default",
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
        let mut turn: Option<String> = None;
        let mut request_id = 10u64;
        let mut start_request = None;
        let mut interrupt_request = None;
        let mut interrupt_pending = false;
        let mut streamed = false;
        let mut text_items = std::collections::HashSet::new();
        let mut pending: HashMap<u64, Pending> = HashMap::new();
        let mut deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let next_deadline = pending.values().map(|p| p.deadline).min().unwrap_or(deadline).min(deadline);
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
                        deadline = Instant::now() + Duration::from_secs(10);
                        for (id,p) in pending.drain() { send(&process, answer(claude,&p.wire,false)).await?; emit(tx,Event::ApprovalClosed(id))?; }
                    }
                },
                _ = tokio::time::sleep_until(next_deadline), if !ready || running || !pending.is_empty() => {
                    if Instant::now() >= deadline { return Err("Vendor deadline exceeded; session stopped. Resume using the session ID.".into()); }
                    let expired: Vec<u64> = pending.iter().filter(|(_,p)|p.deadline <= Instant::now()).map(|(id,_)|*id).collect();
                    for id in expired { let p=pending.remove(&id).unwrap(); send(&process,answer(claude,&p.wire,false)).await?; emit(tx,Event::ApprovalClosed(id))?; emit(tx,Event::Notice("Approval timed out and was denied".into()))?; }
                },
                command = commands.recv() => match command {
                    None => break,
                    Some(Command::Prompt(text)) => {
                        if !ready || running { emit(tx,Event::Notice("Wait for the current operation, or cancel it first".into()))?; continue; }
                        running=true; turn=None; streamed=false; text_items.clear(); interrupt_pending=false;
                        deadline=Instant::now()+Duration::from_secs(600);
                        emit(tx,Event::User(text.clone()))?;
                        emit(tx,Event::Started)?;
                        if claude { send(&process,json!({"type":"user","message":{"role":"user","content":[{"type":"text","text":text}]},"parent_tool_use_id":null})).await?; }
                        else {
                            request_id+=1; start_request=Some(request_id);
                            let mut params=json!({"threadId":session,"input":[{"type":"text","text":text}]});
                            if let Some(model)=&config.model { params["model"]=json!(model); }
                            send(&process,json!({"id":request_id,"method":"turn/start","params":params})).await?;
                        }
                    },
                    Some(Command::Answer{id,allow}) => {
                        if let Some(p)=pending.remove(&id) { send(&process,answer(claude,&p.wire,allow)).await?; emit(tx,Event::ApprovalClosed(id))?; }
                        else { emit(tx,Event::Notice("That approval is no longer active".into()))?; }
                    }
                },
                frame=process.next_frame() => {
                    let v=frame.map_err(|e|e.to_string())?.ok_or("Vendor disconnected. Check its login and installation.")?;
                    if claude {
                        let kind=v["type"].as_str().unwrap_or("");
                        if kind=="control_response" && v.pointer("/response/request_id").and_then(Value::as_str)==Some("lc-init") {
                            if v.pointer("/response/subtype").and_then(Value::as_str)!=Some("success") { return Err("Claude initialization failed".into()); }
                            initialized=true; ready=true; emit(tx,Event::Ready{session:config.resume.clone().unwrap_or_default()})?;
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
                                pending.insert(request_id,Pending{wire:v,deadline:Instant::now()+Duration::from_secs(120)});
                            } else if let Some(reply)=crate::claude_response_for_request(&v) { send(&process,reply).await?; }
                            continue;
                        }
                        if kind=="control_cancel_request" {
                            let ids:Vec<_>=pending.iter().filter(|(_,p)|p.wire["request_id"]==v["request_id"]).map(|(id,_)|*id).collect();
                            for id in ids { pending.remove(&id); emit(tx,Event::ApprovalClosed(id))?; }
                        }
                        if !running { continue; }
                        if kind=="stream_event" {
                            if let Some(text)=v.pointer("/event/delta/text").and_then(Value::as_str) { streamed=true; emit(tx,Event::Text(text.into()))?; }
                            if v.pointer("/event/content_block/type").and_then(Value::as_str)==Some("tool_use") { emit(tx,Event::Tool(limited(v.pointer("/event/content_block/name").and_then(Value::as_str).unwrap_or("tool"))))?; }
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
                            if v["is_error"].as_bool()!=Some(false) && !interrupt_pending { emit(tx,Event::Error(limited(v["result"].as_str().unwrap_or("Claude returned an error"))))?; }
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
                            let mut params=json!({"cwd":config.cwd,"sandbox":"workspace-write","approvalPolicy":"untrusted","approvalsReviewer":"user"});
                            let method=if let Some(id)=&config.resume {params["threadId"]=json!(id); "thread/resume"} else {"thread/start"};
                            if let Some(model)=&config.model{params["model"]=json!(model);}
                            send(&process,json!({"id":2,"method":method,"params":params})).await?;
                        } else if method.is_empty() && v["id"]==2 {
                            session=v.pointer("/result/thread/id").and_then(Value::as_str).ok_or("Codex could not open the session")?.into();
                            ready=true; emit(tx,Event::Ready{session:session.clone()})?;
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
                            if v.get("error").is_some(){ running=false; emit(tx,Event::Error(limited(&v["error"].to_string())))?; emit(tx,Event::Finished{outcome:"failed".into()})?; }
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
                                pending.insert(request_id,Pending{wire:v,deadline:Instant::now()+Duration::from_secs(120)});
                            } else if let Some(reply)=crate::codex_response_for_request(&v){send(&process,reply).await?; emit(tx,Event::Notice(format!("Request declined: {method}")))?;}
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
                                if status=="failed"{emit(tx,Event::Error(limited(&v["params"]["turn"]["error"].to_string())))?;}
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
    result
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
async fn demo(
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
    loop {
        tokio::select! {
            _=stop.changed()=>break,
            _=cancel.changed()=>{},
            command=commands.recv()=>match command{
                Some(Command::Prompt(text))=>{
                    emit(tx,Event::User(text.clone()))?;
                    emit(tx,Event::Started)?;
                    let reply=if text.trim()=="/approval-demo" {
                        emit(tx,Event::Approval{id:1,detail:"Demo only — no command will execute.\n\nWrite a greeting to hello.txt?".into()})?;
                        let allowed=tokio::select!{_=stop.changed()=>return Ok(()),_=cancel.changed()=>false,_=tokio::time::sleep(Duration::from_secs(120))=>false,c=commands.recv()=>matches!(c,Some(Command::Answer{id:1,allow:true}))};
                        emit(tx,Event::ApprovalClosed(1))?;
                        if allowed{"Approved. In a live session, the vendor would now continue.".to_owned()}else{"Denied. No action was performed.".to_owned()}
                    }else{format!("This is an offline demo. Your prompt was:\n\n{text}\n\nThe editor, streaming transcript, approval dialog, history, and cancellation are live. Start with --engine codex or --engine claude to work with a model.\n\nTry /approval-demo to preview a permission request.")};
                    let mut interrupted=false;
                    for word in reply.split_inclusive(' '){tokio::select!{_=stop.changed()=>return Ok(()),_=cancel.changed()=>{interrupted=true;break;},_=tokio::time::sleep(Duration::from_millis(18))=>{emit(tx,Event::Text(word.into()))?;}}}
                    emit(tx,Event::Finished{outcome:if interrupted{"interrupted"}else{"completed"}.into()})?;
                },
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
