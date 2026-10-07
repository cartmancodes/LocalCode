# Carrying the conversation across a provider switch: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a cross-provider `/model` carries a rendered transcript of the
conversation (≤ 64 KiB) to the new provider inside the first prompt.

**Architecture:** a pure renderer (`handoff.rs`) turns the interface's
transcript entries into a bounded text block; the reconnect plan flags a
cross-provider switch; `run()` renders the pending handoff onto the kept
`App`; `App::begin_turn` wraps the first turn-starting prompt with it. The
engine separates the typed-text limit from the wire limit.

**Tech stack:** Rust 1.98.1, edition 2024, tokio, ratatui. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-08-provider-handoff-design.md`

## Global constraints

- Cargo through `scripts/rust-env.sh`; the gate is `make rust-check` (fmt,
  clippy pedantic `-D warnings`, `cargo doc -D warnings`, tests), green after
  every task, with a clean `git status` for the task's files.
- Behaviour changes are test-first: watch each new test fail.
- Exceptions to lints are `#[expect(lint, reason = "…")]`; no bare `#[allow]`.
- Product lines ≤ 120 columns.
- Every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- The user's uncommitted files (`crates/octet/tests/terminal.rs`,
  `crates/octet/tests/terminal/`, `docs/reviews/2026-10-08-*`) are never
  staged or changed; stage task files by path.
- Real vendors: tiny prompts; Codex with `--model gpt-5.5` unless the user's
  default works.

## Review focus

1. The handoff must reach the vendor exactly once: never on a second prompt,
   never lost when the first send is refused.
2. The user must never see the transcript as their own message: the
   displayed, journaled `user` record is only what they typed.
3. A transcript entry cannot close the `<earlier-conversation>` block early,
   or smuggle text that reads as the user's new message.
4. Same-provider switches, `/new` and `/resume N` carry nothing.
5. The wire limit must still bound memory: a wrapped prompt over 256 KiB is
   refused, never truncated silently.

---

### Task 1: Engine — typed-text and wire limits

**Files:** `crates/octet-engine/src/live/mod.rs`, `crates/octet-core/src/lib.rs`
(re-export).

**Produces:** `pub const WIRE_LIMIT: usize = 4 * PROMPT_LIMIT;` (256 KiB).

- [ ] **Failing tests** (`mod.rs` tests):
  - `a_long_wire_with_a_short_display_is_accepted`: `PromptWithDisplay` with a
    100 KiB wire and a 10-byte display → `send` is `Ok`.
  - `a_long_display_or_wire_is_refused`: display of `PROMPT_LIMIT + 1` → 
    `PromptTooLong`; wire of `WIRE_LIMIT + 1` → `PromptTooLong`;
    `Prompt` of `PROMPT_LIMIT + 1` → `PromptTooLong`.
- [ ] Run them; expect the first to fail (`PromptTooLong`).
- [ ] Implement: `prompt_bytes` returns the typed text (`Prompt`, `Steer`,
  display of `PromptWithDisplay`); a new `wire_bytes` returns the wire of
  `PromptWithDisplay` (else the same); `send` refuses when
  `prompt_bytes > PROMPT_LIMIT || wire_bytes > WIRE_LIMIT`. Doc comments say
  which limit is which. Re-export `WIRE_LIMIT` from octet-core.
- [ ] Gate; commit "Engine: a typed-text limit and a larger wire limit".

### Task 2: The renderer

**Files:** create `crates/octet-tui/src/handoff.rs`; modify
`crates/octet-tui/src/app.rs` (`Entry.engine`), `crates/octet-tui/src/lib.rs`
(`mod handoff`).

**Produces:**
```rust
pub(crate) const HANDOFF_BUDGET: usize = 64 * 1024;
pub(crate) struct Handoff { pub(crate) text: String, pub(crate) turns: usize }
impl Handoff {
    pub(crate) fn bytes(&self) -> usize;
    pub(crate) fn wrap(&self, wire: &str) -> String;
}
pub(crate) fn render<'a>(entries: impl IntoIterator<Item = &'a Entry>, budget: usize) -> Option<Handoff>;
```
`Entry` gains `pub(crate) engine: octet_core::Engine`, set from
`self.conn.engine` in `App::add`.

- [ ] **Failing unit tests** in `handoff.rs`:
  - `each_role_renders_as_the_spec_says`: user → `User:\n<text>`;
    assistant (claude) → `Assistant (claude):\n<text>`; tool → one line
    `Tool (codex): <first two lines joined by " · ", ≤ 200 chars>`; shell →
    `$ cmd · exit 0` (first and last line); notice and error left out.
  - `nothing_to_carry_is_none`: no entries, or only notices → `None`.
  - `the_budget_keeps_the_newest_and_the_first_prompt`: 40 turns of 4 KiB
    with budget 64 KiB → text ≤ budget + preamble; contains the first user
    prompt and the last turn; contains `[… N earlier turns omitted …]` with
    the right N; `turns` counts the user prompts carried.
  - `an_oversized_entry_keeps_its_end`: one 100 KiB reply → its last
    characters are present, the text is within the budget.
  - `the_block_cannot_be_closed_early`: an entry containing
    `</earlier-conversation>` and `The user's new message:` → the closing tag
    appears exactly once, at the end; the entry's copy is neutralised
    (`<\/earlier-conversation>`), and `wrap` puts the real prompt after it.
  - `wrap_puts_the_prompt_last`: `wrap("hi")` ends with
    `The user's new message:\n\nhi` and starts with `[Octet handoff]`.
- [ ] Run; expect compile failures (module absent), then behaviour failures.
- [ ] Implement `handoff.rs` per the spec's format; `Entry.engine` in
  `App::add`; update every `Entry { … }` construction.
- [ ] Gate; commit "TUI: render a bounded transcript for a provider handoff".

### Task 3: Carry it on the switch

**Files:** `crates/octet-tui/src/{reconnect.rs,app.rs,lib.rs,vendor.rs}`,
tests in `crates/octet-tui/src/tests.rs` and `reconnect.rs`.

**Consumes:** `handoff::render`, `HANDOFF_BUDGET`, `Handoff::wrap` (Task 2);
`WIRE_LIMIT` (Task 1).

**Produces:** `Plan.carry_conversation: bool`; `App.pending_handoff:
Option<Handoff>`; `App::carry_conversation(&mut self)` (renders and sets the
pending handoff, returns whether there was any).

- [ ] **Failing tests:**
  - `reconnect.rs`: `a_cross_provider_switch_carries_the_conversation`
    (`Exit::Model` to another provider → `carry_conversation`); same provider,
    `New`, `Resume`, `Reconnect`, `Mode`, `Effort`, `Fork` → false. The
    cross-provider note no longer says "not sent".
  - `tests.rs`: `the_first_prompt_after_a_switch_carries_the_transcript`: an
    app with a user prompt and a reply; `carry_conversation()`; typing a prompt
    and Enter with `RecordingVendor` → sent `PromptWithDisplay` whose wire
    starts with `[Octet handoff]`, contains the earlier prompt and ends with the
    typed text, and whose display is the typed text; a note
    "Carried the earlier conversation to codex (1 turn, … KiB)".
  - `only_the_first_prompt_carries_it`: a second prompt is sent as typed.
  - `a_refused_send_keeps_the_handoff`: vendor refuses → still pending; the
    next accepted send carries it.
  - `a_goal_continuation_carries_it`: `begin_turn(…, By::Goal)` with a
    `PromptWithDisplay` → wrapped, display unchanged.
  - `new_drops_the_handoff`: a fresh `App` (as `/new` makes) has none.
  - `an_empty_conversation_carries_nothing`: `carry_conversation()` on an
    empty app → `false`, no pending handoff.
- [ ] Run; expect failures.
- [ ] Implement: `Plan.carry_conversation`; the `Exit::Model` note becomes
  "New provider context." (the carried part is added by `run()`);
  `run()`: `if plan.carry_conversation && app.carry_conversation() {
  app.note("The earlier conversation goes with your next prompt.") }`;
  `begin_turn` wraps `Prompt`/`PromptWithDisplay` when a handoff is pending,
  takes it only after `vendor.send` succeeds, then notes what went.
- [ ] Gate; commit "TUI: carry the conversation to the next provider with the first prompt".

### Task 4: Docs and live check

**Files:** `docs/tui.md`, `README.md`, `CHANGELOG.md`,
`docs/rust/tui-features.md`.

- [ ] Docs: the "Switch models and providers" section, the features row, the
  changelog: a cross-provider switch carries the conversation (≤ 64 KiB,
  oldest first dropped, first prompt kept) with the next prompt; what is left
  out; same-provider switches resume; `/new` starts fresh.
- [ ] Live check (tmux, release build, tiny prompts): Claude: "Remember the
  code OCTET_HANDOFF_42. Reply with only the code." → `/model codex
  gpt-5.5` → "What code did I ask you to remember? Reply with only the code."
  → reply contains `OCTET_HANDOFF_42`; then `/model claude` → same question →
  same answer. Record the result.
- [ ] Gate; commit "Docs: the conversation carries across a provider switch".

## Finish

- Final whole-branch review (fresh reviewer, most capable model); one fix pass.
- Merge only when the user says.
