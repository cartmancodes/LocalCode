//! Codex app-server protocol.
use super::*;

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
