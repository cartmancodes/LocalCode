# Persistent goals in the Rust TUI

`/goal` keeps the current Claude or Codex provider working toward an objective
across multiple turns. Goal state is stored per workspace alongside the preview
journals and is independent of vendor plugins.

## Usage

Start LocalCode normally, for example:

```sh
./target/release/localcode --engine codex
```

| Command | Behavior |
| --- | --- |
| `/goal <objective>` | Saves a new objective and starts its first turn. |
| `/goal` or `/goal status` | Shows the objective, status, turn count, and completion evidence. |
| `/goal pause` | Stops automatic continuation; the current turn may finish. |
| `/goal resume` | Resumes a paused goal in the current provider context. |
| `/goal complete` | Requests an audit turn to verify the objective and address gaps. |
| `/goal clear` | Removes stored goal state; the current turn may finish. |

Esc/Ctrl+C pauses an active goal while interrupting its turn. Failed turns and
the 200-turn guard also pause it. Once the guard is reached, set a new goal to
continue. Model switches pause goals. Switching providers retains the objective
but starts fresh vendor context; earlier conversation is not transferred.

Goal files are written atomically with private permissions. After restart, an
active goal loads paused and requires `/goal resume`; no inference starts
automatically.

If a saved goal cannot be read or parsed, the TUI shows a recovery notice and
still allows ordinary chat. Inspecting `/goal status` leaves the saved file
unchanged. Use `/goal clear` to remove it or set a new objective to replace it.
Failed active-goal turns count toward the turn guard before the goal pauses.

## Completion and limits

After each successful turn, LocalCode requests another turn unless the assistant
provides a nonempty evidence summary followed by `[[LOCALCODE_GOAL_COMPLETE]]`
alone on its final line. LocalCode then records the evidence and marks the goal
complete. This is a model-reported audit, not independent verification.

The design takes inspiration from [Claurst's goal workflow](https://github.com/Kuberwastaken/claurst/blob/main/docs/commands.md),
including persistent state, turn continuation, and a 200-turn guard. LocalCode's
current adapters use the final-line marker instead of Claurst's typed
`GoalComplete` tool. A shared completion tool and normalized token accounting
remain future work. There is no token-budget option yet because the two drivers
do not expose a comparable, reliable per-turn token count.

Goal output retained for completion detection is bounded to 64 KiB. Continuation
uses the existing provider process and event loop, without an additional polling
loop or subprocess.

## Verification

`scripts/rust-env.sh cargo test --locked --workspace` includes real-terminal
tests of goals through both provider adapters in ask, accept-edits, auto, and
full-access modes. Scripted vendor processes make continuation, completion,
restart, pause, cancellation, resume, failure, audit, and clear deterministic.

The opt-in live test uses installed, authenticated vendor CLIs and subscription
usage. Each provider creates and reads back one file in a disposable workspace:

```sh
LOCALCODE_LIVE_ENGINE=codex LOCALCODE_LIVE_CODEX_MODEL=gpt-5.5 \
  scripts/rust-env.sh cargo test --locked -p localcode \
  installed_providers_complete_a_goal_in_auto_mode -- --ignored --nocapture
LOCALCODE_LIVE_ENGINE=claude \
  scripts/rust-env.sh cargo test --locked -p localcode \
  installed_providers_complete_a_goal_in_auto_mode -- --ignored --nocapture
```

Choose a model supported by your account. `LOCALCODE_LIVE_CLAUDE_MODEL` also
overrides the Claude model; without an override, the vendor default is used.
Both live auto-mode checks passed on macOS with Codex 0.154.0 (`gpt-5.5`) and
Claude Code 2.1.270. The configured Codex default `gpt-6.1-sol` was rejected by
the host account, so launching with `--model gpt-5.5` was required for that check.
