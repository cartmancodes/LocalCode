//! Provider-neutral permission modes and the only vendor mapping.
use super::{emit, limited, DriverError, Engine, Event};
use serde_json::{json, Value};
use tokio::sync::mpsc;

/// Provider-neutral permission mode. The vendor mapping lives only in the
/// functions below; `Auto` delegates to each vendor's own reviewer and
/// Octet never answers a vendor approval by itself.
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
    pub fn describe(self, engine: Engine) -> &'static str {
        match (engine, self) {
            (Engine::Claude, Mode::Ask) => "Claude asks before edits and commands (permission mode default)",
            (Engine::Claude, Mode::AcceptEdits) => "File edits proceed; other actions ask (acceptEdits)",
            (Engine::Claude, Mode::Auto) => "Claude's classifier approves or blocks each action (auto)",
            (Engine::Claude, Mode::FullAccess) => "No permission checks at all (bypassPermissions)",
            (Engine::Codex, Mode::Ask) => "Workspace sandbox; untrusted commands ask (untrusted)",
            (Engine::Codex, Mode::AcceptEdits) => {
                "Workspace sandbox; asks only to escalate (on-request). Codex has no edits-only mode"
            }
            (Engine::Codex, Mode::Auto) => {
                "Workspace sandbox; Codex's auto-review agent decides escalations (auto_review)"
            }
            (Engine::Codex, Mode::FullAccess) => "No sandbox; never asks (danger-full-access)",
            (Engine::Demo, Mode::Ask | Mode::AcceptEdits) => "Offline demo: /approval-demo shows the dialog",
            (Engine::Demo, Mode::Auto | Mode::FullAccess) => "Offline demo: /approval-demo is allowed without a dialog",
        }
    }
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
pub(crate) fn codex_thread_params(mode: Mode) -> Value {
    let (sandbox, policy, reviewer) = match mode {
        Mode::Ask => ("workspace-write", "untrusted", "user"),
        Mode::AcceptEdits => ("workspace-write", "on-request", "user"),
        Mode::Auto => ("workspace-write", "on-request", "auto_review"),
        Mode::FullAccess => ("danger-full-access", "never", "user"),
    };
    json!({"sandbox":sandbox,"approvalPolicy":policy,"approvalsReviewer":reviewer})
}
/// The mode Claude reports (`current_permission_mode`, or a switch's reply).
/// None means the value is not one of Octet's modes.
pub(crate) fn claude_reported_mode(raw: &str) -> Option<Mode> {
    Mode::ALL.into_iter().find(|mode| claude_mode(*mode) == raw)
}
/// The mode a Codex thread reply echoes. Outer None: the reply carries no
/// policy (older CLI). Inner None: a policy Octet does not map, with the
/// raw description for the user.
pub(crate) fn codex_reported(result: &Value) -> Option<(Option<Mode>, String)> {
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
pub(crate) fn codex_turn_overrides(mode: Mode) -> Value {
    let params = codex_thread_params(mode);
    json!({"approvalPolicy":params["approvalPolicy"],"approvalsReviewer":params["approvalsReviewer"]})
}
/// Adopt the mode the vendor confirmed. An unmapped report keeps the requested
/// mode and says what the vendor actually uses; Octet never guesses.
pub(super) fn confirm_mode(
    tx: &mpsc::Sender<Event>,
    vendor: &str,
    requested: Mode,
    reported: Option<(Option<Mode>, String)>,
) -> Result<Mode, DriverError> {
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
            let notice = format!(
                "{vendor} reports a permission setting Octet does not map ({raw}); showing the requested {}",
                requested.label()
            );
            emit(tx, Event::Notice(notice))?;
            Ok(requested)
        }
    }
}

#[cfg(test)]
mod tests {
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
        for engine in Engine::ALL {
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
        let legacy = json!({
            "sandbox": {"type": "workspaceWrite"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "guardian_subagent",
        });
        assert_eq!(codex_reported(&legacy).map(|r| r.0), Some(Some(Mode::Auto)));
        let read_only = json!({
            "sandbox": {"type": "readOnly"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        });
        let (mode, raw) = codex_reported(&read_only).unwrap();
        assert_eq!(mode, None);
        assert!(raw.contains("readOnly"), "{raw}");
        assert_eq!(codex_reported(&json!({"thread":{}})), None);
    }
}
