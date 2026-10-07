# Pending Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build F1 to F10 from the spec: follow-up queue, steering,
reasoning effort, fork, compaction, images, session browser,
print/JSON/RPC modes, release archives and the deferred minors.

**Architecture:**
- Vendor capabilities sit behind the `Protocol` trait. New methods have
  defaults, so a provider that lacks a feature says so in one place.
- The TUI drives everything through `octet_core::Command` and the provider
  table.
- Print and RPC modes reuse `Session` without the TUI.

**Tech Stack:** Rust 1.98.1, tokio, serde_json, thiserror. Base64 for Claude
images is hand-rolled; the TUI already has an encoder. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-07-pending-features-design.md`

## Global Constraints

- **Gate:** `make rust-check` passes after every task (fmt, all tests and
  doctests, clippy `-D warnings` with the rust-engineer lints).
- **Skill:** follow `.claude/skills/rust-engineer/SKILL.md`. Public items
  have docs, with `# Errors` where needed. No `unwrap` outside tests.
  Errors are `thiserror` enums at library boundaries.
- **Existing behaviour:** unchanged unless a feature changes it on purpose.
  Pinned messages keep their wording.
- **Limits:** every new input is bounded (queue 8, image 5 MiB, 4 images per
  prompt, 20 sessions listed, 64 KiB read per journal).
- **Approvals:** fail closed everywhere, including print mode.
- **Commits:** one per task. Every message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Esc during a running turn** drops the follow-up queue and cancels. It
   must never send a queued prompt into the cancelled turn.
2. **`/steer` on Codex when the turn ID is not known yet** (before
   `turn/started`) must fall back to queueing, not send a malformed request.
3. **A Claude image over 5 MiB, or with an unknown extension,** is refused
   before anything is sent. The draft stays.
4. **A print-mode approval** is denied, never left waiting forever. The
   process exits non-zero when the turn fails.
5. **Session browsing** over a journal with a torn last line, or a non-journal
   file in the directory, lists the rest and does not error.

---

### Task 1: Deferred minors (F10)

**Steps:**
- **Cmd coverage:** add `const ALL: [Cmd; 11]` (`#[cfg(test)]` or `pub(crate)`)
  and a test that each variant has exactly one `COMMANDS` row.
- **`duration_text`:** add the test cases
  `Duration::from_millis(1500)` → "1500 ms" and `0` → "0 ms". Make it return
  milliseconds whenever `subsec_nanos() != 0`.
- **Vendor titles:** codex.rs and claude.rs use `PROVIDER.title` instead of
  `super::Engine::{CODEX,CLAUDE}.title()`.
- **`Engine::new`** becomes `pub(crate) const fn`.
- **Comments:** on `Limits.mode_confirm` (a protocol's mode-confirmation
  timer; Claude uses it) and on `Core.request_id` (starts at 10; Codex
  reserves 1–3).
- **Docs:** the guide's method table gains the `mode_change_pending` row and
  the `initialize` wording. `docs/README.md` links the guide.
- **Live test `cancel_while_handshaken_is_cancelled`:** a script vendor
  answers `id:1` only; cancel; expect "Connection cancelled".
- **CI:** `.github/workflows/check.yml` moves to `actions/checkout@v5`.
- **Gate:** `make rust-check`. Commit: "Clear the deferred review minors".

### Task 2: Follow-up queue (F1)

**Files:** `octet-tui/src/{app.rs, input.rs, lib.rs, view.rs, commands.rs}`,
`app/tests.rs` and `tests.rs`.

**Interfaces:**
- Produces:
  - `Composer.queue: VecDeque<Queued>`, with
    `Queued { wire: String, display: String }`;
  - `const QUEUE_LIMIT: usize = 8`;
  - `App::queue_prompt(...) -> bool`;
  - `App::next_queued() -> Option<Queued>`;
  - `App::drop_queue() -> usize`;
  - `Cmd::Queue` (`/queue`, `/queue clear`).

**Steps:**
- **Tests first:**
  - `enter_while_running_queues_the_prompt`: running app, type "next",
    press Enter. Queue length is 1, the draft is empty, and the title shows
    "+1 queued".
  - `a_finished_turn_sends_the_next_queued_prompt`: a demo session with one
    queued prompt. When `Finished` arrives, a `Started` follows, and the
    queue is empty.
  - `cancel_drops_the_queue`.
  - `the_queue_is_bounded`: the 9th prompt gives a notice and the draft
    stays.
  - `slash_queue_lists_and_clears`.
- **Implementation:**
  - **Enter while running:** if `app.conn.is_running()`, build the command
    exactly as for a send (attachments included) and push it as a `Queued`
    instead of sending. Clear the draft and attachments, and add it to
    history.
  - **`session_event`:** after `Finished`, when the goal returned
    `Next::Idle`, pop and send the next queued prompt (`Command::PromptWithDisplay`).
  - **`cancel_turn`:** drops the queue and gives the notice when it held
    anything.
  - **Title:** the prompt-box chip gains "+N queued".
- **Gate:** `make rust-check`. Commit: "Queue prompts typed during a turn".

### Task 3: Steering (F2)

**Files:** `octet-engine/src/live/{mod.rs, protocol.rs, codex.rs, claude.rs, driver.rs}`,
the TUI's `commands.rs`, and `protocol-child.rs`.

**Interfaces:**
- `Command::Steer(String)`.
- `Protocol::steer(&mut self, core, text) -> impl Future<Output = Result<bool, DriverError>> + Send`,
  with default `Ok(false)` (not steered).
- `Provider.steer: bool`.
- `Event::Notice` when the driver falls back to queueing.

**Steps:**
- **Fake vendor:** Codex `turn/steer` records the input text and emits
  `item/agentMessage/delta` with "steered:<text>". It answers with an error
  when `expectedTurnId` does not match the active turn.
- **Tests first:**
  - live `codex_steer_adds_to_the_running_turn`: "hold", then `Steer("x")`;
    text "steered:x" arrives.
  - live `steer_without_a_turn_is_refused`: Idle, `Steer` → a Notice.
  - TUI `steer_on_claude_queues_a_follow_up`.
- **Driver (`on_command(Steer)`):**
  - if `phase != InTurn`, a Notice "No turn is running; send it as a prompt";
  - otherwise `protocol.steer(core, &text)`; when it returns `false`, a
    Notice "<title> cannot steer; queued as a follow-up". The TUI queues it
    when `!engine.provider().steer`.
- **Codex `steer`:**
  - if `self.turn` is `None`, return `Ok(false)`;
  - otherwise send `turn/steer {threadId, expectedTurnId, input: [{type: "text", text}]}`
    and emit `Event::User(format!("[steer] {text}"))`.
- **TUI:** `/steer TEXT` sends `Command::Steer` when running and the provider
  can steer; queues it otherwise; sends it as a prompt when idle.
- **Gate:** `make rust-check`. Commit: "Steer a running Codex turn; queue
  steering for Claude".

### Task 4: Reasoning effort (F3)

**Interfaces:**
- `Config.effort: Option<String>`;
- `Command::SetEffort(Option<String>)`;
- `Exit::Effort(Option<String>)`;
- the `--effort LEVEL` flag;
- `Cmd::Effort`.

**Steps:**
- **Fake vendor:** the `params` script already echoes the turn params, so it
  shows `effort`. The Claude `argv` script echoes the launch arguments.
- **Tests first:**
  - live `codex_sends_effort_on_each_turn`;
  - live `claude_launches_with_effort`;
  - TUI `effort_command_shows_and_sets`;
  - CLI `invalid_effort_is_a_startup_error` (an empty or control-character
    value).
- **Implementation:**
  - **Codex:** `send_prompt` adds `params["effort"]` when it is set; `SetEffort`
    updates `core.config.effort` and gives the "applies from the next turn"
    notice.
  - **Claude:** `launch_args` adds `--effort`. `SetEffort` on Claude asks the
    TUI to reconnect: `Exit::Effort` resumes the same session, as `/model`
    does within a provider. The rule is: `provider.name == "claude"` →
    reconnect; otherwise live. Express it with a provider-row field
    `effort_live: bool` (Codex true, Claude false, demo false).
- **Kept** across `/model`, `/new`, `/reconnect` and `Selection::configure`.
- **Gate:** `make rust-check`. Commit: "Set reasoning effort with --effort
  and /effort".

### Task 5: Fork and compaction (F4, F5)

**Interfaces:**
- `Config.fork: bool`, used once at connect;
- `Exit::Fork`;
- `Cmd::Fork` and `Cmd::Compact`;
- `Command::Compact`;
- `Protocol::compact(&mut self, core) -> impl Future<Output = Result<(), DriverError>> + Send`
  (required; the demo has no `Protocol`).

**Steps:**
- **Fake vendor:**
  - Codex `thread/fork` replies `{thread: {id: "forked-thread"}}`, plus the
    mode echo.
  - Codex `thread/compact/start` acknowledges, then emits `turn/started`,
    `item/completed {type: "contextCompaction"}` and `turn/completed` on the
    thread.
  - Claude `argv` shows `--fork-session`. The Claude message "/compact"
    replies with a result "Compacted".
- **Tests first:**
  - live `codex_fork_opens_a_new_thread`: `Config { resume: Some("fixture-thread"), fork: true }`
    gives `Ready { session: "forked-thread" }`;
  - live `codex_compact_runs_as_a_turn`: `Started`, then `Finished(Completed)`;
  - live `claude_fork_passes_fork_session`;
  - TUI `fork_needs_an_idle_vendor_session`.
- **Implementation:**
  - **Codex `response` (id 1):** when `config.fork` and a resume ID are set,
    the method is `thread/fork` with `{threadId}` plus the thread params.
  - **Codex `compact`:** sends `thread/compact/start` as request `id: 4`. The
    driver has already set `InTurn` and emitted `User("/compact")` and
    `Started`; the compaction turn's events flow through the normal handling.
  - **Claude `compact`:** `send_prompt(core, "/compact")`.
  - **Driver:** `on_command(Compact)` is like `start_turn`, with display
    "/compact", but calls `protocol.compact`.
  - **Demo:** `/compact` and `/fork` say "The offline demo has no context to
    compact/fork".
  - **TUI `/fork`:** `Exit::Fork` sets `config.resume = Some(session)` and
    `config.fork = true`, reconnects, then clears `fork`.
- **Gate:** `make rust-check`. Commit: "Fork a session and compact its
  context".

### Task 6: Images (F6)

**Interfaces:**
- `octet_core::ImageAttachment { path: PathBuf, media_type: &'static str, name: String }`;
- `Command::PromptWithImages { wire, display, images: Vec<ImageAttachment> }`.
  Alternatively, extend `PromptWithDisplay`. Decide at implementation and
  record it.
- `Protocol::send_prompt` receives the images.
- `Cmd::Image`.

**Steps:**
- **Validation, in `octet-core`:** `ImageAttachment::open(path) -> Result<Self, ImageError>`
  checks the extension (png, jpg, jpeg, gif, webp), existence and size
  (≤ 5 MiB). Test it with temp files.
- **Fake vendor:** Codex echoes the `localImage` paths it receives as text.
  Claude echoes image blocks as "image:<media_type>:<base64 length>".
- **Tests:**
  - live `codex_receives_local_image_paths`;
  - live `claude_receives_base64_image_blocks`;
  - TUI `image_attaches_and_shows_in_the_title`;
  - TUI `oversized_or_unknown_images_are_refused`;
  - the limit of 4 images.
- **Codex:** the input array gets `{type: "localImage", path}` per image after
  the text. **Claude:** `content` gets one `{type: "image", source: {type: "base64", media_type, data}}`
  per image. The bytes are read at send time; a read failure fails the turn
  with an error event.
- **TUI `/image PATH`:** path resolved against the workspace, `~/` expanded;
  stored in `Composer.images`; title "+ image name.png"; Esc on an empty
  prompt drops it; display text `[+ image name.png]`.
- **Gate:** `make rust-check`. Commit: "Attach images to a prompt".

### Task 7: Session browser (F7)

**Interfaces:**
- `octet_store::read_summary(path) -> Option<JournalSummary>`, with
  `JournalSummary { started, engine, model, session, first_prompt }`;
- `octet_core::recent_sessions(directory, workspace, limit) -> Vec<(PathBuf, JournalSummary)>`;
- `Cmd::Sessions`, and `Cmd::Resume` taking `N`;
- `Exit::Resume { engine, session }`.

**Steps:**
- **Tests first:**
  - store `summary_survives_a_torn_tail`;
  - store `non_journals_are_ignored`;
  - core `recent_sessions_lists_newest_first_for_this_workspace`;
  - TUI `resume_picks_the_listed_session`.
- **Reader:**
  - reads at most 64 KiB and stops at the first `user` record;
  - the header record gives engine, cwd, model and start time (from the
    file name's nanosecond stamp);
  - `ready` gives the session.
  - A line that fails to parse ends the read; lines already read count.
- **Filter:** workspace equals `config.cwd` (canonicalised); the session is
  not empty.
- **`/resume N`:** `Selection` with that provider; `config.resume = Some(session)`.
- **Gate:** `make rust-check`. Commit: "Browse and resume recent sessions".

### Task 8: Print, JSON and RPC modes (F8)

**Files:** create `crates/octet/src/headless.rs`; modify `main.rs`; tests in
`crates/octet/tests/headless.rs`.

**Interfaces:**
- `--print TEXT|-` (`-p`), `--output text|json` and `--rpc`.
- `async fn print(config, directory, prompt, json: bool) -> Result<i32, HeadlessError>`.
- `async fn rpc(config, directory) -> Result<i32, HeadlessError>`.

**Steps:**
- **Tests first (process-level, fake vendor):**
  - `print_writes_the_reply_and_exits_zero`;
  - `print_reads_the_prompt_from_stdin`;
  - `print_json_writes_event_lines`;
  - `print_denies_approvals_and_says_why` (the `approval` script; stderr
    names `--mode auto`);
  - `print_failed_turn_exits_one`;
  - `rpc_runs_prompts_and_answers_approvals`;
  - `rpc_quits_on_eof`.
- **`print`:**
  - opens a `Session`, waits for `Ready`, sends the prompt;
  - streams `Text` to stdout (text mode) or every event (JSON mode);
  - on `Approval`, answers deny with a notice to stderr;
  - ends on `Finished`, with the exit code from the outcome;
  - SIGINT interrupts, then exits 130.
- **`rpc`:**
  - a select loop over stdin lines and session events;
  - commands parse to `Command`, or interrupt, or quit;
  - an invalid command produces an `{"type":"error"}` line;
  - events go out as JSON lines.
- **`main.rs`:** these modes skip the interactive-terminal check. `--print`
  and `--rpc` are mutually exclusive.
- **Gate:** `make rust-check`. Commit: "Add print, JSON and RPC modes".

### Task 9: Release archives (F9)

- **Create `.github/workflows/release.yml`.** On push of tags `v*`:
  - a matrix of (macos-latest, aarch64-apple-darwin),
    (ubuntu-latest, x86_64-unknown-linux-gnu) and
    (ubuntu-24.04-arm, aarch64-unknown-linux-gnu);
  - each runs `cargo build --release --locked -p octet --target $T`, packs
    the binary with README, LICENSE-* and CHANGELOG, writes a SHA-256 file
    and uploads an artifact;
  - a final job, with `permissions: contents: write`, creates the release
    with `gh release create` and attaches everything.
- **Check:** `actionlint`. Commit: "Build release archives for tagged
  versions".

### Task 10: Docs

- **README:** the "Not yet supported" list shrinks to the deferred items.
- **`docs/tui.md`:** gets sections on the queue, steering, effort, fork,
  compaction, images, sessions, and print/JSON/RPC.
- **`docs/rust/parity-matrix.md` and `tui-features.md`:** move the shipped
  rows to done; deferred rows carry their reasons.
- **CHANGELOG:** "Added" entries.
- **Commit:** "Document the new features and what remains".

## Finish

- Final whole-branch review on the most capable model.
- A fix pass, if the review finds anything.
- A live tmux run.
- Real-Claude checks for effort, fork, compact, image and print mode, using
  tiny prompts in a throwaway workspace.
- Merge, as the user directs.
