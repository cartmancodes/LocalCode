# Harness Roadmap

> Two subscriptions, one harness. Published plan:
> <https://claude.ai/code/artifact/9f73f0c3-523a-4b28-91a6-e8b57b29557c>

LocalCode already holds the one idea that makes a dual-subscription harness
legal and durable: **never hold the token, drive the vendor's own binary.**
What it does not yet have is the harness around it. This document is the
plan for building it, sequenced by dependency.

Dated 2026-09-12. Read against `claude/brave-brown-y9raqk`.

---

## 1. The invariant to protect

> A subscription-backed provider is only ever reached by *spawning the
> vendor's own official agent binary* and speaking its protocol. The harness
> never reads, forwards, proxies, or stores an OAuth token.

This is not a stylistic preference. Anthropic's February 2026 terms prohibit
subscription OAuth tokens in third-party tools, with billing enforcement live
since April 2026. Every token-forwarding bridge is on borrowed time; a harness
that spawns `claude` and `codex` and lets each read its own auth store is not.

We get this right today, but only as a README aside. **Make it a test:** fail
the build on any code path that reads `~/.claude/.credentials.json`,
`auth.json`, or assigns `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from a
credential store. That single guard is what keeps the project shippable in a
year.

---

## 2. Why harness work is the highest-leverage work

The 2026 literature converged on one finding: agent capability is a property
of the *model–harness pair*, not the model.

| Figure | What it measures |
| :--- | :--- |
| **+7.3 pp** | Terminal-Bench 2 gain from harness evolution alone (prompt + middleware + verification), model held constant |
| **5.8% → 0.5%** | Infrastructure failure rate from varying only CPU and memory — same model, same harness, same tasks, six points of score |
| **+5.1–10.1 pp** | Cross-family transfer: a frozen evolved harness lifted three unrelated model families |
| **~15×** | Token cost of a multi-agent run vs. single-agent chat (Anthropic). On a subscription this is the binding constraint |

The third figure justifies this project — harness gains generalise across
model families, so one harness genuinely can serve both subscriptions. The
fourth constrains it: the fleet currently mandates planner → coder → reviewer
on *every* turn, read-only ones included. That is a 15× multiplier applied to
trivia.

---

## 3. The structural shift

When this was written the two backends were asymmetric: Claude went through a
first-party SDK, ChatGPT through the third-party OpenCode HTTP/SSE server. That
asymmetry is why approvals, interrupts, sandboxing and `additional_dirs` worked
on one side and not the other — and, once Codex closed the gap, why the
OpenCode provider was retired rather than kept alongside.

Since March 2026 the Codex CLI ships an **app-server** — a JSON-RPC 2.0 agent
runtime with threads, turns, streaming items and bidirectional approval
callbacks. It is a structural mirror of `claude-agent-sdk`. Adopting it makes
the two sides symmetric, which is what lets one set of harness features apply
to both.

```text
BEFORE                               TARGET
──────                               ──────
FastAPI + WS                         Control plane
 (9-type Event union)                 (ACP-shaped bus, artifacts, quota)
   ├─ claude   one-shot query()        ├─ ClaudeSDKClient   persistent
   │           fresh CLI per turn      │                    can_use_tool
   └─ opencode HTTP+SSE, client-       └─ codex app-server  JSON-RPC broker
               side global filter                           execCommandApproval
   ↓ fleet steps                       ↓ fleet steps
 subproc per dispatch                 long-lived clients
 prose-enforced tool limits           disallowed_tools / sandbox policy
```

---

## 4. What's missing, layer by layer

| Layer | Today | What it costs | Fix |
| :--- | :--- | :--- | :--- |
| Claude turn | `query()` spawns a fresh `claude` CLI per turn (`orchestrator/claude.py`) | Zero prompt-cache reuse; no interrupt; no hooks; cold start every message | Hold a `ClaudeSDKClient` per session |
| Permissions | Unknown mode silently becomes `acceptEdits` (`claude.py:55-60`) | Privilege escalation on every fleet step, by default, to dodge a hang | `can_use_tool` answering into the existing approval channel |
| ChatGPT path | `opencode serve` + client-side filter of `/global/event` (the since-deleted `orchestrator/opencode.py`) | Every project's events cross the wire; breaks on opencode releases; no approvals, no `add_dirs` | `codex app-server` over JSON-RPC |
| Fleet step | `python -m …fleet.subproc` per dispatch | Interpreter + SDK import + CLI spawn per step — the exact infra overhead worth six benchmark points | Long-lived clients; the SDK deadlock it works around disappears |
| Tool limits | Bans written in prose in `ORCHESTRATOR_SYSTEM` | Prose is not enforcement — one confident model ignores all of it | `disallowed_tools` / tool preset / read-only sandbox |
| Subagent return | `collect_text` returns the whole transcript | Context runaway in the orchestrator; compaction fires early and often | `{summary, structured, artifact_refs}` envelope |
| Gate verdicts | Parse `LGTM` / `NACK:` from the last line (`fleet/gate.py`) | A chatty reviewer breaks the gate and the workflow ships unreviewed | Structured output schema — both vendors support it |
| Budget | Per-turn `cost_usd`, which the README admits is meaningless | No view of the thing that actually runs out: rolling-window quota | Quota governor + route by remaining headroom |
| Evaluation | `testpaths = ["backend/tests"]` — the directory does not exist | Every harness change is a guess; no regression signal at all | Three-layer harness eval + golden traces |
| Blast radius | `allowed_cwd_roots` empty by default; `bypassPermissions` accepted | Arbitrary working directory plus arbitrary execution, in one request | Default-deny roots; bypass behind an explicit env flag |

---

## 5. The plan

Seven phases, sequenced by dependency rather than size. **Phase 1 unblocks the
most** — it is what makes Phases 2–4 apply to both subscriptions instead of
only to Claude. Durations assume one person.

### Phase 0 — Codify the invariant, stop the bleeding · ~1 week

Cheap, unblocks nothing, but every later phase is wasted if the auth model
breaks or a fleet step wipes a directory.

- Architecture test that fails on any credential-store read or `*_API_KEY`
  assignment from a token file.
- Bump `claude-agent-sdk` from `>=0.0.10` to `>=0.2.152` with an upper bound.
  Two years of harness surface sits behind that pin.
- Replace the forced `acceptEdits` fallback with a real `can_use_tool`
  callback routed into the approval channel the WebSocket layer already
  carries (`RunContext.approval_channel`).
- Ship a default `allowed_cwd_roots`; put `bypassPermissions` behind an
  explicit opt-in.

**Done when** a fleet step cannot edit a file outside the session root without
a visible approval card, and CI rejects a token-forwarding patch.

### Phase 1 — Make Codex a first-class peer · ~2 weeks · **unlock**

The ChatGPT side is currently a second-class citizen routed through a
third-party server. The official app-server closes the gap and makes every
later harness feature apply to both vendors at once.

- New `CodexProvider` speaking `codex app-server` JSON-RPC over stdio,
  mirroring `ClaudeProvider`'s shape exactly.
- Broker pattern, as OpenAI's own reference client uses: one long-lived
  app-server per workspace behind a Unix socket, so cold start is paid once
  rather than per dispatch.
- Map `thread/start|resume|fork` to session lifecycle, `turn/start|steer|interrupt`
  to turn control, `item/*` notifications to the event stream.
- **One approval bus.** Route `execCommandApproval` and `applyPatchApproval`
  into the same channel as Claude's `can_use_tool`. This is the harness's
  central value proposition — the user learns one approval UI, not two.
- Pin the CLI and regenerate types in CI via
  `codex app-server generate-json-schema`; the schema drifts per release.
- Demote OpenCode from the ChatGPT path. It was to stay on as an optional
  third provider for local and non-frontier models; in the event it was
  deleted instead. Once Codex was a peer, nothing routed through it, and an
  HTTP provider nobody selects is a maintenance surface with no user.

**Done when** a fleet run mixes a Claude planner and a Codex coder, and both
raise approval cards through the same UI with the same semantics.

### Phase 2 — Persistent sessions and cache discipline · ~2 weeks

Prompt cache is the difference between a snappy harness and a slow one, and on
a subscription it is also the difference between hitting the window at noon or
at six.

- Swap one-shot `query()` for a per-session `ClaudeSDKClient`, mirroring the
  Codex broker. Gains `interrupt()`, `set_model()` and `set_permission_mode()`
  mid-session for free.
- Freeze the prompt prefix: static system prompt → project memory → session
  state → recent turns → user input. Never add or remove tools mid-session;
  never rewrite the system prompt for dynamic state.
- Artifact store with content-addressed handles. Tool results over a threshold
  are evicted to disk and return a summary plus a reference.
- Instrument cache hit rate and uncached token share.

**Done when** the second turn of a session reports a cache hit, and a 2 MB
test output no longer lands in context.

### Phase 3 — Rebuild the fleet on real enforcement · ~2 weeks

The fleet is the most ambitious part of LocalCode and the least defended. Its
guarantees are currently requests written in prose, and its cost model assumes
tokens are free.

- Delete the per-step subprocess. It exists to dodge an SDK re-entrancy
  deadlock that persistent out-of-process clients no longer have.
- Enforce role limits at the tool layer: orchestrator gets dispatch tools
  only, reviewer gets a read-only sandbox, coder gets scoped write.
- Make planner → coder → reviewer **conditional**. A gate classifier already
  exists in `fleet/gate.py`; use it to route trivial and read-only turns to a
  single agent and reserve the 15× multiplier for work that earns it.
- Subagents return structured envelopes, not transcripts. Verdicts come back
  as a JSON schema, not a string match on the last line.
- Cap concurrent workers and per-turn token budget; no recursive spawning.

**Done when** "how many files are in this repo?" costs one agent call, and a
reviewer that tries to write a file is refused by the runtime rather than by a
paragraph.

### Phase 4 — Quota governor and routing · ~1 week

Two subscriptions are two independent, separately-exhaustible budgets on
different clocks. A harness that spends both should know what is left in each.

- Track consumption per provider against its real window — Codex runs a
  rolling five-hour window shared between local and cloud work, with weekly
  caps on top; Claude has its own.
- Route by headroom and by task class: bulk edits to whichever subscription
  has room, frontier reasoning only when the gate asks for it.
- Replace the per-turn dollar figure with a quota meter. Under OAuth the
  dollar number is decoration; remaining window is the number that changes a
  decision.
- Degrade honestly: when both are near the cap, say so and offer to queue
  rather than failing mid-plan.

**Done when** the UI shows remaining headroom per subscription, and a long
fleet run automatically prefers the one with room.

### Phase 5 — Harness evaluation · ~2 weeks

This is the gap that makes every other gap permanent. There are no tests in
the repository. **Start this in parallel with Phase 1** rather than waiting.

- **Unit:** tools, permission policy, path validation, event translation for
  both providers.
- **Replay:** fixed prompts against recorded provider streams, asserting
  expected artifacts and state transitions.
- **Long-horizon:** resume after interrupt, compaction correctness, approval
  branching, subagent handoff, wedged-backend fast-fail.
- **Golden traces:** ordered tool calls, stable argument hashes, approval
  metadata, side-effect diffs. This is what catches a narrow lookup quietly
  becoming a broad one.
- Run the suite across the full matrix — `{claude, codex} × {single, fleet}`.
  That matrix *is* the product.

**Done when** a harness change that regresses approval routing fails CI
instead of reaching a user.

### Phase 6 — Speak ACP · ~1 week · optional

The Agent Client Protocol now has 25+ agents, native JetBrains and Zed
support, and a live registry. Shaping the event bus to match costs almost
nothing today and saves a rewrite later.

- Reshape the internal `Event` union to ACP semantics now — the expensive part
  is the reshape, not the transport.
- Ship an ACP server later and get JetBrains, Zed and Neovim for free,
  retiring the bespoke VS Code webview.
- Keep MCP for tools, ACP for the editor boundary, A2A only if remote
  delegation ever becomes real. They solve different problems; conflating them
  is a named anti-pattern.

**Done when** LocalCode appears in the ACP registry and the VS Code extension
is a thin client rather than a webview.

---

## 6. Risks worth planning around

- **Schema drift.** The Codex app-server is explicitly experimental and its
  JSON shapes change per CLI release. Pin the CLI version and regenerate types
  in CI — treat an unpinned Codex as an unpinned dependency, because it is one.
- **Terms of service.** Anthropic's restriction is the binding one and it is
  already enforced. The official-binary invariant is not optional and should
  never be relaxed "just for testing".
- **Busy backpressure.** The app-server answers `-32001` when overloaded, and
  a turn without a final message returns no response body. Handle both —
  backoff on the first, inspect items on the second, rather than reporting a
  silent success.
- **Quota, not cost.** Both subscriptions meter in rolling windows, and model
  choice can swing effective message count by an order of magnitude. Design
  the governor around windows, not a dollar estimate that never bills anyone.
- **Multi-agent gravity.** Fleet mode is the most interesting part of the
  codebase and the easiest to over-apply. Every mandatory role is a 15× tax;
  keep the conditional gate honest even when the demo looks better with all
  four agents lit up.

---

## 7. If you only have one week

1. **Bump the SDK.** `claude-agent-sdk 0.0.10 → 0.2.152`. Nothing else here is
   reachable from behind that pin.
2. **Swap `query()` for `ClaudeSDKClient`.** One file, immediate latency and
   cache win, and it unlocks interrupt and mid-session model switching.
3. **Wire `can_use_tool` into the approval channel.** Removes the forced
   `acceptEdits` escalation, and it is the same shape Codex approvals will
   need in Phase 1.
4. **Create `backend/tests/` and write ten.** Event translation for both
   providers, path validation, gate classification. The directory is already
   in `pyproject.toml`; it just doesn't exist.
5. **Spike the Codex app-server.** Fifty lines of stdio JSON-RPC against
   `thread/start` + `turn/start` tells you whether Phase 1 is two weeks or
   four.

---

## Sources

- [Claude Agent SDK — Python reference](https://code.claude.com/docs/en/agent-sdk/python)
  and [hooks documentation](https://platform.claude.com/docs/en/agent-sdk/hooks)
- [Building on codex app-server: JSON-RPC transports, methods, hooks, subagents, skills, MCP](https://gist.github.com/oneryalcin/ee2c27e2d8aa040da8fbe7eebcc2ecea)
- [Codex headless execution mode](https://deepwiki.com/openai/codex/4.2-headless-execution-mode-(codex-exec))
  · [app-server provider notes](https://www.promptfoo.dev/docs/providers/openai-codex-app-server/)
- [Modern Agent Harness Blueprint 2026](https://gist.github.com/amazingvince/52158d00fb8b3ba1b8476bc62bb562e3)
  · [awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering)
- [Harness-Bench: Measuring Harness Effects across Models](https://arxiv.org/html/2605.27922v1)
  · [Stop Comparing LLM Agents Without Disclosing the Harness](https://arxiv.org/pdf/2605.23950)
- [Agent Client Protocol (ACP)](https://www.morphllm.com/agent-client-protocol)
  · [OpenCode vs Codex CLI, September 2026](https://www.morphllm.com/comparisons/opencode-vs-codex)
- [Codex usage limits and the rolling five-hour window](https://explainx.ai/blog/codex-usage-limits-sub2api-sign-in-chatgpt-august-2026)
  · [Agent Harness Engineering](https://addyosmani.com/blog/agent-harness-engineering/)
