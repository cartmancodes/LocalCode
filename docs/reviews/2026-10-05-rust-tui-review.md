# Rust TUI structure and functional review — 2026-10-05

## Scope and result

Reviewed the current Rust-only workspace: subprocess supervision, provider
protocols, session journaling, goal persistence, command handling, rendering,
and terminal lifecycle. The existing crate boundaries are appropriate for the
preview. Two reproduced goal defects were fixed, together with an unnecessary
write on goal queries. The offline suite and live Claude smoke test pass.
Live Codex inference was blocked by the account's usage limit.

## Findings fixed

### Failed goal turns were omitted from the counter

The provider adapters emit an error before the failed terminal outcome. The
TUI paused the goal and cleared `goal_running` on the error, so the later
terminal event never reached `Goal::finish_turn`. A failed first turn displayed
zero turns and did not consume the 200-turn allowance.

The error path now uses `finish_turn("failed", ...)` before clearing the flag.
This counts the turn once, pauses the goal, and prevents continuation. The
existing PTY scenario now asserts the persisted count for both Codex and Claude;
the new assertion failed before the fix and passes after it.

### A malformed goal file prevented the entire TUI from opening

Goal-load errors propagated out of `run`, restoring the terminal and exiting
before ordinary chat or `/goal clear` could be used. Startup now displays a
recovery notice and permits chat. The original file remains available for
inspection until the user explicitly clears or replaces it.

A new PTY test writes a malformed goal, opens the TUI, requests goal status,
completes an ordinary demo turn, verifies that the original bytes remain, and
clears the file through `/goal clear`. The test reproduced the startup failure
before the fix.

### Goal queries performed unnecessary durable writes

The common `/goal` command tail saved state even after status queries, rejected
commands, or attempts to resume a missing goal. Besides unnecessary file creation,
sync, and rename work, this would remove an unreadable goal file during a status
query after startup recovery. Only actual pause/clear mutations now use that
shared save path; start/resume/audit retain their existing explicit persistence.
The malformed-file PTY test covers preservation during a status query.

### Follow-up: paused and cancelled goal turns were also omitted

Re-evaluating the first fix found the same omission on another path.
`/goal pause` and Esc/Ctrl+C set the goal to paused while its turn was still
running, and `Goal::finish_turn` returned early for any non-active goal. Those
turns were never counted. A turn that finished after `/goal pause` also lost
any completion evidence it reported. `finish_turn` now counts every goal turn
that ends, records completion when the marker and evidence are present, and
continues only while the goal is still active. A new unit test and a turn-count
assertion in the pause/cancel PTY scenario both failed before the change.

The PTY test helper's `Drop` waited for the killed child without reading the
terminal. On macOS the child cannot finish exiting until its pending output is
read, so any failed PTY assertion hung the suite instead of failing. `Drop` now
drains the terminal while it waits, for at most five seconds.

## Structure and quality assessment

- `octet-proc` owns process groups, bounded transport, stderr retention, and cleanup.
  Cancellation and shutdown have independent control paths. Transport tests
  cover saturated stdout, partial frames, malformed data, cancelled writes,
  repeated shutdown, and descendant cleanup.
- `octet-engine` translates vendor wire protocols into common events. Contract and
  driver tests cover mode confirmation/refusal, approval timeouts, streaming
  deduplication, turn correlation, late usage, and silence watchdogs.
- `octet-core` places the journal boundary before UI publication and keeps model
  selection and goal state independent of rendering. Export is exclusive and
  does not overwrite an existing destination.
- `octet-tui` bounds visible history, sanitizes streamed escape sequences, caches
  wrapped transcript entries, and paints only when state changes. PTY tests
  exercise actual input, resize, suspend/resume, and exit restoration.
- The main maintainability pressure is concentrated control flow:
  `octet-engine/src/live.rs` combines both provider state machines, and
  `octet-tui/src/lib.rs` combines terminal ownership, commands, and goal orchestration.
  Extracting those responsibilities would be useful when extending them. A broad
  split was not needed for the reproduced defects and was not attempted here.
  The follow-up [quality review](2026-10-05-rust-quality-review.md) made that
  split.

## Verification evidence

- `make rust-check`: 119 passed after the follow-up, zero failed, three opt-in tests ignored;
  rustfmt and Clippy with warnings denied passed.
- The offline terminal suite covers both providers in ask, accept-edits, auto,
  and full-access modes using scripted vendor processes. Goal cases include
  continuation, completion, reload, pause, cancellation, resume, failure,
  audit, clear, and malformed-file recovery.
- Installed Claude live auto-mode goal test: passed. It wrote `smoke.txt` in a
  disposable workspace, read it back, and persisted completion evidence.
- Installed Codex live auto-mode goal test with `gpt-5.5`: connection succeeded,
  but the turn returned `usageLimitExceeded`. The UI recorded the error and
  paused the goal. This run does not establish successful live Codex inference;
  deterministic Codex tests passed.
- Release-mode idle diagnostic: zero repaint bytes over five idle seconds;
  RSS remained 4,016 KiB and reported cumulative CPU remained 0.01 seconds.
  Ready was observed after approximately 1.59 seconds. This is one local sample
  with uncontrolled cache/load, not a startup percentile or long-duration gate.
- Release-mode transport diagnostic: 2,000 sequential JSON echo round trips,
  median 33 µs, p95 59 µs, p99 72 µs. These measure the local transport fixture,
  not model latency or terminal rendering throughput.

## Remaining scope limits

The [parity matrix](../rust/parity-matrix.md) still lists transcript replay and
crash recovery, session browsing, fleet, plugins/resources, additional provider
controls, and platform/release benchmarks as incomplete. These features were not
implemented or represented as passing in this review. Linux, SSH, and tmux
acceptance were not rerun on this macOS host.
