"""``FleetProvider`` — composes Claude + OpenCode into a multi-agent workflow.

Stateless across turns: every per-turn datum is a local in ``run()`` so
concurrent turns sharing the singleton can't clobber each other's state.
Per-role invocations are surfaced as tool_use → tool_result pairs on the
unified event stream so the chat UI renders them as expandable cards.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

from ...config import get_settings
from ..base import Event, RunContext
from .constants import (
    HEARTBEAT_INTERVAL_S,
    STARTUP_GRACE_S,
    STEP_TIMEOUT_S,
    StepNotAttemptedError,
    StepTimeoutError,
)
from .envelope import StepResult
from .gate import GATE_ROLES, parse_verdict
from .loader import _merge_config, load_fleet_config_async
from .models import FleetConfig, RoleConfig, Step
from .pool import WorkerPool, worker_key

logger = logging.getLogger(__name__)

# Worker module + the directory it must be importable from. Derived from this
# module's own dotted name / file location so it's correct whether the app is
# launched as ``backend.app...`` or ``app...``.
_WORKER_MODULE = __name__.rsplit(".", 1)[0] + ".subproc"
# fleet/provider.py → parents: [fleet, orchestrator, app, backend, <root>].
# The number of parents to climb == package depth of __name__.
_REPO_ROOT = str(Path(__file__).resolve().parents[__name__.count(".")])


class FleetProvider:
    """Composes Claude + OpenCode into a planner/coder/reviewer workflow.

    Stateless across turns — every per-turn datum is a local in `run()` so
    concurrent turns don't clobber each other's state.

    The one exception, and it is deliberate: the :class:`WorkerPool`. Worker
    processes are the thing that must NOT be per-turn — paying interpreter
    start + SDK import + CLI spawn on every step was 0.7 to 1.0 s of dead time
    per step. The pool holds no turn state of its own (its keys carry the
    session, and each request builds a fresh sub-provider inside the worker),
    so sharing it across turns cannot leak one turn's context into another's.
    """

    name = "fleet"

    def __init__(self) -> None:
        self._pool: WorkerPool | None = None
        # The loop the pool's futures and tasks belong to. A pool built on a
        # dead loop is unusable, and this singleton outlives any one loop in
        # the test suite.
        self._pool_loop: asyncio.AbstractEventLoop | None = None

    async def open_session(self, ctx: RunContext) -> str:
        return ctx.upstream_session_id or ""

    async def close_session(self, session_id: str) -> None:
        # Stateless across turns by design (see the class docstring): every
        # per-turn datum is a local in ``run()``, so there is nothing to free.
        # The pool is NOT per-session — it is keyed by session and reaps its
        # own idle workers — so closing one session must not close it.
        return None

    async def aclose(self) -> None:
        pool, self._pool = self._pool, None
        self._pool_loop = None
        if pool is not None:
            await pool.aclose()

    def _get_pool(self) -> WorkerPool:
        """The pool for the running loop, created on first use.

        Lazily, because a pool built at import time would bind its lock and
        tasks to whatever loop happened to be current then — and there is no
        loop at import time at all. Rebuilt when the loop changes, killing the
        old pool's workers first: a worker whose reader task lives on a dead
        loop can never be reaped through it, so abandoning it silently is how
        a billable orphan is created.
        """
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._pool_loop is loop:
            return self._pool
        if self._pool is not None:
            for key in self._pool.keys:
                self._pool.kill(key)
        settings = get_settings()
        self._pool = WorkerPool(
            max_workers=int(getattr(settings, "fleet_max_workers", 4)),
            idle_timeout_s=float(getattr(settings, "fleet_worker_idle_s", 300.0)),
            worker_module=_WORKER_MODULE,
            repo_root=_REPO_ROOT,
        )
        self._pool_loop = loop
        return self._pool

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        # ALL per-turn state must live as locals here, not as instance attrs —
        # FleetProvider is a singleton and concurrent turns share `self`.
        t0 = time.time()
        # Async: a cache miss reads and parses YAML, and this runs on the
        # turn's event loop — where a synchronous parse stalls every other
        # session's streaming for its duration.
        cfg = await load_fleet_config_async(ctx.cwd)
        # Per-session UI override layered on top of the file config.
        ui_override = ctx.extras.get("fleet_config_override") if ctx.extras else None
        if isinstance(ui_override, dict) and ui_override:
            base_src = cfg.config_source or "<built-in defaults>"
            cfg = _merge_config(cfg, ui_override)
            cfg.config_source = f"{base_src} + UI override"

        # Exactly one `assistant.done` per turn: every subscriber clears its
        # working indicator on it, and the turn accumulator persists the LAST
        # one it sees. A second, orchestrator-less done therefore overwrote
        # `cost_usd` with None and every fleet turn showed up free in history.
        # So we pass the orchestrator's done through — it is the only event
        # that knows the cost — with our wall time merged into it.
        saw_done = False
        if not cfg.role_names():
            yield Event(
                type="error",
                data={"message": "fleet has no agents configured"},
            )
        else:
            # Single path now: the LLM-driven orchestrator. It handles
            # planner / coder / reviewer / tester registries equally well as
            # single-role registries (it just dispatches the one available
            # agent), so we don't need separate single-agent or linear-
            # pipeline branches.
            async for ev in self._run_orchestrated(ctx, cfg):
                if ev.type == "assistant.done":
                    saw_done = True
                    ev = Event(
                        type="assistant.done",
                        data={**ev.data, "duration_ms": self._elapsed_ms(t0)},
                    )
                yield ev

        if not saw_done:
            # Error paths never reach a ResultMessage, so nothing upstream
            # terminated the turn. Emit the one terminal event ourselves.
            yield Event(
                type="assistant.done",
                data={"duration_ms": self._elapsed_ms(t0)},
            )

    @staticmethod
    def _elapsed_ms(t0: float) -> int:
        """Wall time for the whole fleet turn — which only ``run()`` spans.
        The orchestrator's own ``duration_ms`` covers its model loop, so we
        overwrite it rather than leave the UI showing part of the turn."""
        return int((time.time() - t0) * 1000)

    async def _run_orchestrated(
        self, ctx: RunContext, cfg: FleetConfig
    ) -> AsyncIterator[Event]:
        """LLM-driven dispatch path (Tier-4).

        Builds a per-turn registry from ``cfg.roles``, instantiates an
        ``OrchestratorAgent`` with a fresh MCP dispatch server, and yields
        the merged event stream (orchestrator narrative + sub-agent cards).
        """
        # Lazy imports — these modules import RoleConfig/Step from this
        # package, so we keep the dependency one-way at import time.
        from ..agent_def import registry_from_role_library
        from ..orchestrator import OrchestratorAgent

        registry = registry_from_role_library(cfg.roles)
        orchestrator = OrchestratorAgent(
            registry=registry,
            run_step_fn=self._run_step_with_role,
            require_plan_approval=cfg.require_plan_approval,
        )
        async for ev in orchestrator.run(ctx):
            yield ev

    async def _run_step_with_role(
        self,
        step: Step,
        role_cfg: RoleConfig,
        ctx: RunContext,
        outputs: dict[str, StepResult],
    ) -> AsyncIterator[Event]:
        """Invoke ``role_cfg`` with ``step.prompt`` exactly. The caller is
        responsible for stitching plan + prior-step context into ``prompt`` —
        we just delegate to the sub-provider and emit tool_use/tool_result.

        The step's :class:`StepResult` envelope is recorded in ``outputs`` under
        ``step.id``; the caller decides which view of it to use.

        Two safeguards keep this robust:

        - **Heartbeats** every ``HEARTBEAT_INTERVAL_S`` so the UI doesn't
          look frozen during a multi-minute opus turn. Marked
          ``heartbeat: True`` so the WS handler keeps them out of persisted
          history.
        - **Per-step timeout** ``STEP_TIMEOUT_S``. On exceeding the budget
          we yield a ``tool.result`` with ``is_error=True`` and raise
          ``StepTimeoutError``, which propagates up through ``_safe_run``
          and surfaces as a clean ``error`` + ``assistant.done`` to the
          frontend. Without this a hung sub-provider would pin the WS
          forever.
        """
        display = step.prompt if len(step.prompt) <= 600 else step.prompt[:600] + "…"
        yield Event(
            type="assistant.tool_use",
            data={
                "id": step.id,
                "name": f"{step.role} [{role_cfg.provider}:{role_cfg.model}]",
                "input": {"prompt": display},
            },
        )

        # Run the sub-provider in a SEPARATE OS PROCESS (see pool.py /
        # subproc.py). A thread+loop is not enough — claude-agent-sdk has
        # process-global async-generator state, so a nested query() under the
        # orchestrator's query() raises "aclose(): asynchronous generator is
        # already running". A child process is fully isolated, and lets us
        # KILL a wedged `claude` CLI for real. The process is POOLED: the first
        # step on a key pays the interpreter + SDK import, later steps on the
        # same key pay nothing. `first` flips the instant the worker reports
        # this request's first event.
        _s = get_settings()
        grace_s = float(getattr(_s, "fleet_startup_grace_s", STARTUP_GRACE_S))
        step_budget_s = float(getattr(_s, "fleet_step_timeout_s", STEP_TIMEOUT_S))
        pool = self._get_pool()
        key = worker_key(ctx.session_id, role_cfg.provider, role_cfg.model, ctx.cwd)
        first, collect = await pool.submit(
            key,
            {
                "provider": role_cfg.provider,
                "model": role_cfg.model,
                "system_prompt": role_cfg.system_prompt,
                "prompt": step.prompt,
                "cwd": ctx.cwd,
                "additional_dirs": ctx.additional_dirs or [],
                "permission_mode": ctx.permission_mode,
                "role_name": step.role,
                # Forwarded so the sub-provider can key per-session state on
                # the LocalCode session this step belongs to rather than
                # treating each step as an unrelated session.
                "session_id": ctx.session_id,
            },
        )
        # FLOAT, and advanced by the interval itself rather than ``int()`` of
        # it. Truncating meant any interval under 1 s advanced this by zero, so
        # neither ``grace_s`` nor ``step_budget_s`` was ever reached and the
        # step looped forever — and both of those ARE settings-driven while the
        # interval is not, so lowering them alone could not fast-fail.
        elapsed_s = 0.0
        output: StepResult | None = None
        error_text: str | None = None
        timed_out = False
        not_attempted: StepNotAttemptedError | None = None
        try:
            while True:
                try:
                    output = await asyncio.wait_for(
                        asyncio.shield(collect), timeout=HEARTBEAT_INTERVAL_S
                    )
                    break
                except TimeoutError:
                    elapsed_s += HEARTBEAT_INTERVAL_S
                    started = first.is_set()
                    # A step still QUEUED behind another step on the same
                    # worker has produced no output because nothing has been
                    # asked of it yet. "The backend produced NO output" would be
                    # a false accusation, and after D7.1 it is one that counts
                    # against the role's retry cap. The absolute ceiling below
                    # is unconditional, so a step queued forever is still
                    # bounded — it just is not blamed on the backend.
                    queued = pool.is_queued(key, collect)
                    # Fast-fail: zero output within the startup grace window
                    # means the backend is wedged (auth prompt, dead socket,
                    # nested-SDK deadlock). Don't pretend to wait the full
                    # STEP_TIMEOUT_S — abort loudly now.
                    if not started and not queued and elapsed_s >= grace_s:
                        timed_out = True
                        error_text = (
                            f"{step.role}: the {role_cfg.provider} backend "
                            f"produced NO output within {int(grace_s)}s "
                            f"— treating it as unresponsive and aborting this "
                            f"step. The backend is likely not authenticated, "
                            f"hung, or unreachable; this is NOT a slow model."
                        )
                        logger.warning(
                            "fleet step %s (%s:%s) fast-failed: no output in %ds",
                            step.role, role_cfg.provider, role_cfg.model, int(grace_s),
                        )
                        break
                    # Absolute ceiling for a backend that streams but never
                    # finishes.
                    if elapsed_s >= step_budget_s:
                        timed_out = True
                        error_text = (
                            f"{step.role} step exceeded {int(step_budget_s)}s "
                            f"budget — aborting. Check that the "
                            f"{role_cfg.provider} backend is healthy."
                        )
                        logger.warning(
                            "fleet step %s (%s:%s) hit %ds ceiling",
                            step.role, role_cfg.provider, role_cfg.model,
                            int(step_budget_s),
                        )
                        break
                    # Honest heartbeat: don't say "still working" when we've
                    # heard nothing at all, and don't blame the backend for a
                    # step that is waiting its turn on the worker.
                    if started:
                        msg = f"_…{step.role} still working ({elapsed_s:.0f}s)…_\n"
                    elif queued:
                        msg = (
                            f"_…{step.role} queued behind another step on the "
                            f"same worker ({elapsed_s:.0f}s)…_\n"
                        )
                    else:
                        msg = (
                            f"_…waiting for the {role_cfg.provider} backend — "
                            f"no response yet ({elapsed_s:.0f}s)…_\n"
                        )
                    yield Event(
                        type="assistant.text",
                        data={"text": msg, "heartbeat": True},
                    )
        except StepNotAttemptedError as exc:
            # No sub-provider ever saw this step, so it says nothing about the
            # backend. Kept distinct all the way up so ``dispatch.py`` does not
            # charge it against the role's retry cap.
            not_attempted = exc
            error_text = str(exc) or repr(exc)
        except Exception as exc:
            # Sub-provider raised in the child (propagated through result).
            # Includes the worker's stderr tail + exit code (see
            # WorkerPool._reap) so this is diagnosable from backend.log,
            # never an opaque "exited without a result".
            error_text = str(exc) or repr(exc)
            logger.warning(
                "fleet step %s (%s:%s) failed: %s",
                step.role, role_cfg.provider, role_cfg.model, error_text,
            )
        finally:
            # True cancellation, but ONLY when this step did not finish. A
            # resolved future — success OR a structured error the worker itself
            # reported — means the worker answered and is healthy, so it stays
            # in the pool for the next step; killing it unconditionally (as the
            # per-dispatch handle did, because its process served one step) is
            # what would give the pool back the interpreter-start cost it exists
            # to remove.
            #
            # An UNRESOLVED future is the dangerous case: a timeout, a
            # fast-fail, or a generator aclose() on WS disconnect. ``abandon``
            # — not ``kill`` — because only the pool knows whether THIS request
            # reached a worker: if it did, the worker is wedged holding a vendor
            # CLI and its group is killed; if it was still queued, it owns
            # nothing and killing the key would have failed somebody else's
            # running step to cancel one that never started. Cancel first, so
            # the pool's failure does not resolve a future nobody is left to
            # read.
            if not collect.done():
                collect.cancel()
                pool.abandon(key, collect)

        if error_text is not None:
            yield Event(
                type="tool.result",
                data={"tool_use_id": step.id, "content": error_text, "is_error": True},
            )
            if not_attempted is not None:
                # Same shape as the timeout bubble-up, different type on
                # purpose: the caller must be able to tell "the backend failed"
                # from "this step never ran".
                raise not_attempted
            if timed_out:
                # Bubble up so the outer pipeline aborts cleanly rather than
                # racing on with no output for this step. _safe_run will
                # surface this as an `error` event + `assistant.done`.
                raise StepTimeoutError(error_text)
            return

        # Successful step — record output and emit the result card.
        assert output is not None  # if no error_text, we broke out with output set
        # The ENVELOPE, not text. Callers need two views of one step: the
        # bounded ``context_text()`` that goes into the orchestrator's context,
        # and the full output (via the artifact the envelope points at) for
        # anything written to disk as a document — see ``dispatch._full_output``
        # and the plan file it repairs. Recording only the bounded view makes
        # the second view unrecoverable.
        outputs[step.id] = output
        # What the UI card and the orchestrator see is still bounded: a 2 MB
        # step output is a summary plus an artifact pointer by the time it
        # lands here, and ``context_text()`` is the only place that rule lives
        # (see ``envelope.py``).
        context = output.context_text()
        # Mark gate failures as errored tool results so the UI shows them red.
        # Prefer the verdict the step already parsed from its FULL output; fall
        # back to parsing the envelope text (JSON block first, then the
        # canonical last-line classifier, fail-safe to NACK) so a gate whose
        # envelope arrived without a verdict still routes correctly rather than
        # reading as a pass.
        is_error = False
        if step.role in GATE_ROLES:
            value = (output.structured or {}).get("value")
            if not value:
                value = parse_verdict(context, step.role).value
            is_error = value != "lgtm"
        yield Event(
            type="tool.result",
            # Full envelope text, not just the summary: the UI card is the
            # place a human goes to see what the step actually said.
            data={"tool_use_id": step.id, "content": context, "is_error": is_error},
        )
