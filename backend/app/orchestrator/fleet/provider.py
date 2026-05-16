"""``FleetProvider`` — composes Claude + OpenCode into a multi-agent workflow.

Stateless across turns: every per-turn datum is a local in ``run()`` so
concurrent turns sharing the singleton can't clobber each other's state.
Per-role invocations are surfaced as tool_use → tool_result pairs on the
unified event stream so the chat UI renders them as expandable cards.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from ..base import Event, RunContext
from .collect import collect_text
from .constants import HEARTBEAT_INTERVAL_S, STEP_TIMEOUT_S, StepTimeoutError
from .gate import classify_gate
from .loader import _merge_config, load_fleet_config
from .models import FleetConfig, RoleConfig, Step


class FleetProvider:
    """Composes Claude + OpenCode into a planner/coder/reviewer workflow.

    Stateless across turns — every per-turn datum is a local in `run()` so
    concurrent turns don't clobber each other's state.
    """

    name = "fleet"

    async def open_session(self, ctx: RunContext) -> str:
        return ctx.upstream_session_id or ""

    async def aclose(self) -> None:
        return None

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        # ALL per-turn state must live as locals here, not as instance attrs —
        # FleetProvider is a singleton and concurrent turns share `self`.
        t0 = time.time()
        cfg = load_fleet_config(ctx.cwd)
        # Per-session UI override layered on top of the file config.
        ui_override = ctx.extras.get("fleet_config_override") if ctx.extras else None
        if isinstance(ui_override, dict) and ui_override:
            base_src = cfg.config_source or "<built-in defaults>"
            cfg = _merge_config(cfg, ui_override)
            cfg.config_source = f"{base_src} + UI override"

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
                yield ev

        yield Event(
            type="assistant.done",
            data={"duration_ms": int((time.time() - t0) * 1000)},
        )

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
        outputs: dict[str, str],
    ) -> AsyncIterator[Event]:
        """Invoke ``role_cfg`` with ``step.prompt`` exactly. The caller is
        responsible for stitching plan + prior-step context into ``prompt`` —
        we just delegate to the sub-provider and emit tool_use/tool_result.

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

        # Run collect_text concurrently with a heartbeat ticker. shield()
        # protects the inner task from wait_for's cancel-on-timeout — we want
        # the timeout to fire the heartbeat, not abort the inner work.
        collect = asyncio.create_task(
            collect_text(role_cfg, step.prompt, ctx.cwd, ctx.additional_dirs)
        )
        elapsed_s = 0
        output: str | None = None
        error_text: str | None = None
        timed_out = False
        try:
            while True:
                try:
                    output = await asyncio.wait_for(
                        asyncio.shield(collect), timeout=HEARTBEAT_INTERVAL_S
                    )
                    break
                except TimeoutError:
                    elapsed_s += int(HEARTBEAT_INTERVAL_S)
                    if elapsed_s >= STEP_TIMEOUT_S:
                        timed_out = True
                        error_text = (
                            f"{step.role} step exceeded {int(STEP_TIMEOUT_S)}s budget "
                            f"with no response — aborting turn. Check that the "
                            f"{role_cfg.provider} backend is healthy."
                        )
                        break
                    yield Event(
                        type="assistant.text",
                        data={
                            "text": f"_…{step.role} still working ({elapsed_s}s)…_\n",
                            "heartbeat": True,
                        },
                    )
        except Exception as exc:
            # Sub-provider raised (or our own cancellation cascaded). Capture
            # the message for the tool_result; the finally block will cancel
            # the inner task.
            error_text = str(exc) or repr(exc)
        finally:
            # Belt-and-braces: cancel the inner task on every exit path so
            # it doesn't leak past this scope (the outer generator may be
            # aclose()'d on WS disconnect, in which case the try blocks above
            # don't catch it).
            if not collect.done():
                collect.cancel()
                try:
                    await collect
                except (asyncio.CancelledError, Exception):
                    pass

        if error_text is not None:
            yield Event(
                type="tool.result",
                data={"tool_use_id": step.id, "content": error_text, "is_error": True},
            )
            if timed_out:
                # Bubble up so the outer pipeline aborts cleanly rather than
                # racing on with no output for this step. _safe_run will
                # surface this as an `error` event + `assistant.done`.
                raise StepTimeoutError(error_text)
            return

        # Successful step — record output and emit the result card.
        assert output is not None  # if no error_text, we broke out with output set
        outputs[step.id] = output
        # Mark gate failures as errored tool results so the UI shows them red.
        # We use the canonical classify_gate (last-line parse, fail-safe to
        # NACK) so a reviewer that buries its verdict under prose still gets
        # routed correctly.
        is_error = step.role in ("reviewer", "tester") and classify_gate(
            output, step.role
        ) != "lgtm"
        yield Event(
            type="tool.result",
            data={"tool_use_id": step.id, "content": output, "is_error": is_error},
        )
