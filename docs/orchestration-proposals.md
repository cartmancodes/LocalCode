# Orchestrating Claude Code + OpenCode in a Single Session

Design exploration for letting one chat session use both backends, picking the right one per turn based on user preference, task type, and budget.

> **Status as of 2026-05-10:** **Proposal G's linear-pipeline implementation has been superseded by the orchestrator-as-agent rewrite** (May 2026). See [docs/architecture.md](architecture.md) for the current architecture and [docs/superpowers/plans/2026-05-10-orchestrator-architecture.md](superpowers/plans/2026-05-10-orchestrator-architecture.md) for the rewrite plan.
>
> The shipped fleet now uses an LLM-driven orchestrator with a custom MCP `dispatch_subagent` tool — structurally identical to Claude Code's main-session-with-Task pattern. The Proposal G entry below is preserved for design rationale.
>
> Proposals A–F remain unshipped; the orchestrator-as-agent rewrite incidentally absorbs several of their goals (dynamic dispatch, parallelism-ready, registry-driven workflows). The provider-agnostic dispatch — claude- and opencode-backed subagents in a single workflow — is the architectural unlock that wasn't on any of the original proposals.

## Cross-cutting constraints

These shape every proposal — easy-sounding ideas have hidden costs because of them.

1. **Session state is sticky to one backend.** Claude Code holds tool / edit state in its CLI subprocess; OpenCode holds it in its `opencode serve` session. Switching mid-thread means either replaying history into the new agent (slow, drops tool state) or accepting amnesia.
2. **Tool inventories differ.** Claude's `Read / Edit / Bash` / MCP catalog ≠ OpenCode's. A turn-N edit by Claude isn't visible to turn-N+1 OpenCode unless mediated through the filesystem (mostly fine for code, but loses things like in-memory snippets, search results, todo lists).
3. **Budget signals are noisy under OAuth.** With subscription auth, real $ cost is hidden. We have token counts (both providers report them) and a self-imposed quota — that's it. No LiteLLM-level metering unless we put API keys back in.
4. **Latency penalties compound.** Anything that adds an extra LLM hop (classifier, planner) charges 200–1500ms before the user sees the first token.

---

## Proposal A — Sticky per-turn picker with overrides ("MVP")

Elevate the existing model picker from session-creation-only to per-turn. Default to whatever the last turn used. Add a `/use claude:sonnet-4-6` slash command in the composer. Persist a project-level preference at `.localcode/prefer.json`. Show running token counts in the header.

**Pros**

- Hours of work, all UI. No new orchestration layer.
- Honest: doesn't pretend to make decisions it can't make well.
- Power-user friendly; gives the human full agency.
- Becomes the substrate every other proposal builds on.

**Cons**

- Zero automation — relies on the user noticing they should switch.
- No protection from rate-limits or budget overruns.
- Doesn't feel "smart" in a demo.

---

## Proposal B — Declarative routing policy (`rules.yaml`)

Add a "Smart" pseudo-model to the picker. When chosen, every turn evaluates a policy file. Rules match on cheap signals (prompt regex, file types in `cwd`, token-count estimate, daily-token-budget-remaining) and pick `{provider, model}`. State hand-off is replay-from-DB (we already persist every message).

```yaml
default: { provider: claude, model: claude-haiku-4-5 }
rules:
  - when: { prompt_matches: "(?i)\\b(test|spec|lint)\\b" }
    use:  { provider: opencode, model: openai/gpt-5.4-mini }
  - when: { estimated_input_tokens_gt: 8000 }
    use:  { provider: claude, model: claude-sonnet-4-6 }
  - when: { daily_tokens_remaining_pct_lt: 20 }
    use:  { provider: opencode, model: opencode/big-pickle }   # free tier
budget: { daily_token_cap: 1_000_000, hard_stop: true }
```

**Pros**

- Declarative, version-controllable, debuggable in isolation.
- Familiar pattern (nginx routes, ESLint configs).
- Composes with A: rule-mode is opt-in.
- Replay-from-DB hand-off is robust because we already store messages.

**Cons**

- Stateless rules misfire on follow-ups (*"write tests"* → cheap model; *"now fix the failing one"* → policy doesn't know it's a follow-up, may switch backends).
- Rule writers must anticipate all signals; long tail of "why did it pick that?" debugging.
- Replay loses tool state (open files, sub-shell history) every time we switch.
- Token estimates pre-call are coarse.

---

## Proposal C — Budget-tiered fallback chain

Each model is tagged with a tier. The session declares a tier order; the router picks the highest tier the budget allows, and on `429` / error / budget-exhaustion it cascades down to the next tier — transparently, without ending the turn. `RateLimitEvent` (the SDK already emits these) and `session.error` (OpenCode) are the triggers.

```text
tier_order: [opus, sonnet, haiku, gpt-5.4-mini, opencode/big-pickle]
on_429:    cooldown=30s on offending tier, fallthrough
on_error:  fallthrough
budget:    { daily_tokens: 200_000, hard_stop_on_breach: true }
```

**Pros**

- Resilience is the killer feature: rate-limit storms don't break the chat.
- Natural fit for Claude Pro / Max bursty subscription windows.
- Implementable on top of the unified `Event` stream — provider swaps are invisible to the UI.
- Pairs well with B (B picks the *initial* tier; C handles failure).

**Cons**

- Mid-stream fallback is awkward: partial output exists, and the new agent has zero context of what was already streamed. Pragmatic fix is re-prompting with a *"previously you said: …"* prefix; users will still notice the seam.
- Tiering is one-dimensional — doesn't capture "Codex is good at this specific thing."
- Without LiteLLM, the budget meter is approximate.

---

## Proposal D — Mid-turn handoff via a `delegate` tool

The primary agent (say, Claude) gets an extra tool: `delegate(provider, model, subprompt)`. When Claude decides *"this is a 200-file refactor, that's better as Codex,"* it calls the tool. We spawn the secondary agent for that subtask, stream its output back as a `tool_result`, and Claude continues. The UI renders it as a tool-use card with a labeled inner stream.

**Pros**

- Agent-driven, not rule-driven — uses the model's own judgement.
- Clean state model: subtask is bounded; primary keeps the conversational thread.
- Already maps to our existing tool-use UI.
- Cost discipline: the cheap agent only fires when explicitly invoked.

**Cons**

- Requires injecting a tool into both backends. Trivial for Claude (we own the system prompt + MCP); harder for OpenCode (custom tool plugin).
- Adds latency (synchronous subtask before the primary continues).
- Models may over- or under-delegate without prompt iteration.
- Doesn't help when *the primary itself* is rate-limited — orthogonal mechanism needed.

---

## Proposal E — Pre-flight classifier (LLM-as-router)

A tiny model (Haiku or a free local one) reads each prompt and emits `{provider, model, reasoning}`. Use its choice for the turn. Render the reasoning in the UI as a routing badge.

**Pros**

- Adapts to nuance — intent, domain, ambiguity.
- Cheap (~$0.0001/turn with Haiku, $0 with local).
- Auditable: reasoning is visible.
- Decouples policy from rule maintenance.

**Cons**

- 200–800ms added before first token of every turn.
- Classifier wrongness is opaque; debugging is "tweak the classifier prompt."
- Another model to keep prompt-engineered.
- Overkill for users who already know what they want.

---

## Proposal F — Parallel-and-pick (best-of-N)

Fire the same prompt at multiple agents simultaneously. Show their responses side-by-side. User picks, or an automated judge picks the best, and only the winner enters the persisted history.

**Pros**

- Useful when no single model is trusted (research, tricky prompts).
- Trivial on top of our event-multiplex layer — just N concurrent `provider.run`s.
- A/B-tests provider quality on real workloads.

**Cons**

- N× rate-limit pressure on subscriptions.
- Confusing daily-driver UX.
- "Single session orchestration" is stretched — feels more like split-screen.
- Discarded turns waste subscription quota.

---

## Proposal G — Specialist fleet (Planner / Developer / Coder / Reviewer) ✅ shipped — **architecture superseded May 2026**

> **Original v1** (Mar–May 2026): linear pipeline in [backend/app/orchestrator/fleet.py](../backend/app/orchestrator/fleet.py) — JSON-step plan, fixed execution order, hardcoded retry topology.
>
> **Current implementation** (May 2026 onwards): orchestrator-as-agent — an LLM-driven dispatcher uses a custom MCP `dispatch_subagent` tool to route to claude- or opencode-backed specialists. See [docs/architecture.md](architecture.md). The role set (planner / developer / coder / reviewer / **tester**) is preserved; the dispatch mechanism is rewritten.
>
> Configuration UX in [docs/fleet-config.md](fleet-config.md). Picked as `fleet:default` in the model picker.

Decompose every high-level prompt into a small ordered list: a Planner (Sonnet) drafts steps; an optional Developer (Opus) designs the approach; a Coder (gpt-5.3-codex) executes each step; a Reviewer (Haiku) gates results. One user prompt → orchestrated multi-agent run. Composes naturally with B, C, D.

**Pros**

- Mirrors how senior engineers actually work.
- Strong cost optimization: Opus only at design / planning time.
- Each role's `provider`/`model`/`system_prompt` is independently tunable via [.localcode/fleet.yaml](../.localcode/fleet.yaml.example) or `.json` — no code changes.
- Best ceiling for hard tasks.

**Cons (post-rewrite)**

- ~~Needs a real workflow engine for parallel branches, retries, checkpointing — v1 is linear-only.~~ Resolved: the orchestrator handles retries via its own ReAct loop and the SDK supports parallel `dispatch_subagent` calls (frontend rendering for parallel branches is the only remaining gap).
- Heavy prompt-engineering tax (the default prompts in `fleet.py` work, but you'll want to override for project conventions).
- Long latency per top-level prompt; bad for short questions. Mitigated by the orchestrator's "skip planner for trivial tasks" rule.
- Can *underperform* a single capable agent on tasks that don't decompose cleanly. The orchestrator's freedom to dispatch fewer agents helps but doesn't eliminate this.
- Cost: orchestrator is an extra LLM call per turn. We use claude-sonnet by default to keep this cheap; opus would double the meta-cost.

**v1 limitations to be aware of**

- Linear plans only — `depends_on` is parsed but execution is sequential.
- Sub-step tool calls (Edit / Bash) are silent in the UI; only the step's final text surfaces.
- Reviewer NACKs are surfaced as failed cards but don't auto-retry.
- Per-turn state only — multi-turn chats don't replay prior fleet outputs into a new planner.

---

## Status & recommended order

The original recommendation was A → B → C, skipping G. We shipped G first (the user picked it). Updated state:

| Status     | Proposal                                | Note                                                                                          |
| :--------- | :-------------------------------------- | :-------------------------------------------------------------------------------------------- |
| ✅ shipped | G — Specialist fleet                    | Planner / Developer / Coder / Reviewer. Configurable via YAML or JSON. See [fleet-config.md](fleet-config.md). |
| ⏭ next     | A — Sticky per-turn picker              | Still the missing primitive: today the model is pinned at chat creation. Per-turn switching + slash command unlocks every other proposal. |
| ⏭ then     | B — Declarative routing                 | Composes with G — `fleet:default` could itself become a rule target.                          |
| ⏭ later    | C — Budget-tiered fallback              | Most useful once we have real token-meter data from A+B usage.                                |
| 🤔 maybe   | D — Mid-turn `delegate` tool            | Build after a week of fleet usage informs whether we need agent-driven (vs rule-driven) routing. |
| ⏸ skip     | E — Pre-flight classifier               | Latency cost for unclear gain.                                                                |
| ⏸ skip     | F — Parallel-and-pick                   | Different product (eval harness, not chat).                                                    |

### Cross-cutting work that pays off regardless

- **Token meter** persisted in Postgres, charged per turn, exposed via `/api/budget`. Today's $0 budget bar becomes a tokens/day bar — useful even with OAuth-only auth.
- **History compactor** that produces a short prompt-prefix summary; needed any time we swap backends mid-thread.
- The **`Provider` protocol** (already in place) keeps every proposal pluggable.
