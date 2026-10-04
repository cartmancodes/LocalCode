# Rust Protocol Gate Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for implementation and independent review. Track steps with checkboxes.

**Goal:** Prove Rust can supervise and control the installed official Claude/Codex binaries without Python, including concurrent approvals, cancellation, SDK MCP tools and resume, before implementing the full replacement.

**Architecture:** `octet-proc` owns framed subprocess I/O and bounded process cleanup. A cloneable transport handle writes control messages independently from reading events. `octet-engine` implements a protocol-gate binary/test driver, not the final session/TUI. No application rewrite proceeds until this evidence gate passes.

**Tech Stack:** Rust stable, Tokio, serde_json, thiserror, libc on Unix; cargo tests and native Rust CLI probes. Build/development tools may use the existing Python tree only as a read-only specification, never as a runtime dependency.

**Spec:** docs/superpowers/specs/2026-09-30-rust-harness-v3-design.md §§3–8 and delivery gate 2.

## Global Constraints

- The user explicitly requires no Python runtime or bridge and accepts extension API and plugin compatibility breaks.
- Vendor binaries own their login. Octet does not open credential stores, extract keychain entries or forward vendor tokens.
- Keep separate control and bulk-data paths.
- No application rewrite proceeds if the live protocol gate fails.
- Existing Python/web/editor code and user session data are not removed or migrated.
- Do not log full vendor initialization payloads or user configuration. Evidence uses allowlisted metadata, fixture-owned prompts and IDs.
- All real prompts operate in a fresh temporary project and use harmless fixture actions. No edits to the user's project by probe engines.

## Review Focus

1. A queued data frame cannot prevent interrupt/control replies from being sent.
2. Partial/oversized frames, child EOF and stderr floods yield bounded, explicit failure.
3. Abort during approval/MCP handling settles outstanding requests and reaps children.
4. Captured evidence is versioned and redacted; successful fake tests do not claim live proof.
5. Process-group cleanup must still execute when the leader has already exited.

### Task 1: Supervised Rust JSON-line transport

**Files:** Cargo.toml, Cargo.lock, rust-toolchain.toml, crates/octet-proc/{Cargo.toml,src/lib.rs,tests/transport.rs}, crates/octet-testkit/{Cargo.toml,src/bin/protocol-child.rs}.
**Interface:** `ProcessConfig` contains executable, argv, cwd, max_frame_bytes, queue_bytes, stderr_bytes and shutdown durations. `Process::spawn(config) -> Result<Process>` owns child and read tasks; `Process::sender() -> ProcessSender` is cloneable; `sender.send(&Value).await`; `Process::next_frame().await -> Result<Option<Value>>`; `Process::shutdown().await -> ShutdownReport` explicitly bounded. Retain an independent cancellation/kill path even when receive queues fill.

- [ ] Write transport tests with a Rust test child: echo; split JSON; oversized line; continuous stderr; saturated stdout while interrupt receives ack; leader exits leaving grandchild. Example behavior:
```rust
sender.send(&json!({"op":"flood"})).await?;
sender.send(&json!({"op":"interrupt"})).await?;
let report = timeout(Duration::from_secs(2), process.shutdown()).await??;
assert!(report.reaped);
```
- [ ] Run `cargo test -p octet-proc`, record RED. Implement framing with hard byte ceilings, independent writer/control path, error propagation and process-group shutdown; run GREEN tests.
- [ ] Test timeout/EOF propagation without detached tasks or stdout credentials leaking into logs. Preserve last stderr bytes only in bounded diagnostics and avoid printing unfiltered vendor data.

### Task 2: Real engine protocol gates

**Files:** crates/octet-engine/{Cargo.toml,src/lib.rs,src/claude.rs,src/codex.rs,src/bin/protocol-gate.rs,tests/contracts.rs}; docs/rust/protocol-gate.md.
**Consumes:** Task 1 transport. **Produces:** `cargo run -p octet-engine --bin protocol-gate -- --engine claude|codex --scenario <name> --output <json-file>` with explicit pass/fail/blocked per scenario and installed CLI version. CLI flags include binary override, workdir and timeout; default creates temporary workspace. Invalid scenario is an error.

- [ ] Build fake transcript fixtures independently from Python wire behavior; test initialize response mapping, request-id correlation, control cancellation, multiple agent messages before turn/completed, stale replies and hook/MCP traffic. Watch RED then implement minimal protocol control in Rust.
- [ ] Claude scenarios: initialize; deterministic echo; PreToolUse hook + can_use_tool approval allow/deny; concurrent interrupt while approval pending; SDK MCP initialize/list/call; resume; fork; compact. Keep handling control requests while waiting for result; reject unsupported control requests explicitly. Force harmless `printf`/read/write only in temp workspace; prompts must not inspect other paths. Record inability to provoke a model behavior as blocked, not pass.
- [ ] Codex scenarios: initialize + initialized; thread/start; simple turn; approval allow/deny using isolated workspace; concurrent turn/interrupt; requestUserInput handling if reproducible; resume/fork; compact. Use generated installed schema and existing wire fixtures to validate parameters. Require turn/completed for turn success; collect late usage.
- [ ] Run each live scenario with deadlines, capture only allowlisted outcome/event-type metadata and fixture text; never inspect credential files. Live tests explicitly require invocation, never automatic cargo test network usage.
- [ ] Record clean unsupported behavior as blocked/fail with reason and child cleanup evidence. Do not adjust the protocol to make an expectation green without recording the observed contract.

### Task 3: Verification and gate decision

**Files:** docs/rust/protocol-gate.md; docs/superpowers/plans/2026-09-30-rust-progress.md; scripts/rust-env.sh (workspace toolchain helper if needed).

- [ ] Run cargo fmt check, cargo clippy --workspace --all-targets -- -D warnings, cargo test --workspace. Build Linux ARM64 in an official Rust container; run native fake transport tests there. Keep real host CLI results separate from Linux fake-process evidence.
- [ ] Record exact versions, platform, scenarios and commands. A required scenario lacking real-CLI evidence remains blocked; a fake cannot override it.
- [ ] Independent review of transport, cleanup, auth invariant and gate assertions; fix important findings with failing regression tests first.
- [ ] If every required live contract passes, write the next foundation plan using these verified interfaces and continue. Otherwise stop the rewrite as required by v3, preserve the proof artifact and report the specific failing prerequisite.

## Preflight decisions

The current branch contains paused Python work. Rust files are new paths; no paused code is touched. A root Cargo workspace can coexist with pyproject.toml while compatibility evidence is collected. Installed CLI versions are Claude 2.1.270 and Codex 0.154.0, not the different versions quoted in the v2 proposal. Rust uses a workspace-local installation under .superpowers/rust-tools with no shell-profile modification.
