# Orchestrating Claude Code + OpenCode in a Single Session

Design exploration for letting one chat session use both backends, picking the right one per turn based on user preference, task type, and budget.

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

## Proposal G — Specialist fleet (Planner / Coder / Reviewer / Executor)

Decompose every high-level prompt into a small DAG: a Planner (Opus) drafts steps; specialist Coder (gpt-5.3-codex or Sonnet) executes each step; a Reviewer (Haiku) gates results; an Executor runs commands. One user prompt → orchestrated multi-agent run. Composes naturally with B, C, D.

**Pros**

- Mirrors how senior engineers actually work.
- Strong cost optimization: Opus only at planning time.
- Each role's prompt is independently tunable.
- Best ceiling for hard tasks.

**Cons**

- Needs a real workflow engine (DAG, retries, checkpointing).
- Heavy prompt-engineering tax.
- Long latency per top-level prompt; bad for short questions.
- Can *underperform* a single capable agent on tasks that don't decompose cleanly.

---

## Recommendation

A → B → C, in that order. Optional D later. Skip E / F / G for now.

| Phase   | Proposal | Why now                                                                                       |
| :------ | :------- | :-------------------------------------------------------------------------------------------- |
| Phase 1 | A        | Per-turn switching is the missing primitive everything else needs. Hours of work, big UX win. |
| Phase 2 | B        | Declarative routing covers ~80% of "be smart for me" with debuggable rules.                   |
| Phase 3 | C        | Rate-limit resilience is concretely valuable on subscription auth.                            |
| Later   | D        | Most interesting research bet — but build it *after* a week of A+B+C usage informs the design. |
| Skip    | E        | Adds latency for unclear gain.                                                                |
| Skip    | F        | Different product (eval harness).                                                             |
| Skip    | G        | Needs a workflow engine; not the right scope today.                                           |

### Cross-cutting work that pays off regardless

- **Token meter** persisted in Postgres, charged per turn, exposed via `/api/budget`. Today's $0 budget bar becomes a tokens/day bar — useful even with OAuth-only auth.
- **History compactor** that produces a short prompt-prefix summary; needed any time we swap backends mid-thread.
- The **`Provider` protocol** (already in place) keeps every proposal pluggable.
