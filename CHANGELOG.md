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
- Dual licensing under MIT or Apache-2.0.

[0.1.0]: https://github.com/cartmancodes/octet/commits/master
