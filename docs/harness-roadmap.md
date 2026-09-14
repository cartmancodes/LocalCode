# Harness Roadmap

> Pi's shape, official engines. Published plan:
> <https://claude.ai/code/artifact/9f73f0c3-523a-4b28-91a6-e8b57b29557c>

The target architecture is **pi.dev's** — a minimal core, sessions as a
JSONL tree, one extension API as the only growth path, four run modes, and
packages — with one deliberate substitution: the agent loop does not live in
LocalCode. It lives in each vendor's official binary. That substitution is
the difference between a harness that runs on a Claude subscription and one
that cannot.

Dated 2026-09-13. Implementation lives under `backend/app/core/` (see
`docs/core.md` once written).

---

## 1. The invariant to protect

> A subscription-backed provider is only ever reached by *spawning the
> vendor's own official agent binary* and speaking its protocol. The shell
> never reads, forwards, proxies, or stores an OAuth token.

Pi is the proof of why this matters. Pi owns its own agent loop and calls the
Anthropic API directly with the user's OAuth token — the clean design, and
the one Anthropic shut off. Pi's own provider docs now read: *"Third-party
harness usage draws from extra usage and is billed per token, not against
Claude plan limits."* Issue #3372 on the pi repo is a user discovering that
their Pro plan no longer covers pi. The only route back is the
`pi-sub-anthropic` extension, which reproduces Claude Code's wire fingerprint
byte-for-byte — user-agent, beta headers, a billing attestation hash — and
whose README says using it *"may get your Anthropic account restricted or
banned."*

LocalCode has never had this problem, because it spawns `claude` and lets the
CLI read its own auth. We keep that, and we make it a test: fail the build on
any code path that reads `~/.claude/.credentials.json`, `auth.json`, or
assigns `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from a credential store.

On the OpenAI side pi's ChatGPT-subscription login works and OpenAI has not
restricted it; the "Codex for OSS" endorsement pi cites is a maintainer grant
programme, not a terms guarantee. The `codex app-server` — part of the
official CLI — is the path that needs no tolerance.

---

## 2. Why harness work is the highest-leverage work

| Figure | What it measures |
| :--- | :--- |
| **+7.3 pp** | Terminal-Bench 2 gain from harness evolution alone (prompt + middleware + verification), model held constant |
| **5.8% → 0.5%** | Infrastructure failure rate from varying only CPU and memory — same model, same harness, same tasks; six points of score |
| **+5.1–10.1 pp** | A frozen evolved harness lifted three unrelated model families — harness gains transfer |
| **~15×** | Token cost of a multi-agent run vs. single-agent chat (Anthropic). On a subscription this is the binding constraint |

The third figure justifies one harness for two subscriptions. The fourth
constrains it: the fleet currently mandates planner → coder → reviewer on
*every* turn, read-only ones included.

---

## 3. Pi's shape — what we copy

| Element | Pi | Why it matters here |
| :--- | :--- | :--- |
| Minimal core | Four tools (`read`, `write`, `edit`, `bash`), a tiny system prompt, everything else via extensions | Our core is a *shell*; the engines bring their own tools. The core should own sessions, protocol, extensions, routing — nothing else |
| Sessions as a tree | JSONL; first line a header, every entry has `id` + `parentId`; branch in place, `/fork`, `/tree`; compaction is an entry, full history stays on disk | We already write JSONL per session. Adopting pi's entry schema is cheap and both engines fork natively |
| One extension API | `pi.on(event)`, `registerTool`, `registerCommand`, `registerProvider`; hooks like `tool_call` (block/modify), `tool_result`, `before_agent_start`, `session_*`, `input`, `model_select` | Both engines expose hooks with the same names (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`), so a pi-style hook can compile down to *both* |
| Four run modes | interactive, `-p` print, `--mode json`, `--mode rpc` (LF-delimited JSONL on stdio) | Our WebSocket is one transport of what should be one protocol. RPC on stdio gets us editors and CI for free |
| Packages | `package.json` `pi` key → `extensions/`, `skills/`, `prompts/`, `themes/`; `pi install npm:\|git:\|path` | The fleet, the approval UI, and a quota meter are packages, not core |
| Deliberate omissions | No MCP, no subagents, no permission popups, no plan mode, no background bash in core: *"Run in a container, or build your own confirmation flow with extensions"* | Isolation comes from the OS boundary (Docker), not from the shell. Approvals are an extension over a bus, not core magic |

---

## 4. Pi's engine — what we cannot copy

Pi's `pi-ai` layer speaks raw LLM APIs and pi runs the tools. Claude Code's
SDK speaks *agent*: prompt in, tool-executing agent out. You cannot nest one
loop inside the other without one becoming a dumb pipe — and on a Claude
subscription, the loop **must** be the `claude` binary's.

So the shell's job is everything *around* the loop, on both engines:

```text
                 pi-shaped shell (LocalCode)
   sessions tree · RPC protocol · extensions · packages · routing · quota
                          │ one Engine protocol
             ┌────────────┴─────────────┐
     ClaudeSDKClient               codex app-server
     (official `claude` CLI)       (official `codex` CLI)
     hooks · can_use_tool          hooks · exec/patch approvals
     fork_session · compaction     thread/fork · compact
```

### The fork in the road

**Option A — pi-shaped shell in Python (chosen).** Keep the stack.
Rebuild the core to pi's structure; engines are plugins. Fits the team's
Python/React/Docker expertise and the existing FastAPI service. Cost: we
implement pi's surfaces ourselves.

**Option B — LocalCode as a pi package (TypeScript).** Run pi as the host.
Pi supplies TUI, RPC, sessions, extensions, packages and a working ChatGPT
login; we ship a package with a `claude-code` engine extension (the
TypeScript Agent SDK has the richer hook set — `SessionStart`, `SessionEnd`,
`PostToolBatch`, `PostCompact` are TS-only), the fleet as an extension, and
the React UI over pi's RPC. Cost: a rewrite, and Claude-primary turns bypass
pi's loop anyway, so pi becomes a pass-through shell for exactly the
subscription users care most about.

---

## 5. The mapping

Pi concept → LocalCode target → how each engine provides it.

| Pi | LocalCode target | Claude engine | Codex engine |
| :--- | :--- | :--- | :--- |
| Session file: header + `id`/`parentId` entries | Same schema in `core/session_manager.py`; engine session ids stored as `custom` entries | transcript under `~/.claude/projects/`; `resume=` | rollout under `~/.codex/sessions/`; `thread/resume` |
| `/fork`, `/tree`, in-place branching | `fork` / `get_tree` / navigate RPC over the shell tree | `fork_session=True`, `resume_session_at` | `thread/fork`, `thread/rollback` |
| `compaction` entry (`summary`, `firstKeptEntryId`, `tokensBefore`) | Shell records the entry when the engine compacts | automatic; `PreCompact` hook | `thread/compact/start`; `thread/compacted` |
| `tool_call` hook — block or modify input | Extension hook, compiled to engine hooks | `PreToolUse` (deny / `updatedInput`) + `can_use_tool` | `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` (decline; no input rewrite) |
| `tool_result` hook | same | `PostToolUse`, `PostToolUseFailure` | `item/completed` (observe only) |
| `before_agent_start` — inject message, edit system prompt | same | `UserPromptSubmit` + `system_prompt` preset/append | `developerInstructions` / `baseInstructions` on `thread/start` |
| `session_start` / `session_shutdown` | same | `SessionStart`/`SessionEnd` are TS-only callbacks; Python gets them as settings-file shell hooks | `SessionStart` hook |
| `agent_end` / `agent_settled` | same | `Stop` | `turn/completed` |
| `context` — rewrite messages before every LLM call | **Not offered.** Engines own their context | nearest: `PreCompact` | none |
| `model_select`, thinking level | `set_model` / `set_thinking_level` RPC | `set_model()`, `effort`, `thinking` | per-turn `model`, `effort` |
| RPC `prompt`, `follow_up` | same | streaming-input queue (native) | `turn/start` |
| RPC `steer` (inject after current tool, before next LLM call) | same, best-effort on Claude | `interrupt()` + re-prompt (approximation) | `turn/steer` (native) |
| RPC `abort` | same | `interrupt()` | `turn/interrupt` |
| Skills (Agent Skills standard, `SKILL.md`) | one `skills/` dir per package, registered into each engine | `skills=` option, `.claude/skills` | `.agents/skills`, `skill` input |
| Context files (`AGENTS.md`) | `AGENTS.md` canonical; `CLAUDE.md` contains `@AGENTS.md` | `@` import | native |
| Extension UI (`ctx.ui.confirm/select`) → `extension_ui_request` | same frames on WS and stdio; approval cards are one instance | `PermissionRequest` / `can_use_tool` | approval requests |
| Packages (`pi` manifest key) | `localcode` manifest key; install materialises engine-side hooks and config | `settings.json` hooks, `plugins=`, `.mcp.json` | `hooks.json`, `config.toml` |
| No subagents in core | fleet → package | `agents=` if a package wants it | `.codex/agents/*.toml` if a package wants it |
| No permission popups; isolate at the OS | approval bus is an extension; Docker is the boundary | `permission_mode`, `sandbox` | `sandbox` read-only / workspace-write |
| Quota | rolling-window governor | `RateLimitEvent` from the SDK | `account/rateLimits/read`, `account/rateLimits/updated` |

The one honest gap is `context`. Everything else maps.

---

## 6. What's missing, layer by layer

| Layer | Today | What it costs | Fix (pi vocabulary) |
| :--- | :--- | :--- | :--- |
| Claude turn | `query()` spawns a fresh `claude` CLI per turn (`orchestrator/claude.py`) | Zero prompt-cache reuse; no interrupt; no hooks | Persistent `ClaudeSDKClient` engine |
| Permissions | Unknown mode silently becomes `acceptEdits` | Privilege escalation on every fleet step, to dodge a hang | `tool_call` hook → `can_use_tool` → `extension_ui_request` |
| ChatGPT path | `opencode serve` + client-side filter of `/global/event` | Every project's events cross the wire; breaks on opencode releases; no approvals | `codex app-server` engine |
| Sessions | Flat JSONL, no `parentId`, no fork, no compaction record | Cannot branch; engine compaction is invisible | Pi's entry schema (`core/session_manager.py`) |
| Protocol | 9-type custom WS union; WS only | UI-specific; no stdio, no editors, no CI | Pi's RPC commands + events on WS *and* stdio |
| Extensibility | None | Every workflow change is a core change | `on` / `register_tool` / `register_command`; packages |
| Fleet step | `python -m …fleet.subproc` per dispatch | Interpreter + SDK import + CLI spawn per step | Long-lived engines; fleet becomes a package |
| Tool limits | Bans written in prose in `ORCHESTRATOR_SYSTEM` | Prose is not enforcement | `disallowed_tools` / `sandbox` at the engine |
| Subagent return | `collect_text` returns the whole transcript | Context runaway in the orchestrator | `{summary, structured, artifact_refs}` |
| Gate verdicts | `fleet/gate.py` string-matches `LGTM` / `NACK:` | A chatty reviewer breaks the gate | Structured output schema — both engines support it |
| Task routing | No complexity classifier exists | 15× multiplier on trivia | Build one, inside the fleet package |
| Budget | Per-turn `cost_usd` | No view of rolling-window quota | Quota governor + route by headroom |
| Evaluation | `testpaths = ["backend/tests"]` — did not exist | Every harness change is a guess | Three-layer harness eval + golden traces |
| Blast radius | `allowed_cwd_roots` empty by default; `bypassPermissions` accepted | Arbitrary directory plus arbitrary execution | Default-deny roots; bypass behind an env flag |

---

## 7. The plan

- **Phase 0** — invariant test, SDK bump to ≥0.2.152, `can_use_tool`, default-deny roots.
- **Phase 1** — sessions as a tree (`core/session_manager.py`, pi v3).
- **Phase 2** — two official engines behind one `Engine` protocol; one approval bus.
- **Phase 3** — pi's RPC vocabulary on WebSocket, stdio `--mode rpc`, and `--mode json` / `-p`.
- **Phase 4** — extensions, packages, skills; compile-down layer to engine hooks.
- **Phase 5** — fleet becomes a package; structured verdicts; a real complexity gate.
- **Phase 6** — quota governor on rolling windows.
- **Phase 7** — harness evaluation (start with Phase 1).
- **Phase 8** — ACP adapter (optional).

Exit criteria per phase are in the published artifact.

---

## 8. Risks

- **Parity is a shape, not a promise.** The `context` hook cannot exist over an engine-owned loop; say so in the extension docs.
- **Hook-surface asymmetry.** The Python Agent SDK exposes a subset of the TypeScript hooks; the compile-down layer falls back to settings-file shell hooks and is versioned per engine release.
- **Schema drift.** The Codex app-server is experimental; pin the CLI and regenerate types with `codex app-server generate-json-schema`.
- **Terms of service.** Anthropic's restriction is enforced server-side; the official-binary invariant is never relaxed.
- **Busy backpressure.** `-32001` means back off; a turn with no final message returns no body.
- **Quota, not cost.** Both subscriptions meter in rolling windows.
- **Multi-agent gravity.** Every mandatory role is a 15× tax.

---

## Sources

- Pi: [README](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md) · [extensions.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md) · [rpc.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) · [session-manager.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/session-manager.ts) · [compaction.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md) · [packages.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md) · [sdk.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md) · [providers.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)
- Pi and Claude subscriptions: [issue #3372](https://github.com/earendil-works/pi/issues/3372) · [pi-sub-anthropic](https://github.com/spksoft/pi-sub-anthropic) · [Anthropic bans subscription auth for third-party use](https://alternativeto.net/news/2026/2/anthropic-officially-bans-using-subscription-authentication-for-third-party-claude-use)
- OpenAI: [Codex for Open Source](https://developers.openai.com/community/codex-for-oss) · [Building on codex app-server](https://gist.github.com/oneryalcin/ee2c27e2d8aa040da8fbe7eebcc2ecea)
- Claude Agent SDK: [Python reference](https://code.claude.com/docs/en/agent-sdk/python) · [hooks](https://code.claude.com/docs/en/agent-sdk/hooks) · [streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode)
- Harness research: [Modern Agent Harness Blueprint 2026](https://gist.github.com/amazingvince/52158d00fb8b3ba1b8476bc62bb562e3) · [Harness-Bench](https://arxiv.org/html/2605.27922v1) · [Stop Comparing LLM Agents Without Disclosing the Harness](https://arxiv.org/pdf/2605.23950)
