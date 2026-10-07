//! The fake vendor's scripts, named once for `protocol-child` and the tests
//! that drive it. A prompt with one of these texts (or, where noted, a
//! `--model` value or launch argument) runs that script.

/// Codex and Claude: hold the turn open until interrupted.
pub const HOLD: &str = "hold";
/// Codex: hold the turn, after a text marker that the turn is named.
pub const HOLD_MARKED: &str = "hold-marked";
/// Codex: name the turn late, so steers are held and cancels wait.
pub const LATE_START: &str = "late-start";
/// Codex: refuse `turn/start` after a pause.
pub const REFUSE_START: &str = "refuse-start";
/// Codex: hold the turn and exit when asked to interrupt it.
pub const DIE_ON_INTERRUPT: &str = "die-on-interrupt";
/// Codex and Claude: ask for one approval and echo the answer.
pub const APPROVAL: &str = "approval";
/// Codex and Claude: ask for nine approvals at once.
pub const APPROVALS_9: &str = "approvals-9";
/// Claude: ask for an approval, then cancel the request.
pub const APPROVAL_CANCEL: &str = "approval-cancel";
/// Codex: a tool with large output.
pub const BIGTOOL: &str = "bigtool";
/// Codex: output for another thread that must not show, and no turn end.
pub const CHATTER: &str = "chatter";
/// Codex: a failed turn (usage limit).
pub const FAIL: &str = "fail";
/// Codex: a failed turn whose message is nested JSON.
pub const FAIL_NESTED: &str = "fail-nested";
/// Codex: a slow turn of dots.
pub const SLOW: &str = "slow";
/// Codex: echo the thread and turn parameters received.
pub const PARAMS: &str = "params";
/// Claude: one tool use.
pub const TOOL: &str = "tool";
/// Claude: a failed result with error details.
pub const ERRORS: &str = "errors";
/// Claude: echo the launch arguments.
pub const ARGV: &str = "argv";
/// Claude: echo the permission modes requested so far.
pub const MODES: &str = "modes";
/// Claude: answer held mode switches (with `HANG_MODE`).
pub const FLUSH: &str = "flush";
/// Claude: a result without `is_error`.
pub const NO_IS_ERROR: &str = "no-is-error";

/// Claude launch argument (`--model`): exit at the handshake with stderr.
pub const DIE_STDERR: &str = "die-stderr";
/// Claude launch argument: report `auto` at the handshake.
pub const REPORT_AUTO: &str = "report-auto";
/// Claude launch argument: report `plan` at the handshake.
pub const REPORT_PLAN: &str = "report-plan";
/// Claude launch argument: hold mode switches until `FLUSH`.
pub const HANG_MODE: &str = "hang-mode";
/// Claude launch argument: confirm every switch as `plan`.
pub const ODD_MODE: &str = "odd-mode";
/// Claude launch argument: confirm switches after 2.5 seconds.
pub const LATE_MODE: &str = "late-mode";
/// Claude launch argument: refuse every switch.
pub const REJECT_MODE: &str = "reject-mode";

/// Codex `--model`: refuse to open the thread.
pub const REFUSE_THREAD: &str = "refuse-thread";
/// Codex `--model`: report a stricter policy than asked for.
pub const REPORT_STRICTER: &str = "report-stricter";
