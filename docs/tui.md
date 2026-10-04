# Octet terminal UI — Rust preview

A native terminal application now runs against the official Codex and Claude
CLIs. The harness uses Rust only: no browser, HTTP server or Python runtime.
This is an interactive vertical slice, not the completed feature-parity release.

## Run

From the repository root, with the pinned Rust toolchain and a native linker:

```sh
make rust-build
./target/release/octet --engine demo
./target/release/octet --engine codex --cwd /path/to/project
./target/release/octet --engine claude --cwd /path/to/project
```

`make tui-demo` builds and opens the offline demo. `make tui` opens Codex.
Vendor modes require the corresponding CLI on PATH and its normal login. Use
`--binary /absolute/path/to/cli` when needed. Octet does not read credentials.
`--model MODEL` selects a model when opening the session.

Build on Linux to produce a Linux executable. For a user-level installation:

```sh
scripts/rust-env.sh cargo install --locked --path crates/octet --root "$HOME/.local"
"$HOME/.local/bin/octet" --engine demo
```

The install command deliberately omits `--force`: resolve any existing `octet`
binary before replacing it. The optimized executable can also be run directly.
Published musl archives and cross-architecture installation gates remain pending.

## Interaction

The warm charcoal background, muted crimson accents and amber approval state adapt
to narrow terminals; a workspace/session panel appears at 112 columns. NO_COLOR
is respected. The minimum usable size is 38 columns by 12 rows.

| Key | Action |
| --- | --- |
| Enter | Send prompt |
| Alt+Enter, Shift+Enter, Ctrl+J | Newline (terminal modifier support varies) |
| Left/Right, Home/End | Edit by Unicode grapheme / move within a line |
| Up/Down | Prompt history, or vertical movement in a multiline draft |
| Ctrl+U | Clear draft |
| PageUp/PageDown | Scroll the conversation or approval details |
| Ctrl+End | Follow the latest output |
| Esc / Ctrl+C | Cancel the active operation |
| Ctrl+P | Command palette |
| Shift+Tab | Cycle permission mode: ask → accept-edits → auto |
| F1 | Help |
| Ctrl+Q | Stop vendor children, finish journal writes, exit |
| Ctrl+Z | Restore terminal and suspend; use the shell's `fg` to return |
| A / D / Esc in an approval | Allow once / deny / deny |

Bracketed paste preserves newlines without submitting them. A rejected oversized
paste leaves the draft intact. Approval requests are never answered by pasted
text. Approval expiration (120 seconds), cancellation and unknown request types
fail closed; requests too large to display completely are denied explicitly.
Vendor sandbox policy still applies. The vendor policy depends on the permission
mode below; this is not a promise that every vendor action raises a dialog.

Commands: `/help`, `/model`, `/mode`, `/session`, `/new`, `/reconnect`, `/export [new-path]`, `/quit`.
The preview also supports persistent multi-turn goals through `/goal`.
Changing sessions or exporting requires an idle turn. `/approval-demo` exercises
the dialog in offline demo mode. Unknown preview commands return a visible error and leave the draft in place. A
prompt may start with a path such as `/usr/lib`; it is sent, not treated as a command.
`/reconnect` keeps the visible conversation and prompt history; `/new` clears them.

## Permission modes

`--mode MODE` at launch, `/mode MODE` at runtime, Shift+Tab to cycle. The default
is `ask`; the mode is not remembered between launches but is kept across
`/model`, `/new` and `/reconnect`. `/mode` alone shows the table for the current
provider. The header shows the mode the vendor confirmed.

| Mode | Claude | Codex (sandbox · approval · reviewer) |
| --- | --- | --- |
| `ask` | `default` | workspace-write · untrusted · user |
| `accept-edits` | `acceptEdits` | workspace-write · on-request · user |
| `auto` | `auto` (Claude's classifier) | workspace-write · on-request · `auto_review` |
| `full-access` | `bypassPermissions` | danger-full-access · never · user |

`auto` hands approval decisions to the vendor's own reviewer; Octet never
answers a vendor approval by itself. Codex has no edits-only mode, so
`accept-edits` is its closest analogue: in-workspace edits already proceed under
workspace-write, and the model asks only to escalate.

`ask`, `accept-edits` and `auto` switch live. Claude applies the change
immediately, even mid-turn; Codex applies it from the next turn. A change Claude
refuses (for example, auto mode unavailable for the account) leaves the previous
mode in place with a notice; one Claude does not confirm within 10 seconds is
reported as unconfirmed, and a late confirmation is still applied. At connect
the header shows the mode the vendor reports, with a notice if it differs from
the one requested. `full-access` is never reached by Shift+Tab: type
`/mode full-access` on an idle session, which reconnects to the same vendor
session with all checks off. Leaving it reconnects again.

## Switch models and providers

```text
/model                         Show full model details and live catalog
/model list 2                  Show the second catalog page
/model MODEL_NAME              Change model within the current provider
/model codex MODEL_NAME        Select a Codex model
/model claude MODEL_NAME       Select a Claude model
/model claude/MODEL_NAME        Equivalent provider-qualified form
/model default                 Use the current provider's default model
/model codex                   Switch to Codex with its default model
```

Use a model name supported by your installed vendor CLI and account. Octet
passes the name to that CLI; it does not maintain a hard-coded catalog or guarantee
that every name is available. Vendor errors remain visible in the conversation.

Model changes require an idle session. Changing models within one provider
reconnects with the existing vendor session ID. Switching providers starts fresh
vendor context; the old conversation is not automatically sent to the new provider.
A notice marks this boundary. Earlier messages and prompt history remain visible,
and the previous journal is retained; the new connection writes a new journal.
The header and sidebar show the provider-reported name and full ID where space
permits. `/session` and `/model` print complete, scrollable details: requested
selection, catalog ID, confirmed session model ID, name and description. An alias
or default is marked unconfirmed until the runtime reports the active model.
Names unavailable in the catalog are explicitly marked as not reported.

Codex discovery uses paginated `model/list`; Claude discovery uses initialization
`models` metadata, including `resolvedModel` when supplied. Reconnecting refreshes
the catalog; `/model list <page>` pages through 20 entries at a time. Catalogs are
bounded to 256 entries and eight Codex pages, with a notice on partial results.
Discovery uses the existing CLI process and does not make inference calls or add
an idle polling timer. Provider model metadata is also recorded in the journal.
A missing catalog does not prevent entering a custom model ID.

Existing `--binary` overrides are
remembered per provider for this TUI invocation; a newly selected provider otherwise
uses its CLI from PATH. If a CLI is unavailable or login fails, fix it and use
`/reconnect`, or switch back with `/model`.

## Check the Claude catalog

```sh
make rust-build
./target/release/octet --engine claude
```

Once connected, enter `/model`. The list opens at its beginning and reports its
entry count. Use PageDown/PageUp to read all names, full IDs, descriptions and
selection commands. `/model list 2` opens another page when there are more than
20 entries. `/session` shows the current model details.

From a Codex session, `/model claude` switches to Claude with fresh provider
context; then `/model` lists Claude's catalog. `/model claude opus` selects Opus.
Octet displays all entries returned by the installed Claude CLI (up to the
256-entry discovery bound), including multiple aliases for the same model. This
is its advertised picker catalog, not an exhaustive inventory of every historical
Anthropic API model. Full custom IDs are accepted even when absent from the list.
Update the vendor CLI separately with `claude update`, then `/reconnect` to refresh.

## Persistence and limits

### Goals

`/goal <objective>` begins a persistent multi-turn goal. After each successful
turn, Octet asks the same provider to continue. The goal completes only when
the assistant gives an evidence summary followed by
`[[OCTET_GOAL_COMPLETE]]` on its final line. This is a model-reported audit,
not independent verification. `/goal` or `/goal status` shows state; `/goal pause`,
`/goal resume`, `/goal complete` (request an audit turn), and `/goal clear` manage
it. Esc/Ctrl+C pauses an active goal while interrupting its current turn. Failed
turns and the 200-turn guard also pause it. State is stored in a private goal
file per workspace alongside the preview journals. On restart, an active goal
loads paused and needs `/goal resume`; no inference starts automatically.
There is no token-budget option yet because the two drivers do not expose a
comparable, reliable per-turn token count.

See [the goal workflow guide](rust/goals.md).

`/session` shows the vendor session ID and journal path. `/reconnect` reopens that
vendor context; `--resume VENDOR_SESSION_ID` does the same on a later launch.
Reconnection does not load the earlier local transcript into the viewport yet.
`/new` starts fresh vendor context without deleting previous journals.

Preview journals are separate append-only JSONL files under
`$XDG_DATA_HOME/octet/rust-preview`, or `~/.local/share/octet/rust-preview`.
Use `--journal-dir PATH` to choose another location. Files are created exclusively
with mode 0600. Completed turns and shutdown events are synced. `/export` writes a
new JSONL file and refuses to overwrite an existing file. Journals may contain
source code and tool output; keep them in a private directory.

The viewport retains at most 160 blocks / 512 KiB of text, with 64 KiB per block;
earlier content remains in the journal. Wrapping is cached by block and width.
Rendering is dirty-triggered and capped near 30 Hz, with no idle animation timer.
Prompts are limited to 64 KiB, stdout frames to 8 MiB, queued raw frames to 16 MiB,
and each session journal to 64 MiB. Tool activity is a preview: each tool entry is
cut at 32 KiB, in the journal as well, and the vendor keeps the full output. A Codex
command shows what ran, its status and exit code, then the end of its output. Bounded
event queues stop an overloaded session with an error; they do not silently discard
a completed response.

A turn may run for any length of time. The session is stopped only if the vendor
sends nothing for this session for 10 minutes inside a turn (time spent waiting on
your answer to an approval does not count), does not finish connecting
within 30 seconds, or does not end the turn within 10 seconds of an interrupt.
When a vendor fails or exits, the error shows the vendor's own message and the end
of its stderr, which usually names the cause (an unknown session ID, an expired
login, a usage limit). These capacities
are not a whole-process RSS guarantee. A crash may leave an incomplete journal
line; journals are never reopened for append. Abrupt termination is not a durable
v3 recovery implementation.

## Preview scope

Implemented: real multi-turn conversations, streamed responses, tool events,
per-request command/file approval, cancellation, vendor context resume, model
selection and in-session switching, multiline editing, history, scrollback, new sessions, journal
export, terminal restoration, persistent goals, and an offline demo.

Pending: pi v3 session browsing/recovery, images, steer/follow-up queues, full
thinking/compaction controls, fleet, quota routing, resources, plugins, legacy
session semantics, compatible print/JSON/RPC, and distributable release archives.
The earlier Python application was removed from the repository on 2026-10-04;
Python extensions remain the explicitly accepted compatibility break.

Claude preview launches with vendor setting sources disabled and strict MCP
configuration. It does not yet expose the existing hook/plugin/resource surface.
Codex currently uses its vendor configuration. Do not infer plugin parity from
successful chat. The [acceptance matrix](rust/parity-matrix.md) tracks the remaining
migration, including comparative performance and long-session tests.

## Checks

```sh
make rust-check
scripts/rust-env.sh cargo test --release -p octet terminal_idle_diagnostic -- --ignored --nocapture
```

Tests include real pseudoterminals, Unicode paste, approval responses, resize,
suspension, SIGTERM, terminal mode restoration, duplicate/stale events, cancellation
and non-overwriting journal export. Live CLI tests are separate from the offline
suite and require vendor authentication.

UI backend references: [Ratatui installation](https://ratatui.rs/installation/)
and [Crossterm events](https://docs.rs/crossterm/0.29.0/crossterm/event/index.html).

## Model discovery verification (2026-10-03)

The official [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)
requires using returned model metadata because availability depends on the client
and account. The [Claude model configuration documentation](https://code.claude.com/docs/en/model-config)
also distinguishes aliases, deployment-specific resolution, and organization
restrictions. Consequently Octet does not freeze a global list of latest IDs.

Read-only discovery on this development machine returned GPT-6-Astra,
GPT-5.6-Sol/Terra/Luna and GPT-5.5 from Codex. Claude returned Sonnet 5, Opus 5,
Fable 5.1 and Haiku 4.5, with full resolved IDs and descriptions. These are runtime
observations, not guaranteed availability for other accounts. The online Claude
docs describe newer Opus/Sonnet defaults than this installed CLI advertises;
Octet faithfully displays the installed CLI's metadata rather than claiming
that a documentation example is available to the current account. No model
inference request was made for these discovery checks.
