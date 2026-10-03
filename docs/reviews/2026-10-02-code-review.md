# Code review — 2026-10-02

Scope: the working-tree Rust protocol proof, subprocess transport, and migration
changes touching the existing application. This is not approval to replace the
existing application with the current Rust binary: the terminal UI, persistence,
fleet implementation and installation milestones remain pending.

## Findings fixed

| Priority | Finding | Resolution |
| --- | --- | --- |
| High | Cancellation or I/O failure halfway through a stdin write left the pipe reusable, corrupting subsequent JSON frames. | The write owns stdin until the complete frame is flushed. Cancellation/error closes stdin; subsequent sends fail closed. Cancellation while waiting for the lock does not affect an active writer. |
| High | Repeated shutdown could poll completed task handles and panic. | Cache the shutdown report; take task handles before awaiting them. Shutdown still bypasses the writer lock. |
| High | The failure probe could pass on a successful terminal, missing IDs, or another thread's failed turn. | Require matching thread/turn IDs and the expected terminal status. Offline adversarial transcripts exercise the real gate binary. |
| High | The Python CLI default imported an absent TUI module; prototype tests prevented collection. | Restore RPC/print/JSON behavior, including explicit mode precedence. Preserve the superseded Python prototype and unfinished tests in `docs/archive/python-tui`; remove its incomplete frozen-worker branch and replace draft Make targets with `rust-check`. |
| Medium | Outbound serialization allocated before acquiring the writer lock and before enforcing the frame limit. | Serialize once under the lock through a size-limited writer; reject oversized frames without closing a healthy pipe. |
| Medium | Receive queue accounting charged payload length while retaining larger vector allocations. | Store queued payloads in boxed slices. Queue payload storage is bounded; framing and decoded JSON still have separate overhead. |
| Medium | Malformed JSON left subsequent queued messages consumable. | Make receive failure terminal and discard queued frames. |
| Medium | Fixed TERM sleeps delayed cleanup and group exit could be reported too early after KILL. | Poll bounded process-group exit and reap the leader during the grace period. Treat EPERM as an observable group. |
| Medium | Permissions denial used an array where Codex requires a permission-profile object. | Return an empty profile with turn scope, matching the installed CLI's generated schema. |
| Medium | Interrupt completion depended on acknowledgement arriving first; tool completion could send repeated interrupts. | Retain terminal events while awaiting the acknowledgement for both engines; latch the Codex interrupt send. |
| Medium | Simple probes passed without checking the requested response. | Require READY output; Claude also requires an explicit non-error result. |
| Medium | Numeric server request IDs could be mistaken for client response IDs. | Only method-less messages satisfy Codex response waits. Scope compact notifications to the selected thread and require a known compact turn. |
| Medium | Fixture output validation read an arbitrarily large file into memory. | Read at most six bytes when checking the five-byte READY output. Report fixture initialization failures. |
| Medium | Unquoted absolute fixture paths containing shell syntax could be approved. | Allow absolute command variants only for shell-literal paths; relative fixture commands remain supported. |
| Low | SDK MCP messages during initialization were handled as unsupported requests. | Route fixture MCP initialization traffic through its responder. |
| Low | Clippy rejected PathBuf references and unsupported-scenario errors named the wrong engine. | Accept Path references and report engine/scenario incompatibility accurately. |

## Verification

- macOS ARM64: **23 Rust tests passed**, with one opt-in benchmark excluded from normal tests.
- Linux ARM64 (`rust:1.98.1-slim`, Docker with `--init`): **23 Rust tests passed**. Tests include full stdout queues, stderr flooding, process-group descendants, cancelled writes, malformed frames, repeated shutdown and adversarial protocol transcripts.
- `cargo clippy --locked --workspace --all-targets -- -D warnings` passed; formatting checked.
- Existing Python suite after removing the superseded prototype from the active tree: **865 passed, 2 deselected**. The final mode-precedence addition was then checked with all **3 CLI mode tests**. No production engine, session or fleet semantics were rewritten.
- Repository-wide `ruff check .` passed; archived prototype sources use `.py.txt` to remain non-executable reference material.
- Explicit debug-build transport diagnostic on macOS: 2,000 sequential approximately 280-byte JSON echo round trips, **49 µs median / 73 µs p95 / 77 µs p99**. This is a local transport measurement, not a TUI startup, memory, vendor latency or baseline comparison.
- Live Codex 0.154.0 simple probe passed with READY, late usage and successful cleanup.
- Live Claude 2.1.270 returned an error and cleaned up successfully. A direct CLI invocation reproduced: `Failed to authenticate: OAuth session expired and could not be refreshed`. Claude live functionality cannot be revalidated until authentication is restored. Credentials were not modified.

## Remaining release requirements

The [acceptance matrix](../rust/parity-matrix.md) remains authoritative. The current
binary is a protocol probe, not an installable terminal application. Resume/fork
probes establish protocol/session identity behavior, not retained-history fidelity.
Full parity, long-session resource bounds, Linux x86-64 validation, PTY restoration,
packaging and comparative release performance benchmarks remain open. The local
transport measurements and passing legacy suite do not prove those requirements.

Python extension/plugin compatibility is the explicitly accepted break in the
Rust design. Archiving the cancelled Python TUI does not remove an implemented
feature from the existing application. Its draft tests are preserved as design
references, not counted as passing Rust acceptance tests.
