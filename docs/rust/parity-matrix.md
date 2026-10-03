# Rust migration acceptance matrix

This matrix freezes the scope accepted in the v3 design. Existing Python tests are
behavioral references, not dependencies of the Rust build. Every retained row
needs Rust acceptance tests before release; none is satisfied by crate scaffolding.
A Rust TUI vertical slice is now available (2026-10-03); see [its scope](../tui.md).
The preview does not satisfy the full release matrix. Claude catalog discovery and terminal rendering passed live checks on 2026-10-04.
Claude inference was not revalidated after the earlier expired-authentication
failure; full parity remains pending.

| Contract | Existing reference | Rust acceptance requirement | Status |
| --- | --- | --- | --- |
| Claude streaming, tools, hooks, approval, interrupt, resume/fork | `test_engine_claude.py`, `test_claude_client_reuse.py`, replay fixtures | Live versioned protocol evidence and deterministic cancellation races | Preview driver tested offline; live revalidation blocked by authentication |
| Codex streaming, approval, interrupt, resume/fork/compact | `test_engine_codex.py`, `test_codex_jsonrpc.py`, real trace fixture | Correlated requests; terminal turn completion; live controls | Preview multi-turn/control tests and live TUI chat passed; full gate pending |
| Official authentication ownership | `test_auth_invariant.py`, `test_home_isolation.py` | No reading/extraction of credentials; use vendor login | Pending |
| Session prompt/images, steer/follow-up, queues, model/thinking, compact, shell controls | `test_agent_session.py`, `test_session_manager.py` | Typed commands accessible from TUI, print, JSON and RPC | Pending |
| New/open/list/rename/trash, branch/fork/clone/navigation/labels | `test_session_manager.py`, `test_agent_session.py` | Stable session and turn IDs; stale messages cannot cross sessions | Pending |
| Durable v3 history, paging, recovery and artifacts | `test_storage_cost.py`, `test_storage_offload.py`, `test_artifacts.py` | Torn-tail repair, durable barriers, single writer, bounded page/index memory | Pending |
| Approval, role policy and hook enforcement | `test_permissions.py`, `test_approvals.py`, `test_role_enforcement.py` | Hard denials cannot be overridden; enforcement fails closed; cancelled answers stale | Pending |
| Fleet orchestration and worker lifecycle | `test_worker_pool.py`, `test_fleet_package.py`, golden fleet fixtures | Preserve roles, budgets, routing, gates, verdicts and context semantics | Pending |
| Quota and usage | `test_core_quota.py`, `test_quota.py`, `test_usage.py` | Per-provider accounting, late usage and quota transitions | Pending |
| Resources, package sources, supported non-Python plugins | `test_resources.py`, `test_packages.py` | Preserve resource access including npm sources; capability validation | Pending |
| Stdio RPC, print/JSON, command discovery | `test_rpc.py`, `test_envelope.py` | Shared semantic event stream, paging/replay contract, output compatibility | Pending |
| Legacy live behavior or reviewed migration | `test_core_ws_e2e.py`, legacy integration contracts | Explicit feature mapping and executable compatibility fixtures | Pending |
| Linux installation and terminal behavior | v3 release gate | x86-64/ARM64 installed smoke tests; PTY, SSH, tmux and terminal restoration | ARM64 PTY tests passed; packaging, x86-64, SSH/tmux pending |
| Resource and responsiveness guarantees | `test_latency_budgets.py`, `test_leak_containment.py`, `test_soak_long_session.py` | Apples-to-apples release benchmarks and long-session bounds | Bounded preview and idle diagnostic implemented; release benchmarks pending |
| Python extension API and Python plugin execution | `test_extensions.py` | Clear unsupported error and documented migration; no Python bridge | Accepted compatibility break |

File names above are relative to `backend/tests/`. Historical bugs may be corrected
with an explicit decision and replacement fixture; copying a bug is not required.
Keeping the old source tree does not make it a runtime dependency of the Rust binary.
