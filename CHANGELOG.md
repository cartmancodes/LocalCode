# Changelog

All notable changes to Octet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

The first public preview: a native Rust terminal workspace for the official
Claude Code and Codex CLIs.

### Added

- One model list across providers: `/model` shows every provider's models,
  fetched from each CLI when missing or a day old (no session, no tokens) and
  cached in `models.json`; `/model refresh` fetches them now. `/model NAME`
  switches to the provider that lists NAME, and Tab completes model names.
- Multi-turn sessions with Claude Code or Codex, with streamed replies, tool
  activity, usage and errors in one transcript.
- An offline `demo` engine that needs no vendor CLI or login.
- Model discovery from the installed CLI (`/model`, paged), model changes
  within a provider, and provider switching mid-session (`/model claude`,
  `/model codex`).
- Four permission modes (`ask`, `accept-edits`, `auto`, `full-access`)
  mapped onto each vendor, set with `--mode`, `/mode` or Shift+Tab.
- An approval dialog showing the complete request, failing closed on expiry,
  cancellation, oversize and unknown request types.
- `--approval-timeout SECONDS` (10–3600, default 120) for the approval window.
- A terminal bell and desktop notification (OSC 9) when an approval opens.
- Persistent multi-turn goals (`/goal`), which survive restarts and load
  paused.
- Private, append-only JSONL journals for every session, `/session` to show
  the path and `/export` to copy one without overwriting.
- `/new`, `/reconnect` and `--resume` for vendor sessions.
- A provider switch carries the conversation: the first prompt to the new
  provider includes a transcript of what was shown (at most 64 KiB, newest
  turns and the first prompt kept), while you see only what you typed.
- Phone access over tmux, Tailscale SSH and mosh, with a step-by-step guide
  and `/remote-control`, a read-only check that names each fix and prints the
  phone command.
- A multiline prompt editor with Unicode-aware editing, bracketed paste and
  history; the prompt box grows with the draft.
- A responsive layout down to 38 × 12 cells with a workspace panel from 112
  columns, `NO_COLOR` support, suspend with Ctrl+Z, and terminal restoration
  on exit, signals and panic.
- Octet, the header mascot, whose pose follows the agent's activity.
- A build-time guard that fails if any crate names a vendor credential store.
- A follow-up queue: Enter during a turn queues the prompt (up to 8), and
  `/queue` lists or clears it.
- `/steer TEXT` adds to the running Codex turn; for Claude it queues the text.
- Reasoning effort with `--effort LEVEL` and `/effort`.
- `/fork` continues a conversation in a new vendor session; `/compact` asks
  the vendor to compact its context.
- `/image PATH` attaches PNG, JPEG, GIF or WebP images (up to 5 MiB, 4 per
  prompt).
- `/sessions` lists this workspace's recent vendor sessions and `/resume N`
  reopens one.
- `--print` (`-p`), `--output json` and `--rpc` run Octet without the
  interface, for scripts and other programs.
- Release archives with SHA-256 files for macOS (Apple silicon) and Linux
  (x86-64, ARM64), built when a version is tagged.
- `@` file mentions, Tab completion for paths and commands, `!`/`!!` shell
  commands with output attached to the next prompt, Ctrl+G to write the
  prompt in an external editor, and `/copy` (Ctrl+X) through OSC 52.
- `!` commands run in their own session, so credential prompts (git, ssh,
  sudo) fail at once instead of drawing over the screen; progress output
  keeps its last state, tabs survive in attached output, and the status line
  shows each command's result.
- `/copy` takes the whole reply as sent, tabs included, and keeps the reply
  on screen copyable while a new turn starts.
- The command palette also offers Ctrl+G, `@` and `!`; Tab fills a common
  prefix and lists the matches, without blocking on slow folders.
- The offline demo shows the full text a model would receive when `!`
  attachments or a goal change it.
- Stopping Octet (SIGTERM, SIGHUP) while an external editor is open asks the
  editor to quit instead of leaving it on the terminal.
- At most eight approvals wait at once; more are denied with a notice. Claude
  is told why each approval was denied (your refusal, the timeout, the cap,
  a request too large to show, a cancelled turn).
- Dual licensing under MIT or Apache-2.0.
- CI on macOS and Ubuntu (`make rust-check`) plus a `cargo audit` job.

### Changed

- An approval takes answer keys 0.4 seconds after it opens, so a letter
  typed as it appears cannot allow it.
- While the interface is briefly behind, Octet stops reading the vendor
  instead of stopping the session; a burst of output no longer ends it (a
  reader stalled past 2 seconds still does, with a journaled reason).
- A background job carries on across a reconnect; quitting waits only for
  an export. `/export PATH` is relative to the workspace.
- Paste keeps tabs. The external editor runs as the shell runs `$EDITOR`
  (a quoted path with spaces works), on a draft in a private folder.
- `--print` takes a prompt that starts with dashes, and `--print=TEXT`.
- The protocol gate launches the vendor with Octet's own arguments and
  limits.
- Edition 2024; clippy pedantic, `#[expect(lint, reason)]` for every
  exception, `Debug` on every public type; a `cargo doc` build and a
  `cargo-deny` licence check in CI.

- `@` suggestions rank about four times faster in large workspaces (under
  25 ms for 50,000 files).
- Help, the command palette, Tab completion, the sidebar and `octet --help`
  are built from one command list; help shows one line per command and sizes
  itself to fit narrow terminals.
- A `!` command stopped by its time limit names that limit.
- Release builds are about 40% smaller (thin LTO, stripped symbols).
- The protocol gate moved to its own `octet-gate` crate.
- `/sessions`, `/export` and `/remote-control` run in the background. One
  runs at a time; a second is refused and keeps its line in the prompt box.
  A session that ends waits up to 5 seconds for a running job, so an export
  is never cut short, and shows its result in the next screen.
- Usage errors exit 2 and other failures 1. Journal directories Octet
  creates are private (0700).
- Vendors come from one provider table, and each vendor's wire protocol is
  its own `Protocol` implementation, so adding a vendor CLI is one file and
  one row ([guide](docs/rust/adding-a-provider.md)). No behaviour change.
- Switching provider (`/model claude`, `/model codex`) or `/new` starts the
  next CLI while the old one exits, instead of after it. Commands that
  resume a session still wait, so two CLIs never share one.
- Claude Code gets 1.5 seconds to exit on its own (it needs about 0.9)
  rather than being killed after 0.4; Codex keeps 150 ms. A turn still
  running is interrupted first. Commands that resume a Claude session
  (`/reconnect`, `/effort`, `/mode`, `/fork`, `/model` within Claude) and
  quitting from Claude take about 0.5 seconds longer for it.
- A streaming reply re-wraps only its last line on each update. A reply
  over 64 KiB drops its start in 16 KiB steps, so it may briefly show up
  to 80 KiB.

### Fixed

- A failed disk write could be reported as written (a torn journal record,
  a truncated goal file installed over a good one); every write is now
  flushed and checked, and new files and renames are synced.
- A `!` command's processes outlived a reconnect or quit.
- Claude subagent (Task tool) messages showed as the reply.
- The panic hook restored the terminal for a background job's panic.
- An RPC answer queued behind a pipelined prompt timed out; `--rpc` did
  not exit on `quit` while stdin stayed open.
- The vendor's last error line was occasionally lost; a blank line from
  the vendor ended the session.
- `tput`'s escapes left a stray "B"; an unterminated escape hid the rest
  of a reply; Arabic text wrapped past the screen edge.
- "Cancelling…" stayed after cancelling a connection; a signal during a
  reconnect was lost; a hung `git` stalled the `@` index.

- A reply over 2 MiB is cut with a note instead of stopping the session.
- `NO_COLOR` now removes every colour, not only the mascot's.
- Esc in the instant a queued prompt is sent cancels that prompt's turn.
- `/steer` during a cancel is queued instead of lost; a failed `/image`
  keeps its line; Up recalls a prompt's images even if sending failed.
- `/fork` announces the fork only once the vendor names the new session.
- An effort level Claude does not take is refused instead of breaking every
  reconnect.
- Print mode exits 130 after Ctrl+C even if the vendor then stops, handles
  SIGTERM (143), reports an over-long stdin prompt as one, shuts the session
  down when stdout or stderr is closed, and exits 1 when stdin cannot be
  read.
- A `--cwd` path that is not UTF-8 is refused.
- In the demo, a cancel from before a turn no longer stops the next one.
- The release workflow runs the checks, matches the tag to the version,
  publishes `-` tags as pre-releases and can be re-run.
- PTY acceptance tests stop sending Ctrl+C after terminal restoration and
  capture trailing shutdown output, avoiding false cleanup failures with live
  providers.

[0.1.0]: https://github.com/cartmancodes/octet/commits/master
