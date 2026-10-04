//! The vendor process loop shared by Claude and Codex.
use super::{claude::*, codex::*, mode::*, *};

pub(super) async fn send(process: &Process, value: Value) -> Result<(), DriverError> {
    timeout(Duration::from_secs(3), process.sender().send(&value))
        .await
        .map_err(|_| DriverError::from("Vendor stdin is unresponsive"))?
        .map_err(|e| DriverError::from(e.to_string()))
}
pub(super) struct Pending {
    wire: Value,
    deadline: Instant,
}
pub(super) async fn vendor(
    config: Config,
    limits: Limits,
    mut commands: mpsc::Receiver<Command>,
    mut cancel: watch::Receiver<u64>,
    mut stopping: watch::Receiver<bool>,
    tx: &mpsc::Sender<Event>,
) -> Result<(), String> {
    let claude = config.engine == Engine::Claude;
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
    let result: Result<(), DriverError> = async {
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
            // make every idle timer fire immediately and spin. It is also paused
            // while an approval waits: the vendor is then silent because of us.
            let next_deadline = pending.values().map(|p| p.deadline).chain(mode_request.as_ref().map(|r| r.2)).chain(((!ready || running) && pending.is_empty()).then_some(deadline)).min().unwrap_or(deadline);
            tokio::select! {
                biased;
                _ = stopping.changed() => break,
                changed = cancel.changed() => {
                    if changed.is_err() { break; }
                    if !ready { return Err(DriverError::Cancelled); }
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
                    if (!ready || running) && pending.is_empty() && Instant::now() >= deadline {
                        return Err(if !ready { format!("Vendor did not finish connecting within {}; session stopped.",seconds(limits.connect)) }
                            else if interrupt_pending { format!("Vendor did not stop within {} of the interrupt; session stopped. Resume using the session ID.",seconds(limits.interrupt)) }
                            else { format!("Vendor sent nothing for {}; session stopped. Resume using the session ID.",seconds(limits.turn_idle)) }.into());
                    }
                    let expired: Vec<u64> = pending.iter().filter(|(_,p)|p.deadline <= Instant::now()).map(|(id,_)|*id).collect();
                    for id in expired { let p=pending.remove(&id).unwrap(); send(&process,answer(claude,&p.wire,false)).await?; emit(tx,Event::ApprovalClosed(id))?; emit(tx,Event::Notice("Approval timed out and was denied".into()))?; if pending.is_empty() && running && !interrupt_pending { deadline=Instant::now()+limits.turn_idle; } }
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
                        if let Some(p)=pending.remove(&id) {
                            send(&process,answer(claude,&p.wire,allow)).await?; emit(tx,Event::ApprovalClosed(id))?;
                            // The silence watchdog was paused while the user decided.
                            if pending.is_empty() && running && !interrupt_pending { deadline=Instant::now()+limits.turn_idle; }
                        }
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
                    // Only frames for this session count: another thread's chatter is not progress.
                    let ours=claude || v.pointer("/params/threadId").and_then(Value::as_str).is_none_or(|id| id==session);
                    if running && !interrupt_pending && ours { deadline=Instant::now()+limits.turn_idle; }
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
                        if let Some(model)=actual.filter(|m| valid_identifier(m)) {
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
                            if pending.is_empty() && running && !interrupt_pending { deadline=Instant::now()+limits.turn_idle; }
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
                            emit(tx,Event::Finished{outcome:if interrupt_pending {Outcome::Interrupted} else if v["is_error"]==true {Outcome::Failed} else {Outcome::Completed}})?;
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
                            if let Some(error)=v.get("error") { return Err(format!("Codex could not open the session: {}",error_text(error)).into()); }
                            session=v.pointer("/result/thread/id").and_then(Value::as_str).ok_or("Codex could not open the session")?.into();
                            ready=true; emit(tx,Event::Ready{session:session.clone()})?;
                            mode=confirm_mode(tx,"Codex",mode,codex_reported(&v["result"]))?; emit(tx,Event::ModeChanged(mode))?;
                            if let Some(model)=v["result"]["model"].as_str().filter(|m| valid_identifier(m)) { emit(tx,Event::ModelSelected(model.into()))?; }
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
                            if v.get("error").is_some(){ running=false; emit(tx,Event::Error(error_text(&v["error"])))?; emit(tx,Event::Finished{outcome:Outcome::Failed})?; }
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
                            "item/agentMessage/delta" => {if let Some(text)=v.pointer("/params/delta").and_then(Value::as_str){if let Some(id)=v.pointer("/params/itemId").and_then(Value::as_str){if text_items.len()>=4096{return Err(DriverError::TurnItemLimit);}text_items.insert(id.to_owned());} emit(tx,Event::Text(text.into()))?;}},
                            "item/started" | "item/completed" => {
                                let item=&v["params"]["item"];
                                match item["type"].as_str().unwrap_or("") {
                                    "agentMessage" if method=="item/completed" => {if !text_items.contains(item["id"].as_str().unwrap_or("")){if let Some(text)=item["text"].as_str(){emit(tx,Event::Text(text.into()))?;}}},
                                    "commandExecution" | "fileChange" | "mcpToolCall" => {emit(tx,Event::Tool(codex_tool_detail(item,if method=="item/started"{"running"}else{"finished"})))?;},
                                    _=>{}
                                }
                            },
                            "turn/completed" => {
                                if turn.is_none() || v.pointer("/params/turn/id").and_then(Value::as_str)!=turn.as_deref(){continue;}
                                running=false;
                                for (id,_) in pending.drain(){emit(tx,Event::ApprovalClosed(id))?;}
                                let status=v.pointer("/params/turn/status").and_then(Value::as_str).unwrap_or("unknown");
                                if status=="failed"{emit(tx,Event::Error(error_text(&v["params"]["turn"]["error"])))?;}
                                emit(tx,Event::Finished{outcome:Outcome::from_vendor(status)})?;
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
    result.map_err(|error| with_stderr(error, config.engine, &report.stderr_tail))
}
/// A failure plus what the vendor itself said on stderr, which usually names
/// the real cause (an unknown session ID, an expired login).
pub(super) fn with_stderr(error: DriverError, engine: Engine, tail: &[u8]) -> String {
    // Failures the driver itself caused say nothing about the vendor.
    let DriverError::Vendor(error) = error else {
        return error.to_string();
    };
    let mut end = &tail[tail.len().saturating_sub(1024)..];
    if end.len() < tail.len() {
        // Cut mid-stream: start at the next whole line.
        if let Some(newline) = end.iter().position(|byte| *byte == b'\n') {
            end = &end[newline + 1..];
        }
    }
    let text = String::from_utf8_lossy(end);
    let text = text.trim();
    if text.is_empty() {
        error
    } else {
        format!("{error}\n{engine} stderr: {text}")
    }
}
pub(super) fn seconds(limit: Duration) -> String {
    match limit.as_secs() {
        0 => format!("{} ms", limit.as_millis()),
        1 => "1 second".into(),
        n => format!("{n} seconds"),
    }
}
pub(super) fn answer(claude: bool, wire: &Value, allow: bool) -> Value {
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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stderr_is_attached_only_to_vendor_failures_and_starts_on_a_line() {
        let vendor = || DriverError::from("Vendor disconnected.");
        assert_eq!(
            with_stderr(vendor(), Engine::Claude, b"reason\n"),
            "Vendor disconnected.\nclaude stderr: reason"
        );
        assert_eq!(
            with_stderr(vendor(), Engine::Claude, b" \n"),
            "Vendor disconnected."
        );
        for (local, text) in [
            (DriverError::Cancelled, "Connection cancelled"),
            (
                DriverError::ConsumerOverloaded,
                "Output consumer overloaded; session stopped",
            ),
            (
                DriverError::TurnItemLimit,
                "Turn item limit reached; session stopped",
            ),
        ] {
            assert_eq!(
                with_stderr(local, Engine::Codex, b"unrelated log line\n"),
                text
            );
        }
        let mut long = vec![b'a'; 1500];
        long.extend_from_slice("\nlast line é\n".as_bytes());
        let text = with_stderr("x".into(), Engine::Codex, &long);
        assert!(text.ends_with("codex stderr: last line é"), "{text}");
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
    fn error_text_keeps_structured_kinds_and_names_missing_errors() {
        assert_eq!(
            error_text(&json!({"message":"bad","codexErrorInfo":{"httpStatus":429}})),
            "bad ({\"httpStatus\":429})"
        );
        assert_eq!(
            error_text(&Value::Null),
            "The vendor reported an error without details"
        );
        assert_eq!(
            claude_result_error(&json!({"is_error":true,"subtype":"error_max_turns"})),
            "Claude returned an error (error_max_turns)"
        );
        assert_eq!(seconds(Duration::from_millis(400)), "400 ms");
        assert_eq!(seconds(Duration::from_secs(600)), "600 seconds");
    }
}
