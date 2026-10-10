# Octet terminal UI

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
A pushed `v*` tag builds archives for `aarch64-apple-darwin`,
`x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu`, each with a
SHA-256 file, and attaches them to a GitHub release
(`.github/workflows/release.yml`). The tag must name `crates/octet`'s
version, the full checks run first, a tag with a `-` (`v0.2.0-rc1`) is
published as a pre-release, and re-running replaces the archives.

`--effort LEVEL` sets the reasoning effort (see below).

### Without the interface: print, JSON and RPC

These modes need no terminal, so scripts and other programs can drive a
session.

```sh
octet --engine claude --print "Summarise the README"    # the reply, on stdout
git diff | octet -p - --output json                      # every event, one JSON line each
octet --rpc                                              # JSON-line commands on stdin
```

- **`--print PROMPT` (`-p`)** runs one turn and writes the reply text to
  stdout; notices and errors go to stderr. `-` reads the prompt from stdin.
  The prompt is taken as given, so it may start with dashes
  (`-p "--help me"`); `--print=TEXT` works too.
  The exit code is 0 when the turn completes, 1 when it fails (or stdin
  cannot be read), 2 for a usage error (a bad option, an empty, over-long
  or non-UTF-8 prompt, a `--cwd` path that is not UTF-8), and 130 after
  Ctrl+C (SIGINT), which cancels the turn first (also if the vendor then
  stops). SIGTERM stops the session and exits 143.
- **`--output json`** writes every event as one JSON line in the journal's
  shape, `{"type": …, "data": …}`. A turn that runs ends with
  `{"type":"finished",…}`; a session that stops ends with `stopped`.
- **Approvals in print mode are denied**, with a note on stderr. For
  unattended runs choose the policy up front with `--mode auto` (the
  vendor's reviewer decides) or `--mode full-access`.
- **`--rpc`** reads one JSON command per line and writes events as JSON
  lines. Commands: `{"type":"prompt","text":…}`,
  `{"type":"answer","id":N,"allow":true|false}`, `{"type":"interrupt"}`,
  `{"type":"mode","mode":"auto"}`, `{"type":"effort","level":"high"}` (or
  `null` for the vendor default) and `{"type":"quit"}`. An approval arrives
  as an `approval` event with its `id`; unanswered, it is denied when the
  approval window ends. A command that cannot be read gets an `error` line.
  Commands sent before the `ready` event, and prompts sent while a turn
  runs, wait their turn in order (up to 64), so a script can be piped in.
  An `answer` is sent at once (once `ready`), ahead of any prompt waiting
  for the next turn.
  At the end of stdin Octet finishes the waiting work, denying any approval
  it can no longer ask about, then exits 0, or 1 if a turn failed. `quit`
  exits 0 at once, even while the client keeps stdin open; RPC exits 1 if
  the vendor stops. SIGINT and SIGTERM stop
  the session cleanly, finishing its journal, and exit 130 and 143.

Every headless run is journaled like an interactive one, and shuts its
session down on every exit path, even when stdout or stderr has been closed
(`octet -p … | head`). If the session gives up on a reader that stopped
reading, the run says the turn did not finish (on stderr, or as an `error`
line) and the journal records why.

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
| Esc / Ctrl+C | Cancel the active operation. Ctrl+C also closes help or the palette, and clears a draft when idle |
| Ctrl+P | Command palette (also offers Ctrl+G, `@` and `!`) |
| `@` | Mention a workspace file; a popup suggests paths as you type (Enter or Tab inserts, Esc closes) |
| `/model` | Model picker: Up/Down choose, Enter switches, Tab inserts the name, Esc closes |
| Tab | Complete a path, or a `/command` at the start of the prompt |
| Ctrl+G | Write the prompt in `$VISUAL` or `$EDITOR` (default `vi`), run by the shell as git runs it: quote a path with spaces |
| Ctrl+X | Copy the last reply to the clipboard (same as `/copy`) |
| Shift+Tab | Cycle permission mode: ask → accept-edits → auto |
| F1 | Help |
| Ctrl+C twice | Quit, as in Claude Code: on an idle, empty prompt the first press shows "Press Ctrl+C again to quit", and a second within 1.5 seconds stops vendor children, finishes journal writes and exits |
| Ctrl+Z | Restore terminal and suspend; use the shell's `fg` to return |
| A / D / Esc in an approval | Allow once / deny / deny, from 0.4 s after the dialog appears |

Bracketed paste preserves newlines and tabs without submitting them (a tab
shows as spaces to the next stop of four, and is sent as a tab). A rejected
oversized paste leaves the draft intact; a paste while a dialog is open is
dropped, with a hint. Approval requests are never answered by pasted text, and
answer keys wait 0.4 seconds after the dialog appears on screen (again, after
help, the palette or Ctrl+G's editor covered it), so a letter typed as it appears
cannot answer it. Approval expiration (120 seconds by default; `--approval-timeout SECONDS`
sets 10–3600), cancellation and unknown request types fail closed; requests too large to display completely are denied explicitly.
At most eight approvals wait at once; a ninth is denied with a notice. Claude
receives the reason with each denial (your refusal, the timeout, the cap), so it
can tell a refusal from a limit; Codex's decline carries no reason.
Vendor sandbox policy still applies. The vendor policy depends on the permission
mode below; this is not a promise that every vendor action raises a dialog.
When an approval starts waiting, Octet rings the terminal bell once and sends a desktop
notification (OSC 9). The bell passes through tmux and mosh to a phone app;
the notification reaches only a desktop terminal connected directly.

The prompt box grows with the draft: 4 rows when empty, up to 7 for a draft of
four or more lines, leaving the rest of the screen to the conversation.

A prompt starting with `!` runs the rest as a shell command in the workspace:
`!git status` shows the output in the conversation as a `SHELL` entry and
attaches it to your next prompt (the prompt box title shows
`+ git status (exit 0)`); `!!git status` shows it without attaching. The
vendor receives the output as a fenced block after your text; the
conversation and the journal show `[+ git status]`. Commands run with your
shell and your permissions, without an approval, with stdin closed. Each
keeps its last 32 KiB of output and stops after 10 minutes; Esc stops it
sooner. Attachments wait up to 32 KiB in total; Esc on an empty prompt
removes them. `cd` and `export` do not carry over between commands.

`@` inserts the path only; the vendor reads the file with its own tools.
The file list comes from `git ls-files` (tracked and untracked, not
ignored) or, outside git, a walk that skips hidden folders, `target` and
`node_modules`, up to 50,000 files and folders. A `git` that hangs is
stopped after 10 seconds and the walk used instead. Ctrl+G's draft is written
to a file in a private (0700) folder of its own, removed with it. While the
editor is open, Octet keeps receiving the vendor's output and repaints when
you return; an approval that arrives meanwhile rings the bell and its timer
keeps running.

A `!` command's output is kept as a terminal would show it: a carriage
return starts a line over, so a progress counter keeps only its last state.
Attached output keeps its tabs. Commands run in their own session, without
access to Octet's terminal, so a credential prompt (git, ssh, sudo) fails at
once rather than drawing over the screen. A command, and everything it
started, is stopped when its session ends (a reconnect, `/new`, a quit).

Tab fills the longest common start of several matches and lists them in the
popup. It waits at most half a second for a folder listing, so a slow
network mount cannot freeze the screen.

`/copy` takes the turn's whole reply as the vendor sent it, tabs included.
It uses the OSC 52 escape. Inside tmux it needs
`set -g set-clipboard on`; mosh 1.4 and later pass it on; terminal and
phone apps vary in whether they accept it.

Commands: `/help`, `/model`, `/mode`, `/goal`, `/session`, `/new`, `/reconnect`, `/export [new-path]`, `/copy`,
`/remote-control`, `/queue`, `/steer`, `/effort`, `/fork`, `/compact`, `/image`, `/sessions`, `/resume`,
`/quit` (or `/exit`).
Help (F1) and the command palette (Ctrl+P) scroll when the screen is short.
`/remote-control` checks, without changing anything, whether this session can be
reached from a phone over tmux, Tailscale SSH and mosh, and prints the phone
command. Setup steps: [Use Octet from your phone](remote-control.md).
The preview also supports persistent multi-turn goals through `/goal`.
Changing sessions or exporting requires an idle turn, and `/compact` a
connected one; a refused command explains why on the status line.

`/sessions`, `/export` and `/remote-control` run in the background: the
status line names the job, and replies, approvals and keys keep working.
One runs at a time; a second is refused with "Wait for: …" and its line stays
in the prompt box. A job outlives a reconnect (`/new`, `/reconnect`, `/model`,
`/fork`, `/resume`): it carries on and reports in the next screen. Quitting
waits up to 5 seconds for an export, so it is never cut short, and stops any
other job at once. `/export PATH` is relative to the workspace (`~/` is your
home folder); with no path it writes the journal's name into the workspace.

In the demo, a prompt
sent with `!` attachments (or by a goal) gets a reply that also shows the
full text a model would receive. `/approval-demo` exercises
the dialog in offline demo mode. Unknown preview commands return a visible error and leave the draft in place. A
prompt may start with a path such as `/usr/lib`; it is sent, not treated as a command.
`/reconnect` keeps the visible conversation and prompt history; `/new` clears them.

## During and between turns

**The follow-up queue.** While a turn runs, Enter queues the draft instead of
sending it; the prompt box title shows `+N queued`. When the turn finishes,
the next queued prompt goes, with the `!` output and images attached when it
was queued. The queue holds 8 prompts. Esc or Ctrl+C cancels the turn and
drops the queue ("Dropped N queued prompts"), even a prompt sent the moment
before Esc that the vendor had not started. `/queue` lists it and
`/queue clear` empties it. An active goal's continuation goes first.

**Steering.** `/steer TEXT` adds TEXT to the running turn. Codex takes it
into the same turn (`turn/steer`). Claude has no steering request, so the
text is queued as the next prompt, and Octet says so. While a cancelled turn
is still stopping, `/steer` text is queued as the next prompt too. With no
turn running, `/steer` sends the text as a prompt.

**Reasoning effort.** `--effort LEVEL` at launch, or `/effort LEVEL` later;
`/effort` alone shows it and `/effort default` returns to the vendor's
default. Claude takes `low`, `medium`, `high`, `xhigh` and `max`, and Octet
refuses other levels for it. Codex's levels depend on the model, so any
word is passed on and Codex decides. A `/model` switch to a provider that
does not take the level falls back to its default, with a notice.
Codex takes it with each turn, so a change applies from the next turn.
Claude takes it at launch, so a change reconnects to the same session. The
level carries across `/model`, `/new` and `/reconnect`.

**Fork.** `/fork` continues this conversation in a new vendor session and
leaves the original as it was (Codex `thread/fork`, Claude
`--resume ID --fork-session`). The transcript stays on screen and the new
session's ID replaces the old one. "Forking from X…" shows at once;
"Forked from X into Y" once the vendor names the new session. It
needs an idle session that the vendor has already named. Claude names the
fork with its first turn; a reconnect before then (for example `/effort`)
forks again, so the original is never written to.

**Compaction.** `/compact` asks the vendor to compact its context (Codex
`thread/compact/start`, Claude's own `/compact`). It runs as a turn: its
output streams as usual and Esc cancels it.

**Images.** `/image PATH` attaches an image to the next prompt; the prompt
box title shows `+ image name.png` (or `+N images`). PNG, JPEG, GIF and WebP
are accepted, by extension, up to 5 MiB each and 4 per prompt. A relative
path is in the workspace, `~/` is your home folder, and a path dragged in
from Finder (quoted, or with `\ ` escapes) works as typed. Codex receives
the file's path and reads it itself. Claude receives the bytes inside the
prompt, which limits each image to 3.75 MiB and a prompt's images to
5.25 MiB together; `/image` refuses more. The bytes are read when the prompt
is sent; if a file has gone or grown past the limits by then, the turn fails
with a message and the session carries on. Up recalls a prompt together with
the images it was sent with (unless the prompt you are writing has images of
its own, which stay attached); a failed `/image` leaves the line in the prompt
box to correct. The transcript and journal show `[+ image name.png]`,
never the image. Esc on an empty prompt drops attached images and `!` output.

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
/model                         Pick a model with Up/Down; Enter switches to it
/model list                    Show model details and every provider's models
/model list 2                  Show the second page of the list
/model MODEL_NAME              Select a model; switches provider if another lists it
/model refresh                 Fetch the other providers' model lists again
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
reconnects with the existing vendor session ID. Switching providers starts a new
vendor session, so Octet carries the conversation across: your next prompt goes
to the new provider with a transcript of the conversation shown so far, and a
notice says how much went ("Carried the earlier conversation to codex (3 turns,
2.1 KiB)"). You still see only what you typed.

The transcript holds your prompts, each reply labelled with the provider that
wrote it, one line per tool call and one line per `!` command (`$ cmd · exit 0`).
It leaves out Octet's own notices and errors, image bytes, and the full output of
tools and `!` commands. It is at most 64 KiB: the newest turns are kept, your
first prompt always stays, and a marker says how many turns between them were
dropped. It then lives in the new vendor's own history, so `/reconnect`,
`/effort`, `/fork` and `/compact` keep it. If the first send is refused, the next
prompt carries it instead. A same-provider switch resumes the vendor session and
carries nothing; `/new` starts fresh, and `/resume N` reopens that session's own
history. Earlier messages and prompt history remain visible, and the previous
journal is retained; the new connection writes a new journal.
The header and sidebar show the provider-reported name and full ID where space
permits. `/session` and `/model` print complete, scrollable details: requested
selection, catalog ID, confirmed session model ID, name and description. An alias
or default is marked unconfirmed until the runtime reports the active model.
Names unavailable in the catalog are explicitly marked as not reported.

`/model` lists every provider's models in one list, the current provider's
first; each entry names its provider and the line that selects it. A bare
`/model NAME` goes to the provider whose list holds NAME (`/model opus` from
Codex switches to Claude, carrying the conversation as any provider switch
does); a name in no list goes to the provider whose listed IDs share its
leading word (`gpt-5.5` to the provider listing `gpt-…` models), else stays
with the current provider as a custom ID. `default` is always the current
provider's, and the explicit forms (`/model codex X`, `claude/X`) work as
before.

`/model` alone, or typing `/model ` (with the space), opens the model picker
above the prompt box: every provider's models, the current provider's first,
each as `name · provider · display name`, with the model in use marked `●`
and highlighted. Typing after `/model ` filters it (by selection, full ID or
name). Up/Down move, Enter switches to the highlighted model at once (it
sends `/model <provider> <name>`, so the provider shown is the one used), Tab
puts the name in the draft (as `provider name` when the name alone would go to
another provider), and Esc closes the picker. Without the picker, Tab after
`/model ` completes from the current provider's names, and after
`/model PROVIDER ` from that provider's. While a turn runs,
Enter is refused with the usual hint and the picker stays open. A name typed
in full (one a list holds, or `default`) is sent as typed and resolved like any
`/model NAME`, so `default` stays the current provider's; so is a name that
matches nothing. A partial name takes the highlighted line; to send a custom
name that begins like a listed one, press Esc, then Enter.

The current provider's list comes live from its CLI with each connection
(Codex's paginated `model/list`; Claude's initialization `models`, with
`resolvedModel` when supplied). Every other provider's list comes from a cache,
`models.json` in the journal directory (private, written atomically). When a
cached list is missing or more than 24 hours old, Octet fetches it in the
background once the session is ready, by starting that provider's CLI just far
enough to list its models: no vendor session (Codex opens no thread), no
prompt, no tokens, at most 20 seconds. It runs the CLI Octet runs for that
provider (a `--binary` override, else the one on PATH). Claude Code backs up
its own settings each time it starts (`~/.claude/backups/`), so a fetch of
Claude's list adds one such backup. `/model refresh` fetches every other list
now, and says which are already on their way; a provider whose fetch failed is
not tried again until then. A save keeps any list another window saved more
recently. The
list's header shows each provider's count and freshness (`live`,
`cached 2 h ago`, `probing…`, `unavailable: …`); `/model list <page>` pages
through 20 entries at a time. Lists are bounded to 256 entries per provider
and eight Codex pages, with a notice on partial results. A missing list does
not prevent entering a custom model ID.

Providers come from one table; adding a vendor CLI is one file and one row
([Adding a provider](rust/adding-a-provider.md)).

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
`/sessions` lists up to 20 recent vendor sessions in this workspace, newest
first: provider, session ID, model, age and first prompt. `/resume N` reopens
entry N, switching provider when it belongs to the other vendor, in a fresh
view. The list comes from Octet's own journals (at most the first 64 KiB of
each, skipping a torn last line); reconnects of one session show as one entry.
Reconnection does not load the earlier local transcript into the viewport yet.
`/new` starts fresh vendor context without deleting previous journals.

Preview journals are separate append-only JSONL files under
`$XDG_DATA_HOME/octet/rust-preview`, or `~/.local/share/octet/rust-preview`
(the name dates from the Rust port's preview and is kept so existing journals
are still found).
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
event queues never silently discard output. While the interface is briefly behind, Octet
stops reading the vendor (whose output waits in the pipe) rather than stopping the
session; an interface or output reader stalled for more than 2 seconds still stops
it, and the journal says why. A reply shows at most 2 MiB, and ends with a note where it is cut; the
journal holds the same cut text. A Claude subagent's messages (the Task tool) show
as tool activity, not as the reply. Escape sequences in vendor and command output
are removed, including unterminated ones, which end at the line.

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
per-request command/file approval with a configurable window and an alert,
four permission modes with live switching, cancellation, vendor context resume,
model selection and in-session switching, multiline editing, history,
scrollback, new sessions, journal export, terminal restoration, persistent
goals, phone-access checks (`/remote-control`), `@` file mentions, Tab
completion, `!` shell commands with attachments, an external editor (Ctrl+G),
`/copy`, a follow-up queue, steering, reasoning effort, fork, compaction,
images, a session browser, print/JSON/RPC modes, release archives, and an
offline demo.

Pending, each for a reason the [acceptance matrix](rust/parity-matrix.md)
gives: fleet orchestration and quota routing, the vendors' plugin, hook and
resource surfaces, Octet-owned history with branching and labels, and the
Python era's RPC envelope.
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

To check the real providers using their existing logins, run the live terminal
tests explicitly. They make inference calls and use your subscriptions:

```sh
scripts/rust-env.sh cargo test --release --locked -p octet --test terminal installed_providers_ -- --ignored --nocapture
```

These check a persistent file-creation goal and a shell attachment reaching the
provider, followed by copying its reply through OSC 52. Set `OCTET_LIVE_ENGINE`
to `claude` or `codex` to run just one provider. Optional
`OCTET_LIVE_CLAUDE_MODEL` and `OCTET_LIVE_CODEX_MODEL` select the models; otherwise
each CLI uses its default. Provider usage limits and login errors fail these
checks visibly.

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

### Octet mascot

Octet, the crimson octopus mascot, sits at the top left of the screen at every
size, in a banner modelled on Claude Code's. The banner is three rows tall:
the 9 × 3-cell mini Octet, drawn with coloured Unicode half blocks, sits beside
three lines:

- `Octet vX.Y.Z · preview`;
- the permission mode, then the engine and model. The mode leads its line so
  a long model name cannot push it off a narrow screen;
- the activity name, then the workspace path.

Status and usage stay at the right edge, sized to their text so a narrow
terminal never cuts them. Below the banner, the conversation
area stays empty until the first message; the composer's placeholder says
what to type.

Octet's pose follows the shared provider events: idle, thinking, coding,
searching, delegating, approval, success, error and sleeping. Tool names
choose the activity pose, an open approval overrides it, and an error stays
visible after a provider disconnects. In the mini only the eyes and a small
accent change, so the activity name beside it carries the exact mode. Poses change on events only; there is no animation timer.

`NO_COLOR=1` replaces the mini with a `◇` text mark.

The full pixel-art reference for all nine poses is kept in
`docs/design/octet-agent-modes/` as design material; the app draws only the
mini, from the grids in `crates/octet-tui/src/mascot/art.rs`.
