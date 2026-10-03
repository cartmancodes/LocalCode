# Review: LocalCode Rust proposal v2

Reviewed 2026-09-30 against the pasted proposal, the current Python harness, the
installed Claude SDK transport, and primary runtime/OS documentation. This is an
architecture review, not approval to replace the ongoing Python TUI implementation.
The proposal's benchmark figures were supplied by its author; they were not
independently reproduced in this review.

**Recommendation:** Rust is a credible long-term implementation choice for a
Linux terminal harness. Keep the single-owner session, lazy engines, indexed
storage, virtual transcript, official CLI authentication, and golden-fixture
strategy. Revise the items below before treating this as an implementation-ready
plan. The difficult work is behavioral compatibility and lifecycle correctness,
not drawing the terminal or translating Python classes.

## Decision recorded after review

The user chose to revise the Rust design and explicitly accepted Python extension
API/plugin compatibility breaks in exchange for no Python runtime or bridge.
R1 is therefore resolved for those specific extension removals; it still applies
to unrelated removals such as stdio RPC, npm resource sources and live legacy
behavior. The revised contract is in
[the v3 design](../superpowers/specs/2026-09-30-rust-harness-v3-design.md).

## Blocking findings

### R1 — The feature contract changed without agreement (§0, §1, §8, §12)

The user's requirement was high performance without losing core features. This
proposal intentionally removes stdio RPC, Python extensions, extension engines,
interactive extension dialogs and npm package sources; it reduces legacy session
support to import and leaves legacy fleet behaviors optional. Shell hooks and
prompt templates are not replacements for an interactive extension API.

Make two lists: capabilities required for a parity release, and proposed product
removals requiring an explicit scope decision. Do not count replacements as
preserved unless a migration test demonstrates equivalent behavior. Keeping stdio
RPC does not require a listening server: the proposed SessionHandle command/event
boundary is already most of the mechanism. Its value includes automation and
regression testing even if the web/editor UIs are retired.

### R2 — The engine interface cannot support the required control concurrency (§5, §7)

`prompt(&mut self, ...).await` holds the mutable engine borrow until the turn
finishes. `interrupt(&mut self)`, `steer(&mut self)` and `set_model(&mut self)` cannot
be called on that engine concurrently. Wrapping it in a mutex only moves the
problem: abort waits behind the run it needs to stop. Similarly, a session actor
that awaits a prompt or permission callback inline cannot process the answer
command needed to finish that callback.

Split a cloneable EngineHandle (requests, interrupt, answers) from an EngineDriver
(the process reader/writer and ordered event stream). The session must multiplex
commands, engine events and child completions instead of awaiting a whole turn in
its command handler. Define command acknowledgement versus operation completion,
request/turn/session identifiers, and exactly-once terminal outcomes. Permission
answers need an accepted/stale/cancelled result, not an unobservable `fn answer()`.

With `Rc` and `async_trait(?Send)`, specify LocalSet/spawn_local (or an explicitly
supported local runtime); current_thread alone does not make tokio::spawn accept
non-Send futures. [Tokio LocalSet](https://docs.rs/tokio/latest/tokio/task/struct.LocalSet.html)

Required test: while prompt is active and awaiting approval, deliver a second
control request, cancel it, send a stale answer, then abort; every task settles.

### R3 — Backpressure is not bounded by memory, and can deadlock control (§7)

"Always drain stdout", "bounded producer waits", "semantic events never dropped"
and finite memory require an explicit overload policy. A 1,024-item queue with
64 MiB payloads is potentially 64 GiB before parsing/copies. The session-to-TUI
and writer queues have no byte budgets at all. If one reader awaits an event
queue that is blocked behind a permission prompt, the reader may never reach the
control frame needed to resolve that prompt.

Separate protocol/control dispatch from bulk data delivery. Bound bytes as well
as item counts, define maximum accumulated message/tool sizes, coalesce only
compatible display deltas, spill bulk payloads with references, and specify what
happens when the spill disk fills. No finite design can drain an unlimited producer
forever without blocking, dropping, spilling or terminating it. Make that choice
explicit rather than calling it "always drained".

Required test: saturated output + pending approval + slow storage + cancel; assert
bounded retained memory and timely control handling.

### R4 — Permission and hook failure semantics need a decision table (§6.6, §8)

The proposed order differs from the existing core callback order. More seriously,
CLI permission modes/sandboxes are independent enforcement layers, not a last
step LocalCode can reliably run after its own decision. Some modes avoid normal
approval callbacks entirely. Hook timeouts treated as "no decision" can convert
a guardrail failure into an allowed command under default-allow.

Define hard role/tool/path denials, plugin vetoes, interactive approval and vendor
sandbox behavior separately. A later allow must not overturn a hard denial.
Distinguish observational hooks from enforcement hooks; require explicit fail-open
configuration for an enforcement hook. Revalidate rewritten inputs before tool
execution. Test ordinary, accept-edits, plan, bypass and no-TTY cases per engine.

The implementation today makes this nuance visible in
`backend/app/core/agent_session.py:_before_tool/_permission_request` and the
engine-specific approval adapters; a line-for-line port is not a security proof.

### R5 — Parent-death signals do not contain the full process tree (§6.2)

PR_SET_PDEATHSIG covers the direct child that sets it, is cleared for its children
on fork, and tracks the creating parent thread. It does not guarantee that vendor
grandchildren, MCP servers or shell descendants die with LocalCode. There is also
a parent-death/setup race to handle. pidfds identify/reap processes; they do not
turn them into a recursively supervised process tree.
[Linux PR_SET_PDEATHSIG](https://www.man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)

Specify orderly-exit guarantees separately from supervisor SIGKILL/crash recovery.
Use process groups plus a precise fallback; consider a delegated cgroup/systemd
scope for stronger containment, while documenting its availability requirement.
Test a grandchild, a setsid escape, parent death during spawn, stderr/stdout floods,
and PID reuse. Avoid promising zero orphan processes on all Linux systems without
an enforcement mechanism that actually provides that guarantee.

## Important corrections

### R6 — The storage failure model conflicts with disk-backed bodies (§6.1)

Updating the index before the writer acknowledges bytes creates offsets pointing
to data that may not exist. "Continue in memory" after a write failure requires an
explicit in-memory overlay and a recovery/export path; an offset-only index cannot
serve those entries. Define accepted, written and durable sequence numbers, bounded
pending bytes, and which sequence a successful turn/switch/quit acknowledges.

One write per line is not guaranteed to write the whole line. Handle short writes,
EINTR, ENOSPC and sync failure. A torn trailing line must be repaired/quarantined
before a subsequent append, not merely ignored during reading. Include a
single-writer file lock and a policy for two LocalCode instances opening one file.
Directory sync is needed for durable creation/rename, not just fdatasync of content.
[write(2)](https://man7.org/linux/man-pages/man2/write.2.html),
[fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html)

End-of-turn sync alone does not establish mid-turn crash recovery. Specify the
maximum loss window, partial-response checkpoint format, and behavior before the
first completed assistant message. Preserve v3 and unknown fields through round trips.

The bounded head/tail scan cannot reconstruct exact counts or all metadata after
arbitrary edits to the middle of a file. Use a complete streaming scan on cache
invalidation; incremental append scanning is valid only when that assumption is
verified. A 48-byte index entry estimate should be backed by size_of plus allocator,
map, cache and payload measurements.

### R7 — Warm fleet sessions change semantics and can exhaust the pool (§6.7)

A second dispatch to the same role now retains earlier model context. That is a
behavior change: task contamination, stale reviewer assumptions and different
quotas/costs are possible. Define reuse keys including workspace, role, model,
policy/configuration version and workflow identity; provide fresh-context dispatch.
Keep independent review roles isolated where that is part of the workflow.

With max_concurrency=2, planner and coder can occupy two warm process slots until
turn end, leaving reviewer unable to start. Specify separate active/running and
resident-process limits, idle LRU eviction, cancellation of waiters, and behavior
for nested dispatch. Include all vendor and MCP descendants in resource reports.

The dependency graph also needs a decision: lc-core depends on lc-fleet, but fleet
is described as constructing full child sessions. If those need lc-core, there
is a crate cycle. Inject a dispatch host/session factory through a lower-level
interface or have the composition root register fleet; do not quietly bypass
session permissions/persistence to avoid the cycle.

### R8 — Claude plugin compatibility is overstated (§8)

The directory layout alone does not establish compatibility. The proposed hook
JSON is not the full current Claude hook contract: PreToolUse decision fields,
exit-code semantics, environment expansion, plugin root paths, matcher behavior,
ordering, async hooks and supported events all matter. Current docs distinguish
PreToolUse hookSpecificOutput.permissionDecision from top-level decision fields
used by other hooks. [Claude hooks reference](https://code.claude.com/docs/en/hooks)

Publish a tested compatibility subset. Unsupported fields/events should produce
an actionable error or warning, not silently lose enforcement. Explicitly prevent
hooks running twice through both vendor configuration and the LocalCode adapter.
Do not claim identical behavior across engines when Codex cannot rewrite tool input.

Plugin commands/MCP servers can themselves require Python or Node. "No Python in
LocalCode's runtime" is supportable; "no Python anywhere" is not compatible with
arbitrary existing plugins. Likewise, the process count must include MCP servers.

### R9 — Validate the Claude adapter before building most of the product (§6.3, §16)

Replacing the official SDK transfers its protocol maintenance burden to this
project. Single-module wire constants, version checks and replays are good, but
replaying your own interpretation is insufficient. Move a small live Claude
contract gate to the beginning: initialize, prompt, concurrent tool approval,
control cancellation, SDK MCP dispatch, interrupt, resume, fork and compaction.
Record redacted fixtures from real supported binaries with version provenance.

Also retain Codex's distinction between message completion and turn completion.
The Python adapter closes projected message/turn blocks on agentMessage completion
but continues consuming until turn/completed; intermediate messages must not end
the engine run. Cover tool-only/error/late-usage turns and multiple agent messages.

### R10 — The rendering/runtime guarantees need more precise scheduling (§6.8, §7)

With biased select and events ahead of frame deadlines, a continuously ready event
stream can starve drawing; continuous input/paste can starve session events. Use
bounded batches, overdue-deadline checks and a separate cancellation path, then
test fairness under simultaneous input/output floods.
[Tokio select fairness](https://docs.rs/tokio/latest/tokio/macro.select.html)

A single-thread runtime still stalls on expensive JSON parsing, markdown wrapping
or a large paste, even without blocking syscalls. Budget per-poll CPU work and
move substantial parsing/layout off the interactive thread where required.
Incremental markdown is not universally O(delta): changed wrapping, reference
links and unfinished constructs can invalidate earlier rendering. State the
supported subset and invalidation rules, then benchmark pathological documents.

panic=abort skips destructors. A panic hook can perform best-effort terminal
restoration, but cannot promise async store flush and child shutdown; direct abort,
SIGKILL and severe failures bypass normal teardown. Declare separate normal-exit,
catchable-failure and abrupt-death guarantees.
[Rust destructors](https://doc.rust-lang.org/reference/destructors.html)

### R11 — Benchmarks do not yet isolate the benefit of the rewrite (§2, §9, §14)

Python's time to first RPC response is not equivalent to Rust's time to first
frame with deferred initialization. Measure both languages at the same milestones:
usable editor, session loaded, first command accepted, first engine event, and
steady-state turns. Record imports, versions, fixture sizes, warm/cold method,
CPU governor, sample counts and raw samples. This session's Python baseline was
863 passed, 2 deselected without a timing failure; the proposal's separate flake
claim needs its failing test name and log.

Report parent RSS/PSS and total process-tree memory. Faster shell startup does not
predict faster model generation. Use confidence/noise policy for a 10% CI regression
gate; otherwise shared runners will generate false failures. musl, mimalloc and
fat LTO are candidates to measure, not sufficient explanations of performance.

### R12 — The public interfaces and test plan omit claimed capabilities (§5, §12)

The SessionHandle sketch has no event subscription API and lacks images/multimodal
prompt content, navigate/clone/labels/delete, workspace/additional-directory changes,
and structured usage/tool inspection. Some can be intentionally scoped out, but
then the parity table must say so. A minimal TUI can expose advanced commands
without growing a dedicated screen for every function.

Add session/turn/sequence identities to events so late events after a switch do
not contaminate a new transcript. Golden tests should compare externally visible
semantics and durable files, including ordering and error outcomes; do not force
new code to reproduce known Python bugs merely because a fixture contains them.

## Suggested delivery order

1. Resolve the parity/removal contract and define durable event/control semantics.
2. Prove live Claude and Codex transport, approvals, MCP tools and interruption in
   small executable probes before investing in the full TUI/store rewrite.
3. Build process supervision and v3 storage with failure-injection and concurrency
   tests; establish differential fixtures against Python.
4. Deliver one Rust vertical slice: prompt, tool approval, cancel, restart/resume,
   streamed output, and error recovery through print/JSON and the minimal TUI.
5. Add fleet and plugins behind explicit capability negotiation and compatibility
   tests; verify resource bounds and real session migration.
6. Publish Linux benchmark/artifact evidence on both architectures, then decide
   whether the agreed removal criteria have been met. Keep a runnable compatibility
   release until migrations are demonstrated, not merely until unit tests pass.

The proposal is a useful architecture direction, but the line-count estimate and
crate count should not be used as an effort estimate. The release gate should be
observable behavior under failure, cancellation and recovery—not "all code ported".
