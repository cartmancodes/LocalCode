# Linux terminal application: performance and feature parity

Status: proposed design for review; implementation and measurements have not started.

## Intent

Deliver an installable Linux terminal application launched as `localcode`, with
interactive chat and all existing core capabilities. The user requires high
performance without feature loss. No browser or listening HTTP server is needed.
Assume an installed application bundle is acceptable; a single self-extracting
file is not a stated requirement. Target Linux x86-64 and ARM64, verified separately.

## Architecture

Use a Textual terminal presentation layer over the existing Python AgentSession
and engine interfaces. A small controller owns sessions, subscriptions, task
lifetime, and a terminal UIBridge. Widgets issue commands through the controller;
they do not implement provider policy or own persistence. Vendor CLIs remain
child processes and retain responsibility for their own authentication.

Keep print, JSON, RPC, and package-management commands. Enter the TUI by default
only when stdin and stdout are terminals; preserve existing noninteractive RPC
behavior and provide an explicit TUI option. Load TUI dependencies lazily.

Alternatives: a Rust/Go TUI over stdio RPC adds a process and protocol boundary
but may be reconsidered if measured Python rendering overhead misses the budgets.
A full core rewrite would increase parity risk without evidence of a need.

## Feature parity is a release condition

The repository contains both the legacy provider/session runner and the newer
core. Inventory both before implementation. For each capability, record its
source path, terminal action, and behavioral test. A feature may be reported as
preserved only after that test passes; documentation is not proof of equivalence.

Required inventory includes:

- Claude and Codex streaming, model selection, thinking settings, upstream resume,
  tool results, errors, usage, and quota routing/reporting.
- Fleet dispatch, roles, configuration and overrides, gates, verdicts, concurrency,
  limits, approvals, cancellation, and visibility into child progress/results.
- Session listing, history, continue, rename/delete where supported, branching,
  forking, compaction, steering, follow-ups, and workspace selection.
- Permission prompts, denial/timeouts, project trust, tool restrictions, and all
  supported extension UI dialogs and notifications.
- Extensions, custom tools, skills, prompt templates, context discovery, package
  installation/update/removal, and their supported dependency-loading behavior.
- Artifact access, large output, persistence, interrupted turns, and process cleanup.
- Existing print/JSON/RPC behavior and the authentication invariant.

Legacy session storage and core session storage differ. Provide compatibility or
an explicitly tested import path before claiming history parity; never silently
replace or delete existing data. Importing a transcript alone does not prove
upstream model-context resume. Keep legacy adapters where behavior has not been
matched. Existing provider limitations remain visible and documented.

## Performance design

Render only a bounded transcript window and load older display history on demand.
This does not itself bound AgentSession's in-memory history: profile the core's
session loader separately and preserve context/branch semantics when optimizing it.

Coalesce text updates for display at most about 30 times per second while streaming;
flush final text immediately. Preserve ordered semantic events, tool results, errors,
and approvals. Bounded queues must apply backpressure or durable spooling rather
than silently discard state. Render caching must not suppress persisted content.

Keep provider I/O asynchronous. Audit synchronous filesystem work in SessionManager,
resource discovery, and package operations. Offload blocking I/O through an ordered
writer/worker boundary, with explicit completion and error reporting; do not run
concurrent writes against mutable session state. Preserve or improve durability.

Show the prompt before initializing providers. Reuse engine sessions where their
existing lifecycle permits it. Bound live sessions and fleet concurrency. Idle UI
must avoid continuous repainting and polling. Cancellation and approval input must
remain responsive during output bursts. Arbitrary blocking extension code is not
made nonblocking merely by using an async UI; include it in diagnostics and scope
performance guarantees to declared workloads.

Proposed budgets, not measured claims:

| Measurement | Initial acceptance target |
| --- | --- |
| Launch to usable prompt | p95 <= 1 second on the documented reference Linux host |
| Input to visible feedback during replay | p95 <= 50 ms |
| Engine event receipt to display | p95 <= 100 ms |
| Cancel key to cancellation dispatch | p95 <= 100 ms; provider stop latency reported separately |
| Idle UI CPU | <= 1% of one core over 60 seconds after settling |

Record hardware, distro, terminal, bundle version, workload and warm/cold cache
conditions. Separate local latency from vendor/network latency. Report UI/core RSS
and the complete child-process tree separately; do not claim a memory ceiling
without measuring both. Check retained-memory growth across repeated identical
workloads and large histories, alongside absolute peak usage.

## Linux distribution

First validate an installed wheel outside the source checkout; check the existing
Hatch package layout against the `backend.app.core.cli` entrypoint. Then produce
an installed PyInstaller directory bundle with a launcher on PATH. This avoids
per-launch one-file extraction while bundling the Python runtime. Package that
bundle as a versioned Linux archive initially. Define and test the supported glibc
baseline and architecture for each release; do not claim all Linux compatibility.

Bundle fleet resources explicitly. Verify dynamic extension loading and document
how third-party Python dependencies work in the frozen runtime; failure to support
the agreed extension contract blocks parity. Resolve vendor CLI locations, report
missing/login-required states, and keep credentials out of application bundles.
Git/npm remain dependencies for package sources that invoke them.

Any retained legacy fleet path needs a frozen-safe worker entrypoint: its existing
`sys.executable -m ...` assumes a Python interpreter. Account for external-process
library environments in frozen builds. Quit cancels active work, resolves pending
dialogs safely, flushes storage, reaps children, and restores terminal state.

## Validation and delivery order

1. Build the behavioral parity inventory, including legacy/core differences, and
   establish baseline performance on a Linux reference host.
2. Implement the controller/UIBridge and a complete terminal conversation with
   tool approval, cancellation, persistence, and resume.
3. Add the remaining parity flows, compatibility adapters, and targeted performance
   fixes. A conversation-only prototype is not the finished deliverable.
4. Exercise deterministic engine replays, burst output, large histories, slow disk,
   concurrent fleet activity, pending approvals, resize, SSH/tmux, SIGINT, and exit.
5. Run relevant existing suites plus terminal interaction and lifecycle tests.
   Existing legacy latency tests do not certify the new core/TUI path.
6. Build and smoke-test installed Linux artifacts without the source tree or a
   system Python. Test real vendor flows separately from deterministic replays.

## References

- Textual concurrency: https://textual.textualize.io/guide/workers/
- PyInstaller startup/bundles: https://pyinstaller.org/en/stable/operating-mode.html
- Repository contracts: docs/core.md, docs/harness.md, packages/fleet/README.md.
