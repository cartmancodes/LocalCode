# Carrying the conversation across a provider switch: design

**Date:** 2026-10-08. **Branch:** `feat/provider-handoff`.

## The problem, validated

A cross-provider `/model` (for example Claude Opus 5.5 to Codex GPT-6 Sol)
opens a brand-new vendor session. `Selection::configure`
(`crates/octet-core/src/model.rs`) sets `resume: None` for another provider,
and nothing of the earlier conversation is sent: the new model starts blind.
The transcript stays on screen and in the journal only. The switch notice says
so ("earlier displayed messages are not sent to this provider").
Same-provider switches are not affected: they resume the same vendor session.

## How others do it

- **pi** (`pi-ai`, "Cross-Provider Handoffs") owns the message history and
  resends it to the new provider: user messages and tool results unchanged,
  text and tool calls kept, another provider's thinking blocks turned into
  `<thinking>`-tagged text, provider signatures dropped.
- **opencode** (`session/message-v2.ts`, `toModelMessages`) does the same:
  for an assistant message from a different provider/model, reasoning
  becomes plain text and provider metadata is stripped.
- Both call model APIs directly. Octet drives vendor CLIs that own their
  sessions and accept no injected native history; a multi-backend tool in
  the same position (codeoid) carries the conversation into such "warm"
  backends as a **rendered transcript**, bounded, oldest turns dropped.

## Decisions (agreed)

1. **What:** a rendered transcript built from the conversation Octet shows.
2. **How much:** up to 64 KiB, oldest turns dropped first; the session's
   first prompt is always kept.
3. **How it travels:** inside the first prompt sent in the new session, as
   wire text the vendor receives while the user sees only their own text
   (the mechanism `!` attachments and goal prompts already use). It then
   lives in the vendor's own history, so it survives `/reconnect`,
   `/effort`, `/fork` and `/compact` with no extra flags.

## Behaviour

- **When:** only a cross-provider `/model`. Not a same-provider switch
  (the session resumes), `/new` (fresh context is the point), or
  `/resume N` (that vendor session has its own history).
- On such a switch Octet renders the transcript of the conversation shown so
  far, across every provider the session has used, and holds it as a
  pending handoff on the interface.
- The first prompt that starts a turn in the new session (typed, queued, or
  a goal's continuation) carries it. The wire is the handoff followed by
  the prompt's own wire text (with its `!` attachments); the display, the
  transcript and the journal's `user` record show only what the user sent.
- A note says what went: "Carried the earlier conversation to codex (14
  turns, 22 KiB)". It is journaled as a notice.
- The pending handoff is taken only when the send succeeds; a refused send
  keeps it for the next prompt.
- A second cross-provider switch before any prompt replaces the pending
  handoff with a fresh render. `/new` drops it. An empty conversation
  yields no handoff and no note.
- The switch notice becomes: "New provider context; the earlier
  conversation goes with your next prompt."

## Transcript format

```text
[Octet handoff] You are continuing a conversation the user began with
another assistant in Octet. That session cannot be resumed here, so it is
reproduced below as context. Do not redo its actions; tool calls already
ran. The user's new message follows the transcript.

<earlier-conversation-3f9a…>
User:
Fix the failing build in crates/core.

Assistant (claude):
The failure is a missing import…

Tool (claude): Bash · cargo build -p core

[… 6 earlier turns omitted …]

User:
Now run the tests.
…
</earlier-conversation-3f9a…>

The user's new message:
```

- **Kept:** the user's prompts (as displayed, with `[+ git status]` and
  `[+ image a.png]` markers), assistant replies labelled with the provider
  that wrote them, tool activity as one line each (the first line of the
  tool entry, at most 200 characters), `!` command results as one line
  (`$ cmd · exit 0`).
- **Left out:** Octet's own notes, errors and notices; images' bytes; the
  full output of tools and `!` commands (the vendor's previous session had
  them; the transcript says what ran).
- **Turns:** a turn is one user prompt and what followed it; the note's
  count is the turns the transcript carries.
- **Budget:** 64 KiB of transcript text. Entries are taken newest first
  until the budget; the first user prompt is always included; a marker
  says how many turns were omitted between them. One entry longer than the
  budget is cut from its start, keeping its end.
- **Escaping:** the text is already sanitized (`clean`) by the interface. The
  block's tag carries a random suffix chosen for each handoff
  (`<earlier-conversation-3f9a…>`), so no entry can predict its closing tag
  or close the block early; an entry's literal "The user's new message:" is
  quoted. (Revised after the final review: exact-string escaping missed case
  and spacing variants.)

## Components

- **`crates/octet-tui/src/handoff.rs` (new):** `Handoff { text, turns,
  bytes }`; `render(entries, budget) -> Option<Handoff>` (pure, unit
  tested); `Handoff::wrap(&self, wire) -> String`. The budget is a constant,
  `HANDOFF_BUDGET = 64 KiB`.
- **`Entry`** gains `engine: Engine`, the provider connected when it was
  added, so replies can be attributed.
- **`App`** gains `pending_handoff: Option<Handoff>`.
- **`reconnect::Plan`** gains `carry_conversation: bool`, true for a
  cross-provider `Exit::Model`. `run()` renders the handoff from the kept
  interface when it is set; `/new` (fresh app) has none.
- **`App::begin_turn`** wraps the first turn-starting prompt
  (`Command::Prompt` or `Command::PromptWithDisplay`) with the pending
  handoff, turning it into `PromptWithDisplay` with the same display and
  images, and takes the handoff only after `vendor.send` succeeds, then adds
  the note.
- **Engine limit:** `Command::prompt_bytes` measures the display (typed
  text, still at most `PROMPT_LIMIT`, 64 KiB) and the wire is bounded by a
  new `WIRE_LIMIT` (256 KiB). A send over either is refused as today.

## Error handling

- A wrapped prompt the engine refuses (over `WIRE_LIMIT`, busy) keeps the
  handoff and reports the refusal as any failed send does.
- If rendering finds nothing to carry, no handoff is pending and the
  switch notice says the new provider starts fresh, as today.

## Testing

- **Unit (`handoff.rs`):** rendering of each role, provider labels, tool and
  shell lines, the budget with the first prompt pinned and the omitted
  marker, an oversized single entry, the closing-tag neutralisation, empty
  conversations.
- **TUI:** a cross-provider plan sets `carry_conversation`; same-provider,
  `/new` and `/resume` do not. The first prompt after the switch reaches the
  vendor (`RecordingVendor`) with the transcript in the wire and the typed
  text as display; the second prompt carries nothing; a refused send keeps
  the handoff; a goal continuation carries it too.
- **Engine:** a wire over 64 KiB with a short display is accepted; a display
  over 64 KiB or a wire over 256 KiB is refused.
- **Live (opt-in, `live_workflows.rs`):** Claude → `/model codex` → "What
  code did I ask you to remember?" answers `OCTET_MEMORY_731`, and the
  reverse direction.

## Out of scope

- A summarised handoff written by the outgoing model (pi-handoff's
  approach); a `--summary` option can follow if the transcript proves too
  coarse.
- Native history injection: neither vendor CLI accepts it.
- Carrying context into `/resume N` of another vendor's session.
