"""Fleet vocabulary, timing budgets, and step-control exceptions.

These have no dependencies on the rest of the package, so every other fleet
module can import from here without risking a cycle.
"""
from __future__ import annotations

from typing import Literal

VALID_PROVIDERS = ("claude", "opencode")

# Canonical execution order:
#   planner  → produces the markdown plan (committed to disk)
#   developer→ optional design step (legacy "design + code" presets)
#   coder    → implements the plan
#   reviewer → gates the implementation against the plan; on NACK, the coder
#              is re-run with the feedback and the reviewer runs again
#              (bounded by ``cfg.max_review_retries``)
#   tester   → final smoke test — writes and runs tests against whatever
#              implementation survived the reviewer gate. Tester results are
#              reported but do NOT trigger further retries; the user acts on
#              the report.
#
# Why tester is last: the reviewer is a code-review pass (catches plan-
# compliance + obvious issues), the tester is a behaviour-verification pass.
# In that order, the tester gives the final word: "the code that the reviewer
# signed off actually works under tests" (or doesn't).
VALID_ROLES = ("planner", "developer", "coder", "reviewer", "tester")
WORKER_ROLES: tuple[str, ...] = ("developer", "coder", "reviewer", "tester")
StepRole = Literal["developer", "coder", "reviewer", "tester"]


# Cadence at which a long-running sub-provider step (e.g. opus thinking for 3
# minutes on a complex plan) emits a chat heartbeat so the UI doesn't look
# frozen. Heartbeats carry ``heartbeat: True`` in their data and are filtered
# out of the persisted message blocks — they're chrome for the live UI only.
HEARTBEAT_INTERVAL_S = 30.0

# Maximum wall-clock time we'll wait on a single sub-provider step before
# treating it as hung and aborting the turn. 10 min is generous (claude-opus
# on a complex markdown plan can legitimately take 3-4 min) but bounded so a
# stuck CLI / network blackhole can't pin a session forever. Surfaces as a
# tool_result with is_error=True followed by a ``StepTimeoutError`` that
# propagates up through ``_safe_run`` so the WS gets a clean ``error`` +
# ``assistant.done`` close-out instead of silently waiting.
STEP_TIMEOUT_S = 600.0


class StepTimeoutError(RuntimeError):
    """Raised by ``_run_step_with_role`` when a sub-provider exceeds the
    per-step budget. Distinct from generic exceptions so the outer pipeline
    can recognise "step abandoned" vs "step errored mid-flight"."""
