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
}
