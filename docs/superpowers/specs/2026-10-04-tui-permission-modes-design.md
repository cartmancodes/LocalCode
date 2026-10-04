# TUI permission modes (auto mode for all providers) — design

Date: 2026-10-04
Status: approved in conversation; awaiting written-spec review

## Intent

The Rust TUI preview (`crates/lc-tui`, `crates/lc-engine`) hard-codes the most
restrictive permission setting for every provider: Claude launches with
`--permission-mode default`, Codex opens threads with `workspace-write` +
`approvalPolicy: untrusted` + `approvalsReviewer: user`. Every write or command
stops at the approval dialog, and there is no way to change that.

Goal: a provider-neutral permission-mode picker in the TUI whose headline mode
is each vendor's own classifier-backed **auto** mode, so agents can work
without stopping for routine approvals — for Claude, Codex and the offline demo.

Success:

- `localcode --mode auto` and `/mode auto` put either provider into its native
  auto mode; the header shows the mode the vendor actually accepted.
- Shift+Tab cycles `ask → accept-edits → auto` live, including mid-turn on
  Claude.
- `full-access` exists for both providers but is only reached by typing it.
- Nothing escalates silently; existing fail-closed approval rules are unchanged.

## Verified vendor facts (2026-10-04)

- Claude Code 2.1.270: `--permission-mode` accepts `acceptEdits, auto,
  bypassPermissions, manual, dontAsk, plan`. A live
  `control_request {subtype: "set_permission_mode", mode}` returns `success`
  with `{"mode": "auto"}`; an invalid mode returns an `error` response. A live
  switch to `bypassPermissions` is refused unless the process was launched with
  `--dangerously-skip-permissions`; `--allow-dangerously-skip-permissions`
  makes bypass available without enabling it.
- Codex CLI 0.154 app-server schema: `ApprovalsReviewer` is
  `user | auto_review | guardian_subagent` (`auto_review` = a reviewing
  subagent applying a risk framework; the same setup as `codex
  --approve-for-me`). `AskForApproval` is `untrusted | on-request | never`
  (or granular). `TurnStartParams` accepts `approvalPolicy`,
  `approvalsReviewer`, `sandboxPolicy` overrides that apply "on this turn and
  subsequent turns".

## Modes and provider mapping

One enum, `lc_engine::live::Mode { Ask, AcceptEdits, Auto, FullAccess }`,
spelled `ask`, `accept-edits`, `auto`, `full-access` everywhere user-facing.

| Mode | Claude | Codex (sandbox, approvalPolicy, approvalsReviewer) | Demo |
|---|---|---|---|
| `ask` (default) | `--permission-mode default` | `workspace-write`, `untrusted`, `user` | dialog shown |
| `accept-edits` | `acceptEdits` | `workspace-write`, `on-request`, `user` | dialog shown |
| `auto` | `auto` | `workspace-write`, `on-request`, `auto_review` | auto-allowed with notice |
| `full-access` | `bypassPermissions` + `--allow-dangerously-skip-permissions` | `danger-full-access`, `never`, `user` | auto-allowed with notice |

Notes:

- Codex has no edits-versus-commands split. Under `workspace-write`, edits
  inside the workspace already proceed, so `accept-edits` relaxes `untrusted`
  to `on-request`. This is the closest analogue, not an equivalent; the docs say
  so.
- `auto` delegates to the vendor's reviewer. LocalCode itself never
  auto-answers a real vendor approval in any mode.
- The mapping lives in exactly one place: pure functions in `live.rs`
  (`claude_permission_args(mode)`, `codex_thread_params(mode)`,
  `codex_turn_overrides(mode)`).

## Switching rules

- `ask`, `accept-edits`, `auto` switch live (`Command::SetMode`).
- `full-access` is entered or left only through `/mode full-access` /
  `/mode <other>` from full-access, or `--mode full-access` at launch. Both
  directions reconnect with the same vendor session ID and require an idle,
  ready session. Shift+Tab never reaches it.
- Startup default is `ask`. The mode is not persisted between launches. It is
  carried across `/model`, `/new` and `/reconnect` within one TUI invocation.
- An unknown `--mode` value is a startup error; an unknown `/mode` argument is a
  visible error. Neither falls back to another mode.

## Engine changes (`crates/lc-engine/src/live.rs`)

- `Config.mode: Mode`; `Command::SetMode(Mode)`; `Event::ModeChanged(Mode)`.
- `Mode::parse`, `Mode::label`, `Mode::cycle` (`ask → accept-edits → auto →
  ask`; `FullAccess.cycle()` returns `FullAccess`).
- Launch:
  - Claude: argument list uses `claude_permission_args(config.mode)` in place of
    the hard-coded `--permission-mode default`.
  - Codex: `thread/start` / `thread/resume` params come from
    `codex_thread_params(config.mode)`.
  - After the session is ready the driver emits `ModeChanged(config.mode)`.
- Live switch (`SetMode(target)`):
  - Refused with a `Notice` when `target` or the current mode is `FullAccess`.
  - Claude: send `control_request {request_id: "lc-mode-N", request: {subtype:
    "set_permission_mode", mode}}`; remember it as pending. On a `success`
    `control_response` for that id emit `ModeChanged(target)`; on `error` emit
    `Notice("Mode change refused by Claude: <message>")` and keep the old mode.
    Works while a turn is running.
  - Codex: set the current mode and emit `ModeChanged(target)` immediately. Every
    `turn/start` includes `codex_turn_overrides(mode)` (`approvalPolicy`,
    `approvalsReviewer`). No `sandboxPolicy` override is sent: all live modes
    use `workspace-write`. If a turn is running, emit
    `Notice("Mode applies from the next turn")`.
  - Demo: emit `ModeChanged(target)`. `/approval-demo` auto-allows with a notice
    when the mode is not `ask`.
- Unchanged: pending approvals are not resolved retroactively when the mode
  loosens; approval timeout, oversized-request and unknown-request handling stay
  fail-closed.

## Session/journal (`crates/lc-core`)

- `Session::open` records `mode` in the `session` journal record.
- `ModeChanged` is journaled as `mode` with the mode label.

## TUI (`crates/lc-tui`, `crates/localcode`)

- `main.rs`: `--mode ask|accept-edits|auto|full-access`; listed in `--help`.
- `/mode`: with no argument, shows the current mode and the mapping table for
  the active provider. With `ask|accept-edits|auto` (and current mode not
  full-access): sends `SetMode`; allowed mid-turn. With `full-access`, or any
  mode while in full-access: requires an idle, ready session, shows an amber
  notice ("Full access: the agent can run any command and edit any file without
  asking. Reconnecting…" when entering), and returns `Action::Mode(mode)`, which
  `run` handles like a same-provider `Action::Model` reconnect with
  `config.mode` updated and `config.resume` set.
- Shift+Tab (`KeyCode::BackTab`): `SetMode(app.mode.cycle())`. In full-access it
  only shows "Use /mode to leave full access". Inactive while an approval dialog,
  help or palette is open.
- Header chip after `engine / model`: `ask` muted, `accept-edits` and `auto`
  ACCENT, `full-access` AMBER. Updated only by `ModeChanged`; while a Claude
  switch is pending it shows the target with a trailing `…`.
- `/mode` added to `COMMANDS` (palette), help overlay and sidebar quick commands.

## Testing

1. `lc-engine` unit tests: `Mode` parse/label/cycle; each mapping function
   checked cell by cell against the table above.
2. `lc-engine/tests/live.rs` with `lc-testkit`'s `protocol-child`:
   - Codex: initial `thread/start` params reflect `Config.mode`; after
     `SetMode(Auto)` the next `turn/start` carries `approvalsReviewer:
     "auto_review"` and `approvalPolicy: "on-request"`.
   - Claude: argv carries the mapped `--permission-mode`; `SetMode(Auto)` sends
     `set_permission_mode` and yields `ModeChanged(Auto)` on success, a `Notice`
     and no `ModeChanged` on error.
   - `SetMode(FullAccess)` is refused by the driver.
3. `lc-tui` unit tests (existing `command()` style): `/mode` parsing and errors,
   full-access idle gate and `Action::Mode`, BackTab cycle and full-access
   lockout.
4. PTY test (`crates/localcode/tests/terminal.rs`): `--engine demo --mode auto`
   shows the `auto` chip and `/approval-demo` completes without a dialog.
5. Manual smoke against the installed CLIs before claiming completion: Claude
   launched with `--mode auto` reports the mode; Codex `thread/start` with
   `auto_review` is accepted.

## Docs

`docs/tui.md` gains a "Permission modes" section with the mapping table, the
switching rules and the Codex `accept-edits` caveat; the key table gains
Shift+Tab; the commands list gains `/mode`.

## Out of scope

- Persisting the mode between launches.
- Claude's `plan`, `dontAsk` and `manual` modes, and Codex granular approval
  policies.
- Changing the Python core (`backend/app/core`) or the web UI's picker.
