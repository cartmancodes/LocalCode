# Pending features: design

Date: 2026-10-07. Status: decided under the session goal "find the list of
pending features and implement them as per best practices". The decisions
below were made without stopping to ask the user, as that goal directs; each
one names its reason.

## Where "pending" is written down

Three places list unfinished work:

- the README's "Not yet supported" list;
- `docs/rust/parity-matrix.md`;
- `docs/rust/tui-features.md`, "Preview boundaries".

Together they name:

- image input;
- steering and follow-up queues;
- thinking and compaction controls;
- fork;
- session browsing;
- print, JSON and RPC modes;
- prebuilt release archives;
- the vendors' plugin, hook and resource surfaces;
- multi-agent (fleet) orchestration;
- quota routing;
- durable history with branching, labels and torn-tail repair;
- legacy RPC compatibility.

The deferred minors from the 2026-10-06 and 2026-10-07 reviews are pending
too.

## What both vendors support today

Checked against the installed CLIs: Codex 0.154 (its generated app-server
JSON schema) and Claude Code 2.1.289 (`--help`). The protocol gate's live
`fork`, `compact` and `resume` scenarios add evidence.

| Feature | Codex app-server | Claude Code stream-json |
| --- | --- | --- |
| Steering a running turn | `turn/steer {threadId, expectedTurnId, input}` | No steering request; a message sent mid-turn is queued by the CLI |
| Images | `input: [{type: "localImage", path}]` | user content block `{type: "image", source: {type: "base64", media_type, data}}` |
| Reasoning effort | `turn/start.effort` (string advertised by the model) | `--effort low\|medium\|high\|xhigh\|max` at launch |
| Fork | `thread/fork {threadId}` returns a new thread | `--resume <id> --fork-session` |
| Compaction | `thread/compact/start {threadId}` runs as a turn on the thread | the user message `/compact` (gate scenario `compact`) |
| Session list | `thread/list` (vendor history) | none in stream-json; Octet's own journals list sessions |

## Scope

Each feature below has evidence above and fits Octet's model: one active
vendor session, a bounded transcript, and approvals that fail closed. Each is
built test-first against the fake vendor. Claude features are also checked
with tiny prompts against the real CLI; Codex is checked only where the
user's quota allows.

### F1. Follow-up queue

- **While a turn runs,** Enter queues the draft instead of refusing it:
  - at most 8 queued prompts;
  - the prompt box title shows "+N queued".
- **When the turn finishes,** the next queued prompt is sent. Attachments go
  with the prompt they were sent with.
- **Cancelling a turn** (Esc or Ctrl+C) drops the queue, with a notice
  "Dropped N queued prompts". A user who stops the agent usually wants it
  stopped.
- **`/queue`** lists the queue; **`/queue clear`** empties it.
- **The goal runner** takes priority: a goal continuation goes before queued
  prompts. Queued prompts then wait until the goal stops.

### F2. Steering

`/steer <text>` adds `<text>` to the running turn:

- **Codex:** `turn/steer` with the current turn ID.
- **Claude:** the CLI has no steering request, so `/steer` queues the text as
  a follow-up and says so.
- **Provider row:** a `steer` capability says which.
- **No turn running:** `/steer` behaves like sending a prompt.

### F3. Reasoning effort

- **Set it:** `--effort LEVEL` at launch and `/effort LEVEL` at runtime;
  `/effort` alone shows it.
- **Levels** are free text checked as identifiers, as model names are. The
  vendor says which it accepts. The help suggests `low`, `medium`, `high`,
  `xhigh`, `max`.
- **Codex** sends `effort` on every `turn/start`. A change applies from the
  next turn, like modes.
- **Claude** takes `--effort` at launch, so a change reconnects to the same
  session (as `/model` does within a provider).
- **Persistence:** `Config.effort: Option<String>` is kept across `/model`,
  `/new` and `/reconnect`.

### F4. Fork

`/fork` continues the conversation in a new vendor session, leaving the
original as it was.

- **Codex:** reconnect, opening the session with `thread/fork {threadId}`
  instead of `thread/resume`.
- **Claude:** reconnect with `--resume <id> --fork-session`.
- **The TUI** keeps the transcript and says "Forked from <id>"; the new
  session's ID replaces the old.
- **Conditions:** an idle session with a vendor session ID. The demo has
  neither.

### F5. Compaction

`/compact` asks the vendor to compact its context. It runs as a turn, so
Esc cancels it and the turn's events stream as usual.

- **Codex:** `thread/compact/start`; the compaction turn's events go through
  the normal turn handling.
- **Claude:** sends `/compact` as the user message.
- **Transcript:** shows "/compact".

### F6. Images

`/image PATH` attaches an image to the next prompt, the way `!` output is
attached:

- **Prompt box title:** "+ image name.png".
- **Formats:** PNG, JPEG, GIF and WebP, detected by extension.
- **Limit:** 5 MiB per image, 4 images per prompt. Larger images are refused
  with a message.
- **Path:** resolved against the workspace; `~/` is expanded.
- **Codex** receives `{type: "localImage", path}` (absolute path).
- **Claude** receives a base64 image block, read when the prompt is sent.
- **Journal:** records `[+ image name.png]` in the displayed text, never the
  image bytes.

### F7. Session browser

- **`/sessions`** lists this workspace's recent journals, newest first, up to
  20. Each line shows:
  - date and time;
  - provider and model;
  - the vendor session ID;
  - the first prompt, cut to 60 characters.
- **`/resume N`** reconnects to that entry's vendor session. A Claude entry
  reconnects with Claude, a Codex entry with Codex.
- **Reading journals:**
  - only the first 64 KiB of each file is read (header, `ready` and first
    `user` record);
  - a torn last line is skipped;
  - a file that is not a journal is ignored.
- **The CLI's `--resume`** is unchanged.

### F8. Print, JSON and RPC modes

These share one core (`octet/src/headless.rs`), which runs a `Session`
without the TUI.

- **`octet --print "PROMPT"` (`-p`):** runs one turn and writes the reply text
  to stdout. Notices and errors go to stderr. Exit codes:

  | Outcome | Exit |
  | --- | --- |
  | Completed | 0 |
  | Failed or error | 1 |
  | Interrupted (SIGINT) | 130 |

  The prompt is read from stdin when it is `-`.
- **`--output json`:** writes every event as one JSON line (the journal's
  record shape) instead of plain text.
- **`--rpc`:** reads JSON-line commands from stdin and writes events as JSON
  lines to stdout. The commands are `prompt`, `answer {id, allow}`,
  `interrupt`, `mode`, `effort` and `quit`. It exits when stdin closes.
- **Approvals fail closed.**
  - In print mode, an approval is denied, with a stderr notice saying to use
    `--mode auto` or `--mode full-access` for unattended runs.
  - In RPC mode, approvals are events the client answers. They time out like
    the TUI's.
- **No terminal is needed.** The interactive-terminal check applies only to
  the TUI.

### F9. Release archives

`.github/workflows/release.yml`, on tags `v*`:

- builds `octet` for `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu` and
  `aarch64-unknown-linux-gnu` (each on its native runner);
- packs each with README, LICENSE files and CHANGELOG into
  `octet-<tag>-<target>.tar.gz`, with a SHA-256 file;
- attaches them to a GitHub release.

It is checked with `actionlint`; its first real run is the first tag the
user pushes.

### F10. Deferred minors

- A test that every `Cmd` variant has a registry row.
- `duration_text` uses milliseconds when the limit is not a whole second.
- Vendor files name themselves through their own `PROVIDER.title`.
- `Engine::new` becomes `pub(crate)`.
- Comments on `Limits.mode_confirm` and `Core.request_id`.
- The guide lists `mode_change_pending`, and `docs/README.md` links it.
- A live test cancelling in the `Handshaken` phase.
- `actions/checkout@v5` to clear GitHub's Node 20 deprecation warning.

## Deferred, with reasons

Each item below stays in the parity matrix as pending, with this reason:

| Feature | Why it is not built now |
| --- | --- |
| Fleet / multi-agent orchestration, quota routing | A new subsystem: roles, budgets, routing, verdicts. It needs product decisions (which agents, which budgets, how verdicts gate work) that only the user can make. The provider table and `Protocol` trait are the foundation it will use. |
| Vendor plugin, hook, resource and package surfaces | Codex exposes `plugin/*`, `hooks/list`, `skills/*` and `mcpServer*`, which install code and change config. Octet's rule is to leave vendor configuration to the vendor CLI; managing it needs a security design first. |
| Durable history with branching, labels, trash and cross-session navigation | Depends on a decision about whether Octet keeps its own conversation store or relies on vendor history. F4 and F7 cover the everyday needs (branching and reopening) on the vendor's own sessions. |
| Legacy (Python-era) RPC compatibility | The Python application was removed on 2026-10-04. F8 provides a new RPC; compatibility with the old envelope needs a migration decision. |
| Python extension execution | Already an accepted compatibility break. |
| Benchmarks against the Python release, x86-64 package tests | Need the old release built and a Linux x86-64 machine. CI now runs on Ubuntu x86-64 for every push, which covers the tests themselves. |

## Testing

- **Fake vendors:** `protocol-child` learns `turn/steer`, `thread/fork` and
  `thread/compact/start`. It echoes `effort`, image inputs and Claude
  arguments (`--effort`, `--fork-session`) so tests can check exactly what
  was sent.
- **Coverage:** each feature gets driver tests (`octet-engine/tests/live.rs`),
  TUI tests, and PTY tests where keys or the CLI are involved.
- **Print and RPC modes** get process-level tests that run the `octet` binary
  against the fake vendor.
- **The gate:** `make rust-check` passes after every task.
- **Finish:** a live tmux run, and tiny real-Claude checks of effort, fork,
  compact and an image.
