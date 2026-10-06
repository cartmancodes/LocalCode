# Changelog

All notable changes to Octet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

The first public preview: a native Rust terminal workspace for the official
Claude Code and Codex CLIs.

### Added

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
- Dual licensing under MIT or Apache-2.0.
- CI on macOS and Ubuntu (`make rust-check`) plus a `cargo audit` job.

### Changed

- `@` suggestions rank about four times faster in large workspaces (under
  25 ms for 50,000 files).
- Help, the command palette, Tab completion, the sidebar and `octet --help`
  are built from one command list; help shows one line per command and sizes
  itself to fit narrow terminals.
- A `!` command stopped by its time limit names that limit.
- Release builds are about 40% smaller (thin LTO, stripped symbols).
- The protocol gate moved to its own `octet-gate` crate.

### Fixed

- PTY acceptance tests stop sending Ctrl+C after terminal restoration and
  capture trailing shutdown output, avoiding false cleanup failures with live
  providers.

[0.1.0]: https://github.com/cartmancodes/octet/commits/master
