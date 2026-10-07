# Rust migration acceptance matrix

This matrix freezes the scope accepted in the v3 design. The Python tests named
below are behavioral references at the commit given underneath. Every retained row
needs Rust acceptance tests before release; none is satisfied by crate scaffolding.
A Rust TUI vertical slice is now available (2026-10-03); see [its scope](../tui.md).
The preview does not satisfy the full release matrix. On 2026-10-04 the release
binary passed scripted live terminal checks against Claude Code 2.1.270 and Codex
CLI 0.154 (model `gpt-5.5`): streaming,
tool approval (allow and deny), interrupt, the four permission modes, model switch,
resume after reconnect, `/goal`, `/export` and clean shutdown. Full parity remains
pending.

On 2026-10-07 the preview added the controls both vendors support today: a
follow-up queue, steering, reasoning effort, fork, compaction, images, a
session browser, print/JSON/RPC modes and release archives
([design](../superpowers/specs/2026-10-07-pending-features-design.md)). Each is
tested against the fake vendors; the rows below say what remains, and why.
The same day, every feature was checked live through the release binary
against Claude Code 2.1.289 and Codex CLI 0.154 (`gpt-5.5`), in the terminal
interface and in print, JSON and RPC modes.

The Python implementation these references name was removed from the tree on
2026-10-04. Read a reference at the last commit that has it:
`git show 54f4ecec4f42dbde6fa0fcbbb72af06936937bac:backend/tests/<file>`.

| Contract | Existing reference | Rust acceptance requirement | Status |
| --- | --- | --- | --- |
| Claude streaming, tools, hooks, approval, interrupt, resume/fork | `test_engine_claude.py`, `test_claude_client_reuse.py`, replay fixtures | Live versioned protocol evidence and deterministic cancellation races | Preview driver tested offline and live in the TUI on 2026-10-04 (streaming, tools, approval, interrupt, resume); fork (`--fork-session`) and compaction added 2026-10-07; hooks and the full gate pending |
| Codex streaming, approval, interrupt, resume/fork/compact | `test_engine_codex.py`, `test_codex_jsonrpc.py`, real trace fixture | Correlated requests; terminal turn completion; live controls | Preview multi-turn/control tests passed offline and live in the TUI on 2026-10-04 (streaming, approval, interrupt, resume); `thread/fork`, `thread/compact/start` and `turn/steer` added 2026-10-07; the full gate pending |
| Official authentication ownership | `test_auth_invariant.py`, `test_home_isolation.py` | No reading/extraction of credentials; use vendor login | Static guard in place (`crates/octet/tests/credentials.rs`); runtime argument/log checks pending |
| Session prompt/images, steer/follow-up, queues, model/thinking, compact, shell controls | `test_agent_session.py`, `test_session_manager.py` | Typed commands accessible from TUI, print, JSON and RPC | Done in the preview (2026-10-07): images (`/image`), steering (`/steer`; queued for Claude, which has no steering request), the follow-up queue, model and reasoning effort (`/effort`), compaction (`/compact`), `!` shell commands; the same commands drive print and RPC |
| New/open/list/rename/trash, branch/fork/clone/navigation/labels | `test_session_manager.py`, `test_agent_session.py` | Stable session and turn IDs; stale messages cannot cross sessions | New, list (`/sessions`, from Octet's journals), open (`/resume N`) and fork (`/fork`) done on the vendors' own sessions. Rename, trash, clone, navigation and labels wait on a decision: whether Octet keeps its own conversation store or relies on vendor history |
| Durable v3 history, paging, recovery and artifacts | `test_storage_cost.py`, `test_storage_offload.py`, `test_artifacts.py` | Torn-tail repair, durable barriers, single writer, bounded page/index memory | Pending, with the store decision above. Durable barriers and a single writer per journal exist; the session browser reads at most 64 KiB per journal and skips a torn tail, but nothing repairs or pages history |
| Approval, role policy and hook enforcement | `test_permissions.py`, `test_approvals.py`, `test_role_enforcement.py` | Hard denials cannot be overridden; enforcement fails closed; cancelled answers stale | Approvals fail closed (expired, cancelled, oversized, unknown, over the cap of eight), and an answer to a cancelled or finished request never reaches the vendor (tested 2026-10-07). Role policy and hook enforcement pending |
| Fleet orchestration and worker lifecycle | `test_worker_pool.py`, `test_fleet_package.py`, golden fleet fixtures | Preserve roles, budgets, routing, gates, verdicts and context semantics | Pending: a new subsystem needing product decisions (which agents, which budgets, how verdicts gate work). The provider table and `Protocol` trait are its foundation |
| Quota and usage | `test_core_quota.py`, `test_quota.py`, `test_usage.py` | Per-provider accounting, late usage and quota transitions | Pending, with fleet: routing between providers needs the fleet's budgets. Per-turn usage is shown |
| Resources, package sources, supported non-Python plugins | `test_resources.py`, `test_packages.py` | Preserve resource access including npm sources; capability validation | Pending: these vendor surfaces install code and change configuration, and Octet leaves vendor configuration to the vendor CLI; managing them needs a security design first |
| Stdio RPC, print/JSON, command discovery | `test_rpc.py`, `test_envelope.py` | Shared semantic event stream, paging/replay contract, output compatibility | Print (`--print`), JSON (`--output json`) and stdio RPC (`--rpc`) done 2026-10-07 on one event stream in the journal's record shape; paging and replay pending with history |
| Legacy live behavior or reviewed migration | `test_core_ws_e2e.py`, legacy integration contracts | Explicit feature mapping and executable compatibility fixtures | Pending: the Python application was removed on 2026-10-04, and compatibility with its envelope needs a migration decision |
| Linux installation and terminal behavior | v3 release gate | x86-64/ARM64 installed smoke tests; PTY, SSH, tmux and terminal restoration | ARM64 PTY tests passed; CI runs every test on Ubuntu x86-64; tagged releases build Linux x86-64 and ARM64 archives (`release.yml`). Installed smoke tests on Linux and SSH/tmux acceptance pending |
| Resource and responsiveness guarantees | `test_latency_budgets.py`, `test_leak_containment.py`, `test_soak_long_session.py` | Apples-to-apples release benchmarks and long-session bounds | Bounded preview and idle diagnostic implemented; release benchmarks pending: they need the old Python release built for comparison |
| Python extension API and Python plugin execution | `test_extensions.py` | Clear unsupported error and documented migration; no Python bridge | Accepted compatibility break |

File names above are relative to `backend/tests/` at `54f4ecec4f42`. Historical bugs may be corrected
with an explicit decision and replacement fixture; copying a bug is not required.
The Python tree is no longer part of the repository.
