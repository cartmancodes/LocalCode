//! The gate's fixture replies: each allows only the exact request its
//! scenario expects.
use octet_gate::{
    claude_fixture_allow, claude_fixture_hook_response, claude_fixture_mcp_response,
    claude_fixture_mcp_tool_allow, codex_fixture_allow, codex_fixture_user_input,
};
use serde_json::json;
use std::path::Path;

#[test]
fn codex_fixture_question_maps_each_id_to_an_answer() {
    let request = json!({"id":0,"method":"item/tool/requestUserInput","params":{"questions":[{"id":"choice","question":"Which?","options":[{"label":"ALPHA"},{"label":"BETA"}]}]}});
    let reply = codex_fixture_user_input(&request).unwrap();
    assert_eq!(reply["id"], 0);
    assert_eq!(
        reply["result"]["answers"]["choice"]["answers"],
        json!(["ALPHA"])
    );
    let invalid = json!({"id":1,"method":"item/tool/requestUserInput","params":{}});
    assert!(codex_fixture_user_input(&invalid).is_none());
}

#[test]
fn codex_approval_decisions_are_scoped_to_fixture_command() {
    let cwd = Path::new("/tmp/octet-fixture");
    let request = json!({"id":0,"method":"item/commandExecution/requestApproval","params":{"command":"/bin/zsh -lc 'printf READY > probe.out'","cwd":"/tmp/octet-fixture"}});
    assert_eq!(
        codex_fixture_allow(&request, cwd).unwrap()["result"]["decision"],
        "accept"
    );
    assert_eq!(
        octet_engine::live::codex_stray_reply(&request).unwrap()["result"]["decision"],
        "decline"
    );
    let wrong_path = json!({"id":0,"method":"item/commandExecution/requestApproval","params":{"command":"/bin/zsh -lc 'printf READY > probe.out'","cwd":"/tmp/other"}});
    assert!(codex_fixture_allow(&wrong_path, cwd).is_none());
    let extra_command = json!({"id":0,"method":"item/commandExecution/requestApproval","params":{"command":"/bin/zsh -lc 'printf READY > probe.out; cat /etc/passwd'","cwd":"/tmp/octet-fixture"}});
    assert!(codex_fixture_allow(&extra_command, cwd).is_none());
}

#[test]
fn hook_callback_correlates_request_and_rejects_unknown_callback() {
    let request = json!({"type":"control_request","request_id":"hook-3","request":{"subtype":"hook_callback","callback_id":"hook_0","input":{"tool_name":"Bash"}}});
    let reply = claude_fixture_hook_response(&request).unwrap();
    assert_eq!(reply["response"]["request_id"], "hook-3");
    assert_eq!(reply["response"]["response"]["continue"], true);
    let unknown = json!({"type":"control_request","request_id":"hook-4","request":{"subtype":"hook_callback","callback_id":"hook_99"}});
    assert!(claude_fixture_hook_response(&unknown).is_none());
}

#[test]
fn claude_approval_retains_request_id_and_original_input() {
    let request = json!({"type":"control_request","request_id":"req-7","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"printf READY > probe.out"}}});
    let cwd = std::env::temp_dir();
    let allowed = claude_fixture_allow(&request, &cwd).unwrap();
    assert_eq!(allowed["response"]["request_id"], "req-7");
    assert_eq!(
        allowed["response"]["response"]["updatedInput"],
        request["request"]["input"]
    );
    let denied = octet_engine::live::claude_stray_reply(&request).unwrap();
    assert_eq!(denied["response"]["response"]["behavior"], "deny");
    assert!(claude_fixture_mcp_tool_allow(&request).is_none());
    let target = cwd.canonicalize().unwrap().join("probe.out");
    let safe_absolute = json!({"type":"control_request","request_id":"req-8","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":format!("printf READY > {} && ls -l {}", target.display(), target.display())}}});
    assert!(claude_fixture_allow(&safe_absolute, &cwd).is_some());
    let extra = json!({"type":"control_request","request_id":"req-9","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":format!("printf READY > {} && cat /etc/passwd", target.display())}}});
    assert!(claude_fixture_allow(&extra, &cwd).is_none());
}

#[test]
fn sdk_mcp_initialize_list_call_and_notification_ack() {
    let envelope = |message| json!({"type":"control_request","request_id":"mcp-2","request":{"subtype":"mcp_message","server_name":"fixture","message":message}});
    let (init, called) = claude_fixture_mcp_response(&envelope(
        json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}),
    ))
    .unwrap();
    assert!(!called);
    assert_eq!(
        init["response"]["response"]["mcp_response"]["result"]["serverInfo"]["name"],
        "fixture"
    );
    let (listed, _) = claude_fixture_mcp_response(&envelope(
        json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
    ))
    .unwrap();
    assert_eq!(
        listed["response"]["response"]["mcp_response"]["result"]["tools"][0]["name"],
        "fixture_echo"
    );
    let (result, called) = claude_fixture_mcp_response(&envelope(json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fixture_echo","arguments":{}}}))).unwrap();
    assert!(called);
    assert_eq!(
        result["response"]["response"]["mcp_response"]["result"]["content"][0]["text"],
        "READY"
    );
    let (notification, _) = claude_fixture_mcp_response(&envelope(
        json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    ))
    .unwrap();
    assert_eq!(notification["response"]["request_id"], "mcp-2");
    assert_eq!(
        notification["response"]["response"]["mcp_response"]["result"],
        json!({})
    );
}

#[test]
fn permissions_denial_is_an_empty_profile_not_an_array() {
    let reply = octet_engine::live::codex_stray_reply(
        &json!({"id":91,"method":"item/permissions/requestApproval","params":{}}),
    )
    .unwrap();
    assert_eq!(
        reply,
        json!({"id":91,"result":{"permissions":{},"scope":"turn"}})
    );
}
