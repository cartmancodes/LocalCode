# Linux TUI implementation evidence

Branch: feat/linux-tui.
Baseline: `.venv/bin/python -m pytest -q` — 863 passed, 2 deselected in 50.11s.

Implementation uses independent runtime, terminal and packaging tasks, with
integration and performance verification in the main session. The existing
checkout is retained so the IDE sees changes; runtime logs and user sessions
are excluded from edits and staging.

A dedicated runtime loop thread preserves existing synchronous storage ordering
while keeping it off the terminal loop. This is deliberately narrower than a
rewrite of core persistence. Blocking extensions can still delay runtime work.

## Paused by user

The user switched to revising the Rust design before further implementation.
Partial Python TUI files and tests remain on feat/linux-tui; they are incomplete
and not a runnable release. No full-suite result after these partial edits is
claimed. The command/transcript subset passed 5 tests before the switch.
The prior baseline remains 863 passed, 2 deselected. Rust review and revised design:
`docs/reviews/2026-09-30-rust-proposal-v2.md` and
`docs/superpowers/specs/2026-09-30-rust-harness-v3-design.md`.

## Superseded by the Rust preview (2026-10-04)

The partial Python TUI was archived under `docs/archive/python-tui/` and removed
from the tree on 2026-10-04; it remains in git history. Active implementation is the Rust workspace in
`crates/`, with a working terminal preview and live provider model discovery.
See [the current guide](../../tui.md) and
[remaining parity requirements](../../rust/parity-matrix.md). Earlier sections
record the original Python work and are not the current implementation status.
