# Eval Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A governor-paced, headless SWE-bench Verified runner that turns "is LocalCode better at tasks than last week" into one row in `evals/HISTORY.md`.

**Architecture:** A new `backend/app/eval/` package drives the existing `session_runner.turn.execute_turn` with a real provider over a temp git worktree per instance (the agent runs on the host through the vendor binaries; nothing changes in the invariant), captures `git diff` as the prediction, and hands scoring to SWE-bench's own harness in Docker (`swebench eval verified --task-repo …`, amd64 images run under emulation on this Mac). The quota governor paces instances; every outcome is a recorded enum; runs are resumable.

**Tech Stack:** Python 3.11, asyncio, the existing LocalCode backend (`session_runner`, `orchestrator.registry`, `quota`, `storage.sessions`), `swebench==5.0.2` (dev extra), git plumbing via `subprocess`, Docker Desktop (scoring only), pytest with the existing fakes.

**Spec:** `docs/superpowers/specs/2026-09-14-eval-foundation-design.md` — the plan argues from it; read both.

## Global Constraints

- Python 3.11. Every new module starts with `from __future__ import annotations`. Line length 100 (`[tool.ruff]`); `.venv/bin/ruff check backend` reports zero errors after every task.
- House style: module docstring explaining *why*; dataclasses over dicts for structured data; comments that record the failure the code prevents. No comments that restate the code.
- THE INVARIANT (non-negotiable): never read a credential store (`~/.claude/.credentials.json`, `auth.json`, `~/.codex/auth.json`, the macOS keychain) and never assign `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or any `*_API_KEY` / `*_OAUTH_TOKEN` / `*_SESSION_KEY` from a file, a config value, or another environment variable. The eval runner has NO API-key exception. `backend/tests/test_auth_invariant.py` scans `backend/app` recursively; `backend/app/eval/` is in scope.
- The only new dependency is `swebench==5.0.2`, declared as the `eval` dev extra in `pyproject.toml`. Nothing else is added.
- Tests live under `backend/tests/`, `asyncio_mode = "auto"`. Default-suite tests need no Docker, no vendor CLI, no network — fakes and injected seams only. Docker-backed tests carry `@pytest.mark.slow`; real-CLI tests carry `@pytest.mark.requires_cli`. Every test asserts on behaviour; a test whose only assertion is that a call did not raise is a defect.
- `conftest.py` redirects `HOME` for every test (autouse `_redirect_home`); all eval paths derive from `Path.home() / ".localcode" / "eval"` at call time, never at import time, so tests land under the redirected home.
- Constants named in the spec are used verbatim: `EVAL_HEADROOM_FLOOR = 0.15`, `EVAL_TURN_TIMEOUT_S = 1200.0`, `EVAL_TURN_TOKEN_BUDGET = 400_000`, `EVAL_TURN_MAX_TOOL_CALLS = 200`, `EVAL_MAX_WAIT_S = 6 * 3600.0`, subset size `50`, seed `20260914`, dataset `SWE-bench/SWE-bench_Verified`, split `test`, task repo `SWE-bench/swe-bench-tasks`, `PROMPT_VERSION = 1`.
- Outcome vocabulary (exact strings): `patched`, `no_patch`, `budget_wall_clock`, `budget_tokens`, `budget_tool_calls`, `provider_error`, `checkout_error`, `harness_error`; scoring adds `resolved`, `unresolved`, `eval_error`.
- Commits: `Eval N: <what is now true>` plus a short why, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Never commit `evals/results/`, `~/.localcode/eval/`, `.run/`, `.venv/`.
- macOS has no `timeout`; bound long commands with `perl -e 'alarm N; exec @ARGV' …`.

## Pre-flight notes (measured on the tree at 1c547bb)

- `session_runner.turn.execute_turn(*, session_id, bus, approval_q, provider, provider_name, model, cwd, additional_dirs, upstream_id, fleet_override, permission_mode, prompt) -> None`. The pattern for driving it with a fake is `backend/tests/test_long_horizon.py:70-100`.
- `EventBus(session_id, on_event=callback)` — `on_event` sees every event dict `{"type", "data", "id"?}` as it is broadcast. `Subscription.queue` is the subscriber queue.
- `storage.sessions.create_session(provider=, model=, cwd=, additional_dirs=, title=, permission_mode=, fleet_config_override=)` (async) returns the meta dict with `"id"`.
- `orchestrator.registry.get_provider(name)` (async) for `"claude"`, `"codex"`, `"opencode"`, `"fleet"`. The fleet provider reads the per-session `fleet_config_override` that `execute_turn` threads through `extras`.
- Approval card: `Event(type="pipeline.awaiting_approval", data={"id", "kind": "tool", "tool", "input": <rendered string, paths survive whole>, "timeout_s", …})`. Answer: `await approval_q.put({"id": card_id, "value": "yes"})` allows; any other `value` (use `"no"`) denies; `"feedback"` optional. `pipeline.approval_received` follows.
- Events counted by the budget: `assistant.tool_use` (tool calls), `assistant.done` with `data["usage"]` (token dict: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, optional `cost_usd` at `data["cost_usd"]`), `error`.
- `quota.get_governor()` → `Governor` with `headroom(provider) -> float`, `confidence(provider) -> str` (`"unknown"` when unmeasured), `resets_at(provider) -> float | None`, `QUEUE_THRESHOLD = 0.05`. `quota.governed_providers()` lists the names.
- `SessionRunner._CANCEL_GRACE_S = 5.0`; cancellation = `task.cancel()` then `asyncio.wait({task}, timeout=grace)` (`runner.py:212-256`). The headless runner reuses that shape.
- `swebench` 5.0.2: instances are `SWEbenchInstance` dicts with keys `instance_id, repo, base_commit, patch, test_patch, problem_statement, hints_text, created_at, version, FAIL_TO_PASS, PASS_TO_PASS, environment_setup_commit, image, eval_type, log_parser, eval_script, difficulty`. `swebench.harness.utils.load_swebench_dataset(name, split, instance_ids) -> list[dict]` (HuggingFace, cached). CLI: `swebench eval verified -p <predictions.jsonl> --run-id <id> -j <workers> --task-repo <path-or-ref> [-i <ids…>] [--gold]`; results at `./logs/evaluation/<run_id>/results.json` (schema_version 2: `resolved_ids, unresolved_ids, error_ids, empty_patch_ids, infra_failure_ids, failure_reasons, completed_ids, incomplete_ids, submitted_ids`); per-instance `./logs/evaluation/<run_id>/<model_name_or_path>/<instance_id>/report.json` keyed by instance id with `resolved, patch_is_None, patch_exists, patch_successfully_applied, tests_status`. Predictions: JSONL with `instance_id, model_name_or_path, model_patch`. With `--task-repo` the harness builds each image locally (amd64, Buildx) and **silently pulls the published x86_64 image if the build fails**; `docker image inspect <image> --format '{{.Architecture}}'` tells which was used. The task repo is `https://github.com/SWE-bench/swe-bench-tasks.git`.
- Docker Desktop here: 10 CPUs, ~7.7 GiB allocated (raise to ≥ 12 GiB in Docker Desktop → Resources before the probe), amd64 emulation works.

## File structure

| Path | Responsibility |
|---|---|
| `backend/app/eval/__init__.py` | package docstring only |
| `backend/app/eval/config.py` | `EvalConfig` (named cells), budget constants, `PROMPT_VERSION`, path helpers |
| `backend/app/eval/preflight.py` | `preflight(checks)` with injectable probes; refuses/warns per spec §4 |
| `backend/app/eval/headless.py` | `run_headless_turn`, `TurnRecord`, `Budget`, the approval policy reader |
| `backend/app/eval/instances.py` | subset file load/validate, dataset rows (cached), `test_files_from_patch` |
| `backend/app/eval/checkout.py` | mirror, worktree at `base_commit`, `capture_patch`, cleanup |
| `backend/app/eval/pacing.py` | `GovernorGate.wait_for_headroom` |
| `backend/app/eval/runner.py` | the generate phase: records, `predictions.jsonl`, resume, dirty-tree refusal, SIGINT |
| `backend/app/eval/score.py` | `swebench eval` subprocess, `results.json` + `report.json` parsing, image arch |
| `backend/app/eval/report.py` | result JSON, `HISTORY.md` row |
| `backend/app/eval/probe.py` | subset selection by gold-patch probe |
| `backend/app/eval/__main__.py` | `python -m backend.app.eval {generate,score,run,probe}` |
| `evals/.gitignore`, `evals/HISTORY.md`, `evals/swebench_verified_subset.json` | committed outputs |
| `Makefile` | `eval`, `eval-score`, `eval-probe`, `eval-selftest` |
| `docs/harness.md` | §12 "The eval foundation" |
| `backend/tests/test_eval_*.py`, `backend/tests/fixtures/swebench/` | tests and hand-captured harness fixtures |

---

### Task 1: Package skeleton, config, dev extra, pre-flight

**Files:**
- Create: `backend/app/eval/__init__.py`, `backend/app/eval/config.py`, `backend/app/eval/preflight.py`
- Modify: `pyproject.toml` (dev extra), `Makefile` (targets, wired in Task 10 — only the `eval-deps` helper here)
- Test: `backend/tests/test_eval_config.py`, `backend/tests/test_eval_preflight.py`

**Interfaces:**
- Produces: `EvalConfig(name, provider_name, model, fleet_override) ` with `EvalConfig.named(name) -> EvalConfig`; constants `EVAL_HEADROOM_FLOOR`, `EVAL_TURN_TIMEOUT_S`, `EVAL_TURN_TOKEN_BUDGET`, `EVAL_TURN_MAX_TOOL_CALLS`, `EVAL_MAX_WAIT_S`, `PROMPT_VERSION`, `DATASET_NAME`, `DATASET_SPLIT`, `TASK_REPO_URL`; `eval_root() -> Path` (`~/.localcode/eval`); `PreflightReport(ok, refusals, warnings)`, `preflight(*, probes: Probes) -> PreflightReport`, `Probes` dataclass of callables.

- [ ] **Step 1: Write the failing config test**

```python
# backend/tests/test_eval_config.py
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.eval import config


def test_named_configs_are_the_matrix_cells():
    cell = config.EvalConfig.named("claude-single")
    assert cell.provider_name == "claude"
    assert cell.fleet_override is None
    fleet = config.EvalConfig.named("claude-fleet")
    assert fleet.provider_name == "fleet"
    assert fleet.fleet_override is not None
    assert "claude" in fleet.governed_providers()
    with pytest.raises(KeyError):
        config.EvalConfig.named("nope")


def test_eval_root_is_under_the_redirected_home(tmp_path: Path):
    root = config.eval_root()
    assert root == Path.home() / ".localcode" / "eval"
    assert str(root).startswith(str(tmp_path))


def test_spec_constants_are_verbatim():
    assert config.EVAL_HEADROOM_FLOOR == 0.15
    assert config.EVAL_TURN_TIMEOUT_S == 1200.0
    assert config.EVAL_TURN_TOKEN_BUDGET == 400_000
    assert config.EVAL_TURN_MAX_TOOL_CALLS == 200
    assert config.EVAL_MAX_WAIT_S == 6 * 3600.0
    assert config.PROMPT_VERSION == 1
    assert config.DATASET_NAME == "SWE-bench/SWE-bench_Verified"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest backend/tests/test_eval_config.py -q`
Expected: FAIL — `ModuleNotFoundError: backend.app.eval`

- [ ] **Step 3: Write `config.py` and the package init**

```python
# backend/app/eval/__init__.py
"""The eval foundation: a headless, governor-paced SWE-bench Verified runner.

The agent runs on the host through the vendor binaries exactly as in normal
use — the auth invariant is untouched — and only scoring runs in Docker via
SWE-bench's own harness. See docs/superpowers/specs/2026-09-14-eval-foundation-design.md.
"""
```

```python
# backend/app/eval/config.py
"""Named configurations and the spec's constants.

Why a table of named cells: the number in HISTORY.md is only comparable if
"claude-fleet" means the same provider, model and fleet shape every time it
is run. Free-form flags would let two runs with the same label differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..quota import governed_providers

EVAL_HEADROOM_FLOOR = 0.15
EVAL_TURN_TIMEOUT_S = 1200.0
EVAL_TURN_TOKEN_BUDGET = 400_000
EVAL_TURN_MAX_TOOL_CALLS = 200
EVAL_MAX_WAIT_S = 6 * 3600.0
PROMPT_VERSION = 1
DATASET_NAME = "SWE-bench/SWE-bench_Verified"
DATASET_SPLIT = "test"
TASK_REPO_URL = "https://github.com/SWE-bench/swe-bench-tasks.git"
SUBSET_SIZE = 50
SUBSET_SEED = 20260914


def eval_root() -> Path:
    """``~/.localcode/eval`` resolved at call time — tests redirect HOME."""
    return Path.home() / ".localcode" / "eval"


def _default_model(provider: str) -> str:
    for entry in get_settings().catalog():
        if entry.provider == provider:
            return entry.model
    raise KeyError(f"no catalog model for provider {provider!r}")


@dataclass(frozen=True)
class EvalConfig:
    name: str
    provider_name: str
    model: str
    fleet_override: dict[str, Any] | None

    @classmethod
    def named(cls, name: str) -> EvalConfig:
        if name not in _CELLS:
            raise KeyError(name)
        provider, fleet = _CELLS[name]
        if fleet:
            # Every role served by the same subscription; the orchestrator
            # model is the provider's catalog default.
            override = {
                "roles": {
                    role: {"provider": provider, "model": _default_model(provider)}
                    for role in ("planner", "coder", "reviewer", "tester")
                }
            }
            return cls(name, "fleet", _default_model(provider), override)
        return cls(name, provider, _default_model(provider), None)

    def governed_providers(self) -> tuple[str, ...]:
        """The subscription(s) this cell spends — what the gate must check."""
        if self.fleet_override is None:
            return (self.provider_name,)
        roles = self.fleet_override["roles"].values()
        names = {r["provider"] for r in roles}
        return tuple(n for n in governed_providers() if n in names)


_CELLS: dict[str, tuple[str, bool]] = {
    "claude-single": ("claude", False),
    "claude-fleet": ("claude", True),
    "codex-single": ("codex", False),
    "codex-fleet": ("codex", True),
    "opencode-single": ("opencode", False),
}
```

Check the fleet override key names against `backend/app/orchestrator/fleet/loader.py` (the override shape the UI posts — `roles` keyed by role name with `provider` and `model`); if the loader's shape differs, use the loader's shape and adjust the test's assertion on `fleet_override` accordingly.

- [ ] **Step 4: Run the config test**

Run: `.venv/bin/pytest backend/tests/test_eval_config.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing pre-flight test**

```python
# backend/tests/test_eval_preflight.py
from __future__ import annotations

from backend.app.eval.preflight import Probes, preflight


def _probes(**over):
    base = dict(
        docker_up=lambda: True,
        docker_mem_gib=lambda: 16.0,
        free_disk_gib=lambda: 200.0,
        swebench_version=lambda: "5.0.2",
        binaries_ok=lambda names: [],
    )
    base.update(over)
    return Probes(**base)


def test_a_healthy_machine_passes_with_no_warnings():
    rep = preflight(providers=("claude",), probes=_probes())
    assert rep.ok and rep.refusals == [] and rep.warnings == []


def test_low_docker_memory_refuses_and_names_the_fix():
    rep = preflight(providers=("claude",), probes=_probes(docker_mem_gib=lambda: 7.7))
    assert not rep.ok
    assert any("Docker Desktop" in r and "8 GiB" in r for r in rep.refusals)


def test_middling_docker_memory_only_warns():
    rep = preflight(providers=("claude",), probes=_probes(docker_mem_gib=lambda: 10.0))
    assert rep.ok and any("12 GiB" in w for w in rep.warnings)


def test_missing_binary_refuses_by_name():
    rep = preflight(
        providers=("codex",), probes=_probes(binaries_ok=lambda names: ["codex"])
    )
    assert not rep.ok and any("codex" in r for r in rep.refusals)


def test_wrong_swebench_version_refuses():
    rep = preflight(providers=("claude",), probes=_probes(swebench_version=lambda: "4.0.0"))
    assert not rep.ok and any("5.0.2" in r for r in rep.refusals)


def test_low_disk_refuses():
    rep = preflight(providers=("claude",), probes=_probes(free_disk_gib=lambda: 30.0))
    assert not rep.ok and any("60 GiB" in r for r in rep.refusals)
```

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest backend/tests/test_eval_preflight.py -q`
Expected: FAIL — `ImportError` on `backend.app.eval.preflight`

- [ ] **Step 7: Write `preflight.py`**

```python
# backend/app/eval/preflight.py
"""Refuse early, with the fix in the message.

A benchmark that dies forty minutes in because Docker had 7 GiB is a wasted
five-hour window. Every probe is injectable so the rules are tested without
Docker; the real probes shell out.
"""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .config import eval_root

REQUIRED_SWEBENCH = "5.0.2"
DOCKER_MEM_REFUSE_GIB = 8.0
DOCKER_MEM_WARN_GIB = 12.0
DISK_REFUSE_GIB = 60.0

_BINARY_FOR_PROVIDER = {"claude": "claude", "codex": "codex", "opencode": "opencode"}


@dataclass
class Probes:
    docker_up: Callable[[], bool]
    docker_mem_gib: Callable[[], float]
    free_disk_gib: Callable[[], float]
    swebench_version: Callable[[], str | None]
    binaries_ok: Callable[[Sequence[str]], list[str]]  # returns the MISSING names


@dataclass
class PreflightReport:
    ok: bool
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def preflight(*, providers: Sequence[str], probes: Probes) -> PreflightReport:
    refusals: list[str] = []
    warnings: list[str] = []
    if not probes.docker_up():
        refusals.append("Docker daemon is not reachable; start Docker Desktop.")
    else:
        mem = probes.docker_mem_gib()
        if mem < DOCKER_MEM_REFUSE_GIB:
            refusals.append(
                f"Docker has {mem:.1f} GiB; scoring needs at least "
                f"{DOCKER_MEM_REFUSE_GIB:.0f} GiB — raise it in Docker Desktop → Resources."
            )
        elif mem < DOCKER_MEM_WARN_GIB:
            warnings.append(
                f"Docker has {mem:.1f} GiB; SWE-bench recommends 16 GB and we warn below "
                f"{DOCKER_MEM_WARN_GIB:.0f} GiB — builds may be slow or fail."
            )
    disk = probes.free_disk_gib()
    if disk < DISK_REFUSE_GIB:
        refusals.append(
            f"{disk:.0f} GiB free; images and build layers need at least {DISK_REFUSE_GIB:.0f} GiB."
        )
    version = probes.swebench_version()
    if version != REQUIRED_SWEBENCH:
        refusals.append(
            f"swebench {version or 'is not installed'}; this runner is pinned to "
            f"{REQUIRED_SWEBENCH} — `.venv/bin/pip install -e '.[eval]'`."
        )
    wanted = [_BINARY_FOR_PROVIDER[p] for p in providers if p in _BINARY_FOR_PROVIDER]
    for name in probes.binaries_ok(wanted):
        refusals.append(f"the `{name}` binary is not on PATH or not logged in.")
    return PreflightReport(ok=not refusals, refusals=refusals, warnings=warnings)


# ── real probes ────────────────────────────────────────────────────────────

def _docker_info() -> dict | None:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def real_probes() -> Probes:
    info = _docker_info()

    def docker_up() -> bool:
        return info is not None

    def docker_mem_gib() -> float:
        return float(info.get("MemTotal", 0)) / 2**30 if info else 0.0

    def free_disk_gib() -> float:
        root = eval_root()
        root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(root).free / 2**30

    def swebench_version() -> str | None:
        try:
            return importlib.metadata.version("swebench")
        except importlib.metadata.PackageNotFoundError:
            return None

    def binaries_ok(names: Sequence[str]) -> list[str]:
        # Presence on PATH only. "Logged in" is the binary's own business — we
        # never read its credential store to find out (the invariant).
        return [n for n in names if shutil.which(n) is None]

    return Probes(docker_up, docker_mem_gib, free_disk_gib, swebench_version, binaries_ok)
```

- [ ] **Step 8: Run the pre-flight tests**

Run: `.venv/bin/pytest backend/tests/test_eval_preflight.py backend/tests/test_eval_config.py -q`
Expected: PASS (9 tests)

- [ ] **Step 9: Add the dev extra and confirm the invariant scanner sees the package**

In `pyproject.toml`, under `[project.optional-dependencies]` add `eval = ["swebench==5.0.2"]` (create the table if absent; keep existing `dev` entries). Then:

Run: `.venv/bin/pip install -e '.[eval]' -q && .venv/bin/pytest backend/tests/test_auth_invariant.py -q && .venv/bin/ruff check backend`
Expected: install succeeds; invariant tests PASS; ruff clean.

- [ ] **Step 10: Commit**

```bash
git add backend/app/eval/__init__.py backend/app/eval/config.py backend/app/eval/preflight.py backend/tests/test_eval_config.py backend/tests/test_eval_preflight.py pyproject.toml
git commit -m "Eval 1: named configurations, the spec's constants, and a pre-flight that names the fix

The eval foundation starts with what can refuse early: Docker memory, disk,
the pinned swebench, the vendor binaries on PATH — probes injectable so the
rules are tested without Docker.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Headless turn — `run_headless_turn`, budgets, the approval policy

**Files:**
- Create: `backend/app/eval/headless.py`
- Test: `backend/tests/test_eval_headless.py`

**Interfaces:**
- Consumes: `execute_turn` (signature in Pre-flight notes), `EventBus(session_id, on_event=)`, `storage.sessions.create_session(...)`, `registry.get_provider(name)`, `EvalConfig`, budget constants.
- Produces: `Budget(wall_clock_s, tokens, tool_calls)`, `TurnRecord` (`outcome: str`, `events: int`, `tool_calls: int`, `denials: int`, `tokens: dict[str, int]`, `cost_usd: float | None`, `wall_clock_s: float`, `error: str | None`, `session_id: str`), `run_headless_turn(prompt, cwd, *, config: EvalConfig, budget: Budget, provider: Provider | None = None, clock=time.monotonic) -> TurnRecord`, `ApprovalPolicy(root: Path).decide(card_data) -> tuple[str, str]` (value, reason).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_headless.py
from __future__ import annotations

import asyncio
from pathlib import Path

from backend.app.eval.config import EvalConfig
from backend.app.eval.headless import ApprovalPolicy, Budget, run_headless_turn
from backend.app.orchestrator.base import Event
from backend.tests.fakes.providers import ScriptedProvider, tool_turn


def _cfg() -> EvalConfig:
    return EvalConfig("claude-single", "claude", "m", None)


def _budget(**over) -> Budget:
    base = dict(wall_clock_s=60.0, tokens=1_000_000, tool_calls=100)
    base.update(over)
    return Budget(**base)


async def test_a_scripted_turn_yields_a_record_with_sums(tmp_path: Path):
    prov = ScriptedProvider(
        [
            Event("assistant.text", {"text": "hi"}),
            Event(
                "assistant.done",
                {"usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 1,
                           "cache_creation_tokens": 0}, "cost_usd": 0.01},
            ),
        ]
    )
    rec = await run_headless_turn("fix it", str(tmp_path), config=_cfg(), budget=_budget(),
                                  provider=prov)
    assert rec.outcome == "patched" or rec.outcome == "no_patch"
    assert rec.tokens == {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 1,
                          "cache_creation_tokens": 0}
    assert rec.cost_usd == 0.01
    assert rec.events >= 2 and rec.error is None


def test_policy_approves_inside_and_denies_outside(tmp_path: Path):
    pol = ApprovalPolicy(tmp_path)
    inside = {"tool": "Edit", "input": f"file_path: {tmp_path / 'a.py'}"}
    outside = {"tool": "Edit", "input": "file_path: /etc/passwd"}
    relative = {"tool": "Bash", "input": "command: pytest tests/ -q"}
    escaping = {"tool": "Bash", "input": "command: cat ../../secret"}
    assert pol.decide(inside)[0] == "yes"
    assert pol.decide(relative)[0] == "yes"
    assert pol.decide(outside)[0] == "no"
    assert pol.decide(escaping)[0] == "no"
    assert "outside" in pol.decide(outside)[1]


async def test_tool_call_budget_ends_the_turn_and_names_the_limit(tmp_path: Path):
    calls = [tool_turn("Read", {"file_path": "x"}, "ok") for _ in range(5)]
    prov = ScriptedProvider(calls, done_usage={"input_tokens": 1, "output_tokens": 1,
                                                "cache_read_tokens": 0,
                                                "cache_creation_tokens": 0})
    rec = await run_headless_turn("go", str(tmp_path), config=_cfg(),
                                  budget=_budget(tool_calls=2), provider=prov)
    assert rec.outcome == "budget_tool_calls"
    assert rec.tool_calls >= 2


async def test_wall_clock_budget_uses_the_injected_clock(tmp_path: Path):
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    prov = ScriptedProvider([tool_turn("Read", {"file_path": "x"}, "ok") for _ in range(3)])
    rec = await run_headless_turn("go", str(tmp_path), config=_cfg(),
                                  budget=_budget(wall_clock_s=10.0), provider=prov,
                                  clock=lambda: next(ticks))
    assert rec.outcome == "budget_wall_clock"


async def test_a_provider_error_is_recorded_not_raised(tmp_path: Path):
    prov = ScriptedProvider([Event("error", {"message": "boom"})])
    rec = await run_headless_turn("go", str(tmp_path), config=_cfg(), budget=_budget(),
                                  provider=prov)
    assert rec.outcome == "provider_error" and "boom" in (rec.error or "")
```

If `ScriptedProvider` / `tool_turn` in `backend/tests/fakes/providers.py` take different constructor arguments than shown, adapt the test to their real signatures (read the file); the assertions stay.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_headless.py -q`
Expected: FAIL — `ImportError` on `backend.app.eval.headless`

- [ ] **Step 3: Write `headless.py`**

```python
# backend/app/eval/headless.py
"""One turn, no UI, a fixed approval policy, three budgets.

Why drive the real ``execute_turn`` instead of calling the provider directly:
the number must measure what a user gets — persistence, the approval bus,
the fleet, the tail check — not a shortcut around them.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..orchestrator.base import Provider
from ..orchestrator import registry
from ..session_runner.bus import EventBus
from ..session_runner.turn import execute_turn
from ..storage import sessions as session_store
from .config import EvalConfig

_CANCEL_GRACE_S = 5.0  # SessionRunner._CANCEL_GRACE_S; kept equal on purpose
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
_ABS_PATH = re.compile(r"(?<![\w-])(/[^\s'\"`:;,)]+)")


@dataclass(frozen=True)
class Budget:
    wall_clock_s: float
    tokens: int
    tool_calls: int


@dataclass
class TurnRecord:
    outcome: str
    session_id: str
    events: int = 0
    tool_calls: int = 0
    denials: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _TOKEN_KEYS})
    cost_usd: float | None = None
    wall_clock_s: float = 0.0
    error: str | None = None


class ApprovalPolicy:
    """Approve what stays inside the checkout; deny the rest with a reason.

    The card's ``input`` is a rendered string in which paths survive whole,
    so the decision is: every absolute path in it resolves under ``root`` and
    no ``..`` segment escapes. Anything undecidable is denied — a denial
    costs the agent a retry; an approval outside the checkout costs the host.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def decide(self, data: dict[str, Any]) -> tuple[str, str]:
        rendered = str(data.get("input", ""))
        if ".." in rendered.replace("...", ""):
            return "no", "path escapes the checkout with '..'"
        for raw in _ABS_PATH.findall(rendered):
            candidate = Path(raw).resolve()
            if candidate != self.root and self.root not in candidate.parents:
                return "no", f"path {raw} is outside the checkout {self.root}"
        return "yes", "inside the checkout"


async def run_headless_turn(
    prompt: str,
    cwd: str,
    *,
    config: EvalConfig,
    budget: Budget,
    provider: Provider | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TurnRecord:
    meta = await session_store.create_session(
        provider=config.provider_name, model=config.model, cwd=cwd,
        additional_dirs=[], title="eval", permission_mode="acceptEdits",
        fleet_config_override=config.fleet_override,
    )
    session_id = meta["id"]
    record = TurnRecord(outcome="no_patch", session_id=session_id)
    policy = ApprovalPolicy(Path(cwd))
    approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    limit_hit: list[str] = []
    started = clock()
    turn_task: asyncio.Task[None] | None = None

    def observe(ev: dict[str, Any]) -> None:
        record.events += 1
        kind, data = ev.get("type"), ev.get("data") or {}
        if kind == "assistant.tool_use":
            record.tool_calls += 1
            if record.tool_calls > budget.tool_calls:
                limit_hit.append("budget_tool_calls")
        elif kind == "assistant.done":
            usage = data.get("usage") or {}
            for k in _TOKEN_KEYS:
                record.tokens[k] += int(usage.get(k) or 0)
            if data.get("cost_usd") is not None:
                record.cost_usd = (record.cost_usd or 0.0) + float(data["cost_usd"])
            if sum(record.tokens.values()) > budget.tokens:
                limit_hit.append("budget_tokens")
        elif kind == "error":
            record.error = str(data.get("message") or data)
        elif kind == "pipeline.awaiting_approval":
            value, reason = policy.decide(data)
            if value != "yes":
                record.denials += 1
            approval_q.put_nowait({"id": data["id"], "value": value, "feedback": reason})
        if clock() - started > budget.wall_clock_s:
            limit_hit.append("budget_wall_clock")
        if limit_hit and turn_task is not None and not turn_task.done():
            turn_task.cancel()

    prov = provider or await registry.get_provider(config.provider_name)  # type: ignore[arg-type]
    bus = EventBus(session_id, on_event=observe)
    turn_task = asyncio.create_task(
        execute_turn(
            session_id=session_id, bus=bus, approval_q=approval_q, provider=prov,
            provider_name=config.provider_name, model=config.model, cwd=cwd,
            additional_dirs=[], upstream_id=None, fleet_override=config.fleet_override,
            permission_mode="acceptEdits", prompt=prompt,
        )
    )
    try:
        await asyncio.wait_for(asyncio.shield(turn_task), timeout=budget.wall_clock_s + 1.0)
    except asyncio.TimeoutError:
        limit_hit.append("budget_wall_clock")
        turn_task.cancel()
    except asyncio.CancelledError:
        if not limit_hit:
            raise
    if not turn_task.done():
        # The same bounded wait SessionRunner.cancel_turn uses: a wedged turn
        # is detached, never awaited forever.
        await asyncio.wait({turn_task}, timeout=_CANCEL_GRACE_S)
    record.wall_clock_s = clock() - started
    if limit_hit:
        record.outcome = limit_hit[0]
    elif record.error is not None:
        record.outcome = "provider_error"
    else:
        record.outcome = "patched"  # the caller downgrades to no_patch on an empty diff
    return record
```

Note on `outcome`: `patched` means "the turn ended normally"; Task 4's `capture_patch` is what turns it into `no_patch` when the diff is empty. Keep that contract in the docstring.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest backend/tests/test_eval_headless.py -q`
Expected: PASS (5 tests). If the wall-clock test flakes on ordering, drive the clock with more ticks — the assertion is on the outcome name, never on elapsed time.

- [ ] **Step 5: Leak check and lint**

Run: `.venv/bin/pytest backend/tests/test_eval_headless.py -q && pgrep -fl "fleet.subproc|echo_worker|tree_worker" ; .venv/bin/ruff check backend`
Expected: tests PASS; `pgrep` prints nothing; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/eval/headless.py backend/tests/test_eval_headless.py
git commit -m "Eval 2: a headless turn through the real execute_turn, with a fixed approval policy and three budgets

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Instances — the subset file, dataset rows, test files from a patch

**Files:**
- Create: `backend/app/eval/instances.py`, `backend/tests/fixtures/swebench/dataset_snapshot.jsonl` (3 hand-authored rows), `backend/tests/fixtures/swebench/subset_valid.json`
- Test: `backend/tests/test_eval_instances.py`

**Interfaces:**
- Produces: `Subset(ids: list[str], seed: int, size: int, skipped: list[dict], swebench_version: str, probed_at: str, file: Path)`, `load_subset(path) -> Subset` (raises `SubsetError` on duplicates / wrong size / missing seed), `load_rows(ids, *, loader=None) -> list[dict]` (the loader seam defaults to `swebench.harness.utils.load_swebench_dataset`), `test_files_from_patch(test_patch: str) -> set[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_instances.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.eval.instances import SubsetError, load_rows, load_subset, test_files_from_patch

FIX = Path(__file__).parent / "fixtures" / "swebench"


def test_test_files_come_from_diff_headers():
    patch = (
        "diff --git a/astropy/tests/test_a.py b/astropy/tests/test_a.py\n"
        "--- a/astropy/tests/test_a.py\n+++ b/astropy/tests/test_a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/docs/conftest.py b/docs/conftest.py\n"
    )
    assert test_files_from_patch(patch) == {"astropy/tests/test_a.py", "docs/conftest.py"}


def test_a_valid_subset_loads(tmp_path: Path):
    sub = load_subset(FIX / "subset_valid.json")
    assert sub.size == len(sub.ids) == 3 and sub.seed == 20260914
    assert sub.swebench_version == "5.0.2"


@pytest.mark.parametrize("mutation", ["dup", "size", "seed"])
def test_an_invalid_subset_is_refused(tmp_path: Path, mutation: str):
    data = json.loads((FIX / "subset_valid.json").read_text())
    if mutation == "dup":
        data["ids"][1] = data["ids"][0]
    elif mutation == "size":
        data["size"] = 99
    else:
        del data["seed"]
    p = tmp_path / "s.json"
    p.write_text(json.dumps(data))
    with pytest.raises(SubsetError):
        load_subset(p)


def test_rows_are_loaded_by_id_through_the_seam():
    snapshot = [json.loads(l) for l in (FIX / "dataset_snapshot.jsonl").read_text().splitlines()]

    def loader(name, split, ids):
        assert name == "SWE-bench/SWE-bench_Verified" and split == "test"
        return [r for r in snapshot if r["instance_id"] in ids]

    rows = load_rows(["astropy__astropy-12907"], loader=loader)
    assert rows[0]["repo"] == "astropy/astropy"
    assert set(rows[0]) >= {"instance_id", "repo", "base_commit", "problem_statement",
                            "test_patch", "image"}
```

Create the fixtures: `subset_valid.json` with `{"ids": ["astropy__astropy-12907", "django__django-11099", "sympy__sympy-20590"], "seed": 20260914, "size": 3, "skipped": [], "swebench_version": "5.0.2", "probed_at": "2026-09-14T00:00:00Z"}`; `dataset_snapshot.jsonl` with three rows containing the keys listed in the Pre-flight notes, using the astropy row's real values shown there (`repo`, `base_commit d16bfe05a744909de4b27f5875fe0d4ed41ce607`, `version 4.3`, `image swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`) and short placeholder text for `problem_statement`/`test_patch` (a one-hunk diff against a `tests/` path) — hand-authored per Ruling 6, say so in a `_note` field.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_instances.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `instances.py`**

```python
# backend/app/eval/instances.py
"""The standing subset and the dataset rows behind it.

Why a committed subset file with a seed and a skip list: a benchmark whose
membership drifts is not a benchmark. Every HISTORY row names the file it ran
on; a re-probe writes a new file with a new date.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import DATASET_NAME, DATASET_SPLIT

_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)

Loader = Callable[[str, str, Sequence[str]], list[dict[str, Any]]]


class SubsetError(ValueError):
    pass


@dataclass(frozen=True)
class Subset:
    ids: list[str]
    seed: int
    size: int
    skipped: list[dict[str, Any]]
    swebench_version: str
    probed_at: str
    file: Path


def load_subset(path: Path) -> Subset:
    data = json.loads(Path(path).read_text())
    for key in ("ids", "seed", "size", "swebench_version", "probed_at"):
        if key not in data:
            raise SubsetError(f"{path}: missing {key!r}")
    ids = list(data["ids"])
    if len(set(ids)) != len(ids):
        raise SubsetError(f"{path}: duplicate instance ids")
    if data["size"] != len(ids):
        raise SubsetError(f"{path}: size {data['size']} != {len(ids)} ids")
    return Subset(ids, int(data["seed"]), int(data["size"]), list(data.get("skipped", [])),
                  str(data["swebench_version"]), str(data["probed_at"]), Path(path))


def _default_loader(name: str, split: str, ids: Sequence[str]) -> list[dict[str, Any]]:
    from swebench.harness.utils import load_swebench_dataset  # heavy import, on demand

    return list(load_swebench_dataset(name, split, list(ids)))


def load_rows(ids: Sequence[str], *, loader: Loader | None = None) -> list[dict[str, Any]]:
    rows = (loader or _default_loader)(DATASET_NAME, DATASET_SPLIT, ids)
    by_id = {r["instance_id"]: r for r in rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SubsetError(f"dataset has no rows for {missing}")
    return [by_id[i] for i in ids]


def test_files_from_patch(test_patch: str) -> set[str]:
    """Paths the instance's own test patch touches — never part of a prediction."""
    return {b for _a, b in _DIFF_HEADER.findall(test_patch)}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_instances.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/instances.py backend/tests/test_eval_instances.py backend/tests/fixtures/swebench/
git commit -m "Eval 3: the standing subset file, dataset rows through a seam, and test files from a patch

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Checkout — mirror, worktree, patch capture

**Files:**
- Create: `backend/app/eval/checkout.py`
- Test: `backend/tests/test_eval_checkout.py`

**Interfaces:**
- Produces: `Checkout(instance_id, repo, base_commit, worktree: Path, mirror: Path)`, `ensure_mirror(repo: str, *, root: Path | None = None, url: str | None = None) -> Path` (clone `--mirror` on first use, `git fetch` after), `make_worktree(mirror, base_commit, instance_id, *, root=None) -> Path`, `capture_patch(worktree, *, exclude: set[str]) -> str` (stages everything, `git diff --cached --binary`, drops hunks whose path is in `exclude`), `remove_worktree(mirror, worktree) -> None`, `CheckoutError`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_checkout.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.app.eval import checkout as co


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "pkg.py").write_text("x = 1\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_pkg.py").write_text("def test(): pass\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "base")
    return src, _git(src, "rev-parse", "HEAD").strip()


def test_mirror_then_worktree_at_base_commit(upstream, tmp_path: Path):
    src, sha = upstream
    mirror = co.ensure_mirror("owner/name", root=tmp_path / "repos", url=str(src))
    assert mirror.name == "owner__name.git"
    wt = co.make_worktree(mirror, sha, "owner__name-1", root=tmp_path / "work")
    assert (wt / "pkg.py").read_text() == "x = 1\n"
    assert _git(wt, "rev-parse", "HEAD").strip() == sha
    co.remove_worktree(mirror, wt)
    assert not wt.exists()


def test_patch_capture_excludes_test_files_and_includes_new_files(upstream, tmp_path: Path):
    src, sha = upstream
    mirror = co.ensure_mirror("owner/name", root=tmp_path / "repos", url=str(src))
    wt = co.make_worktree(mirror, sha, "owner__name-2", root=tmp_path / "work")
    (wt / "pkg.py").write_text("x = 2\n")
    (wt / "new.py").write_text("y = 1\n")
    (wt / "tests" / "test_pkg.py").write_text("def test(): assert False\n")
    patch = co.capture_patch(wt, exclude={"tests/test_pkg.py"})
    assert "diff --git a/pkg.py b/pkg.py" in patch
    assert "diff --git a/new.py b/new.py" in patch
    assert "test_pkg.py" not in patch


def test_empty_diff_is_empty_string(upstream, tmp_path: Path):
    src, sha = upstream
    mirror = co.ensure_mirror("owner/name", root=tmp_path / "repos", url=str(src))
    wt = co.make_worktree(mirror, sha, "owner__name-3", root=tmp_path / "work")
    assert co.capture_patch(wt, exclude=set()) == ""


def test_bad_base_commit_is_a_checkout_error(upstream, tmp_path: Path):
    src, _sha = upstream
    mirror = co.ensure_mirror("owner/name", root=tmp_path / "repos", url=str(src))
    with pytest.raises(co.CheckoutError):
        co.make_worktree(mirror, "0" * 40, "owner__name-4", root=tmp_path / "work")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_checkout.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `checkout.py`**

```python
# backend/app/eval/checkout.py
"""Git plumbing for one instance: a shared mirror, a throwaway worktree, a patch.

Why a mirror per repository: fifty instances of django would otherwise clone
django fifty times. Why ``git worktree`` rather than ``git clone`` from the
mirror: a worktree shares objects and is removed in one command.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import eval_root


class CheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class Checkout:
    instance_id: str
    repo: str
    base_commit: str
    worktree: Path
    mirror: Path


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                             timeout=600)
    except subprocess.CalledProcessError as exc:
        raise CheckoutError(f"{' '.join(args)}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError(f"{' '.join(args)}: timed out") from exc
    return out.stdout


def ensure_mirror(repo: str, *, root: Path | None = None, url: str | None = None) -> Path:
    root = root or eval_root() / "repos"
    root.mkdir(parents=True, exist_ok=True)
    mirror = root / (repo.replace("/", "__") + ".git")
    if not mirror.exists():
        _run(["git", "clone", "--mirror", "-q", url or f"https://github.com/{repo}.git",
              str(mirror)])
    else:
        _run(["git", "-C", str(mirror), "fetch", "-q", "--prune"])
    return mirror


def make_worktree(mirror: Path, base_commit: str, instance_id: str, *,
                  root: Path | None = None) -> Path:
    root = root or eval_root() / "work"
    root.mkdir(parents=True, exist_ok=True)
    wt = root / instance_id
    if wt.exists():
        remove_worktree(mirror, wt)
    _run(["git", "-C", str(mirror), "worktree", "add", "-q", "--detach", str(wt), base_commit])
    return wt


def remove_worktree(mirror: Path, worktree: Path) -> None:
    subprocess.run(["git", "-C", str(mirror), "worktree", "remove", "--force", str(worktree)],
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(mirror), "worktree", "prune"], capture_output=True)


_FILE_BLOCK = re.compile(r"^diff --git a/(\S+) b/\S+$", re.MULTILINE)


def capture_patch(worktree: Path, *, exclude: set[str]) -> str:
    """Everything the agent changed, as one binary-safe patch, minus test files.

    Excluding the instance's test files is what SWE-bench expects: its harness
    applies the gold test patch itself, and a prediction that also edits those
    files conflicts with it and fails to apply.
    """
    _run(["git", "add", "-A"], cwd=worktree)
    raw = _run(["git", "diff", "--cached", "--binary"], cwd=worktree)
    if not exclude:
        return raw
    # Split on file headers; keep the blocks whose path is not excluded.
    pieces = _FILE_BLOCK.split(raw)
    kept: list[str] = []
    # pieces = [preamble, path1, block1, path2, block2, ...]
    for path, block in zip(pieces[1::2], pieces[2::2]):
        if path in exclude:
            continue
        b_path = path
        kept.append(f"diff --git a/{path} b/{b_path}{block}")
    return "".join(kept)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_checkout.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/checkout.py backend/tests/test_eval_checkout.py
git commit -m "Eval 4: one mirror per repository, a worktree per instance, and a patch that excludes the test files

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Pacing — the governor gate

**Files:**
- Create: `backend/app/eval/pacing.py`
- Test: `backend/tests/test_eval_pacing.py`

**Interfaces:**
- Consumes: a governor-shaped object with `headroom(p)`, `confidence(p)`, `resets_at(p)`.
- Produces: `Wait(provider, seconds, until)`, `MaxWaitExceeded(Exception)` carrying `.wait: Wait`, `GovernorGate(governor, *, floor=EVAL_HEADROOM_FLOOR, max_wait_s=EVAL_MAX_WAIT_S, clock=time.time, sleep=asyncio.sleep)`, `async wait_for_headroom(providers) -> list[Wait]`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_pacing.py
from __future__ import annotations

import pytest

from backend.app.eval.pacing import GovernorGate, MaxWaitExceeded


class FakeGov:
    def __init__(self, headroom, confidence="measured", resets_at=None):
        self._h, self._c, self._r = headroom, confidence, resets_at

    def headroom(self, p):
        return self._h[p]

    def confidence(self, p):
        return self._c

    def resets_at(self, p):
        return self._r


async def test_enough_headroom_does_not_wait():
    gate = GovernorGate(FakeGov({"claude": 0.5}))
    assert await gate.wait_for_headroom(("claude",)) == []


async def test_unknown_confidence_proceeds():
    gate = GovernorGate(FakeGov({"claude": 0.0}, confidence="unknown"))
    assert await gate.wait_for_headroom(("claude",)) == []


async def test_low_headroom_sleeps_to_resets_at_and_records_it():
    slept: list[float] = []
    now = [1000.0]
    gov = FakeGov({"claude": 0.1}, resets_at=1300.0)

    async def sleep(s):
        slept.append(s)
        now[0] += s
        gov._h["claude"] = 1.0

    gate = GovernorGate(gov, clock=lambda: now[0], sleep=sleep)
    waits = await gate.wait_for_headroom(("claude",))
    assert slept == [300.0]
    assert waits[0].provider == "claude" and waits[0].seconds == 300.0


async def test_a_wait_over_the_maximum_raises_with_the_wait():
    gov = FakeGov({"claude": 0.1}, resets_at=1000.0 + 7 * 3600)
    gate = GovernorGate(gov, clock=lambda: 1000.0, max_wait_s=6 * 3600.0)
    with pytest.raises(MaxWaitExceeded) as info:
        await gate.wait_for_headroom(("claude",))
    assert info.value.wait.seconds == 7 * 3600


async def test_low_headroom_without_a_reset_time_waits_a_fixed_backoff():
    slept: list[float] = []
    gov = FakeGov({"claude": 0.1}, resets_at=None)

    async def sleep(s):
        slept.append(s)
        gov._h["claude"] = 1.0

    gate = GovernorGate(gov, sleep=sleep)
    await gate.wait_for_headroom(("claude",))
    assert slept == [GovernorGate.NO_RESET_BACKOFF_S]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_pacing.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `pacing.py`**

```python
# backend/app/eval/pacing.py
"""Let the governor pace the benchmark.

Why a floor above the governor's own queue threshold: the governor's 0.05 is
"stop before the vendor refuses"; a benchmark must stop earlier so the user's
own work keeps headroom. Why never stall on "unknown": an unmeasured limit is
not evidence of a limit.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from .config import EVAL_HEADROOM_FLOOR, EVAL_MAX_WAIT_S


@dataclass(frozen=True)
class Wait:
    provider: str
    seconds: float
    until: float | None


class MaxWaitExceeded(Exception):
    def __init__(self, wait: Wait) -> None:
        super().__init__(f"{wait.provider} headroom resets in {wait.seconds / 3600:.1f} h")
        self.wait = wait


class GovernorGate:
    NO_RESET_BACKOFF_S = 900.0  # 15 min: long enough to matter, short enough to notice

    def __init__(self, governor: Any, *, floor: float = EVAL_HEADROOM_FLOOR,
                 max_wait_s: float = EVAL_MAX_WAIT_S,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._gov, self._floor, self._max = governor, floor, max_wait_s
        self._clock, self._sleep = clock, sleep

    def _blocked(self, provider: str) -> bool:
        if self._gov.confidence(provider) == "unknown":
            return False
        return self._gov.headroom(provider) < self._floor

    async def wait_for_headroom(self, providers: Sequence[str]) -> list[Wait]:
        waits: list[Wait] = []
        for provider in providers:
            while self._blocked(provider):
                until = self._gov.resets_at(provider)
                seconds = (max(0.0, until - self._clock()) if until is not None
                           else self.NO_RESET_BACKOFF_S)
                wait = Wait(provider, seconds, until)
                if seconds > self._max:
                    raise MaxWaitExceeded(wait)
                waits.append(wait)
                await self._sleep(seconds)
        return waits
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_pacing.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/pacing.py backend/tests/test_eval_pacing.py
git commit -m "Eval 5: the governor paces the benchmark, with a floor above its own threshold and a bounded wait

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Runner — the generate phase

**Files:**
- Create: `backend/app/eval/runner.py`
- Test: `backend/tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `EvalConfig`, `Budget`, `run_headless_turn`, `checkout.*`, `instances.*`, `GovernorGate`, `PROMPT_VERSION`.
- Produces: `RunPaths(run_id, root, predictions, records_dir, events_dir, waits, meta)` via `RunPaths.for_run(run_id, *, root=None)`; `run_id_for(config, tree_sha) -> str`; `build_prompt(row) -> str`; `InstanceRecord` (TurnRecord fields + `instance_id`, `patch_bytes`, `prompt_version`, `interrupted`, `limit`); `generate(config, subset, *, rows, gate, budget, turn=run_headless_turn, checkout_root=None, resume: str | None = None, tree_sha: str, dirty: bool, on_progress=None) -> RunPaths` (async); `GenerateRefused(Exception)`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_runner.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.eval.config import EvalConfig
from backend.app.eval.headless import Budget, TurnRecord
from backend.app.eval.instances import Subset
from backend.app.eval.pacing import MaxWaitExceeded, Wait
from backend.app.eval import runner as rn


class NoGate:
    async def wait_for_headroom(self, providers):
        return []


def _cfg():
    return EvalConfig("claude-single", "claude", "m", None)


def _subset(ids, tmp_path):
    return Subset(list(ids), 20260914, len(ids), [], "5.0.2", "2026-09-14T00:00:00Z",
                  tmp_path / "subset.json")


def _rows(ids, upstream_path):
    return [
        {"instance_id": i, "repo": "owner/name", "base_commit": "HEAD",
         "problem_statement": f"Fix {i}", "test_patch": "", "_url": str(upstream_path)}
        for i in ids
    ]


async def _fake_turn_writes(prompt, cwd, *, config, budget, **_):
    Path(cwd, "pkg.py").write_text("x = 2\n")
    return TurnRecord(outcome="patched", session_id="s", tokens={"input_tokens": 5,
                      "output_tokens": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0})


async def _fake_turn_noop(prompt, cwd, *, config, budget, **_):
    return TurnRecord(outcome="patched", session_id="s")


@pytest.fixture
def upstream(tmp_path: Path):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(src), *args], check=True)
    (src / "pkg.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "base"], check=True)
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return src, sha


def test_prompt_has_the_statement_and_no_hints():
    row = {"repo": "a/b", "problem_statement": "It breaks.", "hints_text": "SECRET HINT"}
    p = rn.build_prompt(row)
    assert "It breaks." in p and "SECRET HINT" not in p and "a/b" in p
    assert "do not modify" in p.lower() and "tests" in p.lower()


def test_dirty_tree_is_refused(tmp_path: Path):
    with pytest.raises(rn.GenerateRefused):
        rn.check_tree(dirty=True)


async def test_generate_writes_records_predictions_and_resumes(tmp_path: Path, upstream):
    src, sha = upstream
    rows = _rows(["owner__name-1", "owner__name-2"], src)
    for r in rows:
        r["base_commit"] = sha
    paths = await rn.generate(
        _cfg(), _subset([r["instance_id"] for r in rows], tmp_path), rows=rows, gate=NoGate(),
        budget=Budget(60, 10**6, 100), turn=_fake_turn_writes, checkout_root=tmp_path / "co",
        tree_sha="abc1234", dirty=False, run_root=tmp_path / "runs",
    )
    preds = [json.loads(l) for l in paths.predictions.read_text().splitlines()]
    assert [p["instance_id"] for p in preds] == ["owner__name-1", "owner__name-2"]
    assert preds[0]["model_name_or_path"] == "claude-single@abc1234"
    assert "diff --git a/pkg.py" in preds[0]["model_patch"]
    rec = json.loads((paths.records_dir / "owner__name-1.json").read_text())
    assert rec["outcome"] == "patched" and rec["tokens"]["input_tokens"] == 5
    # resume: a third instance runs, the first two are skipped
    rows.append({**rows[0], "instance_id": "owner__name-3"})
    calls: list[str] = []

    async def counting(prompt, cwd, **kw):
        calls.append(cwd)
        return await _fake_turn_writes(prompt, cwd, **kw)

    await rn.generate(
        _cfg(), _subset([r["instance_id"] for r in rows], tmp_path), rows=rows, gate=NoGate(),
        budget=Budget(60, 10**6, 100), turn=counting, checkout_root=tmp_path / "co",
        tree_sha="abc1234", dirty=False, run_root=tmp_path / "runs", resume=paths.run_id,
    )
    assert len(calls) == 1 and calls[0].endswith("owner__name-3")


async def test_empty_diff_records_no_patch_and_is_not_predicted(tmp_path: Path, upstream):
    src, sha = upstream
    rows = _rows(["owner__name-9"], src)
    rows[0]["base_commit"] = sha
    paths = await rn.generate(
        _cfg(), _subset(["owner__name-9"], tmp_path), rows=rows, gate=NoGate(),
        budget=Budget(60, 10**6, 100), turn=_fake_turn_noop, checkout_root=tmp_path / "co",
        tree_sha="abc1234", dirty=False, run_root=tmp_path / "runs",
    )
    assert paths.predictions.read_text() == ""
    rec = json.loads((paths.records_dir / "owner__name-9.json").read_text())
    assert rec["outcome"] == "no_patch"


async def test_max_wait_checkpoints_and_exits_3(tmp_path: Path, upstream):
    src, sha = upstream
    rows = _rows(["owner__name-5"], src)
    rows[0]["base_commit"] = sha

    class Gate:
        async def wait_for_headroom(self, providers):
            raise MaxWaitExceeded(Wait("claude", 7 * 3600, None))

    with pytest.raises(rn.ResumeLater) as info:
        await rn.generate(
            _cfg(), _subset(["owner__name-5"], tmp_path), rows=rows, gate=Gate(),
            budget=Budget(60, 10**6, 100), turn=_fake_turn_noop,
            checkout_root=tmp_path / "co", tree_sha="abc1234", dirty=False,
            run_root=tmp_path / "runs",
        )
    assert info.value.exit_code == 3 and "--resume" in str(info.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_runner.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `runner.py`**

```python
# backend/app/eval/runner.py
"""The generate phase: one instance at a time, every outcome a file.

Why each step leaves a file: a run that dies at instance 37 resumes at 37,
not at 1. Why a dirty tree is refused: the run id names a commit, and a
number that names a commit it does not describe is worse than no number.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from . import checkout as co
from .config import PROMPT_VERSION, eval_root
from .headless import Budget, TurnRecord, run_headless_turn
from .instances import Subset, test_files_from_patch
from .pacing import MaxWaitExceeded

Turn = Callable[..., Awaitable[TurnRecord]]


class GenerateRefused(RuntimeError):
    pass


class ResumeLater(RuntimeError):
    exit_code = 3


@dataclass
class InstanceRecord:
    instance_id: str
    outcome: str
    session_id: str
    events: int
    tool_calls: int
    denials: int
    tokens: dict[str, int]
    cost_usd: float | None
    wall_clock_s: float
    error: str | None
    patch_bytes: int
    prompt_version: int = PROMPT_VERSION
    interrupted: bool = False
    limit: str | None = None


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    root: Path
    predictions: Path
    records_dir: Path
    events_dir: Path
    waits: Path
    meta: Path

    @classmethod
    def for_run(cls, run_id: str, *, root: Path | None = None) -> RunPaths:
        base = (root or eval_root() / "runs") / run_id
        for d in ("records", "events"):
            (base / d).mkdir(parents=True, exist_ok=True)
        return cls(run_id, base, base / "predictions.jsonl", base / "records",
                   base / "events", base / "waits.jsonl", base / "meta.json")


def run_id_for(config_name: str, tree_sha: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{config_name}@{tree_sha[:7]}-{stamp}"


def check_tree(*, dirty: bool) -> None:
    if dirty:
        raise GenerateRefused("the working tree is dirty; commit or stash before a run "
                              "so the run id names the tree it measures")


def build_prompt(row: dict[str, Any]) -> str:
    """The problem statement, verbatim, inside a versioned preamble. No hints."""
    return (
        f"You are working in a checkout of the repository {row['repo']}.\n"
        "Fix the issue described below by editing the source. Do NOT modify or add "
        "tests; the maintainers' tests will be run against your change. "
        "Run the existing tests you consider relevant. When the fix is complete, stop.\n\n"
        "<issue>\n" + row["problem_statement"].strip() + "\n</issue>\n"
    )


async def generate(
    config: Any,
    subset: Subset,
    *,
    rows: Sequence[dict[str, Any]],
    gate: Any,
    budget: Budget,
    turn: Turn = run_headless_turn,
    checkout_root: Path | None = None,
    resume: str | None = None,
    tree_sha: str,
    dirty: bool,
    run_root: Path | None = None,
    on_progress: Callable[[InstanceRecord], None] | None = None,
) -> RunPaths:
    check_tree(dirty=dirty)
    paths = RunPaths.for_run(resume or run_id_for(config.name, tree_sha), root=run_root)
    if not paths.meta.exists():
        paths.meta.write_text(json.dumps({
            "run_id": paths.run_id, "config": config.name, "tree_sha": tree_sha,
            "subset": str(subset.file), "prompt_version": PROMPT_VERSION,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    by_id = {r["instance_id"]: r for r in rows}
    repos_root = (checkout_root or eval_root()) / "repos"
    work_root = (checkout_root or eval_root()) / "work"
    fetch_failures = 0
    for instance_id in subset.ids:
        if (paths.records_dir / f"{instance_id}.json").exists():
            continue
        row = by_id[instance_id]
        try:
            waits = await gate.wait_for_headroom(config.governed_providers())
        except MaxWaitExceeded as exc:
            _append(paths.waits, {"instance_id": instance_id, "provider": exc.wait.provider,
                                  "seconds": exc.wait.seconds, "aborted": True})
            raise ResumeLater(
                f"{exc}; resume with --resume {paths.run_id}"
            ) from exc
        for w in waits:
            _append(paths.waits, {"instance_id": instance_id, "provider": w.provider,
                                  "seconds": w.seconds, "aborted": False})
        try:
            mirror = co.ensure_mirror(row["repo"], root=repos_root, url=row.get("_url"))
            fetch_failures = 0
        except co.CheckoutError as exc:
            fetch_failures += 1
            _write_record(paths, _errored(instance_id, "checkout_error", str(exc)), on_progress)
            if fetch_failures >= 3:
                raise GenerateRefused("three consecutive mirror fetches failed — "
                                      "that is the network, not the instances") from exc
            continue
        try:
            wt = co.make_worktree(mirror, row["base_commit"], instance_id, root=work_root)
        except co.CheckoutError as exc:
            _write_record(paths, _errored(instance_id, "checkout_error", str(exc)), on_progress)
            continue
        interrupted = False
        try:
            rec = await turn(build_prompt(row), str(wt), config=config, budget=budget)
        except asyncio.CancelledError:
            interrupted = True
            rec = TurnRecord(outcome="budget_wall_clock", session_id="")
        try:
            patch = co.capture_patch(wt, exclude=test_files_from_patch(row.get("test_patch", "")))
        except co.CheckoutError:
            patch = ""
        finally:
            co.remove_worktree(mirror, wt)
        outcome = rec.outcome
        if outcome == "patched" and not patch.strip():
            outcome = "no_patch"
        if patch.strip():
            _append(paths.predictions, {"instance_id": instance_id,
                                        "model_name_or_path": f"{config.name}@{tree_sha[:7]}",
                                        "model_patch": patch})
        record = InstanceRecord(
            instance_id=instance_id, outcome=outcome, session_id=rec.session_id,
            events=rec.events, tool_calls=rec.tool_calls, denials=rec.denials,
            tokens=dict(rec.tokens), cost_usd=rec.cost_usd, wall_clock_s=rec.wall_clock_s,
            error=rec.error, patch_bytes=len(patch.encode()), interrupted=interrupted,
            limit=outcome if outcome.startswith("budget_") else None,
        )
        _write_record(paths, record, on_progress)
        if interrupted:
            raise ResumeLater(f"interrupted; resume with --resume {paths.run_id}")
    return paths


def _errored(instance_id: str, outcome: str, error: str) -> InstanceRecord:
    return InstanceRecord(instance_id, outcome, "", 0, 0, 0,
                          {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                           "cache_creation_tokens": 0}, None, 0.0, error, 0)


def _write_record(paths: RunPaths, record: InstanceRecord,
                  on_progress: Callable[[InstanceRecord], None] | None) -> None:
    (paths.records_dir / f"{record.instance_id}.json").write_text(
        json.dumps(dataclasses.asdict(record), indent=2))
    if on_progress:
        on_progress(record)


def _append(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(obj) + "\n")
```

The `rows[i]["_url"]` seam exists only so tests can point the mirror at a local repository; production rows have no `_url` and the mirror clones GitHub.

- [ ] **Step 3b: The between-instance disk re-check (spec §5)**

Add to `runner.py`, next to the other constants and used at the top of the per-instance loop (before the governor gate):

```python
import shutil

DISK_PAUSE_GIB = 20.0


def _free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / 2**30
```

and inside `generate`, as the first statement of the `for instance_id in subset.ids:` body after the resume-skip:

```python
        free = disk_probe(work_root if work_root.exists() else work_root.parent)
        if free < DISK_PAUSE_GIB:
            raise ResumeLater(
                f"{free:.0f} GiB free is below {DISK_PAUSE_GIB:.0f} GiB; free space, then "
                f"resume with --resume {paths.run_id}"
            )
```

with a new keyword parameter on `generate`: `disk_probe: Callable[[Path], float] = _free_gib`. Add the test:

```python
async def test_low_disk_pauses_with_the_resume_hint(tmp_path: Path, upstream):
    src, sha = upstream
    rows = _rows(["owner__name-7"], src)
    rows[0]["base_commit"] = sha
    with pytest.raises(rn.ResumeLater) as info:
        await rn.generate(
            _cfg(), _subset(["owner__name-7"], tmp_path), rows=rows, gate=NoGate(),
            budget=Budget(60, 10**6, 100), turn=_fake_turn_noop,
            checkout_root=tmp_path / "co", tree_sha="abc1234", dirty=False,
            run_root=tmp_path / "runs", disk_probe=lambda p: 5.0,
        )
    assert "20 GiB" in str(info.value) and "--resume" in str(info.value)
    assert not list((tmp_path / "runs").glob("*/records/*.json"))
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_runner.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/runner.py backend/tests/test_eval_runner.py
git commit -m "Eval 6: the generate phase — every instance leaves a record, empty diffs are no_patch, runs resume

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Score — SWE-bench's harness, its report, the image architecture

**Files:**
- Create: `backend/app/eval/score.py`, `backend/tests/fixtures/swebench/results.json`, `backend/tests/fixtures/swebench/report_resolved.json`, `backend/tests/fixtures/swebench/report_unresolved.json`
- Test: `backend/tests/test_eval_score.py`

**Interfaces:**
- Produces: `ensure_task_repo(*, root=None, git=subprocess.run) -> Path` (clones `TASK_REPO_URL` depth 1 into `eval_root()/swe-bench-tasks`, pulls after); `score_command(predictions, run_id, *, task_repo, workers=2, instance_ids=None, gold=False) -> list[str]` (the exact argv); `run_scoring(predictions, run_id, *, cwd, task_repo, workers=2, runner=subprocess.run) -> int`; `Verdict(instance_id, status: str, resolved: bool, image: str | None, image_arch: str | None, report_path: str | None)`; `parse_results(eval_logs: Path, run_id, model_name, expected_ids, *, image_for: dict[str,str], inspect=docker_image_arch) -> list[Verdict]`; `docker_image_arch(image) -> str | None`.

- [ ] **Step 1: Capture fixtures**

`results.json` (schema_version 2) hand-authored from the harness's key list in Pre-flight notes:

```json
{"total_instances": 3, "submitted_instances": 2, "completed_instances": 2,
 "resolved_instances": 1, "unresolved_instances": 1, "infra_failure_instances": 0,
 "ambiguous_failure_instances": 0, "empty_patch_instances": 1, "error_instances": 0,
 "completed_ids": ["a__a-1", "b__b-2"], "incomplete_ids": [], "empty_patch_ids": ["c__c-3"],
 "submitted_ids": ["a__a-1", "b__b-2"], "resolved_ids": ["a__a-1"],
 "unresolved_ids": ["b__b-2"], "infra_failure_ids": [], "ambiguous_failure_ids": [],
 "failure_reasons": {}, "error_ids": [], "schema_version": 2}
```

`report_resolved.json`: `{"a__a-1": {"patch_is_None": false, "patch_exists": true, "patch_successfully_applied": true, "resolved": true, "tests_status": {"FAIL_TO_PASS": {"success": ["t1"], "failure": []}, "PASS_TO_PASS": {"success": ["t2"], "failure": []}}}}`; `report_unresolved.json` the same shape for `b__b-2` with `resolved: false` and `t1` under `failure`. Label each with a top-level comment file `README.md` in the fixtures dir saying they are hand-authored to 5.0.2's shape (Ruling 6).

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/test_eval_score.py
from __future__ import annotations

import json
import shutil
from pathlib import Path

from backend.app.eval import score as sc

FIX = Path(__file__).parent / "fixtures" / "swebench"


def test_score_command_is_the_documented_cli(tmp_path: Path):
    argv = sc.score_command(tmp_path / "p.jsonl", "run-1", task_repo=tmp_path / "tasks",
                            workers=2, instance_ids=["x__x-1"])
    assert argv[:3] == ["swebench", "eval", "verified"]
    assert "-p" in argv and str(tmp_path / "p.jsonl") in argv
    assert "--run-id" in argv and "run-1" in argv
    assert "-j" in argv and "2" in argv
    assert "--task-repo" in argv and str(tmp_path / "tasks") in argv
    assert "-i" in argv and "x__x-1" in argv
    assert "--gold" not in argv
    assert "--gold" in sc.score_command(tmp_path / "p.jsonl", "r", task_repo=tmp_path, gold=True)


def test_parse_results_joins_report_and_arch(tmp_path: Path):
    logs = tmp_path / "logs" / "evaluation" / "run-1"
    (logs).mkdir(parents=True)
    shutil.copy(FIX / "results.json", logs / "results.json")
    for iid, fixture in (("a__a-1", "report_resolved.json"), ("b__b-2", "report_unresolved.json")):
        d = logs / "cfg@abc1234" / iid
        d.mkdir(parents=True)
        shutil.copy(FIX / fixture, d / "report.json")
    verdicts = sc.parse_results(tmp_path / "logs" / "evaluation", "run-1", "cfg@abc1234",
                                ["a__a-1", "b__b-2", "c__c-3", "d__d-4"],
                                image_for={"a__a-1": "img:a", "b__b-2": "img:b"},
                                inspect=lambda img: "amd64")
    by = {v.instance_id: v for v in verdicts}
    assert by["a__a-1"].status == "resolved" and by["a__a-1"].resolved
    assert by["b__b-2"].status == "unresolved" and not by["b__b-2"].resolved
    assert by["c__c-3"].status == "no_patch"
    assert by["d__d-4"].status == "eval_error"  # not in the report at all
    assert by["a__a-1"].image_arch == "amd64" and by["a__a-1"].report_path.endswith("report.json")


def test_run_scoring_uses_the_runner_seam_and_cwd(tmp_path: Path):
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))

        class R:
            returncode = 0
        return R()

    code = sc.run_scoring(tmp_path / "p.jsonl", "run-1", cwd=tmp_path, task_repo=tmp_path,
                          runner=runner)
    assert code == 0 and calls[0][1]["cwd"] == tmp_path and calls[0][0][0] == "swebench"


def test_ensure_task_repo_clones_once_then_pulls(tmp_path: Path):
    seen = []

    def git(argv, **kw):
        seen.append(argv)
        if argv[1] == "clone":
            (tmp_path / "swe-bench-tasks").mkdir()

        class R:
            returncode = 0
            stderr = ""
        return R()

    p1 = sc.ensure_task_repo(root=tmp_path, git=git)
    p2 = sc.ensure_task_repo(root=tmp_path, git=git)
    assert p1 == p2 == tmp_path / "swe-bench-tasks"
    assert seen[0][1] == "clone" and seen[1][1:3] == ["-C", str(p1)]
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_score.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 4: Write `score.py`**

```python
# backend/app/eval/score.py
"""Hand the patches to SWE-bench's own harness and read back what it says.

Why a subprocess and the documented CLI rather than importing the harness:
the CLI's flags are what 5.0.2 documents for M-series (``--task-repo`` builds
images locally); a Python signature drifted between releases already. Why we
inspect the image architecture: with ``--task-repo`` the harness silently
pulls the published x86_64 image when a local build fails, and a number that
does not say which image ran is not reproducible.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import TASK_REPO_URL, eval_root


def ensure_task_repo(*, root: Path | None = None,
                     git: Callable[..., Any] = subprocess.run) -> Path:
    root = root or eval_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / "swe-bench-tasks"
    if not dest.exists():
        res = git(["git", "clone", "--depth", "1", "-q", TASK_REPO_URL, str(dest)],
                  capture_output=True, text=True, timeout=600)
    else:
        res = git(["git", "-C", str(dest), "pull", "-q", "--ff-only"],
                  capture_output=True, text=True, timeout=600)
    if getattr(res, "returncode", 0) != 0:
        raise RuntimeError(f"task repo: {getattr(res, 'stderr', '')}")
    return dest


def score_command(predictions: Path, run_id: str, *, task_repo: Path, workers: int = 2,
                  instance_ids: Sequence[str] | None = None, gold: bool = False) -> list[str]:
    argv = ["swebench", "eval", "verified", "--run-id", run_id, "-j", str(workers),
            "--task-repo", str(task_repo)]
    argv += ["--gold"] if gold else ["-p", str(predictions)]
    if instance_ids:
        argv += ["-i", *instance_ids]
    return argv


def run_scoring(predictions: Path, run_id: str, *, cwd: Path, task_repo: Path,
                workers: int = 2, instance_ids: Sequence[str] | None = None,
                gold: bool = False, runner: Callable[..., Any] = subprocess.run) -> int:
    argv = score_command(predictions, run_id, task_repo=task_repo, workers=workers,
                         instance_ids=instance_ids, gold=gold)
    res = runner(argv, cwd=cwd)
    return int(getattr(res, "returncode", 1))


@dataclass(frozen=True)
class Verdict:
    instance_id: str
    status: str  # resolved | unresolved | no_patch | eval_error
    resolved: bool
    image: str | None
    image_arch: str | None
    report_path: str | None


def docker_image_arch(image: str) -> str | None:
    try:
        out = subprocess.run(["docker", "image", "inspect", image, "--format",
                              "{{.Architecture}}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def parse_results(eval_logs: Path, run_id: str, model_name: str, expected_ids: Sequence[str],
                  *, image_for: dict[str, str],
                  inspect: Callable[[str], str | None] = docker_image_arch) -> list[Verdict]:
    results = json.loads((eval_logs / run_id / "results.json").read_text())
    resolved = set(results.get("resolved_ids", []))
    unresolved = set(results.get("unresolved_ids", []))
    empty = set(results.get("empty_patch_ids", []))
    verdicts: list[Verdict] = []
    for iid in expected_ids:
        report = eval_logs / run_id / model_name / iid / "report.json"
        if iid in resolved:
            status = "resolved"
        elif iid in unresolved:
            status = "unresolved"
        elif iid in empty:
            status = "no_patch"
        else:
            status = "eval_error"
        image = image_for.get(iid)
        verdicts.append(Verdict(
            instance_id=iid, status=status, resolved=status == "resolved", image=image,
            image_arch=inspect(image) if image and status in ("resolved", "unresolved") else None,
            report_path=str(report) if report.exists() else None,
        ))
    return verdicts
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_score.py -q`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/eval/score.py backend/tests/test_eval_score.py backend/tests/fixtures/swebench/
git commit -m "Eval 7: SWE-bench's own harness scores the patches, and the run records which image actually ran

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Report — the result JSON and the HISTORY row

**Files:**
- Create: `backend/app/eval/report.py`, `evals/HISTORY.md` (header only), `evals/.gitignore`
- Test: `backend/tests/test_eval_report.py`

**Interfaces:**
- Produces: `Summary(run_id, date, tree_sha, config, subset, n, resolved, no_patch, budget_outs, errors, tokens_per_task, cost_per_task, wall_clock_s, waits_s, prompt_version, swebench_version)`; `summarize(paths: RunPaths, verdicts, *, swebench_version) -> Summary`; `write_result_json(paths, verdicts, summary, out_dir) -> Path`; `history_row(summary) -> str`; `append_history(path, summary) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_report.py
from __future__ import annotations

import json
from pathlib import Path

from backend.app.eval import report as rp
from backend.app.eval.runner import RunPaths
from backend.app.eval.score import Verdict


def _paths(tmp_path: Path) -> RunPaths:
    p = RunPaths.for_run("claude-single@abc1234-20260914T000000Z", root=tmp_path / "runs")
    p.meta.write_text(json.dumps({"run_id": p.run_id, "config": "claude-single",
                                  "tree_sha": "abc1234def", "subset": "evals/s.json",
                                  "prompt_version": 1, "started_at": "2026-09-14T00:00:00+00:00"}))
    for iid, outcome, toks, wall in (("a", "patched", 1000, 10.0), ("b", "patched", 3000, 20.0),
                                     ("c", "no_patch", 500, 5.0), ("d", "budget_tokens", 8000, 60.0)):
        (p.records_dir / f"{iid}.json").write_text(json.dumps({
            "instance_id": iid, "outcome": outcome, "session_id": "s", "events": 1,
            "tool_calls": 1, "denials": 0, "tokens": {"input_tokens": toks, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0}, "cost_usd": 0.5,
            "wall_clock_s": wall, "error": None, "patch_bytes": 10, "prompt_version": 1,
            "interrupted": False, "limit": None}))
    p.waits.write_text(json.dumps({"instance_id": "b", "provider": "claude", "seconds": 120.0,
                                   "aborted": False}) + "\n")
    return p


def test_summary_counts_and_rates(tmp_path: Path):
    p = _paths(tmp_path)
    verdicts = [Verdict("a", "resolved", True, "i", "amd64", None),
                Verdict("b", "unresolved", False, "i", "amd64", None),
                Verdict("c", "no_patch", False, None, None, None),
                Verdict("d", "unresolved", False, "i", "amd64", None)]
    s = rp.summarize(p, verdicts, swebench_version="5.0.2")
    assert s.n == 4 and s.resolved == 1 and s.no_patch == 1 and s.budget_outs == 1
    assert s.tokens_per_task == (1000 + 3000 + 500 + 8000) / 4
    assert s.cost_per_task == 0.5 and s.wall_clock_s == 95.0 and s.waits_s == 120.0
    assert s.tree_sha == "abc1234" and s.config == "claude-single"


def test_history_row_is_byte_stable_and_appends(tmp_path: Path):
    p = _paths(tmp_path)
    s = rp.summarize(p, [Verdict("a", "resolved", True, "i", "amd64", None)] +
                     [Verdict(i, "unresolved", False, "i", "amd64", None) for i in "bcd"],
                     swebench_version="5.0.2")
    row = rp.history_row(s)
    assert row == rp.history_row(s)
    assert row.startswith("| 2026-09-14 | abc1234 | claude-single |")
    assert "1/4" in row and "25.0%" in row
    hist = tmp_path / "HISTORY.md"
    hist.write_text("# header\n\n| date |\n|---|\n")
    rp.append_history(hist, s)
    assert hist.read_text().rstrip().endswith(row)


def test_result_json_round_trips(tmp_path: Path):
    p = _paths(tmp_path)
    verdicts = [Verdict("a", "resolved", True, "i", "amd64", None)]
    s = rp.summarize(p, verdicts, swebench_version="5.0.2")
    out = rp.write_result_json(p, verdicts, s, tmp_path / "results")
    data = json.loads(out.read_text())
    assert data["summary"]["run_id"] == p.run_id and len(data["records"]) == 4
    assert data["verdicts"][0]["image_arch"] == "amd64"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_report.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `report.py` and the committed files**

```python
# backend/app/eval/report.py
"""The number, and the row that makes it comparable to last week's.

Why HISTORY.md is committed and results are not: the row is the answer; the
result JSON is the evidence, and evidence for fifty runs is megabytes.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .runner import RunPaths
from .score import Verdict

HISTORY_HEADER = (
    "# Eval history\n\n"
    "One row per `make eval` run. Resolve rate is on the subset the row names; "
    "tokens and cost are per attempted task.\n\n"
    "| date | sha | config | subset | n | resolved | rate | tokens/task | cost/task | "
    "wall clock | waited | prompt | swebench | notes |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


@dataclass(frozen=True)
class Summary:
    run_id: str
    date: str
    tree_sha: str
    config: str
    subset: str
    n: int
    resolved: int
    no_patch: int
    budget_outs: int
    errors: int
    tokens_per_task: float
    cost_per_task: float | None
    wall_clock_s: float
    waits_s: float
    prompt_version: int
    swebench_version: str


def _records(paths: RunPaths) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(paths.records_dir.glob("*.json"))]


def summarize(paths: RunPaths, verdicts: Sequence[Verdict], *, swebench_version: str) -> Summary:
    meta = json.loads(paths.meta.read_text())
    records = _records(paths)
    n = len(records)
    tokens = sum(sum(r["tokens"].values()) for r in records)
    costs = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
    waits = 0.0
    if paths.waits.exists():
        waits = sum(json.loads(l)["seconds"] for l in paths.waits.read_text().splitlines()
                    if l.strip())
    return Summary(
        run_id=paths.run_id, date=meta["started_at"][:10], tree_sha=meta["tree_sha"][:7],
        config=meta["config"], subset=Path(meta["subset"]).name, n=n,
        resolved=sum(1 for v in verdicts if v.resolved),
        no_patch=sum(1 for r in records if r["outcome"] == "no_patch"),
        budget_outs=sum(1 for r in records if r["outcome"].startswith("budget_")),
        errors=sum(1 for r in records if r["outcome"].endswith("_error")),
        tokens_per_task=(tokens / n) if n else 0.0,
        cost_per_task=(sum(costs) / len(costs)) if costs else None,
        wall_clock_s=sum(r["wall_clock_s"] for r in records), waits_s=waits,
        prompt_version=int(meta["prompt_version"]), swebench_version=swebench_version,
    )


def history_row(s: Summary) -> str:
    rate = (s.resolved / s.n * 100) if s.n else 0.0
    cost = f"${s.cost_per_task:.2f}" if s.cost_per_task is not None else "n/a"
    return (f"| {s.date} | {s.tree_sha} | {s.config} | {s.subset} | {s.n} | "
            f"{s.resolved}/{s.n} | {rate:.1f}% | {s.tokens_per_task:.0f} | {cost} | "
            f"{s.wall_clock_s / 60:.0f} min | {s.waits_s / 60:.0f} min | v{s.prompt_version} | "
            f"{s.swebench_version} | |")


def append_history(path: Path, s: Summary) -> None:
    if not path.exists():
        path.write_text(HISTORY_HEADER)
    with path.open("a") as fh:
        fh.write(history_row(s) + "\n")


def write_result_json(paths: RunPaths, verdicts: Sequence[Verdict], s: Summary,
                      out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{paths.run_id}.json"
    out.write_text(json.dumps({
        "summary": dataclasses.asdict(s),
        "meta": json.loads(paths.meta.read_text()),
        "records": _records(paths),
        "verdicts": [dataclasses.asdict(v) for v in verdicts],
        "waits": [json.loads(l) for l in paths.waits.read_text().splitlines() if l.strip()]
        if paths.waits.exists() else [],
    }, indent=2))
    return out
```

`evals/.gitignore`: `results/`. `evals/HISTORY.md`: exactly `HISTORY_HEADER`'s content.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_report.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/report.py backend/tests/test_eval_report.py evals/HISTORY.md evals/.gitignore
git commit -m "Eval 8: a result JSON per run and one byte-stable row per run in evals/HISTORY.md

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Probe — selecting the standing subset

**Files:**
- Create: `backend/app/eval/probe.py`
- Test: `backend/tests/test_eval_probe.py`

**Interfaces:**
- Consumes: `load_rows` (via a `loader` seam), `run_scoring(..., gold=True)` (via a `score` seam returning the `results.json` dict), `docker_image_arch` (seam).
- Produces: `probe_subset(*, all_ids: Sequence[str], seed: int, size: int, score: Callable[[str], dict], inspect, image_for: dict[str,str], swebench_version: str, now: str) -> dict` (the subset file content), `seeded_order(ids, seed) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_eval_probe.py
from __future__ import annotations

from backend.app.eval import probe as pb


def test_seeded_order_is_deterministic():
    ids = [f"i{n}" for n in range(20)]
    a = pb.seeded_order(ids, 20260914)
    assert a == pb.seeded_order(list(reversed(ids)), 20260914) and sorted(a) == sorted(ids)
    assert a != pb.seeded_order(ids, 1)


def test_probe_keeps_gold_resolved_and_records_skips_and_arch():
    ids = [f"i{n}" for n in range(8)]
    outcomes = {"i0": "ok", "i1": "fail", "i2": "ok", "i3": "error", "i4": "ok", "i5": "ok"}

    def score(iid):
        o = outcomes.get(iid, "ok")
        if o == "ok":
            return {"resolved_ids": [iid], "error_ids": [], "failure_reasons": {}}
        if o == "fail":
            return {"resolved_ids": [], "unresolved_ids": [iid], "error_ids": [],
                    "failure_reasons": {}}
        return {"resolved_ids": [], "error_ids": [iid],
                "failure_reasons": {iid: "docker build failed: no space"}}

    sub = pb.probe_subset(all_ids=ids, seed=20260914, size=3, score=score,
                          inspect=lambda img: "amd64", image_for={i: f"img:{i}" for i in ids},
                          swebench_version="5.0.2", now="2026-09-14T00:00:00Z")
    assert sub["size"] == len(sub["ids"]) == 3 and sub["seed"] == 20260914
    assert all(outcomes.get(i, "ok") == "ok" for i in sub["ids"])
    skipped = {s["instance_id"]: s for s in sub["skipped"]}
    assert set(skipped) <= {"i1", "i3"}
    if "i3" in skipped:
        assert "no space" in skipped["i3"]["reason"]
    assert all(a["arch"] == "amd64" for a in sub["images"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_probe.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `probe.py`**

```python
# backend/app/eval/probe.py
"""Choose the fifty instances this machine can actually evaluate.

The criterion is not "the image builds" but "the GOLD patch resolves here":
that proves the build, the emulation, the test runner and the log parser all
work for this instance on this machine. Everything about the choice is
written down — seed, order, skips with reasons, the image architecture used —
so the subset is a fact, not a memory.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Sequence


def seeded_order(ids: Sequence[str], seed: int) -> list[str]:
    order = sorted(ids)
    random.Random(seed).shuffle(order)
    return order


def probe_subset(*, all_ids: Sequence[str], seed: int, size: int,
                 score: Callable[[str], dict[str, Any]],
                 inspect: Callable[[str], str | None], image_for: dict[str, str],
                 swebench_version: str, now: str) -> dict[str, Any]:
    kept: list[str] = []
    skipped: list[dict[str, str]] = []
    images: list[dict[str, str | None]] = []
    for iid in seeded_order(all_ids, seed):
        if len(kept) == size:
            break
        result = score(iid)
        if iid in set(result.get("resolved_ids", [])):
            kept.append(iid)
            images.append({"instance_id": iid, "image": image_for.get(iid),
                           "arch": inspect(image_for[iid]) if iid in image_for else None})
            continue
        reason = (result.get("failure_reasons") or {}).get(iid) or (
            "gold patch did not resolve" if iid in set(result.get("unresolved_ids", []))
            else "harness error")
        skipped.append({"instance_id": iid, "reason": str(reason).splitlines()[0]})
    return {"ids": kept, "seed": seed, "size": len(kept), "skipped": skipped,
            "images": images, "swebench_version": swebench_version, "probed_at": now,
            "criterion": "gold patch resolves on this machine via `swebench eval verified --gold "
                         "--task-repo`; arch is the image the harness actually ran"}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest backend/tests/test_eval_probe.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval/probe.py backend/tests/test_eval_probe.py
git commit -m "Eval 9: the subset is chosen by whether the gold patch resolves here, and every skip is recorded

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: CLI, Makefile, docs, the two gated tests

**Files:**
- Create: `backend/app/eval/__main__.py`, `backend/tests/test_eval_cli.py`, `backend/tests/test_eval_live.py`
- Modify: `Makefile` (targets), `docs/harness.md` (§12), `README.md` (one paragraph + file-table rows), `pyproject.toml` (nothing new — markers exist)

**Interfaces:**
- Produces: `python -m backend.app.eval generate --config C [--n N] [--resume RUN_ID] [--floor F]`, `… score --run RUN_ID [-j 2]`, `… run --config C` (generate then score then report), `… probe [--size 50] [--seed 20260914]`; `main(argv) -> int` with exit codes 0 ok / 2 refused (pre-flight, dirty tree) / 3 resume later / 1 other.

- [ ] **Step 1: Write the failing CLI test**

```python
# backend/tests/test_eval_cli.py
from __future__ import annotations

from backend.app.eval import __main__ as cli


def test_refused_preflight_exits_2(monkeypatch, capsys):
    from backend.app.eval.preflight import PreflightReport
    monkeypatch.setattr(cli, "_preflight", lambda providers: PreflightReport(False, ["no docker"]))
    assert cli.main(["generate", "--config", "claude-single"]) == 2
    assert "no docker" in capsys.readouterr().err


def test_unknown_config_exits_2(monkeypatch, capsys):
    from backend.app.eval.preflight import PreflightReport
    monkeypatch.setattr(cli, "_preflight", lambda providers: PreflightReport(True))
    assert cli.main(["generate", "--config", "nope"]) == 2


def test_tree_sha_and_dirty_are_read_from_git(tmp_path):
    sha, dirty = cli.tree_state(cwd=tmp_path.parent)  # any git checkout
    assert isinstance(sha, str) and len(sha) >= 7 and isinstance(dirty, bool)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_eval_cli.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `__main__.py`**

```python
# backend/app/eval/__main__.py
"""``python -m backend.app.eval`` — generate, score, run, probe.

Exit codes are part of the contract: 0 ok, 2 refused before any work (pre-flight,
dirty tree, unknown config), 3 "resume later" (the governor would wait longer
than EVAL_MAX_WAIT_S, or Ctrl-C), 1 anything else.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..quota import get_governor
from . import checkout as co
from .config import (EVAL_HEADROOM_FLOOR, EVAL_TURN_MAX_TOOL_CALLS, EVAL_TURN_TIMEOUT_S,
                     EVAL_TURN_TOKEN_BUDGET, SUBSET_SEED, SUBSET_SIZE, EvalConfig, eval_root)
from .headless import Budget
from .instances import load_rows, load_subset
from .pacing import GovernorGate
from .preflight import preflight, real_probes
from .probe import probe_subset
from .report import append_history, summarize, write_result_json
from .runner import GenerateRefused, ResumeLater, RunPaths, generate
from .score import docker_image_arch, ensure_task_repo, parse_results, run_scoring

REPO_ROOT = Path(__file__).resolve().parents[3]
SUBSET_FILE = REPO_ROOT / "evals" / "swebench_verified_subset.json"
HISTORY_FILE = REPO_ROOT / "evals" / "HISTORY.md"
RESULTS_DIR = REPO_ROOT / "evals" / "results"


def _preflight(providers: Sequence[str]):
    return preflight(providers=providers, probes=real_probes())


def tree_state(*, cwd: Path = REPO_ROOT) -> tuple[str, bool]:
    sha = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain"], check=True,
                            capture_output=True, text=True).stdout
    return sha, bool(status.strip())


def _swebench_version() -> str:
    return importlib.metadata.version("swebench")


def _progress(rec) -> None:
    print(f"{rec.instance_id:40s} {rec.outcome:18s} {sum(rec.tokens.values()):>8d} tok "
          f"{rec.wall_clock_s:6.0f}s", flush=True)


async def _generate(args) -> int:
    try:
        config = EvalConfig.named(args.config)
    except KeyError:
        print(f"unknown config {args.config!r}", file=sys.stderr)
        return 2
    rep = _preflight(config.governed_providers())
    for w in rep.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not rep.ok:
        for r in rep.refusals:
            print(f"refused: {r}", file=sys.stderr)
        return 2
    sha, dirty = tree_state()
    subset = load_subset(SUBSET_FILE)
    ids = subset.ids[: args.n] if args.n else subset.ids
    rows = load_rows(ids)
    gate = GovernorGate(get_governor(), floor=args.floor)
    budget = Budget(EVAL_TURN_TIMEOUT_S, EVAL_TURN_TOKEN_BUDGET, EVAL_TURN_MAX_TOOL_CALLS)
    try:
        paths = await generate(config, subset, rows=rows, gate=gate, budget=budget,
                               resume=args.resume, tree_sha=sha, dirty=dirty,
                               on_progress=_progress)
    except GenerateRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ResumeLater as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(f"generated {paths.run_id}: {paths.predictions}")
    args.run = paths.run_id
    return 0


def _score(args) -> int:
    paths = RunPaths.for_run(args.run)
    meta = json.loads(paths.meta.read_text())
    subset = load_subset(Path(meta["subset"]) if Path(meta["subset"]).is_absolute()
                         else REPO_ROOT / meta["subset"])
    rows = load_rows(subset.ids)
    task_repo = ensure_task_repo()
    code = run_scoring(paths.predictions, paths.run_id, cwd=eval_root(), task_repo=task_repo,
                       workers=args.workers)
    if code != 0:
        print(f"swebench eval exited {code}; predictions kept at {paths.predictions} — "
              f"re-run `make eval-score RUN={paths.run_id}`", file=sys.stderr)
        return 1
    model_name = f"{meta['config']}@{meta['tree_sha'][:7]}"
    verdicts = parse_results(eval_root() / "logs" / "evaluation", paths.run_id, model_name,
                             subset.ids, image_for={r["instance_id"]: r["image"] for r in rows},
                             inspect=docker_image_arch)
    summary = summarize(paths, verdicts, swebench_version=_swebench_version())
    out = write_result_json(paths, verdicts, summary, RESULTS_DIR)
    append_history(HISTORY_FILE, summary)
    print(f"{summary.resolved}/{summary.n} resolved; {out}")
    return 0


def _probe(args) -> int:
    from .instances import _default_loader
    from .config import DATASET_NAME, DATASET_SPLIT
    rep = _preflight(())
    if not rep.ok:
        for r in rep.refusals:
            print(f"refused: {r}", file=sys.stderr)
        return 2
    all_rows = _default_loader(DATASET_NAME, DATASET_SPLIT, [])  # whole split
    image_for = {r["instance_id"]: r["image"] for r in all_rows}
    task_repo = ensure_task_repo()
    logs = eval_root() / "logs" / "evaluation"

    def score(iid: str) -> dict:
        run_id = f"probe-{iid}"
        run_scoring(Path("gold"), run_id, cwd=eval_root(), task_repo=task_repo, workers=1,
                    instance_ids=[iid], gold=True)
        results = logs / run_id / "results.json"
        return json.loads(results.read_text()) if results.exists() else {"error_ids": [iid]}

    sub = probe_subset(all_ids=list(image_for), seed=args.seed, size=args.size, score=score,
                       inspect=docker_image_arch, image_for=image_for,
                       swebench_version=_swebench_version(),
                       now=datetime.now(timezone.utc).isoformat())
    SUBSET_FILE.write_text(json.dumps(sub, indent=2) + "\n")
    print(f"kept {sub['size']}, skipped {len(sub['skipped'])}: {SUBSET_FILE}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.app.eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--config", required=True)
    g.add_argument("--n", type=int, default=0)
    g.add_argument("--resume", default=None)
    g.add_argument("--floor", type=float, default=EVAL_HEADROOM_FLOOR)
    s = sub.add_parser("score")
    s.add_argument("--run", required=True)
    s.add_argument("--workers", "-j", type=int, default=2)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--n", type=int, default=0)
    r.add_argument("--floor", type=float, default=EVAL_HEADROOM_FLOOR)
    r.add_argument("--workers", "-j", type=int, default=2)
    p = sub.add_parser("probe")
    p.add_argument("--size", type=int, default=SUBSET_SIZE)
    p.add_argument("--seed", type=int, default=SUBSET_SEED)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "generate":
            return asyncio.run(_generate(args))
        if args.cmd == "score":
            return _score(args)
        if args.cmd == "run":
            args.resume = None
            code = asyncio.run(_generate(args))
            return code if code else _score(args)
        return _probe(args)
    except KeyboardInterrupt:
        print("interrupted; the run is resumable with --resume", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the CLI tests**

Run: `.venv/bin/pytest backend/tests/test_eval_cli.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Makefile targets** (append, keep the `##` help convention)

```make
eval: ## Generate + score + record one eval run: make eval CONFIG=claude-single [N=50]
	.venv/bin/python -m backend.app.eval run --config $(CONFIG) $(if $(N),--n $(N),)

eval-score: ## Re-score an existing run: make eval-score RUN=<run_id>
	.venv/bin/python -m backend.app.eval score --run $(RUN)

eval-probe: ## Rebuild evals/swebench_verified_subset.json by gold-patch probing (hours; Docker)
	.venv/bin/python -m backend.app.eval probe

eval-selftest: ## The Docker-backed scoring self-test (slow marker)
	.venv/bin/pytest backend/tests/test_eval_live.py -q -m slow -s
```

- [ ] **Step 6: The two gated tests**

```python
# backend/tests/test_eval_live.py
"""Gated end-to-end checks. Neither runs in the default suite."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend.app.eval.score import ensure_task_repo, parse_results, run_scoring
from backend.app.eval.config import eval_root


@pytest.mark.slow
def test_gold_patch_resolves_one_instance_through_the_real_harness():
    """Docker + network. Proves the score phase end to end on this machine."""
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH")
    iid = "sympy__sympy-20590"  # the README's own validate-gold example
    task_repo = ensure_task_repo()
    code = run_scoring(Path("gold"), "selftest-gold", cwd=eval_root(), task_repo=task_repo,
                       workers=1, instance_ids=[iid], gold=True)
    assert code == 0
    logs = eval_root() / "logs" / "evaluation"
    verdicts = parse_results(logs, "selftest-gold", "gold", [iid], image_for={}, inspect=lambda i: None)
    assert verdicts[0].status == "resolved"


@pytest.mark.requires_cli
async def test_one_instance_through_the_real_claude_binary(tmp_path: Path):
    """Real `claude` on PATH. Proves the generate phase against the vendor binary."""
    from backend.app.eval.config import EvalConfig
    from backend.app.eval.headless import Budget, run_headless_turn
    (tmp_path / "hello.py").write_text("def hello():\n    return 'helo'\n")
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    rec = await run_headless_turn("Fix the typo in hello.py so hello() returns 'hello'.",
                                  str(tmp_path), config=EvalConfig.named("claude-single"),
                                  budget=Budget(300.0, 100_000, 30))
    assert rec.outcome == "patched"
    assert "hello" in (tmp_path / "hello.py").read_text()
```

Confirm `addopts` in `pyproject.toml` still excludes both markers (`-m 'not requires_cli and not slow'`).

- [ ] **Step 7: Docs**

`docs/harness.md` — add `## 12. The eval foundation` after §11: what the number is (SWE-bench Verified, the committed 50, official harness), how to run (`make eval CONFIG=claude-single`), how to read `HISTORY.md`, the three budgets and the headroom floor with their values, what a `no_patch` / `budget_*` / `eval_error` means, the arm64 truth (amd64 images built with Buildx, run emulated; the probe records the arch), exit codes, and what is deliberately absent (retries, parallel instances, API keys). `README.md` — one paragraph under the quota paragraph: "The number that says whether the harness got better: `make eval` …", and rows for `eval/` and `evals/` in the file table.

- [ ] **Step 8: Full verification**

Run:
```bash
perl -e 'alarm 900; exec @ARGV' .venv/bin/pytest backend/tests -q
.venv/bin/pytest backend/tests -q -W error::UserWarning
.venv/bin/ruff check backend && make lint
pgrep -fl "fleet.subproc|echo_worker|tree_worker|fake_codex_app_server"
ls ~/.localcode/quota.json 2>/dev/null; stat -f '%Sm %z' ~/.localcode/usage.jsonl
```
Expected: all tests PASS (the default suite grows by ~35), `-W error` clean, ruff and lint clean, no strays, no `quota.json`, `usage.jsonl` mtime unchanged (Sep 13 11:38).

- [ ] **Step 9: Commit**

```bash
git add backend/app/eval/__main__.py backend/tests/test_eval_cli.py backend/tests/test_eval_live.py Makefile docs/harness.md README.md
git commit -m "Eval 10: make eval runs generate, score and record; the docs say what the number means

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: The probe and the two baselines

This task runs the instrument; it produces committed data, not code. Budget the wall clock: the probe builds ~55-70 images at several minutes each under emulation (hours; run it overnight with Docker Desktop memory raised to ≥ 12 GiB); each baseline is 50 agent turns paced by the governor (several 5-hour windows).

**Files:**
- Create: `evals/swebench_verified_subset.json` (probe output)
- Modify: `evals/HISTORY.md` (two rows)

- [ ] **Step 1: Raise Docker memory and run the self-test**

Docker Desktop → Settings → Resources → Memory ≥ 12 GiB, Apply & Restart. Then:

Run: `make eval-selftest`
Expected: `1 passed` — the gold patch for `sympy__sympy-20590` resolves through the real harness on this machine (first run pulls/builds the image; minutes).

- [ ] **Step 2: Run the probe**

Run: `perl -e 'alarm 43200; exec @ARGV' make eval-probe`
Expected: ends with `kept 50, skipped N: …/evals/swebench_verified_subset.json`. Inspect the file: 50 ids, every `images[].arch` present, every skip with a reason.

- [ ] **Step 3: Commit the subset**

```bash
git add evals/swebench_verified_subset.json
git commit -m "Eval 11a: the standing subset — 50 Verified instances whose gold patch resolves on this machine

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 4: Baseline, single agent**

Run: `make eval CONFIG=claude-single`
Expected: fifty progress lines, waits logged when headroom dips, then `R/50 resolved; evals/results/<run_id>.json`; one new row in `evals/HISTORY.md`. If it exits 3, rerun with `.venv/bin/python -m backend.app.eval generate --config claude-single --resume <run_id>` then `make eval-score RUN=<run_id>`.

- [ ] **Step 5: Baseline, fleet**

Run: `make eval CONFIG=claude-fleet`
Expected: as above; a second row.

- [ ] **Step 6: Commit the baselines**

```bash
git add evals/HISTORY.md
git commit -m "Eval 11b: baselines — claude-single and claude-fleet on the standing subset

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.** §1 components → Tasks 1-10 (one file each; `make` targets in Task 10). §2 six steps → Task 4 (checkout, patch, cleanup), Task 6 (prompt, turn, record, predictions, resume, dirty tree, run id). §3 pacing/budgets → Task 5 (gate, floor, unknown-proceeds, max wait) + Task 2 (three limits through the cancel path, token/cost sums). §4 subset/pre-flight → Task 9 (probe, seed, skips, arch) + Task 1 (pre-flight thresholds) + Task 11 (the run). §5 failures → Task 6 (checkout_error and the three-strikes rule, budget outcomes, interrupted, ResumeLater exit 3), Task 7 (eval_error, re-score with cached run_id), Task 2 (provider_error, denials counted). Disk re-check between instances → Task 6 Step 3b (`DISK_PAUSE_GIB`, the `disk_probe` seam, `ResumeLater` with the resume hint, and its test). §6 tests → each task; gated tests in Task 10; outputs → Task 8 (results JSON, HISTORY), Task 9/11 (subset). Baseline → Task 11.

**Placeholder scan.** No TBD/TODO. Every code step has code. The `fleet_override` shape in Task 1 carries an explicit instruction to verify against the loader rather than a guess.

**Type consistency.** `TurnRecord` fields (Task 2) match `InstanceRecord` construction (Task 6) and the record JSON read by `summarize` (Task 8). `RunPaths.for_run(run_id, root=)` is used identically in Tasks 6, 8, 10. `Verdict` fields (Task 7) match `write_result_json` and the tests in Task 8. `score_command`/`run_scoring` signatures match Task 10's calls (`Path("gold")` with `gold=True` ignores the predictions path, as `-p` is not emitted). `Subset` (Task 3) is what Tasks 6 and 10 pass. `EvalConfig.governed_providers()` (Task 1) is what the gate receives in Task 6.
