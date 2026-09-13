"""Fleet vocabulary, timing budgets, and step-control exceptions.

These have no dependencies on the rest of the package, so every other fleet
module can import from here without risking a cycle.
"""
from __future__ import annotations

from typing import Literal

VALID_PROVIDERS = ("claude", "codex", "opencode")

# "Whichever subscription has the most headroom left." NOT a member of
# VALID_PROVIDERS and never added to it: it names no backend, and everything
# downstream of ``dispatch_subagent`` (the worker key, the sub-provider
# lookup) must only ever see a real one. It is resolved — by the quota
# governor, at the single site where a role becomes a RoleConfig — BEFORE
# that validation, which is why the loader lets it through.
AUTO_PROVIDER = "auto"

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

# Startup grace: a *healthy* sub-provider streams its first event within a
# few seconds (model warm-up + first token). If we get ZERO events from it
# within this window, the backend is almost certainly wedged (auth prompt,
# nested-SDK deadlock, dead socket) — fail fast and loud instead of pretending
# "still working" for the full STEP_TIMEOUT_S. This is the single biggest
# anti-"silent communication break" guardrail: a 10-minute silent hang
# becomes a ~1-minute clearly-surfaced error.
STARTUP_GRACE_S = 75.0

# How many times the orchestrator may re-dispatch the SAME role after it
# hard-fails (timeout / unresponsive backend) before dispatch refuses and
# tells the orchestrator to abort. Stops the unbounded silent retry loop
# where a wedged planner is re-dispatched forever.
DISPATCH_HARD_FAIL_CAP = 2


# ─────────────────────────────────────────────────────────────────────────────
# Worker wire protocol (v2) — shared vocabulary between the pool (parent) and
# ``subproc.py`` (child). They live here, with no dependencies, so neither side
# can drift from the other by editing its own private copy of a marker string.
# ─────────────────────────────────────────────────────────────────────────────

# ``@@FIRST@@ <request id>`` — the child's first sign of life for one request.
FIRST_MARKER = "@@FIRST@@"
# ``@@RESULT@@ <request id> <byte length>`` followed by EXACTLY that many bytes
# of JSON. The length prefix, not a newline, is what terminates the payload:
# the previous newline-delimited framing made correctness depend on the result
# staying under the stdout reader's (undocumented, 64 KiB) line limit, and a
# plan larger than that deadlocked the parent — it gave up on the line while
# the child was still blocked writing it into a pipe nobody was draining.
RESULT_MARKER = "@@RESULT@@"

# Generous ceiling for ONE line on a worker's stdout. The framed result no
# longer depends on it, so this only has to survive a long diagnostic line the
# vendor CLI prints; 8 MiB means an oversize log line is a logged warning
# rather than a dead worker. Raising this alone was never the fix — see
# ``RESULT_MARKER``.
WORKER_STDOUT_LIMIT = 8 * 1024 * 1024

# Environment variable naming the pool-owned directory a worker writes its
# pidfile into. Passed rather than derived so parent and child always agree on
# the path even when ``HOME`` differs between them (which is exactly what the
# test suite does).
WORKER_PID_DIR_ENV = "LOCALCODE_WORKER_PID_DIR"


class StepNotAttemptedError(RuntimeError):
    """Raised when a step is dropped before any sub-provider saw it.

    Distinct from every other failure because the retry cap must NOT count it.
    A step that was still queued behind another step on the same worker did not
    demonstrate anything about its backend — charging it against
    ``DISPATCH_HARD_FAIL_CAP`` would refuse a role for a failure it was never
    given the chance to have.
    """


class StepTimeoutError(RuntimeError):
    """Raised by ``_run_step_with_role`` when a sub-provider exceeds the
    per-step budget. Distinct from generic exceptions so the outer pipeline
    can recognise "step abandoned" vs "step errored mid-flight"."""
