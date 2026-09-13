# Harness Roadmap — Implementation Plan

Execution plan for the published roadmap (`docs/roadmap.md` content, dated
2026-09-12). The roadmap is the **spec**; this file is its argument. Where the
two conflict, the roadmap wins.

Branch: `worktree-harness-roadmap`. Base: `5bb1268`.

**16 tasks. Execution order is 1, 13, 14, 15, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
12, 16** — not numeric order. Tasks 13–16 were added after an audit of
long-running sessions found seven real defects the roadmap does not mention;
the three reliability ones run immediately after the test harness because
Tasks 3, 4 and 7 build directly on the files they repair. Task 16 runs last
because it verifies everything before it. Task 7 additionally absorbs two audit
defects in the worker protocol. The audit's findings and the reasoning are in
the ledger at `.superpowers/sdd/harness-roadmap/progress.md`.

---

## Spec summary (the authority)

1. **The invariant.** A subscription-backed provider is only ever reached by
   spawning the vendor's own official agent binary and speaking its protocol.
   The harness never reads, forwards, proxies, or stores an OAuth token. This
   must be enforced by a test, not a README aside.
2. **Phase 0** — codify the invariant; bump the SDK; replace the forced
   `acceptEdits` fallback with a real `can_use_tool` routed into the existing
   approval channel; default-deny `cwd` roots; `bypassPermissions` behind an
   explicit opt-in.
3. **Phase 1** — make Codex a first-class peer via `codex app-server`
   JSON-RPC, with one approval bus shared with Claude. Demote OpenCode to an
   optional third provider.
4. **Phase 2** — persistent `ClaudeSDKClient` per session; frozen prompt
   prefix; artifact store with content-addressed handles; cache
   instrumentation.
5. **Phase 3** — kill the per-dispatch process cost; enforce role limits at
   the tool layer; make planner → coder → reviewer conditional; structured
   envelopes instead of transcripts; structured gate verdicts; cap workers and
   per-turn budget.
6. **Phase 4** — quota governor against real rolling windows; route by
   headroom; replace the per-turn dollar figure with a quota meter; degrade
   honestly.
7. **Phase 5** — three-layer harness eval (unit / replay / long-horizon) plus
   golden traces, run across `{claude, codex} × {single, fleet}`.
8. **Phase 6** — speak ACP. Explicitly marked *optional* in the spec.

---

## Global Constraints

These bind every task. A task that violates one is not done.

**Language and style**

- Python 3.11. Every new module starts with `from __future__ import annotations`.
- Line length 100 (`[tool.ruff]` in `pyproject.toml`). `.venv/bin/ruff check backend`
  must report zero errors after every task.
- Match the house style of the existing code: module docstring explaining
  *why*, dataclasses over dicts for structured data, comments that record the
  failure the code prevents. Do not add comments that restate the code.
- No new third-party runtime dependencies. Standard library plus what
  `pyproject.toml` already declares.

**The invariant (non-negotiable)**

- Never read a credential store (`~/.claude/.credentials.json`, `auth.json`,
  `~/.codex/auth.json`, the macOS keychain) and never assign `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or any `*_API_KEY` /
  `*_OAUTH_TOKEN` / `*_SESSION_KEY` from a file, a config value, or another
  environment variable. Providers spawn the vendor binary and let it find its
  own auth. Task 1 makes this a test; later tasks must keep it passing.

**Tests**

- All tests live under `backend/tests/`. Run with `.venv/bin/pytest -q` from
  the repo root.
- `asyncio_mode = "auto"`: async test functions need no decorator.
- **No test may require the network, the `claude` CLI, or the `codex` CLI.**
  Use fakes and injected seams. A test that genuinely needs a real binary is
  marked `@pytest.mark.requires_cli` and is excluded from the default run.
- Every test asserts on behaviour. A test whose body has no `assert` (or whose
  only assertion is that a call did not raise) is a defect.

**Cross-cutting wiring — each of these lists must stay in sync**

- A new event type goes in three places: `backend/app/orchestrator/base.py`
  (`EventType`), `backend/app/schemas.py` (`StreamEvent.type`), and
  `frontend/src/types.ts`.
- A new provider name goes in five places: `backend/app/config.py` (the
  `Provider` literal, `_validate_default_provider`, `catalog()`),
  `backend/app/schemas.py` (`CreateSessionRequest.provider`),
  `backend/app/orchestrator/registry.py` (`_build_provider`, `warm_up`),
  `backend/app/orchestrator/fleet/constants.py` (`VALID_PROVIDERS`), and
  `frontend/src/types.ts`.
- `backend/app/orchestrator/fleet/__init__.py` re-exports the package's public
  surface including underscore back-compat aliases. Do not remove an existing
  export; add new ones.

**Provider-agnostic by construction**

Codex lands in Task 10, after the approval bus (Task 3), the step envelope
(Task 6), the worker pool (Task 7) and role policy (Task 9). Those four must
be keyed on a provider *name*, never on "is this Claude". No `if provider ==
"claude"` branches in shared code. Task 10 must be able to add a provider
without editing them.

**Commits**

- One commit per task (more if a task has natural checkpoints). Message:
  `Task N: <what>` followed by a blank line and a short why.
- Never commit `.run/` (process logs and pids), `.venv/`, or `.localcode/plans/`
  output generated while testing.

---

## Pre-flight notes (established before execution)

- `claude-agent-sdk` 0.2.152 is installed in `.venv` and the app imports
  cleanly against it. `ClaudeSDKClient`, `CanUseTool`, `PermissionResultAllow`,
  `PermissionResultDeny`, `PermissionMode`, `HookMatcher`, `SandboxSettings`,
  `RateLimitInfo`, `RateLimitStatus` and `ResultMessage.usage` are all present.
- The `codex` CLI is **not installed on this machine**. Task 10 is therefore
  built and verified against a fake app-server that speaks the documented
  protocol. A `requires_cli` test covers the real binary for whoever has it.
- `backend/tests/` does not exist. `pyproject.toml` already points `testpaths`
  at it.
- `.venv/bin/ruff check backend` reports 28 pre-existing errors. Task 1 clears
  them so the lint gate means something afterwards.
- `.localcode/fleet.yaml` is tracked in this repo and overrides
  `defaults.py`, so changes to built-in defaults do not change this repo's own
  fleet behaviour.

---

## Task 1 — Test harness, the auth-invariant gate, and a clean lint baseline

**Phase 0.** Nothing else in this plan is verifiable until a test suite exists.

### Files

- `backend/tests/__init__.py` (empty)
- `backend/tests/conftest.py` (new)
- `backend/app/invariants.py` (new)
- `backend/tests/test_auth_invariant.py` (new)
- `pyproject.toml` (pytest markers + addopts)
- lint fixes across `backend/app/` (see below)

### `backend/app/invariants.py`

A source scanner that fails the build on any code path that could hold a
subscription credential. Pure functions over source text, so it is both the
CI gate and unit-testable.

```python
@dataclass(frozen=True)
class Violation:
    filename: str
    line: int
    rule: str          # "credential-store" | "key-assignment" | "keychain"
    detail: str
```

- `CREDENTIAL_STORE_MARKERS`: substrings that may not appear in any string
  literal in `backend/app/`:
  `".credentials.json"`, `"credentials.json"`, `"/auth.json"`, `"auth.json"`,
  `".claude/.credentials"`, `".codex/auth"`, `"find-generic-password"`,
  `"CLAUDE_CODE_OAUTH_TOKEN"`, `"ANTHROPIC_API_KEY"`, `"OPENAI_API_KEY"`,
  `"OPENAI_SESSION_KEY"`, `"ANTHROPIC_AUTH_TOKEN"`.
- `SECRET_KEY_PATTERN`: `re.compile(r"(API_KEY|OAUTH_TOKEN|SESSION_KEY|AUTH_TOKEN)$")`.
- `scan_source(source: str, filename: str) -> list[Violation]`, using `ast`:
  - every `ast.Constant` holding a `str` is checked against
    `CREDENTIAL_STORE_MARKERS` → rule `credential-store`. A marker appearing in
    a *comment or docstring* is fine (docstrings are `ast.Constant` inside
    `ast.Expr` at the head of a module/class/function — skip those
    explicitly, because `claude.py`'s docstring legitimately explains the auth
    model; comments never reach the AST).
  - any `ast.Subscript` assignment target of the form `os.environ[<literal>]`
    or `environ[<literal>]` whose literal matches `SECRET_KEY_PATTERN` → rule
    `key-assignment`. Also catch `os.environ.setdefault(<literal>, ...)`,
    `os.putenv(<literal>, ...)` and a `dict` literal passed as an `env=`
    keyword whose keys match.
  - a call to `subprocess.*` / `asyncio.create_subprocess_*` whose argument
    list contains the literal `"security"` together with
    `"find-generic-password"` → rule `keychain` (the literal check already
    catches it; keep the rule name for a clearer message).
  - Syntax errors raise — a file that does not parse is a failure, not a pass.
- `scan_tree(root: Path, *, skip_dirs=("tests",)) -> list[Violation]`:
  walks `root.rglob("*.py")`, skipping any path with a skipped directory
  component and `__pycache__`.
- `format_violations(violations) -> str`: one line per violation,
  `"<file>:<line> [<rule>] <detail>"`, with a trailing paragraph naming the
  invariant and why it exists (Anthropic's February 2026 terms; enforcement
  live since April 2026).

### `backend/tests/conftest.py`

Fixtures the whole suite uses:

- `repo_root` — `Path(__file__).resolve().parents[2]`, session-scoped.
- `app_root` — `repo_root / "backend" / "app"`.
- `fresh_settings` — clears `get_settings.cache_clear()` before and after, so a
  test that monkeypatches environment variables sees them.
- `tmp_localcode(tmp_path, monkeypatch)` — points `HOME` at `tmp_path` and
  returns it, so anything writing under `~/.localcode` in a test is contained.
- `anyio_backend` is not needed (asyncio_mode=auto).

### `backend/tests/test_auth_invariant.py`

- Unit: each of the three rules fires on a minimal bad source string
  (a `Path("~/.claude/.credentials.json")` read; an
  `os.environ["ANTHROPIC_API_KEY"] = token` assignment; a
  `create_subprocess_exec("security", "find-generic-password", ...)` call).
- Unit: a clean source string produces no violations; a *docstring* mentioning
  `.credentials.json` produces no violations (guards the false positive that
  would otherwise make the gate unusable against `claude.py`).
- Unit: `scan_source` on unparseable source raises `SyntaxError`.
- **The gate:** `scan_tree(app_root)` returns `[]`; on failure the assertion
  message is `format_violations(...)`.

### Lint baseline

Clear all 28 errors in `backend/`:

- `ruff check backend --fix` handles the 11 import-sort / `UP041` ones.
- `E501` in `fleet/defaults.py` (the aligned `ROLE_LIBRARY` table),
  `fleet/loader.py:177`, `fleet/presets.py:15,63,69`,
  `routes/sessions.py:114`: rewrap. Keep `ROLE_LIBRARY` readable — a dict
  entry per line is fine.
- `F401` in `fleet/__init__.py` (the deliberate underscore back-compat
  re-exports) and `storage/sessions.py:52`: add the names to `__all__` where
  they are genuinely public, otherwise `# noqa: F401` with a short reason.
  `collections.abc.Iterator` in `storage/sessions.py` is genuinely unused —
  delete it.
- `B008` in `routes/sessions.py:114` (`Query(...)` in a default) is the
  documented FastAPI idiom. Add `# noqa: B008` with that reason, or extend
  `[tool.ruff.lint.per-file-ignores]` for `routes/`. Prefer the per-file
  ignore with a comment.

### `pyproject.toml`

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["backend/tests"]
addopts = "-m 'not requires_cli'"
markers = [
    "requires_cli: needs a real vendor CLI (claude / codex) on PATH; excluded by default",
]
```

### Verification

```
.venv/bin/pytest -q
.venv/bin/ruff check backend
```

Both clean. The auth gate must genuinely pass against today's
`backend/app/` — if it reports a violation in existing code, that is a finding
to report, not a reason to weaken a rule.

---

## Task 2 — Settings, permission policy, and the SDK pin

**Phase 0.** The pure decision layer that Task 3 wires into the SDK. No
behaviour changes on its own, which is what makes it testable in isolation.

### Files

- `pyproject.toml` (SDK pin)
- `backend/app/config.py` (new settings)
- `backend/app/orchestrator/permissions.py` (new)
- `backend/tests/test_permissions.py` (new)

### `pyproject.toml`

`"claude-agent-sdk>=0.2.152,<0.3.0"` — an upper bound so a 0.3 release cannot
silently change the harness surface.

### `backend/app/config.py` — new settings

```python
    # Default-deny. A session whose cwd is outside every root is rejected with
    # HTTP 400. "~" means "anywhere under the user's home" — broad enough for
    # real work, narrow enough that "/" and "/etc" are refused.
    allowed_cwd_roots: str = "~"
    # Always refused even when under an allowed root. These hold the very
    # credentials the invariant exists to protect.
    denied_cwd_paths: str = (
        "~/.ssh,~/.aws,~/.gnupg,~/.config/gh,~/.claude,~/.codex,"
        "~/.local/share/opencode,~/Library/Keychains"
    )
    # bypassPermissions disables every tool gate in the spawned CLI. Off
    # unless the operator explicitly opts in. Env: ALLOW_BYPASS_PERMISSIONS.
    allow_bypass_permissions: bool = False
    # How long a tool-approval card waits for the user before it is denied.
    tool_approval_timeout_s: float = 300.0
```

Add `denied_path_list() -> list[Path]` alongside the existing
`cwd_allowlist()`, resolving `~` and symlinks.

Note for the implementer: changing `allowed_cwd_roots` from `""` to `"~"`
changes `/api/system/cwd`'s `permissive` flag to `False`. That is intended —
but check `frontend/src/components/ProjectPicker.tsx` for a branch that only
shows the root list when permissive, and make sure a non-permissive default
still renders sensibly. If the component needs a change, make it.

### `backend/app/orchestrator/permissions.py`

The whole module is pure: no I/O, no SDK imports, no events. That is what lets
Task 3 wire it into two different providers and lets the tests be a table.

```python
VALID_PERMISSION_MODES = frozenset({"default", "acceptEdits", "plan", "bypassPermissions"})

WRITE_TOOLS  = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
EXEC_TOOLS   = frozenset({"Bash", "BashOutput", "KillBash"})
NETWORK_TOOLS= frozenset({"WebFetch", "WebSearch"})
# Tool-input keys that carry a filesystem path, in priority order.
PATH_ARG_KEYS = ("file_path", "path", "notebook_path", "filePath")

@dataclass(frozen=True)
class ToolPolicy:
    name: str                       # the role this policy belongs to
    writable: bool                  # may mutate the filesystem at all
    exec_allowed: bool              # may run Bash
    roots: tuple[Path, ...]         # writable/readable roots (resolved)
    denied: tuple[Path, ...]        # always refused
    allow_tools: tuple[str, ...] | None = None   # None = vendor default set
    deny_tools: tuple[str, ...] = ()
    # Tools that need a human yes even when otherwise permitted.
    ask_tools: frozenset[str] = frozenset()

@dataclass(frozen=True)
class Decision:
    outcome: Literal["allow", "deny", "ask"]
    reason: str
```

Functions:

- `normalize_permission_mode(mode: str | None, *, allow_bypass: bool) -> str`
  - `None` or unknown → `"default"`. **Not** `acceptEdits`: the silent
    escalation is the defect this plan removes. Unknown values are logged at
    WARNING with the value and the fallback.
  - `"bypassPermissions"` → returned only when `allow_bypass` is true;
    otherwise `"default"` plus a WARNING naming the setting that would enable
    it.
- `resolve_roots(cwd: str | None, additional_dirs: Sequence[str] | None) -> tuple[Path, ...]`
  — expanduser + resolve, de-duplicated, order preserved, empties dropped.
- `is_within(path: Path, roots: Sequence[Path]) -> bool` — true when `path`
  equals a root or has one as an ancestor, compared after `resolve()` so a
  symlink pointing out of the tree is caught.
- `path_decision(path: Path, policy: ToolPolicy) -> Decision` — denied list
  first (it wins over roots), then roots.
- `extract_paths(tool_name: str, tool_input: Mapping[str, Any]) -> list[Path]`
  — reads `PATH_ARG_KEYS`; for `MultiEdit` also each `edits[*].file_path`;
  for `Bash` returns `[]` (a command line is not a path — `exec_allowed` and
  the ask-gate govern Bash instead).
- `decide(tool_name: str, tool_input: Mapping[str, Any], policy: ToolPolicy, *, mode: str) -> Decision`
  — the single decision function. Order matters; implement exactly this order:
  1. `tool_name` in `policy.deny_tools` → deny ("role <name> may not use <tool>").
  2. `policy.allow_tools` is not None and `tool_name` not in it → deny.
  3. `tool_name` in `WRITE_TOOLS` and not `policy.writable` → deny
     ("role <name> is read-only").
  4. `tool_name` in `EXEC_TOOLS` and not `policy.exec_allowed` → deny.
  5. any extracted path fails `path_decision` → deny, naming the path and the
     roots.
  6. `mode == "bypassPermissions"` → allow.
  7. `tool_name` in `policy.ask_tools` → ask.
  8. `mode == "plan"` and `tool_name` in `WRITE_TOOLS | EXEC_TOOLS` → deny
     ("plan mode").
  9. `mode == "acceptEdits"` and `tool_name` in `WRITE_TOOLS` → allow.
  10. `tool_name` in `WRITE_TOOLS | EXEC_TOOLS` → ask (this is `default` mode).
  11. otherwise allow.
- `policy_for_role(role: str | None, roots, denied, *, writable_default: bool = True) -> ToolPolicy`
  — the role table. Exactly these roles; anything else gets the
  `writable_default` policy named `"session"`:

  | role | writable | exec | allow_tools | deny_tools | ask_tools |
  | :--- | :--- | :--- | :--- | :--- | :--- |
  | `planner` | no | no | `Read, Glob, Grep, LS, TodoWrite` | write + exec + `Agent, Task, Skill` | — |
  | `reviewer` | no | yes | — | write tools + `Agent, Task` | — |
  | `tester` | yes | yes | — | `Agent, Task` | — |
  | `coder` | yes | yes | — | `Agent, Task` | — |
  | `developer` | no | no | `Read, Glob, Grep, LS` | write + exec | — |
  | `orchestrator` | no | no | `()` — MCP dispatch only | everything built-in | — |

  `reviewer` keeps `Bash` because reading a repo honestly needs `git diff` and
  `rg`; its *write* denial is what the spec asks for. Record that reasoning in
  a comment.
- `policy_extras(policy: ToolPolicy) -> dict[str, Any]` — renders the policy
  into the `ctx.extras` keys `claude.py` already understands
  (`claude_allowed_tools`, `claude_disallowed_tools`,
  `claude_disable_settings`, `claude_disable_skills`). Read-only policies set
  `claude_disable_settings=True` and `claude_disable_skills=True` so
  user/project settings cannot re-grant the built-in catalogue — the comment in
  `orchestrator.py` explains why that belt-and-braces is load-bearing.

### `backend/tests/test_permissions.py`

Table-driven. At minimum:

- `normalize_permission_mode`: each valid mode round-trips; `None` → `default`;
  `"acceptEdits "` (whitespace) and `"AcceptEdits"` → `default`;
  `"bypassPermissions"` → `default` without the flag, itself with it.
- `is_within`: inside, equal, outside, parent-of-root, a symlink inside the
  root pointing outside (create it with `tmp_path`), a relative path.
- `decide` matrix over `(mode, role, tool, path)`: at least 20 rows covering
  every numbered branch above, each asserting outcome *and* that the reason
  names the cause.
- `policy_for_role`: reviewer denies `Write`, permits `Bash`; planner denies
  `Bash`; orchestrator's `allow_tools` is empty; an unknown role gets the
  default policy.
- `policy_extras`: a read-only policy disables settings and skills; a writable
  one does not set `claude_allowed_tools`.

### Verification

`.venv/bin/pytest backend/tests -q` and `.venv/bin/ruff check backend` clean.

---

## Task 3 — One approval bus: `can_use_tool` wired into the existing channel

**Phase 0, the centrepiece.** Replaces the forced `acceptEdits` escalation
with a real callback answering into `RunContext.approval_channel`, and makes
the approval surface generic so Codex's `execCommandApproval` (Task 10) lands
in the same place.

### Files

- `backend/app/orchestrator/base.py` — `RunContext.session_id`, `.role`;
  `EventType` unchanged (reuse `pipeline.awaiting_approval`)
- `backend/app/orchestrator/approvals.py` (new — `EventSink`, `await_approval`,
  `build_can_use_tool`)
- `backend/app/orchestrator/dispatch.py` — import `EventSink` / `await_approval`
  from `approvals.py` instead of defining them
- `backend/app/orchestrator/claude.py` — normalized mode, `can_use_tool`,
  merged event loop
- `backend/app/session_runner/turn.py` — pass `session_id` into `RunContext`
- `frontend/src/types.ts`, `frontend/src/components/ChatPane.tsx` — render a
  tool approval card
- `backend/tests/test_approvals.py` (new)

### `base.py`

Add to `RunContext`:

```python
    # The LocalCode session this turn belongs to. Providers key persistent
    # per-session state (an SDK client, a quota window) on it. None in
    # headless/unit use.
    session_id: str | None = None
    # The fleet role this context is running as ("planner", "coder", ...) or
    # None for a plain single-agent session. Selects the ToolPolicy.
    role: str | None = None
```

Also extend the `Provider` protocol with an optional lifecycle hook, defaulted
so existing providers stay conformant:

```python
    async def close_session(self, session_id: str) -> None:
        """Release any state held for one session. Default: nothing."""
```

Implement it as a no-op on `OpenCodeProvider` and `FleetProvider`.

### `approvals.py`

Move `EventSink` and `await_approval` verbatim out of `dispatch.py` (they are
already correct and already tested by use), then add:

- `APPROVAL_ID_PREFIX = "approval.tool"` and a module-level monotonic counter
  helper `next_approval_id(prefix)` → `f"{prefix}.{n}"`.
- `summarize_tool_input(tool_input: Mapping[str, Any], *, max_chars: int = 600) -> dict`
  — a display-safe preview: keep `file_path` / `command` / `pattern` whole,
  truncate long `content` / `new_string` values to 200 chars with a
  `"… (<n> more chars)"` marker, never include a value longer than
  `max_chars`.
**Two layers, and the split is load-bearing.** The spec's "one approval bus"
is unachievable if the shared layer returns one vendor's SDK types. So:

`evaluate_tool_request(...) -> Decision` — **provider-neutral, async, the real
logic.** Signature:
`(tool_name: str, tool_input: Mapping[str, Any], *, policy, mode, sink, approval_channel, timeout_s) -> Decision`.
Its returned `Decision.outcome` is only ever `"allow"` or `"deny"` — it
resolves `"ask"` itself. Task 10's Codex approval handler calls this directly.

  - `decide(...)` from `permissions.py` for the synchronous part.
  - `allow` / `deny` are returned as-is.
  - `ask` with `approval_channel is None` → **deny**, reason explaining that
    no human is attached so the tool could not be approved. Rationale: the old
    code escalated to `acceptEdits` to dodge a hang; the correct headless
    answer is a clean refusal the model can react to, not a silent grant.
    Fleet steps get their *allowances* from their role policy, so a correctly
    configured coder never reaches this branch for an in-roots edit.
  - `ask` with a channel → push `pipeline.awaiting_approval` onto `sink` with
    `{"id", "kind": "tool", "tool": tool_name, "input": <preview>, "reason",
    "timeout_s"}`, `await await_approval(...)`, push
    `pipeline.approval_received`, then return allow / deny accordingly. A
    `timeout` decision denies with a reason saying no response arrived.
  - Wrap the whole body in `try/except Exception` → a deny carrying the
    exception text. A callback that raises would otherwise take the turn down.
  - Log every deny at INFO with tool, role and reason — this is the audit
    trail for "why did my agent refuse".

`build_can_use_tool(*, policy, mode, sink, approval_channel, timeout_s)` — **a
thin Claude adapter, nothing more.** Returns a callable matching the SDK's
`CanUseTool` signature
`(tool_name: str, tool_input: dict, context: ToolPermissionContext) -> PermissionResult`
that awaits `evaluate_tool_request` and maps the outcome: `allow` →
`PermissionResultAllow()`, `deny` → `PermissionResultDeny(message=reason)`. The
deny message is what the model sees, so it must say what was refused and why in
one sentence. This adapter holds no policy logic of its own — if you find
yourself adding a branch here, it belongs in `evaluate_tool_request`.

### `claude.py`

Restructure `run()` into the same two-producer merge `orchestrator.py` already
uses, because the callback now emits events from outside the message stream:

1. Build `roots = resolve_roots(ctx.cwd, ctx.additional_dirs)` and
   `policy = policy_for_role(ctx.role, roots, denied_paths)`, unless
   `ctx.extras` already carries explicit `claude_*` overrides — in which case
   those still apply on top (Task 9 removes the ad-hoc path).
2. `mode = normalize_permission_mode(ctx.permission_mode, allow_bypass=settings.allow_bypass_permissions)`.
   Delete `_VALID_PERMISSION_MODES` and the `acceptEdits` fallback comment.
3. `sink = EventSink()`; `can_use_tool = build_can_use_tool(...)`.
4. `ClaudeAgentOptions(..., permission_mode=mode, can_use_tool=can_use_tool)`.
   Keep `add_dirs`, `resume`, `include_partial_messages`, the extras handling
   and `disallowed_tools` (now the union of the extras list and
   `policy.deny_tools`).
5. One task drains `query(...)` → translate → merged queue; one task drains
   the sink → merged queue; `run()` yields from the merged queue until both
   producers are done. Copy the `_DONE` sentinel / `_seal_when_drained` /
   cancel-on-exit shape from `orchestrator.py` rather than inventing a second
   pattern. The existing `_translate` is unchanged.
6. On any exception, still yield the `error` Event as today.

### `turn.py`

Pass `session_id=session_id` when constructing `RunContext`. Nothing else.

### Frontend

`ChatPane.tsx` already renders the plan approval card for
`pipeline.awaiting_approval`. Generalize it:

- `kind === "plan"` keeps today's rendering (plan markdown + Approve/Reject).
- `kind === "tool"` renders the tool name as the heading, the reason, a
  `<pre>` of the input preview, and Approve / Deny buttons that post the same
  `{type: "approval", id, value, feedback}` frame.
- Add the `kind`, `tool`, `input`, `reason` fields to whatever type in
  `types.ts` describes the event payload. Read the existing component first and
  match its idiom; do not restyle the card.

### `backend/tests/test_approvals.py`

- `summarize_tool_input`: long `content` truncated with the marker, `file_path`
  intact, no value exceeds the cap.
- `build_can_use_tool` allow path returns `PermissionResultAllow`.
- deny path returns `PermissionResultDeny` whose message names the tool.
- ask path with a channel: the sink receives `pipeline.awaiting_approval` with
  `kind == "tool"`; feeding `{"id": <that id>, "value": "yes"}` into the
  channel yields Allow, and `"no"` with feedback yields Deny carrying the
  feedback; the sink also receives `pipeline.approval_received`.
- ask path with a zero timeout denies with a message mentioning the timeout.
- ask path with `approval_channel=None` denies (and does **not** allow).
- a policy whose `decide` raises (inject via a stub policy object) produces a
  Deny, not an exception.
- `claude.py` wiring: monkeypatch `claude.query` with an async generator
  yielding a fake `ResultMessage`, assert `run()` completes, emits
  `assistant.done`, and that the `ClaudeAgentOptions` it built carries
  `permission_mode == "default"` (not `acceptEdits`) and a non-None
  `can_use_tool`. Capture the options by monkeypatching `ClaudeAgentOptions`
  or by asserting on the `options` argument the fake `query` receives.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`, and — if
`frontend/node_modules` exists — `cd frontend && npm run build`. If the
frontend has no installed dependencies, run `npx tsc --noEmit -p frontend`
only when it works offline; otherwise state plainly in the report that the
frontend was type-checked by inspection, and list exactly what you changed.

---

## Task 4 — Persistent `ClaudeSDKClient` per session

**Phase 2.** One-shot `query()` spawns a fresh `claude` CLI per turn: no
prompt-cache reuse, no interrupt, cold start every message.

### Files

- `backend/app/orchestrator/claude.py`
- `backend/app/config.py` (`claude_max_live_clients`)
- `backend/app/session_runner/registry.py` and/or
  `backend/app/routes/sessions.py` — call `close_session` when a session is
  dropped
- `backend/tests/test_claude_client_reuse.py` (new)

### Design

```python
@dataclass
class _ClientHandle:
    client: Any                 # ClaudeSDKClient
    signature: tuple            # what the client was built with
    lock: asyncio.Lock
    last_used: float
```

- `ClaudeProvider.__init__` gains `self._clients: dict[str, _ClientHandle]` and
  `self._clients_lock: asyncio.Lock`.
- `_signature(ctx, mode, options_extras) -> tuple` — `(model, cwd,
  tuple(additional_dirs), system_prompt, mode, tuple(allowed), tuple(denied))`.
  **Prompt-prefix discipline:** the signature exists precisely so a changed
  prefix rebuilds the client instead of mutating a live one. Never call a
  setter to change the system prompt or tool set on a live client.
- `run()`:
  - key = `ctx.session_id or f"anon:{id(ctx)}"` (anonymous keys are closed at
    the end of the run, so unit tests and headless use do not accumulate
    clients).
  - under the lock: fetch the handle; if missing or `signature` differs, close
    the old one and build a new `ClaudeSDKClient(options=...)` + `connect()`.
  - evict to `settings.claude_max_live_clients` (default 8) by `last_used`,
    closing evicted clients.
  - hold `handle.lock` for the turn, then `await client.query(prompt)` and
    `async for message in client.receive_response()` → the existing
    `_translate`.
  - `except asyncio.CancelledError:` → `await client.interrupt()` (guarded by
    try/except), then re-raise. This is the interrupt the spec asks for.
- `close_session(session_id)` — pops and disconnects.
- `aclose()` — disconnects everything.
- A **seam for tests**: module-level `_client_factory = ClaudeSDKClient` and
  build through `self._factory = _client_factory`, so a test can inject a fake
  without monkeypatching the SDK. Keep the resume semantics: pass
  `resume=ctx.upstream_session_id` when building, exactly as today.

### Session teardown

`drop_runner(session_id)` in `backend/app/session_runner/registry.py` already
cancels the turn. Extend the path that knows the provider (the route in
`routes/sessions.py` for `DELETE /{session_id}`, and `delete_all_sessions`) to
also call `provider.close_session(session_id)` for the session's provider.
Swallow and log failures — a teardown error must not fail the delete.

### Tests

With a fake client class (records `connect`/`query`/`receive_response`/
`interrupt`/`disconnect` calls, yields a scripted message list):

- two turns on the same `session_id` with identical context build the client
  once and call `query` twice.
- changing `ctx.model` between turns closes the first client and builds a
  second.
- `close_session` disconnects and a later turn builds fresh.
- a `CancelledError` raised mid-stream calls `interrupt()` and propagates.
- `claude_max_live_clients = 2` with three session ids leaves two live and
  disconnects the least recently used.
- `session_id=None` does not leave a client behind after `run()` completes.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`.

---

## Task 5 — Artifact store and cache/usage instrumentation

**Phase 2.** A 2 MB test output must not land in context, and cache hit rate
must be observable.

### Files

- `backend/app/artifacts.py` (new)
- `backend/app/usage.py` (new)
- `backend/app/orchestrator/claude.py` (emit `usage` on `assistant.done`)
- `backend/app/routes/system.py` (`GET /api/system/usage`)
- `backend/app/config.py` (`artifact_inline_max_bytes`, `artifact_root`)
- `backend/tests/test_artifacts.py`, `backend/tests/test_usage.py` (new)

### `artifacts.py`

```python
@dataclass(frozen=True)
class ArtifactRef:
    id: str          # sha256 hex
    path: Path
    size_bytes: int
    kind: str        # "step-output" | "tool-result" | ...
```

- `ArtifactStore(root: Path)`; default root `Path.home() / ".localcode" / "artifacts"`.
- `put_text(text: str, *, kind: str) -> ArtifactRef` — sha256 of the UTF-8
  bytes; stored at `root/<sha[:2]>/<sha>.txt`; writing an existing id is a
  no-op (content-addressed, so identical content dedupes).
- `get_text(artifact_id) -> str | None`.
- `summarize_for_context(text, ref, *, max_bytes) -> str` — when
  `len(text.encode()) <= max_bytes` returns `text` unchanged. Otherwise returns
  head `2 * max_bytes // 3` chars + a marker line
  `"\n… [truncated <n> bytes — full output at <path> (artifact <id[:12]>)]\n"`
  + tail `max_bytes // 3` chars. Split on a character boundary; never produce
  invalid UTF-8.
- `store_if_large(text, *, kind, max_bytes) -> tuple[str, ArtifactRef | None]`
  — the function callers actually use: returns `(summary, ref_or_None)`.
- Writes are atomic (`tmp` + `os.replace`) and failures raise `OSError` for the
  caller to handle — an artifact store that silently drops data is worse than
  one that complains.

### `usage.py`

```python
@dataclass(frozen=True)
class TurnUsage:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None
    session_id: str | None
    ts: float
```

- `parse_claude_usage(result_message, *, provider, model, session_id) -> TurnUsage`
  — defensive `getattr` / `.get` over `ResultMessage.usage`, accepting both
  snake_case and the Anthropic camelCase spellings, missing keys → 0.
- `UsageLog(path)` — append JSONL to `~/.localcode/usage.jsonl`; `append(u)`,
  `recent(window_s) -> list[TurnUsage]`, and rotation when the file exceeds
  8 MB (rename to `.1`, start fresh). Reads tolerate a truncated final line.
- `cache_hit_rate(entries) -> float` — `cache_read / (cache_read + input)`,
  `0.0` when the denominator is 0.
- `uncached_share(entries) -> float` — `input / (input + cache_read)`.

### Claude wiring

`_translate`'s `ResultMessage` branch gains
`"usage": asdict(parse_claude_usage(...))` in the `assistant.done` data, and
`ClaudeProvider.run` appends the entry to the `UsageLog`. Keep `cost_usd`
where it is — Task 11 changes what the UI *emphasises*, not what is recorded.

### `GET /api/system/usage`

Returns `{"window_s": 3600, "turns": n, "cache_hit_rate": f,
"uncached_share": f, "input_tokens": n, "output_tokens": n,
"cache_read_tokens": n}` over the last hour, plus `by_provider`.

### Tests

- same content stored twice yields one file and the same id.
- `summarize_for_context` under the threshold returns the input unchanged;
  over it, the result is ≤ `max_bytes` + the marker length, contains the
  artifact path, and begins/ends with the original head/tail.
- a 2 MB string round-trips: `store_if_large` returns a short summary and a ref
  whose `get_text` equals the original.
- `parse_claude_usage` handles a fake `ResultMessage` with snake_case keys,
  one with camelCase, and one with `usage=None`.
- `UsageLog` append/recent/rotation, and a truncated trailing line is skipped
  rather than raising.
- `cache_hit_rate` math including the zero-denominator case.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`.

---

## Task 6 — Structured step envelopes and schema gate verdicts

**Phase 3.** `collect_text` returns the whole transcript, and the gate parses
`LGTM` off the last line. Both are context and correctness hazards.

### Files

- `backend/app/orchestrator/fleet/envelope.py` (new)
- `backend/app/orchestrator/fleet/collect.py`
- `backend/app/orchestrator/fleet/subproc.py`
- `backend/app/orchestrator/fleet/provider.py`
- `backend/app/orchestrator/fleet/gate.py`
- `backend/app/orchestrator/fleet/prompts.py`
- `backend/app/orchestrator/fleet/__init__.py` (exports)
- `backend/app/orchestrator/dispatch.py`
- `backend/tests/test_envelope.py`, `backend/tests/test_gate_verdict.py` (new)

### `envelope.py`

```python
@dataclass
class StepResult:
    summary: str                      # what goes into the orchestrator's context
    structured: dict[str, Any] | None # parsed verdict / metrics, when present
    artifact_id: str | None           # full output, when it was evicted
    artifact_path: str | None
    tool_digest: str                  # capped digest of tool activity
    full_bytes: int                   # size of the output before summarizing
    # Token counts the sub-provider reported on its `assistant.done`, when it
    # reported any. Task 7's per-turn budget and Task 11's quota governor both
    # read this, so the field exists from the start rather than being bolted on
    # after the wire format has shipped twice.
    usage: dict[str, int] | None = None
```

- `to_wire() -> dict` / `from_wire(d) -> StepResult` — plain JSON, because this
  crosses the worker process boundary.
- `context_text() -> str` — `summary`, then the digest (capped), then the
  artifact pointer line when there is one. This is the single place that
  decides what an orchestrator sees, so the eviction rule lives here and
  nowhere else.

### `collect.py`

- `collect_step(role, prompt, cwd, additional_dirs, *, permission_mode,
  role_name, progress, session_id=None) -> StepResult` becomes the real
  function. It keeps today's event loop (text chunks, tool calls, tool
  results, `progress.set()` on first event, `sub.aclose()` in `finally`,
  `error` event → `RuntimeError`) and then:
  - caps the tool digest at 4000 chars, `"… (<n> more tool calls)"`.
  - captures `usage` from the sub-provider's `assistant.done` event data when
    it carries one (the `usage` key Task 5 adds), otherwise leaves it `None`.
  - `summary, ref = store_if_large(text, kind="step-output",
    max_bytes=settings.artifact_inline_max_bytes)`.
  - `structured = parse_verdict(text, role_name).to_dict()` when
    `role_name` is a gate role (`reviewer`, `tester`), else `None`.
- `collect_text(...)` stays, now a thin wrapper returning
  `collect_step(...).context_text()`, so the existing export and any caller
  keep working.
- `_role_extras` is untouched here — Task 9 replaces it.

### `gate.py`

Keep `classify_gate` exactly as it is (it is the fallback and it is good).
Add:

```python
@dataclass(frozen=True)
class Verdict:
    value: str          # "lgtm" | "nack" | "nack_code" | "nack_tests"
    reason: str
    source: str         # "json" | "line"
```

- `parse_verdict(output: str, role: str | None) -> Verdict`:
  - strip the tool digest marker first, as `classify_gate` does.
  - look for the **last** fenced block tagged `json` (case-insensitive) and, if
    that fails, the last balanced `{...}` in the text. Parse it; if it is an
    object containing `"verdict"`, normalize the value
    (`lower().strip()`, `tests_ok` → `lgtm`) and take `"reason"` (or
    `"summary"`) as the reason → `source="json"`.
  - otherwise fall back to `classify_gate(output, role)` → `source="line"`,
    reason = the classifier line it matched, or
    `"no verdict found — failing safe"`.
  - an unknown `"verdict"` string falls back to the line parse rather than
    inventing a pass.
  - `to_dict()` for the envelope.

### `prompts.py`

Append to `REVIEWER_SYSTEM` and `TESTER_SYSTEM` a requirement to end the reply
with a fenced JSON block, and keep the existing last-line classifier
instruction as a belt-and-braces fallback:

````
End your reply with BOTH a classifier line and a JSON verdict block:

LGTM

```json
{"verdict": "lgtm", "reason": "<one sentence>"}
```
````

Reviewer values: `lgtm` | `nack`. Tester values: `lgtm` | `nack_code` |
`nack_tests`. Do not otherwise rewrite these prompts.

### `subproc.py` and `provider.py`

- Worker request gains `"session_id"`. Worker response becomes
  `@@RESULT@@ {"ok": true, "result": <StepResult.to_wire()>}`; the error shape
  is unchanged.
- `_SubprocHandle.result` resolves to a `StepResult` (built via `from_wire`).
- `provider._run_step_with_role` stores `result.context_text()` in `outputs`
  and uses `parse_verdict` (via `result.structured`, falling back to a parse)
  for the `is_error` flag on the gate's `tool.result`. The `tool.result`
  content stays the full `context_text()` so the UI card is still useful.

### `dispatch.py`

The `dispatch_subagent` tool returns `result.context_text()` — which is now
bounded — instead of the raw transcript. `_effective_prompt`'s "Full Planner
Artifact" stitching keeps working because a plan under the threshold is
unchanged; a plan over it carries its artifact path, and the coder can read
the file. Say that in a comment.

### Tests

- `StepResult` wire round-trip, including `None`s.
- `context_text` with: small output (verbatim), large output (summary +
  artifact line), digest-only output, empty output.
- `parse_verdict` table: clean JSON block; JSON block plus trailing prose;
  JSON with `verdict` spelled `TESTS_OK`; malformed JSON → line fallback;
  line-only `**LGTM**`; chatty reviewer ("LGTM\nThanks!"); nothing at all →
  fail-safe `nack` for reviewer and `nack_code` for tester; a JSON block with
  an unknown verdict value → line fallback.
- `collect_step` against a stub provider (yield text + tool events via a
  monkeypatched `_build_provider`): a 2 MB text body produces a short summary,
  a non-None `artifact_id`, and `full_bytes` equal to the original length.
- `collect_text` still returns a string and still contains the tool digest.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`.

---

## Task 7 — Long-lived worker pool instead of a process per dispatch

**Phase 3.** Today every fleet step pays interpreter start + SDK import + CLI
spawn. That is the infrastructure overhead worth six benchmark points.

**Ruling carried into this task (do not re-litigate):** the spec says delete the
per-step subprocess because persistent out-of-process clients no longer hit the
SDK re-entrancy deadlock. We keep the process boundary and make the process
long-lived instead. The deadlock the subprocess was built to dodge
(`aclose(): asynchronous generator is already running`, process-global SDK
state, documented in `subproc.py`'s docstring) is a real, previously observed
failure, and the orchestrator still runs its own in-process SDK session. A
pooled worker removes the per-dispatch cost — the actual goal — without
re-opening that failure. If you find evidence the deadlock cannot recur,
record it in your report; do not act on it.

### Two audit defects this task must also fix

Both live exactly where this task already works, so they belong here rather
than in a task of their own.

- **D7.1 — the 64 KiB stdout line limit corrupts large results, and that
  failure is not counted against the retry cap.** `fleet/provider.py:78-89`
  passes no `limit=` to `create_subprocess_exec`, so a stdout line is capped at
  64 KiB, while the worker emits the entire result on one line
  (`subproc.py:61-63`). A plan larger than 64 KiB makes `readline()` raise
  `ValueError`, which `provider.py:120` swallows into `payload = None`, which
  becomes `RuntimeError("sub-provider worker exited without a result")`. In
  `dispatch.py` that lands on the generic `except Exception` at lines 193-195,
  which — unlike the `StepTimeoutError` branch — **does not increment
  `hard_fail`**. So `DISPATCH_HARD_FAIL_CAP` never trips, the orchestrator
  re-dispatches the planner, and the planner deterministically reproduces the
  same oversize output. The loop is bounded only by `max_turns = 30` at up to
  600 s each, holding the session lock for hours while producing nothing.
  Fix both halves:
  1. Frame the result so line length cannot break it. Pass a generous `limit=`
     (at least 8 MiB) **and** make the payload length-prefixed rather than
     newline-delimited for the result line: `@@RESULT@@ <id> <byte-length>\n`
     followed by exactly that many bytes. A protocol whose correctness depends
     on a payload staying under an undocumented reader limit is the defect;
     raising the limit alone leaves it one large plan away from returning.
  2. **Every** sub-agent failure class must count against the cap, not only
     `StepTimeoutError`. Increment `hard_fail` on the generic exception path
     too, and make the refusal message name the failure. A cap that bounds one
     of five failure modes is not a cap.
- **D7.2 — `_step_counters` grows one key per role forever**
  (`dispatch.py:383`). `reset_step_counters()` already exists; call it at the
  start of each turn.

Write the regression test for D7.1 as part of this task: a worker result of
512 KiB on one logical line must round-trip intact, and a deterministically
failing sub-agent must be refused after the cap for a non-timeout failure.
Task 16 re-asserts both against the real pool.

### Files

- `backend/app/orchestrator/fleet/subproc.py` — request **loop**, not one-shot
- `backend/app/orchestrator/fleet/pool.py` (new)
- `backend/app/orchestrator/fleet/provider.py` — use the pool
- `backend/app/config.py` — pool settings
- `backend/tests/test_worker_pool.py` (new), plus a fake worker script under
  `backend/tests/fakes/`

### Worker protocol (v2)

This **supersedes the one-shot framing Task 6 leaves in place.** Task 6 changed
the result *payload* to a `StepResult` wire dict; this task re-frames the
*envelope* around it so one process can serve many requests. Expect to edit the
same lines Task 6 touched — that is intended, not a merge accident.

Line-oriented on stdout, now multiplexed by request id:

```
→ {"id": "r1", "provider": "claude", "model": "...", ...}\n
← @@FIRST@@ r1
← @@RESULT@@ r1 {"ok": true, "result": {...}}
→ {"id": "r2", ...}
← @@FIRST@@ r2
← @@RESULT@@ r2 {"ok": false, "error": "..."}
→ EOF → exit 0
```

- `subproc.py`'s `_main` becomes `async for line in stdin: handle(line)`,
  reading with `asyncio`'s stdin reader or a thread — whichever keeps the
  existing `BaseException` → structured-error guarantee. Each request is
  handled to completion before the next is read (one step per worker at a
  time; the pool provides concurrency by running several workers).
- `_StdoutFirstSignal` takes the request id so `@@FIRST@@ <id>` is
  attributable.
- A malformed request line replies with an error result and keeps the loop
  alive; an empty line is ignored; EOF exits cleanly.
- Keep the `_describe` ExceptionGroup flattening and the last-resort result
  emission exactly as they are.

### `pool.py`

```python
@dataclass
class _Worker:
    proc: asyncio.subprocess.Process
    key: str
    pending: dict[str, _Pending]   # request id -> futures
    last_used: float
    requests_served: int
```

- `WorkerPool(max_workers, idle_timeout_s, worker_module, repo_root)`.
- Key: `f"{session_id or 'anon'}|{provider}|{model}|{cwd}"`. A session never
  shares a worker with another session.
- `submit(key, request) -> (first: asyncio.Event, result: Future[StepResult])`
  — the same two-signal contract `_SubprocHandle` exposes today, so
  `provider._run_step_with_role`'s heartbeat / fast-fail / ceiling logic is
  reused verbatim rather than rewritten.
- One reader task per worker demultiplexes `@@FIRST@@ <id>` and
  `@@RESULT@@ <id> <json>`; an unknown id is logged and dropped; EOF fails all
  pending requests with the captured stderr tail (keep that diagnostic — it
  was hard-won).
- `kill(key)` terminates a worker and fails its pending requests. Called when
  a step times out or fast-fails, so a wedged `claude` CLI is still reclaimed.
- Eviction: over `max_workers`, close the least recently used **idle** worker;
  a worker with pending requests is never evicted. An idle worker past
  `idle_timeout_s` is reaped by a background sweeper started lazily.
- `aclose()` kills everything; called from `FleetProvider.aclose()`.
- **Client reuse inside a worker is off by default.** The worker builds a
  fresh sub-provider per request (as `collect_step` does today). The win here
  is the process, not the conversation: reusing a `ClaudeSDKClient` across
  steps of different turns would leak one role's context into the next. A
  role may opt in later via a `reuse_client` flag; do not implement it now.

### `config.py`

```python
    fleet_max_workers: int = 4           # concurrent sub-provider processes
    fleet_worker_idle_s: float = 300.0   # reap an idle worker after this
    fleet_turn_token_budget: int = 0     # 0 = unlimited; see Task 7 dispatch cap
```

### `provider.py`

Replace `_SubprocHandle` construction with `pool.submit(...)`. Keep: the
`assistant.tool_use` card, `HEARTBEAT_INTERVAL_S` heartbeats with the honest
"no response yet" wording, the `grace_s` fast-fail, the `step_budget_s`
ceiling, `StepTimeoutError`, and the `finally` that kills the worker. The
`handle.kill()` call becomes `pool.kill(key)`.

The pool is owned by the `FleetProvider` instance (a singleton), created
lazily on first use so it binds to the running loop.

### Per-turn budget cap

In `dispatch.py`: a per-turn counter of dispatches and of tokens, summed from
each `StepResult.usage` (the field Task 6 adds; `None` contributes nothing). When `fleet_turn_token_budget > 0` and the turn exceeds it,
`dispatch_subagent` refuses with an error result telling the orchestrator to
stop and summarize. Also cap total dispatches per turn at
`max(8, 2 * len(registry))` and refuse beyond that with the same shape as the
existing `DISPATCH_HARD_FAIL_CAP` refusal. No subagent ever receives dispatch
tools, so recursive spawning is already impossible — assert that in a test
rather than adding a guard.

### Tests

Use a **fake worker script** (`backend/tests/fakes/echo_worker.py`) that
implements the v2 protocol with no SDK import, so the pool is testable and
fast:

- two sequential submits on the same key reuse one process (assert the pid /
  `requests_served`).
- different session ids get different processes.
- `max_workers = 2` with three keys evicts the least recently used idle
  worker.
- a worker killed mid-request fails its pending future with a diagnostic that
  includes the stderr tail.
- `@@FIRST@@` sets the first-signal event before the result resolves.
- a malformed request line yields an error result and the worker stays alive
  for the next request.
- idle reaping closes a worker after the timeout.
- `aclose()` leaves no child processes.
- a real `subproc.py` round-trip with a stubbed `collect_step`
  (monkeypatch via an environment variable the worker honours, or by
  importing the module and calling its handler directly — prefer calling the
  handler directly and testing the loop separately from the SDK).
- dispatch budget: a turn that exceeds the dispatch cap gets a refusal
  mentioning the cap.

### Verification

`.venv/bin/pytest backend/tests -q` and `.venv/bin/ruff check backend`.
Also confirm no orphan processes: the test module's teardown asserts
`pool.aclose()` left `pool._workers` empty.

---

## Task 8 — Conditional routing: stop taxing trivia 15×

**Phase 3.** The orchestrator prompt currently mandates planner → coder →
reviewer on every turn, read-only ones included.

### Files

- `backend/app/orchestrator/fleet/router.py` (new)
- `backend/app/orchestrator/fleet/models.py` (`FleetConfig.always_full_crew`)
- `backend/app/orchestrator/fleet/loader.py` (parse + serialize the new field)
- `backend/app/orchestrator/fleet/provider.py` (compute the decision, pass it)
- `backend/app/orchestrator/orchestrator.py` (routing block in the prompt)
- `backend/app/orchestrator/fleet/__init__.py` (exports)
- `backend/tests/test_router.py` (new)

### `router.py`

Deterministic, cheap, inspectable. No model call — a classifier that costs a
model call to decide whether to spend model calls is self-defeating.

```python
TaskClass = Literal["lookup", "simple", "standard"]

@dataclass(frozen=True)
class RouteDecision:
    task_class: TaskClass
    agents: tuple[str, ...]   # roles to use, in order
    rationale: str            # one line, shown in the prompt and logged
```

- `MUTATION_VERBS`: add, implement, fix, refactor, write, create, delete,
  remove, rename, migrate, build, update, change, bump, install, wire,
  generate, port, upgrade, patch, revert, merge, split, extract, replace.
- `LOOKUP_MARKERS`: how many, what is, what are, which, where, why, explain,
  describe, summarize, list, show, find, count, read, look at, inspect,
  review, does, is there, can you tell.
- `classify(prompt: str) -> TaskClass`:
  - normalize to lowercase; strip code fences before matching so a pasted diff
    does not read as a mutation request.
  - `lookup` when a lookup marker is present **and** no mutation verb appears
    as a whole word **and** the stripped prompt is ≤ 400 characters.
  - `simple` when exactly one mutation verb appears, the prompt is ≤ 400
    characters, and it contains no multi-step markers (`"then"`, `"also"`,
    `"and then"`, a numbered list, more than two sentences).
  - `standard` otherwise. Ambiguity resolves *upward* — spending too much on a
    borderline task is recoverable, under-planning a real one is not.
- `decide(prompt, available_roles, *, always_full_crew=False) -> RouteDecision`:
  - `always_full_crew` → every available role in canonical order, rationale
    `"always_full_crew is set in the fleet config"`.
  - `lookup` → one agent: the first of `("reviewer", "coder", "developer",
    "planner")` that is available (reviewer first: it is the read-only role).
  - `simple` → `("coder", "reviewer")` filtered to available, falling back to
    whatever single role exists.
  - `standard` → every available role in canonical order.
  - the rationale always names the class and the trigger, e.g.
    `"lookup: question-shaped, no mutation verb → 1 agent instead of 4"`.
- `render_routing_block(decision) -> str` — the Markdown inserted into the
  orchestrator system prompt.

### `orchestrator.py`

Replace the mandatory-crew paragraph. Today it reads:

> For every user task, use the mandatory core sequence below when those agents
> are registered. Do NOT skip planner, coder, or reviewer because a task looks
> trivial, read-only, or informational. The user's preference is to see all
> three core agents participate on every fleet turn.

It becomes a `{routing_block}` placeholder filled by
`render_routing_block(decision)`. The block states the class, the agents to
use, the rationale, and that the orchestrator may escalate to more agents if
the work turns out larger than the classification suggested — but must not
*de*-escalate below the named set. When `always_full_crew` is set, the block
renders the old mandatory wording, so the previous behaviour is one config flag
away.

`OrchestratorAgent.__init__` takes `route: RouteDecision | None = None`;
`FleetProvider._run_orchestrated` computes it from `ctx.prompt`,
`cfg.role_names()` and `cfg.always_full_crew`, logs it at INFO, and passes it.
`None` → the full-crew block (safe default for direct/unit use).

### `models.py` / `loader.py`

`FleetConfig.always_full_crew: bool = False`, parsed from YAML/JSON like the
existing `require_plan_approval`, included in `config_to_dict`, and merged by
`_merge_config`. Follow the existing field's code path exactly.

### Tests

- `classify` table of ~25 prompts: clear lookups ("how many files are in this
  repo?", "explain the fleet config"), clear mutations ("implement a quota
  governor"), one-verb simples ("fix the typo in README.md"), multi-step
  ("add X then refactor Y"), long prompts, a prompt that is mostly a pasted
  diff, a prompt containing "review this PR" (lookup), "review and fix"
  (standard, two verbs' worth of work).
- `decide`: role availability filtering; `always_full_crew` overrides every
  class; a single-role registry always yields that role; the rationale is
  non-empty and names the class.
- `render_routing_block` contains each chosen agent name.
- `ORCHESTRATOR_SYSTEM.format(...)` still formats with the new placeholder
  (a `KeyError` here is the classic failure) — assert on a rendered prompt for
  both the routed and `always_full_crew` paths.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`.

---

## Task 9 — Role limits enforced at the tool layer

**Phase 3.** Role guarantees are currently prose in `ORCHESTRATOR_SYSTEM` and
one hand-rolled `_role_extras` dict for the planner. Prose is not enforcement.

### Files

- `backend/app/orchestrator/fleet/collect.py` (`_role_extras` → policy)
- `backend/app/orchestrator/claude.py` (policy applied for `ctx.role`)
- `backend/app/orchestrator/fleet/provider.py` / `subproc.py` (thread `role`
  into `RunContext.role`)
- `backend/tests/test_role_enforcement.py` (new)

### Changes

- `collect_step` sets `RunContext.role = role_name` and derives extras from
  `policy_extras(policy_for_role(role_name, roots, denied))` instead of the
  hand-written planner dict. Keep the planner's effective permissions the same
  as the current code grants (`Read, Glob, Grep, LS`, no settings, no skills,
  no write/exec/dispatch) — the policy table in Task 2 already encodes that;
  verify it matches and fix the table if it does not.
- `claude.py` already builds its policy from `ctx.role` (Task 3). Remove the
  "extras win" special case: extras now *narrow* a policy (intersect allowed,
  union denied) and can never widen it. A comment must say so.
- The reviewer's read-only guarantee is enforced twice on purpose:
  `disallowed_tools` stops the tool being offered, and `can_use_tool` refuses
  it if it is offered anyway (user settings, a future SDK default). The
  comment in `orchestrator.py` about `allowed_tools` alone being insufficient
  is the evidence for why both are needed.

### Tests

- `policy_for_role("reviewer", ...)` → `decide("Write", {...})` denies, at
  every permission mode including `acceptEdits` and `bypassPermissions`.
  (`bypassPermissions` must **not** override a role's read-only status — the
  role policy is a structural limit, the mode is a human-in-the-loop
  preference. Order the branches in `decide` accordingly and assert it here.)
- `policy_for_role("coder", ...)` → `Write` inside a root allows, outside
  denies, and a path under `denied_cwd_paths` denies.
- `collect_step` for `role_name="planner"` builds a `RunContext` whose
  `extras["claude_disallowed_tools"]` contains `Edit`, `Write` and `Bash` and
  whose `role` is `"planner"` (assert via a stub provider capturing the ctx).
- extras cannot widen: a context carrying
  `claude_allowed_tools=["Write"]` for the reviewer role still denies `Write`.

### Note on `decide` ordering

Task 2 lists `bypassPermissions → allow` at step 6, after the role checks
(1–5). That ordering is what makes this task's first test pass. If Task 2
shipped it differently, fix `decide` here and say so in the report.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`.

---

## Task 10 — `CodexProvider`: Codex as a first-class peer

**Phase 1.** The ChatGPT side currently routes through a third-party server
with a client-side global-event filter, no approvals and no extra-directory
support. The official `codex app-server` is a structural mirror of
`claude-agent-sdk`.

**The codex CLI is not installed on this machine.** Build against the
documented protocol and verify with a fake app-server. Mark the one live test
`requires_cli`.

### Files

- `backend/app/orchestrator/codex/__init__.py` (new)
- `backend/app/orchestrator/codex/protocol.py` (new)
- `backend/app/orchestrator/codex/jsonrpc.py` (new)
- `backend/app/orchestrator/codex/client.py` (new)
- `backend/app/orchestrator/codex/provider.py` (new)
- wiring: `config.py`, `schemas.py`, `orchestrator/registry.py`,
  `fleet/constants.py`, `frontend/src/types.ts`
- `backend/tests/fakes/fake_codex_app_server.py` (new)
- `backend/tests/test_codex_jsonrpc.py`, `backend/tests/test_codex_provider.py`
  (new)
- `docs/codex.md` (new), `Makefile` (`codex-schema` target)

### `protocol.py` — every wire name, in one file

The app-server is explicitly experimental and its JSON shapes change per CLI
release. Isolating the vocabulary means a schema bump is a one-file edit.

```python
# Pinned to the codex app-server protocol as documented for CLI 0.5x.
# Regenerate with:  codex app-server generate-json-schema
# and reconcile this module against the output (see `make codex-schema`).
PROTOCOL_NOTE = "..."

M_INITIALIZE      = "initialize"
M_THREAD_START    = "thread/start"
M_THREAD_RESUME   = "thread/resume"
M_THREAD_FORK     = "thread/fork"
M_TURN_START      = "turn/start"
M_TURN_STEER      = "turn/steer"
M_TURN_INTERRUPT  = "turn/interrupt"

N_ITEM_STARTED    = "item/started"
N_ITEM_UPDATED    = "item/updated"
N_ITEM_COMPLETED  = "item/completed"
N_TURN_COMPLETED  = "turn/completed"
N_TURN_FAILED     = "turn/failed"

# Server → client requests (the approval callbacks).
R_EXEC_APPROVAL   = "execCommandApproval"
R_PATCH_APPROVAL  = "applyPatchApproval"

ERR_BUSY = -32001   # app-server overloaded; back off and retry
```

Plus `ITEM_TYPE_*` constants for the item kinds the translator handles
(`agent_message`, `reasoning`, `command_execution`, `file_change`,
`mcp_tool_call`, `web_search`, `error`) and `APPROVAL_DECISIONS`
(`"approved"`, `"approved_for_session"`, `"denied"`, `"abort"`).

Every name above is a **best-effort reading of the documented protocol**. Put
that caveat in the module docstring, and make every translator branch tolerant
of an unknown type (log once at DEBUG, skip) rather than raising.

### `jsonrpc.py`

`StdioJsonRpc` — newline-delimited JSON-RPC 2.0 over a child process's stdio.

- `__init__(proc, *, logger)`; `start()` launches the reader task.
- `request(method, params, *, timeout_s) -> Any` — monotonic integer ids,
  futures keyed by id, `asyncio.wait_for` on the future. A JSON-RPC `error`
  response raises `JsonRpcError(code, message, data)`. `ERR_BUSY` is retried
  up to 3 times with 0.25 s / 0.5 s / 1 s backoff before it surfaces.
- `notify(method, params)`.
- `on_notification(method, handler)` — `handler(params)`, sync or async.
- `on_request(method, handler)` — for **server → client** requests: the handler
  returns the result payload and `StdioJsonRpc` writes the JSON-RPC response
  with the original id. A handler that raises produces an error response, not a
  dropped request (a dropped approval request hangs the agent forever).
- Unknown notification → DEBUG log, dropped. Unknown server request → an error
  response with code `-32601`.
- `close()` — cancel the reader, close stdin, terminate then kill the child,
  fail every pending future with a clear message that includes the stderr
  tail.
- Lines that are not JSON are logged at DEBUG and skipped: the real binary
  prints diagnostics on stdout.

### `client.py`

- `CodexAppServer` — owns one `codex app-server` process for one workspace.
  - `start()` → spawn `[settings.codex_binary, "app-server"]` with
    `cwd=<workspace>`, `stdin/stdout/stderr=PIPE`, then `initialize` with the
    client name/version and capabilities, within
    `settings.codex_startup_timeout_s`.
  - `FileNotFoundError` on spawn → raise `CodexUnavailable` with a message
    naming the binary, that it is not on PATH, and `codex login` as the fix.
    Never fall back to an API key — the invariant.
  - `thread_start(cwd, additional_dirs)`, `thread_resume(thread_id)`,
    `turn_start(thread_id, text) -> AsyncIterator[dict]` yielding raw item
    notifications through an `asyncio.Queue` until `turn/completed` or
    `turn/failed`, `turn_interrupt(thread_id)`.
  - **Silent turn:** if `turn/completed` arrives with no `agent_message` item,
    yield a synthetic error item built from whatever items did arrive. The spec
    calls this out: a turn without a final message returns no response body and
    must not be reported as a silent success.
  - `set_approval_handler(fn)` — registers handlers for both approval requests;
    `fn(kind, params) -> str` returns a decision string.
- `CodexBroker` — `get(workspace) -> CodexAppServer`, one long-lived server per
  workspace behind an `asyncio.Lock`, `aclose_all()`. Cold start is paid once
  per workspace, not per dispatch.

### `provider.py`

`CodexProvider` mirroring `ClaudeProvider`'s shape exactly:

- `name = "codex"`; `open_session(ctx)` returns the thread id (resuming
  `ctx.upstream_session_id` when present, starting one otherwise);
  `run(ctx)` yields Events; `aclose()`; `close_session(session_id)`.
- Item → Event translation:
  - `agent_message` delta → `assistant.text`
  - `reasoning` → `assistant.text` only if it carries user-visible text;
    otherwise skipped
  - `command_execution` started → `assistant.tool_use`
    `{"name": "Bash", "input": {"command": ...}}`; completed → `tool.result`
    with the output and `is_error` from the exit code
  - `file_change` → `assistant.tool_use` named `Edit`/`Write` with the path,
    then `tool.result`
  - `mcp_tool_call` / `web_search` → the same tool_use/tool_result pair
  - `error` → `error`
  - `turn/completed` → `assistant.done` with `usage` (when the payload carries
    token counts) and `upstream_session_id` = the thread id
- **Approvals route into the same bus as Claude.** The approval handler calls
  the same `decide` / `build_can_use_tool` machinery: map
  `execCommandApproval` to tool name `Bash` with `{"command": ...}` and
  `applyPatchApproval` to `Edit` with the changed paths, run the shared
  decision, and translate the result back to a protocol decision string
  (`allow` → `"approved"`, `deny` → `"denied"`). The user therefore sees the
  same card with the same semantics for both vendors — this is the harness's
  central value proposition, so do not build a second approval path.
- `additional_dirs` is forwarded to `thread/start`; note in a comment that
  OpenCode could not do this, which is half the reason for this provider.

### Wiring

Add `"codex"` everywhere the Global Constraints list says. `VALID_PROVIDERS`
gains `"codex"`. New settings:

```python
    codex_binary: str = "codex"
    codex_startup_timeout_s: float = 30.0
    codex_request_timeout_s: float = 120.0
```

**Defaults stay put.** `ROLE_LIBRARY`'s coder remains
`opencode:openai/gpt-5.3-codex`, because `codex` is not installed here and a
default that cannot run is worse than one that can. Add a commented
`codex` alternative beside it and document the one-line switch in
`docs/codex.md`. OpenCode keeps working and is documented as the optional
third provider for local and non-frontier models.

### `backend/tests/fakes/fake_codex_app_server.py`

A standalone Python script (no project imports, so it runs as a child process)
that speaks the protocol and is driven by a scenario name in `argv[1]`:

- `happy` — initialize, thread/start, a turn emitting reasoning, a
  command_execution pair, an agent_message, then turn/completed.
- `approval` — emits an `execCommandApproval` server request and waits for the
  response before continuing; the command only runs on `"approved"`.
- `busy` — answers the first `turn/start` with `-32001`, then succeeds.
- `silent` — a turn that completes with no `agent_message`.
- `crash` — exits non-zero mid-turn.
- `patch_approval` — an `applyPatchApproval` request.

### Tests

`test_codex_jsonrpc.py` (against the fake):

- request/response correlation with two in-flight requests resolving out of
  order.
- a JSON-RPC error response raises `JsonRpcError` carrying the code.
- `ERR_BUSY` retries and then succeeds; exhausting retries raises.
- a server→client request reaches the registered handler and its return value
  is written back as a response with the matching id; a handler that raises
  produces an error response.
- non-JSON stdout lines are ignored.
- `close()` fails pending futures and leaves no child process.

`test_codex_provider.py`:

- `happy` → the Event sequence is exactly the expected types in order, ending
  in `assistant.done` with the thread id.
- `approval` → a `pipeline.awaiting_approval` Event with `kind == "tool"`
  appears; answering `"yes"` lets the turn finish; answering `"no"` produces a
  denial the fake reports and the provider surfaces.
- `silent` → an `error` Event, never a bare `assistant.done` implying success.
- `crash` → an `error` Event naming the exit.
- binary missing (`codex_binary="definitely-not-a-real-binary"`) → a single
  `error` Event whose message names the binary and `codex login`; no
  exception escapes `run()`.
- broker reuse: two `open_session` calls for the same workspace spawn one
  process.
- `@pytest.mark.requires_cli` — the real `codex app-server` handshake, skipped
  unless the binary is on PATH.

### `make codex-schema`

```
codex-schema: ## Dump the codex app-server JSON schema for reconciliation
	codex app-server generate-json-schema > docs/codex-app-server.schema.json
```

Plus a note in `docs/codex.md`: when the schema changes, `protocol.py` is the
only module that should need editing, and the fake server must be updated
alongside it.

### Verification

`.venv/bin/pytest backend/tests -q` (fake-backed tests green, live test
skipped), `.venv/bin/ruff check backend`, and the app still boots:
`.venv/bin/python -c "from backend.app.main import create_app; create_app()"`.

---

## Task 11 — Quota governor and routing by headroom

**Phase 4.** Two subscriptions are two independently exhaustible budgets on
different clocks. The per-turn `cost_usd` figure bills nobody.

### Files

- `backend/app/quota.py` (new)
- `backend/app/orchestrator/claude.py` (record provider-reported limits)
- `backend/app/orchestrator/codex/provider.py` (record what the app-server
  reports)
- `backend/app/routes/system.py` (`GET /api/system/quota`)
- `backend/app/orchestrator/dispatch.py` (headroom routing + honest refusal)
- `backend/app/orchestrator/agent_def.py` (`provider: "auto"`)
- `frontend/src/components/Topbar.tsx`, `frontend/src/api.ts`,
  `frontend/src/types.ts` (the meter)
- `backend/tests/test_quota.py` (new)

### `quota.py`

```python
@dataclass
class WindowState:
    provider: str
    window_s: float            # rolling window length
    started_at: float          # when the current window opened
    used: float                # consumed units in this window
    limit: float | None        # None = unknown
    source: str                # "provider" | "local"
    resets_at: float | None

@dataclass
class QuotaSnapshot:
    windows: dict[str, list[WindowState]]   # provider -> windows (5h, weekly)
    generated_at: float
```

- `Governor(path)` persisting `~/.localcode/quota.json` atomically
  (`tmp` + `os.replace`), tolerating a corrupt file by starting fresh with a
  WARNING.
- Default windows: Claude — one 5-hour window (the SDK's reported reset time
  overrides it when present); Codex — a 5-hour rolling window *and* a weekly
  cap, per the spec.
- `record(provider, *, tokens, reported=None)` — rolls the window when
  `now - started_at >= window_s`; a `reported` payload (authoritative, from the
  vendor) replaces `used`/`limit`/`resets_at` and sets `source="provider"`;
  otherwise accumulates locally.
- `headroom(provider) -> float` in `0.0..1.0` — the **minimum** headroom across
  that provider's windows; `1.0` when the limit is unknown, with
  `confidence="unknown"` exposed in the snapshot so the UI does not draw a full
  bar as if it were measured.
- `choose(candidates: Sequence[str]) -> str | None` — the candidate with the
  most headroom; `None` when every candidate is below
  `QUEUE_THRESHOLD = 0.05`.
- `should_queue(candidates) -> bool`.
- `snapshot() -> QuotaSnapshot` and `to_dict()`.
- A module-level `get_governor()` with an `lru_cache`, matching
  `get_settings()`'s idiom.

### Providers report in

- Claude: the SDK delivers rate-limit data as its OWN message type, not on the
  result — `RateLimitEvent(rate_limit_info, uuid, session_id)` carrying
  `RateLimitInfo(status: allowed|allowed_warning|rejected, resets_at: unix int,
  rate_limit_type: five_hour|seven_day|seven_day_opus|seven_day_sonnet|overage,
  utilization: float 0-1, overage_*, raw)` — see
  `claude_agent_sdk/types.py:1370-1411` in the installed SDK. `claude.py`'s
  `_translate` has NO branch for it today and drops it silently; add one, read it
  defensively (`getattr`, tolerate missing fields and a `None` info), and pass it
  to the governor as `reported`. Two consequences for the design: (a) the CLI
  emits this event only when the status TRANSITIONS, not every turn, so the
  governor must persist the last-known per-window state and treat absence as
  "no change", never as "reset", with local token accumulation as the fallback
  between events; (b) `rate_limit_type` includes model-specific seven-day
  windows, so Claude has up to four concurrent windows, not one — keep one
  `WindowState` per `rate_limit_type` seen, and `headroom()` is the minimum
  across them. Never crash on a shape change — a governor that takes the turn
  down is worse than one that estimates.
- Codex: same treatment for whatever `turn/completed` carries.

### Routing

- `AgentDef.provider` may be `"auto"`. `dispatch_subagent` resolves it via
  `governor.choose(candidates)` where candidates come from a new
  `AgentDef.metadata["providers"]` list (default: the configured providers
  that are actually available). Log the choice and include it in the step card
  name so the user can see which subscription served the step.
- When `choose` returns `None`, `dispatch_subagent` refuses with a message
  telling the orchestrator to stop and tell the user both subscriptions are
  near their caps, and that the work can be queued. Degrade honestly: no
  silent retry, no mid-plan failure.

### `GET /api/system/quota`

Returns `to_dict()` plus a `queue_suggested: bool`.

### Frontend

`Topbar.tsx` has no cost element today — the per-turn dollar figure is rendered
per message in `ChatPane.tsx` (around lines 737-743, the `lc-msg__cost` span),
typed at `types.ts:109` and `:165`. So the quota meter is a NEW element in the
top bar, not a replacement: one compact meter per provider, a labelled bar with
percent remaining and a reset-time tooltip, plus an "unknown" state when no
limit has been reported. Leave the per-message `cost_usd` in `ChatPane.tsx`
exactly where it is — the spec's intent is that the prominent, always-visible
number becomes remaining headroom, and putting the meter in the top bar is what
achieves that. Poll `/api/system/quota` on session change and turn completion
(there is no existing polling interval to reuse). Run `npm run build`.

### Tests

- window rollover: a `record` after `window_s` resets `used` and sets a new
  `started_at`.
- a `reported` payload overrides local accumulation and flips `source`.
- `headroom` math: known limit, unknown limit → `1.0` with
  `confidence="unknown"`, the minimum across two windows wins.
- `choose` prefers the larger headroom; ties resolve deterministically (first
  candidate); all-exhausted returns `None`.
- `should_queue` at the threshold boundary (exactly `0.05` does not queue;
  below does).
- persistence round-trip through a temp path, including a corrupt file
  recovering with a warning rather than raising.
- the atomic write leaves no `.tmp` file behind.
- a malformed vendor payload records locally instead of raising.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`, frontend
build if installable.

---

## Task 12 — Replay, golden traces, the provider matrix, and the docs

**Phase 5** plus the documentation the rest of the plan earns. This is the gap
that makes every other gap permanent.

### Files

- `backend/tests/fakes/__init__.py`, `backend/tests/fakes/providers.py` (new —
  scriptable fake providers)
- `backend/tests/replay/claude_basic.json`,
  `backend/tests/replay/claude_tools.json`,
  `backend/tests/replay/codex_basic.json`,
  `backend/tests/replay/codex_approval.json` (new fixtures)
- `backend/tests/test_replay.py` (new)
- `backend/tests/test_long_horizon.py` (new)
- `backend/tests/golden/fleet_standard.json`,
  `backend/tests/golden/fleet_lookup.json` (new)
- `backend/tests/test_golden_traces.py` (new)
- `backend/tests/test_matrix.py` (new)
- `docs/harness.md` (new), `docs/architecture.md`, `docs/codex.md`,
  `README.md`, `Makefile`

### Replay layer

Fixtures are JSON arrays of recorded provider messages (for Claude: the
`StreamEvent` / `AssistantMessage` / `ResultMessage` shapes `_translate`
consumes, as plain dicts plus a `__type__` tag; for Codex: raw item
notifications). A small loader rehydrates them into the objects each
translator expects.

`test_replay.py` asserts, for each fixture: the exact ordered list of Event
types, the text assembled from `assistant.text` deltas, tool_use ids matching
their tool_result ids, and the final `assistant.done` payload keys. Then it
feeds the same Event stream through `TurnAccumulator` and asserts the persisted
message blocks — every `tool_use` has a matching `tool_result`, text is
flushed in order, and heartbeats are absent.

### Long-horizon layer

`test_long_horizon.py`, each case end-to-end through `execute_turn` with a
fake provider and a temp storage root:

- resume after interrupt: a turn cancelled mid-stream persists a coherent
  message (dangling `tool_use` gets a synthetic result) and a following turn on
  the same session continues.
- approval branching: approve, deny-with-feedback, and timeout each produce
  the documented downstream behaviour and an `assistant.done`.
- subagent handoff: a fleet turn where the planner's output exceeds the inline
  threshold passes an artifact reference to the coder, and the coder can read
  the artifact file.
- wedged backend fast-fail: a fake that yields nothing triggers the startup
  grace abort with the honest "no response yet" wording, and the turn ends with
  an `error` plus `assistant.done` rather than hanging.
- context eviction: a 2 MB tool result does not appear in the orchestrator's
  context text.

### Golden traces

`test_golden_traces.py` runs a fleet turn against fake providers and records,
per step: ordered tool calls, a stable hash of each call's arguments (sorted
JSON, sha256, first 12 hex chars), approval metadata (id, kind, tool,
decision), and a side-effect diff (files created under the temp cwd). The
trace is compared to a committed JSON golden. A mismatch prints a readable
diff. `UPDATE_GOLDEN=1 .venv/bin/pytest backend/tests/test_golden_traces.py`
rewrites the goldens; document that in `docs/harness.md` and in a comment at
the top of the test.

Two goldens: a `standard` turn (full crew) and a `lookup` turn (one agent) —
together they are the regression net for Task 8's routing and for "a narrow
lookup quietly becoming a broad one".

### Matrix

`test_matrix.py` parameterizes `{claude, codex} × {single, fleet}` over the
fakes: a single-provider session turn, and a fleet turn whose roles are served
by that provider. Each case asserts the same invariants — an ordered event
stream, a terminal `assistant.done`, persisted blocks that round-trip, and
approvals surfacing through the one bus. This matrix is the product; if a case
cannot be expressed against fakes, say so in the report rather than weakening
the assertion.

### Docs

- `docs/harness.md` (new) — the invariant and its test; the one approval bus
  and how a vendor-specific callback maps onto it; the provider table (claude /
  codex / opencode, what each supports); persistent sessions and prompt-prefix
  discipline; the artifact store; the worker pool and its budgets; conditional
  routing with the class table; the quota governor; the three eval layers and
  how to refresh goldens.
- `docs/architecture.md` — update the provider/event diagram and the fleet
  section to match what now exists; rewrite rather than append. Specifically:
  the heading at line ~259 still names `backend/app/session_runner.py` as a
  single FILE — it has been a package (`bus.py`, `runner.py`, `turn.py`,
  `accumulator.py`, `registry.py`, `config.py`) since before this plan; and
  there are NO headings for `approvals.py`, `permissions.py`, `artifacts.py`,
  `usage.py`, `quota.py`, `fleet/pool.py`, `fleet/router.py`,
  `fleet/envelope.py`, or `codex/` — add one per module in the existing `####`
  style, each a few sentences on responsibility and interface.
- `README.md` — the invariant paragraph (lines ~23-35, the OAuth / "no keys to
  manage" text) now points at `backend/tests/test_auth_invariant.py`; the cost
  paragraph at line ~104 ("Per-turn cost (USD) is reported ... There is no daily
  aggregate — under OAuth-only mode that figure would always be a subscription,
  not a metered spend") is the sentence the roadmap paraphrases as "admits is
  meaningless" — REWRITE it: the prominent number is now remaining subscription
  headroom per provider (the top-bar meter, `/api/system/quota`) and the
  per-message dollar figure stays inline as informational; the "Three providers" table
  (lines ~39-45) and "Three provider prefixes are valid" (line ~88) become four
  with `codex` and its `codex login` prerequisite; the file table (lines ~56-61)
  gains rows for `approvals.py`, `permissions.py`, `artifacts.py`, `usage.py`,
  `quota.py`, `fleet/pool.py`, `fleet/router.py`, `fleet/envelope.py`, and
  `codex/`.
- `Makefile` — `test`, `lint`, `typecheck` and `codex-schema` targets that use
  `.venv/bin/...` so they work without an activated venv.

### Verification

```
.venv/bin/pytest -q
.venv/bin/ruff check backend
.venv/bin/python -c "from backend.app.main import create_app; create_app()"
```

All clean. Report the final test count.

---

## Task 13 — Persistence cost: get blocking I/O off the loop and stop the quadratic writes

**Reliability work, from the audit.** Not in the original roadmap; added after an
audit of long-running sessions. Execution order puts this, Task 14 and Task 15
immediately after Task 1, because Tasks 3, 4 and 7 build directly on the files
they repair.

### The defects this task fixes

Each is evidenced; do not re-derive them, but do verify each one still holds
before you change the code, and say so in your report if one does not.

- **D13.1 — blocking I/O on the event loop.** `backend/app/storage/sessions.py`
  declares every function `async def` and then does plain `open()`,
  `json.load()`, `os.fsync()` and `shutil.rmtree()` inline, with no
  `asyncio.to_thread` anywhere in the module. `append_message` flushes and
  fsyncs at `sessions.py:386-390`. One event loop serves every session, so this
  stalls all sessions, not just the writing one. **Measured at 0.04 ms average,
  0.14 ms worst on this machine's SSD**, so it is a latent risk rather than a
  live stall — fix it because it is wrong in kind and becomes a freeze on a
  network filesystem, not because it hurts today. Do not inflate the claim.
- **D13.2 — checkpoint writes are quadratic in bytes.** `TurnAccumulator.checkpoint`
  calls `_snapshot()` (`accumulator.py:67-99`), which returns *every block
  accumulated so far*, and `append_message` writes that whole growing snapshot
  as a new JSONL line. A 200-tool-call turn ending at 5 MB writes roughly
  500 MB across 200 fsynced appends. This is the real performance defect.
- **D13.3 — `messages.jsonl` never shrinks for an active session.**
  `_compact_messages` (`sessions.py:556-587`) is reachable only from
  `cleanup_expired` (`sessions.py:469-528`), which runs at lifespan startup
  behind a 24-hour sentinel and a retention window. A long-lived backend never
  compacts a session that is in use.
- **D13.4 — one page of messages costs a full-file scan.** `list_messages`
  (`sessions.py:395-465`) reads every line and materializes a `latest` dict of
  every unique message id ever seen, then sorts and slices. Hundreds of MB of
  RAM and a multi-second loop stall on a large session.
- **D13.5 — `GET /api/sessions` reads every session's `meta.json`.**
  `list_sessions` (`sessions.py:278-300`) does one synchronous open+parse per
  session inline on the loop.
- **D13.6 — `load_fleet_config` parses YAML on the loop** on a cache miss
  (`fleet/loader.py:92-109`). Low severity; the mtime cache is effective.

### Files

- `backend/app/storage/sessions.py`
- `backend/app/session_runner/accumulator.py`
- `backend/app/orchestrator/fleet/loader.py`
- `backend/app/config.py` (checkpoint throttle settings)
- `backend/tests/test_storage_cost.py` (new)
- `backend/tests/test_storage_offload.py` (new)
- `docs/storage.md` (it documents the current on-disk format — update it)

### The design

**The in-progress message moves out of the append-only log.** This one change
removes D13.2, D13.3 and D13.4 together, which is why it is worth more than
three separate patches.

Today a turn appends a full snapshot of the in-progress assistant message to
`messages.jsonl` on every tool boundary, and readers dedupe by id, keeping the
last line per id. Instead:

- The in-progress assistant message is written to
  `<session_dir>/current.json` by atomic replace (`tmp` + `os.replace`). Each
  checkpoint overwrites it. The file never grows beyond one message.
- When the turn finalizes, the message is appended to `messages.jsonl` **once**,
  and `current.json` is removed.
- Readers: `list_messages` reads `messages.jsonl` plus `current.json` when it
  exists, treating the latter as the newest message. Crash recovery is
  preserved — a `current.json` left behind by a killed backend is still a
  coherent message and is promoted into the log on the next read or on the next
  append.
- Because each message now appears in the log exactly once, **the dedupe is
  gone**: `list_messages` can read the tail it needs instead of the whole file.
  Implement a bounded reverse read (read the last N KiB, split on newlines,
  parse the last `limit` entries, widen the window if it did not yield enough).
  Keep the existing `before` / `limit` / `has_more` / `next_before` contract
  byte-for-byte — the frontend pages on it.

**fsync discipline.** Mid-turn checkpoints do not fsync; the final write of a
message does. A checkpoint exists so a crash does not lose everything, not so a
crash loses nothing, and the accumulator's own comment already identifies this
path as the hot one.

**Checkpoint throttle.** Add to `config.py`:

```python
    # A checkpoint exists so a crash does not lose the whole turn. One per tool
    # boundary is more often than that needs, and on a tool-heavy turn the
    # writes dominate. Throttle by both time and growth; a final checkpoint
    # always writes regardless.
    checkpoint_min_interval_s: float = 2.0
    checkpoint_min_growth_bytes: int = 64 * 1024
```

`TurnAccumulator.checkpoint(session_id, final=False)` skips the write when
neither threshold is met and `final` is false. `final=True` always writes and
always fsyncs. Track the last write's timestamp and serialized size on the
accumulator.

**Thread offload.** Every filesystem operation in `sessions.py` moves behind
`await asyncio.to_thread(...)`. Do this by extracting the synchronous body of
each function into a private `_sync_*` helper and awaiting it — do not sprinkle
`to_thread` over individual `open()` calls, which would multiply the hop count.
Keep the existing per-session lock semantics; the lock is held across the
offload, which is correct and is what serializes writers.
`load_fleet_config`'s parse moves behind `to_thread` on the cache-miss path
only; the cache-hit `stat()` stays inline (one syscall, measured negligible).

**`GET /api/sessions` stops reading every `meta.json`.** The index already
mirrors `updated_at`. Widen the index entry to carry the fields
`SessionOut` needs — `title`, `provider`, `model`, `cwd`, `created_at`,
`updated_at` — written on create and on update, and fall back to reading
`meta.json` only for an index entry that predates the widening (so existing
installs keep working). `meta.json` remains the source of truth on disk.

### Tests

`test_storage_cost.py` — these are the regression net for the defects:

- a simulated turn of 200 checkpoints with a message growing to ~2 MB writes
  **less than 3×** the final message size in total bytes to
  `messages.jsonl` + `current.json`. Assert on real measured bytes (sum of file
  sizes plus a wrapper that counts bytes written), not on call counts. This test
  fails loudly on today's code — record that as your RED evidence.
- `messages.jsonl` contains exactly one line per finalized message after 20
  turns, and `current.json` does not exist once a turn has finalized.
- a `current.json` left on disk (write one by hand, no finalize) is returned by
  `list_messages` as the newest message, and is promoted into the log exactly
  once on the next append.
- the checkpoint throttle: 50 rapid checkpoints within the interval produce one
  write; a `final=True` checkpoint always writes.
- `list_messages` paging contract is unchanged: page size, ordering,
  `next_before`, `has_more`, and the `before` filter all behave as before
  (write the assertions against the current behaviour first, then refactor).
- `list_messages` on a session with 500 messages does not read the whole file:
  assert via a counting wrapper around the reader that bytes read are bounded,
  not via timing.
- `list_sessions` with 50 sessions performs **zero** `meta.json` reads when the
  index is current, and still returns complete `SessionOut` data; an index
  entry missing the new fields still resolves by falling back to `meta.json`.

`test_storage_offload.py`:

- every public coroutine in `sessions.py` that touches the filesystem does its
  work in a thread. Test it behaviourally with an **event-loop stall detector**:
  run the operation while a watchdog coroutine samples `loop.time()` every 5 ms,
  with a patched filesystem call that sleeps 200 ms synchronously; assert the
  watchdog's worst observed gap stays under 50 ms. A stall detector that would
  pass on today's code is not a test — verify it fails before the fix.
- put the detector in `backend/tests/fakes/stall_detector.py` as a reusable
  async context manager; Task 16 uses it too.

### Verification

```
.venv/bin/pytest backend/tests -q
.venv/bin/ruff check backend
.venv/bin/python -c "from backend.app.main import create_app; create_app()"
```

Plus: report the measured total-bytes-written figure before and after your
change for the 200-checkpoint case. That number is the point of the task.

---

## Task 14 — Turn lifecycle: shutdown, orphans, and the turn that never ends

**Reliability work, from the audit.** Four independent defects that all live in
the turn's lifecycle.

### The defects this task fixes

- **D14.1 — shutdown leaks `claude` processes.** `main.py:44-45` awaits only
  `shutdown_all()`. `drop_all_runners()`
  (`session_runner/registry.py:40-46`) is wired solely to
  `DELETE /api/sessions`. On SIGINT the detached turn task is never cancelled,
  so `_run_step_with_role`'s `finally` (`fleet/provider.py:345-352`) never runs
  and `handle.kill()` never fires. The child is spawned without
  `start_new_session`, so it is re-parented and survives: an orphan
  `python` + `claude` pair per in-flight step after every restart, consuming
  the user's subscription with no UI attached.
- **D14.2 — every fleet turn emits `assistant.done` twice, and the second wipes
  the cost.** `orchestrator.py:391-399` yields it with `cost_usd` on
  `ResultMessage`; `fleet/provider.py:193-196` then yields an unconditional
  second one carrying only `duration_ms`. `turn.py` calls `acc.set_done` for
  each, so the final checkpoint persists `cost_usd: null`. Fleet turns
  therefore always show no cost in history.
- **D14.3 — a prompt on a deleted session spins the UI forever.** In
  `turn.py`, `append_message` at line 47 sits *outside* the `try` that begins at
  line 77, and raises `FileNotFoundError` once the session directory is gone. No
  `error` and no `assistant.done` is broadcast, so the working indicator never
  clears; the exception surfaces only as "Task exception was never retrieved" at
  GC. Reachable from both `DELETE /api/sessions/{id}` and the wipe-all route
  while a tab is open.
- **D14.4 — a detached turn can run beside a new turn for the same session.**
  `registry.py:34-37` pops the runner *before* `cancel_turn`, which gives up
  after `_CANCEL_GRACE_S = 5` and returns. A later WS connect calls
  `get_runner` and builds a brand-new `SessionRunner` with a fresh lock and
  bus, so two turns run for one session id. The audit confirmed this cannot
  corrupt the session directory (`append_message` raises and
  `accumulator.py:102-106` swallows it), so the damage is orphan processes and
  events delivered to a dead bus.
- **D14.5 — minors.** `cancel_turn` swallows `asyncio.CancelledError`
  (`runner.py:126`), so a cancelled `DELETE` handler still proceeds to rmtree.
  `dispatch.py:383`'s `_step_counters` grows one key per role forever.

### Files

- `backend/app/main.py`
- `backend/app/session_runner/registry.py`
- `backend/app/session_runner/runner.py`
- `backend/app/session_runner/turn.py`
- `backend/app/orchestrator/fleet/provider.py`
- `backend/app/orchestrator/dispatch.py`
- `backend/tests/test_lifecycle.py` (new)

### The changes

**D14.1.** In the lifespan shutdown path, `await drop_all_runners()` *before*
`shutdown_all()`, so turns are cancelled while the loop is still running and
each step's `finally` gets to kill its child. Then make the kill actually
reach the whole tree: spawn the worker with `start_new_session=True` and, in
`kill()`, `os.killpg(os.getpgid(pid), SIGKILL)` with a `ProcessLookupError`
guard, falling back to `proc.kill()`. A `claude` CLI spawned by the worker is a
grandchild — killing only the worker leaves it running, which is the actual
leak. Note in a comment that this is why the process group matters.

**D14.2.** `FleetProvider.run` must not emit a second terminal event. Track
whether the orchestrator already yielded `assistant.done`; if it did, do not
yield another — instead merge `duration_ms` into the one the orchestrator
yields, by having `_run_orchestrated` pass the turn's start time down or by
buffering the orchestrator's done event and re-emitting it once with
`duration_ms` added. Prefer whichever keeps `run()` readable. If no done was
seen (an error path), yield one, as today. Exactly one `assistant.done` per
turn is the invariant; assert it in a test.

**D14.3.** Move the user-message append inside the `try`, and make the
`except`/`finally` path that already exists cover it, so a failure still
broadcasts `error` and `assistant.done`. Guard the specific case too: if the
session no longer exists, broadcast a clear `error` saying the session was
deleted, then `assistant.done`, and return without raising.

**D14.4.** Give each runner an epoch and retire it explicitly. `drop_runner`
marks the runner retired *before* popping it; `SessionRunner` refuses to start a
turn when retired; and `get_runner` does not resurrect a session whose
directory no longer exists (raise or return None so the WS can report "session
not found" as it already does for an unknown id). Keep the bounded
`_CANCEL_GRACE_S` detach — it exists so `DELETE` cannot hang — but record the
detached task so shutdown can make one final attempt, and log at WARNING with
the session id when a turn is detached, because that log line is the only
evidence an orphan may exist.

**D14.5.** Re-raise `CancelledError` in `cancel_turn` after attempting the
cancel. Bound `_step_counters` (reset it per turn, or key it on the turn and
drop it at the end — `reset_step_counters` already exists; call it).

### Tests

`test_lifecycle.py`, each asserting on observable behaviour:

- **exactly one `assistant.done`** per fleet turn: drive `FleetProvider.run`
  with a stubbed orchestrator that yields a done carrying `cost_usd`, and assert
  the event stream contains one done and that it carries both `cost_usd` and
  `duration_ms`.
- a turn whose session directory has been removed broadcasts an `error`
  mentioning the deleted session **and** an `assistant.done`, and does not
  raise out of the task. Assert against a recording bus.
- `drop_all_runners` is called on lifespan shutdown: drive the app's lifespan
  (FastAPI's `TestClient` or the lifespan context directly) with a registered
  runner and assert its turn was cancelled.
- the process group kill: spawn a fake worker script that itself spawns a
  sleeping grandchild and prints both pids, call the kill path, then assert both
  pids are gone (`os.kill(pid, 0)` raises `ProcessLookupError`). This is the
  test that proves the orphan is actually reaped; without the grandchild it
  proves nothing.
- a retired runner refuses `start_turn`; `get_runner` does not resurrect a
  deleted session.
- `cancel_turn` propagates `CancelledError` to its caller.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`, app
imports. Additionally: state in your report how you verified no orphan process
survives, with the command and its output.

---

## Task 15 — Event delivery integrity: never lose an event silently

**Reliability work, from the audit.** Three audits independently reached the
same conclusion from different angles, which is why this is its own task.

### The defects this task fixes

- **D15.1 — a slow subscriber loses events with no signal.** `bus.py:79-87`
  does `put_nowait` and, on `QueueFull`, logs a warning and drops. The browser
  is never told. A dropped `assistant.tool_use` leaves a card that never
  appears; a dropped `assistant.done` leaves the working indicator spinning
  until the user refetches.
- **D15.2 — the replay ring is smaller than the subscriber queue**, so a drop
  is unrecoverable. `SUBSCRIBER_QUEUE_MAX = 512` against
  `REPLAY_BUFFER_SIZE = 256` (`session_runner/config.py:11,17`). By the time a
  viewer has dropped one event, `?since=` can no longer cover the gap.
- **D15.3 — a reconnecting tab can never answer a pending approval.** The
  `pipeline.awaiting_approval` card exists only in the bus ring, and replay
  happens only when `?since=` is supplied (`routes/sessions.py:205-210`). A
  user who closes the only tab and reopens it fresh waits out
  `APPROVAL_TIMEOUT_S` (300 s) and the turn aborts. Bounded, but a
  five-minute dead end for no reason.
- **D15.4 — the ring trim is O(buffer) per event** past the cap, because the
  slice re-triggers on every append (`bus.py:73-76`). Measured at 0.48 µs per
  event, so this is a shape fix, not a performance fix: use
  `collections.deque(maxlen=...)`.

### Files

- `backend/app/session_runner/bus.py`
- `backend/app/session_runner/config.py`
- `backend/app/session_runner/runner.py` (track the outstanding approval)
- `backend/app/routes/sessions.py` (re-emit it on connect)
- `backend/app/orchestrator/base.py`, `backend/app/schemas.py`,
  `frontend/src/types.ts` (the new `stream.gap` event)
- `frontend/src/components/ChatPane.tsx` (react to a gap)
- `backend/tests/test_event_integrity.py` (new)

### The changes

**Sizes.** `REPLAY_BUFFER_SIZE` becomes larger than `SUBSCRIBER_QUEUE_MAX`
(2048 against 512), with a comment stating the invariant: *the ring must be
able to cover any gap a subscriber queue can open, or `?since=` is a lie.*
Assert the relationship in a test so a future edit cannot quietly invert it.

**Terminal events are never dropped.** `assistant.done` and `error` bypass the
queue cap — if `put_nowait` would raise, drop the *oldest* queued event instead
and enqueue the terminal one. A UI that never clears its spinner is the worst
failure mode in this system; losing an intermediate delta is not.

**Gap signalling.** A new event type `stream.gap` with data
`{"dropped": <n>, "resume_from": <last delivered _id>}`. When a subscriber's
queue overflows, count the drops against that subscriber and enqueue one
`stream.gap` as soon as there is room (coalesce: one gap event per contiguous
run of drops, carrying the total). The frontend treats it as "refetch
`/messages` for this session", which it already knows how to do.

**Pending approval survives a reconnect.** `SessionRunner` keeps the
currently-outstanding `pipeline.awaiting_approval` payload (set when it passes
through the bus, cleared on `pipeline.approval_received` or turn end). The WS
handler re-emits it to a newly-subscribed viewer when one is outstanding, after
any `?since=` replay and before live events. De-duplicate: if the replay already
contained it, do not send it twice — compare the approval id.

**Ring shape.** `collections.deque(maxlen=REPLAY_BUFFER_SIZE)`.

### Tests

- the size invariant: `REPLAY_BUFFER_SIZE > SUBSCRIBER_QUEUE_MAX`.
- a subscriber that never drains: broadcast `SUBSCRIBER_QUEUE_MAX + 50` events,
  then drain, and assert the subscriber sees exactly one `stream.gap` carrying
  `dropped == 50`, positioned where the loss began, and that no event is
  silently missing otherwise.
- `assistant.done` is delivered even to a full queue, and the event it displaced
  was the oldest.
- two contiguous overflow runs separated by successful deliveries produce two
  gap events, not one and not fifty.
- replay covers a gap: disconnect a subscriber, broadcast 600 events, resubscribe
  with `since=<id>`, and assert the replay contains the tail and no gap is
  reported (this is what the size change buys).
- pending approval: a turn emits an approval card, the subscriber unsubscribes,
  a fresh subscriber with no `since` receives the card exactly once; after
  `pipeline.approval_received` a fresh subscriber receives nothing.
- ordering is still monotonic in `_id` after all of the above.

### Verification

`.venv/bin/pytest backend/tests -q`, `.venv/bin/ruff check backend`, frontend
builds if installable. Report the gap-event behaviour you observed for the
overflow test, with numbers.

---

## Task 16 — Soak, latency and leak verification

**The verification layer the audit's findings demand.** Tasks 13–15 fix what the
audit found; this task is what catches the next one. It runs last, after the
eval layers of Task 12, because it reuses their fakes.

This is not a second copy of Task 12. Task 12 asserts *behaviour* (the right
events in the right order). Task 16 asserts *cost and containment* — how long,
how much memory, how many bytes, how many processes, and what happens when
things break.

### Files

- `backend/tests/fakes/stall_detector.py` (created by Task 13; reuse it)
- `backend/tests/fakes/load.py` (new — scriptable load generators)
- `backend/tests/test_soak_long_session.py` (new)
- `backend/tests/test_latency_budgets.py` (new)
- `backend/tests/test_leak_containment.py` (new)
- `backend/tests/test_failure_injection.py` (new)
- `pyproject.toml` (a `slow` marker), `Makefile` (a `soak` target)
- `docs/harness.md` (document how to run it and how to read a failure)

### Marker policy

The stall detector and the failure injections are fast — they run in the default
suite, because a reliability test nobody runs is decoration. The long soak is
marked `@pytest.mark.slow` and excluded from the default run via `addopts`,
with `make soak` running it. Say in `docs/harness.md` which is which and why.

### `test_soak_long_session.py` (marked `slow`)

One session, a fake provider, a temp `HOME`, **200 turns of 50 events each**
(10 000 events, including tool_use/tool_result pairs and one 256 KiB tool
result every 20th turn). Assert, with real measurements:

- **disk**: total bytes under the session directory stay within a small
  constant multiple (≤ 3×) of the total content bytes the fake produced. This is
  the permanent guard against Task 13's quadratic regression.
- **memory**: peak RSS growth over the soak stays under a stated budget. Use
  `tracemalloc` for Python-object growth (portable, no new dependency) and
  `resource.getrusage(RUSAGE_SELF).ru_maxrss` as a coarse cross-check. State the
  budget as a constant with a comment explaining how it was chosen; a budget
  nobody can justify will be raised the first time it fails.
- **tasks**: `len(asyncio.all_tasks())` at the end equals the start, within a
  small tolerance, proving no per-turn task leak.
- **file descriptors**: the count of open fds (`len(os.listdir('/dev/fd'))` on
  macOS/Linux) does not grow across turns.
- **paging stays cheap**: after all 200 turns, `GET /messages` for the first
  page completes within a stated wall-clock budget and reads a bounded number
  of bytes.
- the final session state is coherent: every `tool_use` has a matching
  `tool_result`, message count equals the number of turns × 2, and ordering is
  monotonic.

Print the measured numbers on success (not only on failure) so a human reading
CI output can see the trend. A soak test whose numbers are invisible until it
breaks teaches nobody anything.

### `test_latency_budgets.py`

Fast, deterministic, no real CLI:

- **no event-loop stall during a turn**: run a 2000-event turn with the stall
  detector active and assert the worst loop gap stays under 50 ms.
- **bus throughput**: `broadcast` 10 000 events to 3 subscribers within a
  stated budget, asserting on throughput rather than absolute time where the
  machine may vary (compute events/second and compare against a floor).
- **checkpoint cost is bounded per event**: a turn with 500 tool boundaries
  performs at most the number of writes the throttle allows, and the total
  bytes written stay within the Task 13 bound.
- **first-event latency**: time from `provider.run()` being driven to the first
  event reaching a subscriber stays under a stated budget.
- Every budget is a named module constant with a comment explaining what it
  protects and how it was derived from the audit's measurements, e.g. the 50 ms
  stall budget against a measured 0.04 ms fsync — so a future reader knows
  whether a failure means a regression or a slow machine.

### `test_leak_containment.py`

- after a fleet turn using the Task 7 worker pool, no child process remains:
  track the pids the pool spawned and assert each is gone after `aclose()`.
- after a turn **cancelled mid-step**, the same holds — this is the path D14.1
  leaked on.
- a worker that spawns a grandchild has the whole group reaped (the Task 14
  process-group behaviour, asserted here against the real pool rather than a
  fake).
- the pool honours `fleet_max_workers` under concurrent submits.
- 50 sequential sessions created and deleted leave no runner, no bus and no task
  behind.

### `test_failure_injection.py`

Every failure mode the audit named, as a test. Each asserts the user-visible
outcome — a clear `error` event and a terminal `assistant.done`, never a hang
and never a silent success:

- a sub-agent result **larger than 64 KiB on one line** (the D-B2 defect Task 7
  fixes) round-trips intact.
- a worker that exits 0 without emitting a result produces a diagnostic naming
  the exit code and the stderr tail.
- a worker killed mid-request fails its pending future, and the step surfaces an
  error rather than hanging.
- a provider that yields **nothing at all** trips the startup-grace fast-fail
  within the grace window, with the honest "no response yet" wording, and does
  not wait the full step budget.
- a provider that streams forever hits the step ceiling and aborts.
- a repeatedly-failing sub-agent is bounded: assert the orchestrator cannot
  dispatch it more than the hard-fail cap allows, **for every failure class**,
  not only for timeouts. This is the regression test for the unbounded-retry
  chain the audit found.
- the WebSocket disconnects mid-turn: the turn keeps running, and a reconnect
  with `?since=` receives the tail.
- the session is deleted mid-turn: the turn terminates without corrupting state
  and without an unretrieved task exception.
- an approval that nobody answers times out and the turn ends cleanly.

### Verification

```
.venv/bin/pytest -q                 # fast tests, including injections
.venv/bin/pytest -q -m slow         # the soak
.venv/bin/ruff check backend
```

Report every measured number the soak and latency suites print, and state
explicitly which budgets you chose versus which came from the audit.

---

## Out of scope, with reasons

- **Phase 6 (ACP).** The spec marks it optional. Reshaping the `Event` union to
  ACP semantics touches the backend, the persisted message format, the React
  frontend and the VS Code extension, and buys a user nothing today; shipping
  an ACP server is a separate project. `docs/harness.md` records the decision
  and what reshaping would involve, so the option stays open.
- **Deleting the OpenCode provider.** The spec demotes it from the ChatGPT
  path; it does not remove it. It stays as the documented third provider for
  local and non-frontier models.
- **Flipping the default coder to `codex`.** The binary is absent here. Task 10
  documents the one-line switch.
