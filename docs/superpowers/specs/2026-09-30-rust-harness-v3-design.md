# LocalCode Rust harness — revised design v3

Status: approved for staged implementation by the user's subsequent “yes”. Supersedes the
technical recommendations in the supplied Rust v2 proposal; its proposed feature
removals are not assumed to be approved. The previously authorized Python TUI
implementation is paused at the user's request, with partial work preserved on
`feat/linux-tui`. Implementation starts with the protocol proof gate below;
Python-tree removal is not authorized by this revision.

Review basis: [v2 review](../../reviews/2026-09-30-rust-proposal-v2.md).

## 1. Product contract

Build an installable Linux terminal harness with a Rust implementation, responsive
input under load, official Claude/Codex child processes, and tested preservation
of existing core behaviors. No browser or listening network server is required.

The distributable for built-in features should be a static Rust executable where
supported by the verified dependency graph. Vendor binaries, login, git and
explicitly supported non-Python plugin runtimes remain external dependencies.
"One LocalCode binary" does not mean "one process".

Retain print, JSON-event and stdio RPC modes. Stdio RPC is an automation transport,
not a network server. Preserve its piped default and explicit flags; changing
non-TTY behavior requires a documented compatibility/version decision.

Preserve live use of existing v3 sessions, multimodal prompts where engines support
them, session trees and labels, permissions, queues, resources, fleet configuration,
quota and artifact access. Record provider-specific limitations rather than making
a new frontend appear to support operations the provider cannot perform.

Do not remove the existing Python/web/editor trees during this project merely
because the Rust binary runs. Removal is a separate release decision after the
behavioral compatibility and migration gates pass. No user files are deleted or
rewritten by an upgrade without an explicit action.

### Extension compatibility decision — resolved

The user explicitly requires no Python runtime or bridge and accepts extension
API and plugin compatibility breaks. LocalCode therefore ships and runs its
harness entirely in Rust. Python `def setup(api)` extensions, extension-registered
Python engines, and Python-dependent plugins are unsupported. There is no optional
Python compatibility process and no fallback that installs or invokes Python to
restore those APIs.

This is an intentional breaking release for extensibility, not full extension
parity. Publish a migration table covering supported skills/templates, command
hooks and MCP tools, and capabilities with no equivalent (including interactive
Python extension dialogs and arbitrary session access). Existing packages must be
inspected and unsupported executable components reported; do not silently treat a
partially loaded package as fully compatible.

“No Python” means no Python dependency in the delivered harness or its supported
plugin configuration. It does not prohibit a coding agent from editing Python
projects or running a Python command explicitly requested as project work.
Supported plugins declare their runtime dependencies; Python-dependent plugins
are rejected with an actionable compatibility message. Arbitrary shell commands
cannot be proven Python-free by checking their first executable, so the claim is
about the supported dependency contract, not a security sandbox guarantee.

The accepted break does not authorize removal of stdio RPC, npm resource sources,
live session behavior or legacy fleet capabilities. Preserve those contracts
unless separately changed. No-Python compatible resource packages may still use
npm as their source; npm itself is an optional external installation dependency.

## 2. Architecture and crate ownership

Use the proposed eight crates, with composition at the binary boundary:

```text
localcode (composition root)
  ├── lc-tui ──────────> lc-core ──> lc-engine ──> lc-proc
  │                         └────> lc-store
  └── lc-fleet ─────────> lc-core / lc-engine
lc-testkit supplies deterministic drivers, recordings and fault injection.
```

`lc-core` does not depend on `lc-fleet`. The binary registers fleet as a custom
tool/command through core interfaces. Fleet receives a session factory and policy
context so child sessions pass through the same lifecycle, permission and quota
contracts. This avoids a core/fleet crate cycle and avoids a second, weaker
orchestration path.

The terminal uses only session commands, immutable snapshots, paged history and
session events. It does not manage vendor children or mutate storage directly.

Prefer one owner per session. Decide Send versus local-task execution explicitly:
`Rc`/non-Send adapters require LocalSet/spawn_local or another supported local
runtime. A current-thread Tokio runtime by itself does not relax tokio::spawn's
Send requirements. Substantial CPU work and blocking I/O run outside the
interactive executor with bounded concurrency.

## 3. Control, data and lifecycle contracts

Replace the long-lived `Engine::prompt(&mut self).await` interface with a driver
and a cloneable command handle. The driver owns transport I/O and demultiplexing;
request callers do not hold a mutable borrow or mutex for an entire generation.

Illustrative contract (names may change during the implementation plan):

```rust
struct EngineHandle { /* bounded control sender + interrupt token */ }
struct EngineDriver { /* owns process, parser, pending requests */ }
struct RequestTicket { id: RequestId /* completion receiver */ }

impl EngineHandle {
    async fn start_turn(&self, input: UserMessage) -> Result<RequestTicket>;
    async fn steer(&self, turn: TurnId, input: UserMessage) -> Result<RequestTicket>;
    async fn interrupt(&self, turn: TurnId) -> Result<RequestTicket>;
    async fn answer(&self, prompt: PromptId, answer: PromptAnswer)
        -> Result<AnswerStatus>; // accepted / stale / cancelled
}
```

The session actor selects among commands, engine events, request completions,
deadlines and cancellation. It never awaits a whole turn or a user answer inside
the sole command-receive path. Hook work is represented by pending operations;
its completion arrives as another actor message.

Specify each command's accepted/started/completed semantics. State-changing
operations are serialized; approval answers and cancellation remain routable while
one is pending. Every accepted request receives one terminal completion outcome,
even on shutdown. Duplicate/stale answers are observable and harmless.

Event envelopes contain session identity, turn identity when applicable, a
monotonic sequence and typed payload. Switching sessions cannot attach a late
message to the new transcript. Subscribers have a defined cursor/replay policy.
Print, JSON and RPC are consumers of the same semantic stream as the TUI; display
coalescing does not change that stream.

Expose the full supported session surface: prompt with images, abort, steer,
follow-up, queue controls, model/thinking, compact, bash/abort-bash, new/open/list/
rename/trash, fork/clone/navigate/labels, workspace/additional roots, quota/usage,
commands/tools/resources, history paging and artifact inspection. Dedicated
screens are optional; typed commands remain available.

States include initialization, idle, running, compacting, switching, degraded
storage and shutting down. Pending approvals are tracked operations rather than
an exclusive state that prevents other control work. Define transitions for
interrupt-during-initialize, shutdown-during-compaction and switch-during-run.

## 4. Backpressure and resource limits

Keep separate control and bulk-data paths. A full text queue must not stop the
transport from dispatching an approval answer, cancellation or request completion.
Bound both item count and retained bytes, including parsed objects and copies.

| Boundary | Policy |
| --- | --- |
| Child framing | Configured maximum frame bytes, bounded reusable buffer; oversized/malformed frames produce a typed failure and stop the affected run |
| Control requests | Bounded pending requests with deadlines; reserved cancellation signal independent of ordinary queue capacity |
| Engine data | Byte-accounted queue; bulk content spills to bounded disk storage and is referenced by ID |
| Session events | Ordered semantic sequence; visual subscribers may merge adjacent deltas for the same block without crossing a semantic boundary |
| Writer | Bounded serialized bytes plus acknowledged write/durability sequence |
| TUI | Bounded visible blocks, decoded-body cache and pending render work |

Final capacity values are selected from measured normal/large fixtures, then
published with the memory target. Frame limits and queue item limits alone are
not a whole-process memory limit. The initial investigation must include the
existing Python adapters' 8 MiB line limit and real supported vendor messages.

Specify overload outcomes: await a consumer while control remains live, spill,
or terminate the affected operation with an explicit error. Disk-full exhaustion
must not silently discard tool results or hang quit. Full output remains retrievable
up to the documented quota; quota exhaustion is reported, never presented as a
complete result. Partial output and error metadata survive where storage permits.

## 5. Permissions, trust and authentication

Vendor binaries own their login. LocalCode does not open credential stores,
extract keychain entries or forward vendor tokens. Avoid direct vendor API clients.
Static source/dependency scans are regression guards, not proof of sandboxing.
Test relevant process arguments, emitted frames and logs as well as source text.

Define a decision table per engine and mode:

1. Non-overridable role, tool and path restrictions.
2. Supported input rewrites, followed by validation of the effective input.
3. Plugin enforcement vetoes and explicit error policy.
4. Session permission policy and user approval where required.
5. Independent vendor sandbox/mode enforcement; LocalCode cannot grant access the
   vendor denies, nor assume every vendor mode emits an approval callback.

A later allow cannot override a hard denial. Observational hook failure produces
a notice. Enforcement hook failure denies the pending action by default; fail-open
requires an explicit setting and is visible. Denial/timeout/abort is never promoted
to approval on resume, retry or reconnect. No-TTY ask fails closed.

Project resources that execute code require existing trust. Plugin installation
and plugin execution are separate actions. Decide ownership of vendor-native hooks
and settings to prevent the same hook executing twice. Capability negotiation makes
Codex input-rewrite limitations visible. Preserve current role-enforcement tests
and add a cross-product of hooks, policies, sandbox modes and rewritten inputs.

## 6. Process supervision

Each owned child gets tracked identity, an owned process group where possible,
stdout draining, bounded stderr diagnostics and a stop deadline. Normal stop is
protocol interrupt/close, grace, group TERM, grace, group KILL, then reap. Report
which stage was necessary and which descendants could not be verified stopped.

PR_SET_PDEATHSIG is additional protection for direct children, not a recursive
kill guarantee. Handle the parent-death setup race and creating-thread lifetime.
pidfd use avoids ambiguous process identity where supported but does not contain
descendants. Do not compete with Tokio for wait/reap ownership; choose one owner.

For stronger crash containment, support a delegated cgroup/systemd scope where
available. Document fallback guarantees when it is unavailable. Test ordinary
grandchildren, MCP servers, setsid escape, parent death during spawn, and PID reuse.
Do not claim zero orphans under supervisor SIGKILL on every Linux installation.

Count resident processes and memory across foreground sessions, fleet children,
MCP servers, hooks and shell commands. Report LocalCode-only and whole-tree metrics.

## 7. Storage, recovery and compatibility

Read/write pi v3 without destructive migration. Preserve unknown fields where
round-tripping a supported file, original IDs, branch ancestry and engine-session
associations. Reject unsupported schema versions explicitly. Golden fixtures cover
context reconstruction, labels, counts, fork semantics and provider resume IDs.

Keep compact metadata in memory and bodies on disk with a bounded decode cache.
Measure actual entry/map/cache size; a proposed 48-byte struct does not account for
all allocations. Cached listing metadata is an optimization, not the authority.
Use a full streaming scan after uncertain invalidation; incremental scanning is
allowed only after validating unchanged prefix/append assumptions.

One owner holds an exclusive writable-session lock. A second application opens
read-only or receives a clear conflict; independent writers do not interleave.
Use a bounded writer service rather than an unbounded number of per-file threads.

Track accepted, written and durable sequence numbers. Pending entries have an
explicit in-memory overlay until their bytes exist at acknowledged offsets.
Successful switch/quit and a durable-turn acknowledgement await the required
barrier. Handle short writes, EINTR, ENOSPC, sync failures and permissions errors.
Sync directory metadata for durable creation/rename where required.

On failure, expose degraded/read-only state with recovery/export choices; never
claim persistence succeeded. If continuing execution in memory is allowed, bound
its size and stop before exhausting memory. Do not retry into a half-written file.
Quarantine or repair a torn tail under the write lock before any new append.

Define a bounded interrupted-turn loss window. Preserve the existing first-assistant
creation rule for canonical files; a separate recovery journal may checkpoint
in-flight content before it becomes a completed v3 message. A journal replay cannot
invent completed tool calls or an upstream state the engine did not commit. Test
SIGKILL before/after each persistence boundary and interrupted first responses.

Trash is reversible, path-validated and collision-safe. Purge is explicit. Legacy
import leaves source metadata/messages/current files untouched and reports transcript
import separately from upstream resume. Live legacy behavior is a parity requirement
unless the user explicitly accepts import-only support; do not present conversion
of a transcript as conversion of its orchestration semantics.

## 8. Engine adapter verification before the rewrite

First prove small real-CLI transport slices with pinned/tested versions:

- Claude: initialize, streaming, concurrent control requests, hook callbacks,
  approve/deny/cancel, SDK MCP tool list/call, interrupt, resume, fork and compaction.
- Codex: generated schema plus real captured messages, request cancellation,
  approvals, user questions, tool-only turns, multiple agent messages, late usage,
  failures and interrupted turns.

Keep wire vocabulary centralized. Capture redacted fixtures with version/source
provenance and compare externally visible semantics against the existing harness.
Fakes are intentionally adversarial and independent of adapter assumptions. A
recording made by the implementation under test cannot alone validate that adapter.

Distinguish message/block completion from engine turn completion. Do not stop the
Codex run at an intermediate agentMessage; wait for the protocol's terminal turn
outcome while preserving the core's projected message events.

Unsupported vendor versions return actionable compatibility information. Specify
whether an override is permitted and what evidence is required to expand the tested
range. Real login/provider verification is a release gate, not replaced by mocks.

## 9. Fleet behavior

Fleet runs full child sessions through the shared factory and policy contracts.
Preserve config aliases, role policies, gates, verdicts, dispatch budgets, hard-fail
limits, artifacts, auto/quota routing and plan approval where existing workflows
require them. Presets/router behavior is not removed merely because another gate
has a similar name; compare routing outcomes with fixtures.

A warm pool has separate active-dispatch and resident-process caps. Idle sessions
can be evicted before admitting a new role. A turn with planner and coder resident
must still be able to start reviewer at resident cap two. Queued dispatches are
cancellable; shutdown cannot wait behind the queue it needs to cancel.

Reuse requires matching workspace, workflow/turn, role, engine, model, policy and
configuration fingerprint. Continuing a role's context is an explicit dispatch
mode. Fresh-context mode preserves independent review/task semantics. Do not reuse
context solely because two tasks have the same role name. Artifacts/evidence handed
to reviewer/tester are explicit; a coder's claims are not treated as validation.

Progress is throttled for presentation, while semantic completion/errors and usage
remain complete. Abort reaches every owned descendant and queued dispatch. Measure
both active and warm resources, including provider-owned MCP processes.

## 10. Terminal behavior and performance

A minimal ratatui UI may use commands instead of dedicated screens, but keeps the
full agreed command surface. Include accessible approval detail, multiline editing,
bracketed paste, history, viewport scrolling, output inspection and safe export.
Treat model/tool output as untrusted terminal text; sanitize control sequences and
do not allow OSC/escape injection into the surrounding terminal.

Virtualize blocks and cache wrapped content by version and width. Bound decoded
bodies, terminal rows and markdown work. Incremental markdown has explicit
invalidation rules; unfinished blocks, references and width changes may require
bounded reparsing. O(delta) is a benchmark hypothesis for supported cases, not a
universal guarantee.

Use dirty-triggered rendering with a maximum streaming rate of about 30 Hz and
immediate final flush. Check overdue deadlines and process bounded batches so
input floods, event floods and painting cannot starve each other. Cancellation
has a separate wakeup/control path. No idle spinner or periodic repaint timer.

The input read thread, parsing and rendering have explicit work budgets. Large
JSON/markdown/paste processing moves to bounded workers where necessary. A Tokio
current-thread runtime does not make CPU-heavy code preemptible.

Normal exit and catchable signals restore terminal state after shutdown deadlines.
Suspension/resumption restores raw/alternate-screen state. Panic hooks perform
best-effort synchronous terminal restoration. panic=abort skips destructors and
cannot promise async flushing/reaping; abrupt death uses the separate process/
recovery guarantees. Test real PTYs at multiple sizes, over SSH and under tmux.

## 11. Plugins and compatibility claims

Use a versioned manifest and capability schema. Claude layout may be a discovery
format, but compatibility is a documented tested subset, not an implication of
matching directory names. Validate hook JSON fields, exit codes, matchers, event
names, ordering, expansion, environment and plugin-root resolution. Unknown
security-relevant features cannot silently become no-ops.

MCP is a custom-tool transport, not a substitute for every extension capability.
Declare per-engine support and dispatch ownership. Supported plugins may have
explicit non-Python runtime dependencies (for example Node or a native executable);
Python-dependent plugins and the old Python extension API are excluded by the
user-approved decision in §1. Keep those requirements separate from LocalCode's
built-in installation requirements. Install/update must not execute lifecycle
scripts implicitly; trust, source pinning and execution are separately visible.

Legacy manifests containing unsupported extensions produce a compatibility report.
Skills/prompts may be migrated only with explicit reporting of excluded executable
features. Do not claim existing Claude plugins work unchanged: publish fixture-
backed compatibility for the supported subset.

## 12. Measurement and distribution

Retain the original release floor: p95 usable prompt <=1 second, input feedback
<=50 ms, event-to-display <=100 ms, cancellation dispatch <=100 ms and <=1% idle
CPU over 60 seconds on the documented reference host. Investigate the Rust targets
(50/16/50/16 ms respectively, <=15 MiB idle RSS, <=40 MiB for the defined large
session fixture) as stretch targets until measured. Do not relax correctness to
hit an unverified number.

Measure equal milestones for Python and Rust: usable editor, session loaded,
command accepted, first engine event and steady-state turns. Record raw samples,
p50/p95/p99, fixture bytes/tool sizes, software versions, cache method, CPU and
terminal conditions. Separate headless/source tests from installed real-terminal
results. Measure all children separately and in aggregate. Define plateau/slope
limits and observation duration for memory/fd soaks rather than simply saying flat.

CI uses deterministic correctness gates and controlled benchmark comparisons;
a 10% regression threshold requires a measured noise/confidence policy. Absolute
budgets are checked on the reference host. No published claim is based only on
first frame while essential initialization remains unavailable.

Try musl x86-64/ARM64 builds, inspect dynamic dependencies and benchmark against
GNU builds. mimalloc/LTO/strip choices are measured tradeoffs. If GNU is needed,
publish its glibc floor and do not label that artifact fully static. Package
versioned archives with checksums, safe user-level installation and rollback.
Smoke-test without system Python for built-in flows and every supported plugin
fixture; unsupported Python packages must fail with a clear compatibility report. ARM64 support requires real ARM64 smoke evidence.

## 13. Delivery gates

1. **Scope and contracts:** record the accepted no-Python extension break, retain
   other existing core contracts, and freeze the parity matrix, command/event
   semantics and failure guarantees.
2. **Protocol proof:** real Claude/Codex minimal transport and concurrent-control
   tests, with recorded versioned fixtures. Stop the rewrite if this fails.
3. **Foundations:** process supervision, v3 storage/recovery and fake driver; fault
   injection, property tests and differential fixtures pass.
4. **Vertical slice:** print/JSON/RPC plus minimal TUI, prompt/approval/cancel/
   restart/resume, long output and failures through the actual event pipeline.
5. **Parity:** fleet, resources, supported non-Python plugins, quotas and live legacy or
   agreed migration behavior. Every preserved row maps to an action and test.
6. **Release evidence:** real vendor suite, PTY/SSH/tmux tests, resource soaks,
   installed x86-64/ARM64 smoke tests and published performance measurements.
7. **Optional retirement:** separately review removal of old components after
   users can migrate. Archive executable compatibility releases and fixtures;
   never delete user data as part of source cleanup.

For each gate, name the artifact, fixture, test and failure outcome. A fixture may
record a known Python bug; document corrected behavior rather than copying the bug
as a parity obligation. Crate/line counts are organization aids, not delivery
estimates or evidence of readiness.

## References

- [Tokio local task execution](https://docs.rs/tokio/latest/tokio/task/struct.LocalSet.html)
- [Tokio select scheduling/fairness](https://docs.rs/tokio/latest/tokio/macro.select.html)
- [Linux parent-death signals](https://www.man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)
- [Linux writes](https://man7.org/linux/man-pages/man2/write.2.html)
- [Linux file/directory synchronization](https://man7.org/linux/man-pages/man2/fsync.2.html)
- [Claude hook contract](https://code.claude.com/docs/en/hooks)
- [Rust destructor behavior](https://doc.rust-lang.org/reference/destructors.html)
