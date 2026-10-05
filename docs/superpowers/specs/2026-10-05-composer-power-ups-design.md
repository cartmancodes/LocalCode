# Composer power-ups — design

Date: 2026-10-05
Status: approved in conversation; awaiting written-spec review
Sub-project A of the pi-inspired feature set (see "Context" below).

## Intent

Make writing a prompt in Octet as quick as in pi or Claude Code. Five
features, all inside the terminal UI (`crates/octet-tui`):

1. `@` file mentions with fuzzy search.
2. Tab completion for paths, `@` mentions and slash commands.
3. `!cmd` / `!!cmd` to run a shell command without leaving Octet.
4. Ctrl+G to write the draft in an external editor.
5. `/copy` and Ctrl+X to copy the last reply through OSC 52, which reaches a
   phone's clipboard over tmux and mosh.

Success:

- Each feature works the same with Claude, Codex and the demo engine.
- Nothing blocks the event loop: vendor events keep draining while files are
  indexed, commands run or the external editor is open.
- Every new data path is bounded and fails visibly, leaving the session
  intact.
- Approvals are unchanged. `!` runs what the user typed, so it is not a
  vendor action and needs no approval.
- Tests cover each unit and each key path end to end; `docs/tui.md`, the
  README, `docs/rust/tui-features.md`, the phone guide and the CHANGELOG are
  updated; `make rust-check` passes.

## Context

The pi-inspired features were split into sub-projects A–L, each with its own
spec, plan and implementation: A composer power-ups, B context and cost, C
turn control, D sessions, E modes, F customisation, G export, H vendor
resources with trust, I images, J headless modes, K display, L later. This
spec covers A only.

Decisions taken in conversation:

- `!cmd` output is shown in the transcript and attached to the next prompt
  the user sends; `!!cmd` shows it without attaching. No turn starts on its
  own.
- An `@` mention inserts the path only. The vendor reads the file with its
  own tools, under its own permissions.
- Small focused modules in `octet-tui`, following the pattern of
  `remote.rs`, rather than a service in `octet-core` or one large module.

Out of scope: inlining file contents, images (sub-project I), steering and
follow-up queues (sub-project C), configurable keys (sub-project F).

## Architecture

| Unit | Responsibility | Interface |
| --- | --- | --- |
| `files.rs` | Workspace file list and ranking | `index(root) -> Index` builds the list; `Index::rank(query) -> Vec<&str>` returns the best 8. |
| `shell.rs` | Run one user command | `run(command, cwd, cancel) -> Ran { command, status, output }`, bounded in size and time. |
| `clipboard.rs` | OSC 52 encoding | `osc52(text) -> Option<Vec<u8>>`; `None` only for empty text. |
| `external.rs` | External-editor round trip | `prepare(draft)` writes the temp file and returns the command to run; `finish(file, status)` reads back the new draft or an error. |
| `view.rs`, `App` | Popup and attachment chip | `App::completion: Option<Completion>`, `App::attachments: Vec<Attachment>`, drawn above and in the title of the prompt box. |
| `lib.rs` | Key routing, background results | Routes `@`, Tab, Ctrl+G, Ctrl+X, `/copy`, `!`/`!!`; adds `select!` branches for the index, shell and editor tasks. |

Slow work runs as background tasks whose results arrive through the session
loop's `select!`, the way `/remote-control` does since the PR #3 review: the
index build and shell commands are `tokio::spawn`ed `JoinHandle`s; the
external editor is a `tokio::process::Child` awaited in its own branch.

### `files.rs`

- Source: in a git work tree, `git ls-files --cached --others
  --exclude-standard -z` run in the workspace; otherwise, or when that fails,
  a walk that skips hidden entries, `target` and `node_modules`, and
  unreadable directories.
- Bounds: at most 50,000 paths. Reaching the cap records a flag; the popup
  then shows "Indexed the first 50,000 files".
- Built in the background on the first `@` or path completion of a session,
  and again after `/reconnect` or `/new`. Never on the event loop.
- Ranking: case-insensitive subsequence match of the query against the path.
  Score, best first: match entirely within the file name; then fewer gaps
  between matched characters; then shorter path; then alphabetical. An empty
  query lists the first 8 paths in that order.

### `shell.rs`

- Runs `$SHELL -c <command>` (falling back to `sh -c`) in the workspace, in
  its own process group, with stdin closed and stderr merged into stdout.
- Output: at most 32 KiB kept, from the end; when more arrived, the result
  starts with `[earlier output cut]`.
- Time: 10 minutes, then the process group is killed and the result says
  "Timed out after 10 minutes".
- Cancel: a `watch` or oneshot signal kills the process group; the result says
  "Cancelled".
- Failure to start returns an error naming the shell and the reason.
- The environment is Octet's own; `cd` and `export` don't persist between
  commands.

### `clipboard.rs`

- `ESC ] 52 ; c ; <base64> BEL`. Text over 100 KB is cut to 100 KB (on a
  character boundary) and the caller reports the cut.
- Base64 is implemented locally (a few lines) rather than adding a
  dependency.

### `external.rs`

- Editor: `$VISUAL`, then `$EDITOR`, then `vi`, split on whitespace so
  `code --wait` works.
- Temp file: created exclusively with mode 0600 in the system temp
  directory, named `octet-prompt-<pid>-<n>.md`; removed in every outcome.
- Result: the file's text with one trailing newline removed. An error exit,
  a read failure or a result over the 64 KiB prompt limit keeps the original
  draft and returns the reason.

## Behaviour

### `@` file mentions

- `@` typed at the start of the draft or after whitespace opens the popup.
  The token runs from `@` to the cursor; each character typed narrows the
  list.
- Up/Down move the selection. Tab or Enter accepts: the token becomes
  `@path ` (paths containing whitespace become `@"path" `). Esc closes the
  popup without cancelling a turn. Typing whitespace or deleting the `@`
  closes it.
- While the popup is open, Enter accepts instead of sending.
- Before the index is ready the popup reads "Indexing files…"; with no match,
  "No matching files".
- Available while a turn runs.

### Tab completion

- On a word containing `/` or starting with `./`, `../` or `~/`: complete
  from the filesystem relative to the workspace (`~` expands to `$HOME`). One
  match fills it in (directories get a trailing `/`); several fill their
  common prefix and open the popup with up to 8 candidates.
- On an `@word`: open the `@` popup.
- On a `/word` that starts the draft and contains no further `/`: complete
  from the existing command list (`view::COMMANDS`), with the same
  one/several rule. `/usr/li` contains a second `/`, so it completes as a
  path, matching how Enter already treats such drafts as prompts.
- Elsewhere: no effect.
- Shift+Tab keeps cycling the permission mode.

### `!cmd` and `!!cmd`

- On Enter, a draft starting with `!` (after trimming) runs locally: `!!` →
  show only; `!` → show and attach. An empty command is a notice.
- The draft clears and the line enters prompt history.
- The transcript gets a new `SHELL` role entry: `$ <command>`, the output and
  `exit <code>`; a non-zero exit, timeout or cancel is drawn in amber.
- One command at a time: a second gets "A command is already running".
- Esc while a command runs cancels the command before it would cancel a
  turn. Ctrl+C behaves the same.
- Attachments: `!cmd` appends one. Several can wait; their combined output is
  at most 32 KiB, older attachments dropping first with a notice. The prompt
  box title shows `+ <command> (exit <code>)` for one, `+N attached` for more.
  Esc on an empty draft with no turn running removes them all
  ("Attachments removed").
- Sending a prompt with attachments uses `Command::PromptWithDisplay`:
  - wire: the draft, then for each attachment a blank line,
    ``Output of `<command>` (exit <code>):`` and a fenced block of the
    output (the fence grows if the output contains backticks);
  - display: the draft followed by `[+ <command>]` per attachment.
  If the wire would exceed the 64 KiB prompt limit, attachment outputs are
  cut from their start, with `[earlier output cut]`, until it fits.
  The vendor receives the wire and keeps it in its own session history; the
  transcript and Octet's journal record the display text, as they already do
  for goal prompts (`Event::User` carries the display).
  Attachments clear after a successful send and stay after a failed one.
- Goal continuation prompts never carry attachments.

### Ctrl+G external editor

- Available when no dialog has focus, including while a turn runs.
- Sequence: stop the input reader (drop `InputReader`, which joins its
  thread); leave the alternate screen and raw mode (the existing
  `TerminalGuard::restore`); spawn the editor; keep handling vendor events
  without drawing; when the editor exits, re-enter raw mode and the
  alternate screen, start a new input reader, apply the result, repaint.
- An approval arriving while the editor is open still rings the bell, and
  its timer keeps running; the docs say so.
- While the editor is open no keys reach Octet (its input reader is
  stopped), so a second Ctrl+G cannot arrive.

### `/copy` and Ctrl+X

- Copies the most recent assistant entry as plain text. Notices: "Copied
  1.2 KB to the clipboard", "Nothing to copy yet", and "Copied the first
  100 KB" when cut.
- Written to stdout between frames, as the approval alert is; write errors
  are ignored.
- Requirements, documented: tmux needs `set -g set-clipboard on`; mosh 1.4+
  passes OSC 52; phone apps vary.

## Errors and limits

| Situation | Outcome |
| --- | --- |
| `git ls-files` fails or git missing | Fall back to the walk |
| More than 50,000 files | First 50,000 kept, note in the popup |
| Shell cannot start | `ERROR` entry naming the shell and the reason |
| Output over 32 KiB | End kept, `[earlier output cut]` |
| Command over 10 minutes, or Esc | Process group killed, result says why |
| Second `!` while one runs | Notice, nothing started |
| Editor missing, error exit, oversize, temp-file failure | Original draft kept, notice says why |
| Editor crashes | Terminal restored, original draft kept |
| Attachments would overflow the prompt | Outputs cut from the start until it fits |
| Terminal ignores OSC 52 | Not detectable; documented |

## Testing

Test first for every behaviour, as in the earlier phases.

- `files.rs`: ranking order (file-name match beats path match, fewer gaps,
  shorter path); the 50,000 cap flag; listing from a temp git repo
  (including an untracked, non-ignored file and excluding an ignored one);
  the walk in a plain temp folder skipping hidden, `target` and
  `node_modules`.
- `shell.rs`: exit code and output; stderr merged; the 32 KiB cap keeping
  the end; cancel kills a child of the shell (a pipeline's `sleep`); timeout
  with a short test limit; failure to start.
- `clipboard.rs`: exact bytes for a known string; the cap; empty text.
- `external.rs`: a stand-in editor script that rewrites the file; error
  exit; oversized result; trailing newline handling; temp file removed.
- Composer logic (unit, in `lib.rs`/`view.rs` tests): `@` token detection
  and replacement, including whitespace paths; Tab on a path, an `@word`
  and a `/word`; popup open, narrow, accept, close; wire versus display
  text for attachments; the attachment cap and prompt-limit cut; goal
  prompts without attachments; Esc order (command, then turn).
- Terminal end to end (`crates/octet/tests/terminal.rs`, demo engine): `@`
  plus a query accepts a file into the draft; `!echo hi` shows a `SHELL`
  entry and the chip, and the next prompt's demo echo contains the output;
  `!!` attaches nothing; Ctrl+G with `EDITOR` pointing at a stand-in script
  replaces the draft; `/copy` after a reply emits the OSC 52 bytes.

## Documentation

- `docs/tui.md`: keys (`@`, Tab, Ctrl+G, Ctrl+X), commands (`/copy`), `!` and
  `!!` semantics, the limits above.
- README: feature list, keys and commands tables.
- `docs/rust/tui-features.md`: the prompt editor row.
- `docs/remote-control.md`: tmux `set-clipboard on` for `/copy` from a phone.
- `CHANGELOG.md`: the new features under 0.1.0.
- In-app: the help screen and the command palette list `/copy`, Ctrl+G,
  Ctrl+X, `@` and `!`.
