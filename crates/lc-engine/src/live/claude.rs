//! Claude Code stream-json protocol.
use super::{mode::*, *};

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
            json!({"type":"control_response","response":{"subtype":"success","request_id":id,"response":{"behavior":"deny","message":"Denied by LocalCode: no turn is waiting for this request"}}})
        } else {
            json!({"type":"control_response","response":{"subtype":"error","request_id":id,"error":"LocalCode does not support this control request"}})
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
