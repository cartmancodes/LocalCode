# LocalCode in the terminal

The terminal application uses the same Python engines, extensions and provider
processes as LocalCode. It does not start a web server. Vendor CLI installation
and vendor login are still required for real conversations; `--engine fake`
provides a local smoke test without a vendor or network call.

```sh
localcode --engine claude --cwd /path/to/project
localcode --engine codex --session continue
localcode --engine fake --no-extensions --no-packages
```

Running `localcode` in an interactive terminal opens the TUI. Piped invocations
retain stdio RPC behavior. Explicit print/JSON/RPC modes remain available:

```sh
localcode -p 'Explain this project'
localcode --mode json 'Explain this project'
localcode --mode rpc
```

## Core and legacy sessions

LocalCode currently has two session formats and two orchestration implementations.
The TUI keeps them separate. New core sessions use AgentSession and its session
tree. Legacy sessions retain their original session runner, upstream identity,
permission mode, additional directories and fleet configuration. Opening legacy
history does not import it into a new core conversation or erase its metadata.

The core fleet extension and the legacy fleet provider are different workflows.
Use the legacy path when you need the exact existing fleet-provider behavior.
Provider limitations documented in [core.md](core.md) still apply: tree navigation
is not a native rewind for every provider, and experimental Codex dynamic tools
are not made universally available by the terminal UI.

## Complete protocol access

`/rpc` accepts a JSON command and routes it directly to the runtime, without
HTTP or WebSockets. This is useful for advanced commands and structured payloads:

```text
/rpc {"type":"get_state"}
/rpc {"type":"get_commands"}
/rpc {"type":"get_available_models"}
/rpc {"type":"get_entries"}
/rpc {"type":"get_tree"}
/rpc {"type":"get_session_stats"}
/rpc {"type":"get_quota"}
/rpc {"type":"set_steering_mode","mode":"all"}
/rpc {"type":"set_follow_up_mode","mode":"one-at-a-time"}
/rpc {"type":"clear_queue"}
/rpc {"type":"set_auto_compaction","enabled":true}
/rpc {"type":"bash","command":"git status --short","excludeFromContext":true}
/rpc {"type":"abort_bash"}
```

Existing core extension commands, `/skill:name` and prompt-template commands are
passed to AgentSession. Project extensions/packages remain subject to the existing
`--trust-project` setting; explicit `-e` extension paths preserve their current
meaning. Image payloads can be supplied using the core `prompt` command's `images`
field; display support depends on the terminal and is not required to send them.

## Parity verification map

This map identifies the existing behavioral contracts that must remain intact.
Passing a provider fake test does not certify a real vendor login or upstream API.

| Capability | Existing implementation | Behavioral tests |
| --- | --- | --- |
| Streaming, tools, model and thinking changes | core/agent_session.py, core/engines | test_agent_session.py, test_engines_fake.py, test_engine_claude.py, test_engine_codex.py |
| Resume, tree, fork and compaction | core/session_manager.py, core/agent_session.py | test_session_manager.py, test_agent_session.py |
| Queues, steering and follow-ups | core/agent_session.py | test_agent_session.py, test_rpc.py |
| Extension dialogs, commands, custom tools | core/extensions, core/rpc | test_extensions.py, test_rpc.py |
| Skills, prompts and context discovery | core/resources | test_resources.py |
| Package discovery/install/removal | core/packages.py | test_packages.py |
| Core fleet roles, gates and dispatch | packages/fleet | test_fleet_package.py, test_gate_verdict.py |
| Legacy fleet roles, workers and budgets | orchestrator/fleet | test_worker_pool.py, test_role_enforcement.py, test_matrix.py |
| Legacy persistence, replay and detached turns | storage, session_runner | test_storage_offload.py, test_event_integrity.py, test_lifecycle.py |
| Quota and usage | core/quota.py, quota.py, usage.py | test_core_quota.py, test_quota.py, test_usage.py |
| Artifact retention | artifacts.py | test_artifacts.py |
| Credential ownership and permissions | invariants.py, core/engines, orchestrator | test_auth_invariant.py, test_invariants.py, test_permissions.py |

Terminal-specific tests cover the additional presentation, command routing and
lifecycle boundary; existing tests alone do not prove terminal feature parity.

## Performance and measurement

The terminal loop is separate from the runtime loop. Core session writes and
extension callbacks keep their existing ordering on the runtime thread. This
protects terminal input from blocking filesystem calls, but does not make an
arbitrary blocking extension cancellable. Provider shutdown latency and core
cancellation dispatch must be measured separately from key feedback.

Transcript display is bounded and updates are coalesced while streaming. Full
output remains accessible through history/export rather than being silently lost
when the visible window fills. Rendering bounds do not imply a bound on the core's
session tree, vendor context, or vendor child-process memory.

Release targets are p95 launch-to-input <=1 second, input feedback <=50 ms,
received-event-to-display <=100 ms, cancel-to-dispatch <=100 ms, and idle UI CPU
<=1% of one core. These require measurement on the documented Linux reference
host, including architecture, terminal, dependency versions and cache conditions.
They are not universal hardware guarantees. Source/headless measurements must be
reported separately from installed Linux and real-terminal measurements.
