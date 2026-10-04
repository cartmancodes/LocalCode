# Octet TUI features

This document describes the **Rust terminal UI preview**. It is a native terminal
application for Linux and macOS that runs the installed `codex` or `claude` CLI as
a child process. It does not need a browser, localhost server, or Python runtime.
The vendor CLIs handle their own login. The [usage guide](../tui.md) has the full
key reference and installation details; the [parity matrix](parity-matrix.md)
tracks the work remaining for parity with the earlier Python harness, which was
removed from the repository on 2026-10-04.

## What works today

| Feature | Current behavior |
| --- | --- |
| Provider sessions | Start a multi-turn conversation with Codex or Claude. Responses stream into the transcript alongside tool activity, usage, and errors. An offline `demo` engine exercises the interface without a vendor login. |
| Model discovery | `/model` lists the models advertised by the **active installed CLI**, with names, full model IDs, descriptions, and selection commands. Codex pages through `model/list`; Claude uses its initialization catalog. `/model list N` opens another page. |
| Model selection | Start with `--model ID`, or use `/model ID`, `/model codex ID`, or `/model claude ID`. `/model default` selects the current provider's default. `/session` shows the requested selection, catalog ID, and confirmed session model ID separately. Custom IDs may be entered even when absent from the catalog. |
| Provider switching | `/model claude` and `/model codex` switch the active engine. The visible transcript and prompt history remain on screen. A same-provider switch resumes the vendor session; switching providers opens a fresh vendor context and a new journal. |
| Prompt editor | Multiline input, Unicode-aware cursor movement, bracketed paste, a 50-prompt history, and a 64 KiB prompt limit. Enter sends; Alt+Enter or Ctrl+J inserts a newline. |
| Conversation view | Scrollback, cached word wrapping, a responsive workspace panel, a help view, and a command palette. The interface accepts terminals as small as 38 by 12 cells and respects `NO_COLOR`. |
| Approval and cancellation | Command and file-change requests can be allowed once or denied in a modal showing the complete request. Esc or Ctrl+C cancels an active turn. Oversized or expired approval requests are denied. |
| Sessions and journals | `/new` starts a fresh vendor context; `/reconnect` reopens the current one. `--resume ID` opens a vendor session on launch. Events are written to a private JSONL journal before appearing in the UI. `/session` shows its path; `/export [NEW_PATH]` creates a copy without overwriting an existing file. |
| Terminal lifecycle | Ctrl+Q exits, Ctrl+Z suspends for shell use, and terminal modes are restored on ordinary exit, supported signals, and panic. |
| Persistent goals | `/goal <objective>` continues across successful turns, with status, pause, resume, audit, and clear commands. Goals survive restart in a private workspace-scoped file and load paused. Completion currently relies on a model-reported audit marker. |

Model availability depends on the installed CLI, its version, account, and
provider settings. The catalog is a live picker list, not a promise that every
historical model is available. Up to 256 entries are retained, with 20 shown per
page. The header may clip long names; `/model` and `/session` show the complete
details in scrollback.

## Start and use it

From the repository root, build with the pinned Rust toolchain and open a real
terminal:

```sh
make rust-build
./target/release/octet --engine demo
./target/release/octet --engine codex --cwd /path/to/project
./target/release/octet --engine claude --cwd /path/to/project
```

Codex and Claude modes require the corresponding vendor CLI installed and signed
in. The engine defaults to Codex and the workspace defaults to the current
directory. `--binary PATH` selects a custom vendor executable, and
`--journal-dir PATH` changes where preview journals are written.

For a user-level Linux installation, build **on Linux** and install from the
repository:

```sh
scripts/rust-env.sh cargo install --locked --path crates/octet --root "$HOME/.local"
"$HOME/.local/bin/octet" --engine codex
```

An existing executable at the destination needs to be resolved before install;
this command intentionally does not force replacement. Prebuilt release archives
and cross-architecture installers have not been published.

Once the TUI is open, a typical sequence is:

```text
/model                 List models for the active engine
/model claude opus     Switch to Claude and select its Opus alias
/session               Inspect the active model, session ID, and journal
/model codex           Switch back to Codex's default model
/export /tmp/chat.jsonl  Save a new copy of the journal
```

Switching to Claude does **not** send the earlier Codex conversation to Claude.
Only one provider runs as the active conversation at a time. The two engines do
not yet collaborate on one shared task. Switching providers preserves the
displayed messages for reference, but the new provider starts without them.

## Preview boundaries

- There is no browser for old journals, automatic local transcript replay after
  reconnect, or crash recovery for an incomplete journal tail. Sessions from the
  earlier Python application are not read.
- Images, steer and follow-up queues, full thinking and compaction controls,
  fleet orchestration, quota routing, resources, general plugin parity, and compatible
  print/JSON/RPC modes remain to be implemented in Rust.
- The Rust preview does not execute Python extensions. This is an accepted
  compatibility break for the Rust design; the earlier Python application was
  removed from the repository on 2026-10-04.
- The visible transcript is bounded to 160 blocks or 512 KiB, while the journal
  retains older events up to its 64 MiB limit. The journal stores prompts,
  model output, and tool details, so treat its path as private workspace data.
- Linux ARM64 terminal tests pass. Linux x86-64 distribution packages, SSH and
  tmux acceptance, full parity, and comparative performance gates remain open.

The source of truth for the exact commands and current constraints is the
[TUI guide](../tui.md). The [acceptance matrix](parity-matrix.md) records each
remaining release requirement.
