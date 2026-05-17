"""``FleetProvider`` — composes Claude + OpenCode into a multi-agent workflow.

Stateless across turns: every per-turn datum is a local in ``run()`` so
concurrent turns sharing the singleton can't clobber each other's state.
Per-role invocations are surfaced as tool_use → tool_result pairs on the
unified event stream so the chat UI renders them as expandable cards.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

from ..base import Event, RunContext
from .constants import (
    HEARTBEAT_INTERVAL_S,
    STARTUP_GRACE_S,
    STEP_TIMEOUT_S,
    StepTimeoutError,
)
from .gate import classify_gate
from .loader import _merge_config, load_fleet_config
from .models import FleetConfig, RoleConfig, Step

# Worker module + the directory it must be importable from. Derived from this
# module's own dotted name / file location so it's correct whether the app is
# launched as ``backend.app...`` or ``app...``.
_WORKER_MODULE = __name__.rsplit(".", 1)[0] + ".subproc"
# fleet/provider.py → parents: [fleet, orchestrator, app, backend, <root>].
# The number of parents to climb == package depth of __name__.
_REPO_ROOT = str(Path(__file__).resolve().parents[__name__.count(".")])


class _SubprocHandle:
    """One out-of-process sub-provider run.

    Owns the child process and exposes:
      - ``first`` : an ``asyncio.Event`` set the instant the child reports its
        first sub-provider event (drives honest heartbeats / fast-fail);
      - ``result``: a future resolving to the collected text, or raising;
      - ``kill()``: true OS-level cancellation of a wedged ``claude`` CLI.
    """

    def __init__(self) -> None:
        self.first: asyncio.Event = asyncio.Event()
        self.result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._proc: asyncio.subprocess.Process | None = None

    async def start(
        self,
        role_cfg: RoleConfig,
        prompt: str,
        cwd: str | None,
        additional_dirs: list[str] | None,
        permission_mode: str | None,
    ) -> None:
        req = json.dumps(
            {
                "provider": role_cfg.provider,
                "model": role_cfg.model,
                "system_prompt": role_cfg.system_prompt,
                "prompt": prompt,
                "cwd": cwd,
                "additional_dirs": additional_dirs or [],
                "permission_mode": permission_mode,
            }
        )
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _WORKER_MODULE,
            cwd=_REPO_ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write((req + "\n").encode())
        await self._proc.stdin.drain()
        self._proc.stdin.close()
        asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        payload: dict | None = None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\n")
                if line == "@@FIRST@@":
                    self.first.set()
                elif line.startswith("@@RESULT@@ "):
                    payload = json.loads(line[len("@@RESULT@@ ") :])
                    break
        except Exception:  # noqa: BLE001
            payload = None
        finally:
            try:
                await self._proc.wait()
            except Exception:
                pass
        if self.result.done():
            return
        if payload and payload.get("ok"):
            self.result.set_result(payload.get("text", ""))
        else:
            err = (payload or {}).get("error") if payload else None
            self.result.set_exception(
                RuntimeError(err or "sub-provider worker exited without a result")
            )

    def kill(self) -> None:
        p = self._proc
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass


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

        # Run the sub-provider in a SEPARATE OS PROCESS (see _SubprocHandle /
        # subproc.py). A thread+loop is not enough — claude-agent-sdk has
        # process-global async-generator state, so a nested query() under the
        # orchestrator's query() raises "aclose(): asynchronous generator is
        # already running". A child process is fully isolated, and lets us
        # KILL a wedged `claude` CLI for real. `handle.first` flips the
        # instant the child reports its first event.
        handle = _SubprocHandle()
        await handle.start(
            role_cfg,
            step.prompt,
            ctx.cwd,
            ctx.additional_dirs,
            ctx.permission_mode,
        )
        collect = handle.result
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
                    started = handle.first.is_set()
                    # Fast-fail: zero output within the startup grace window
                    # means the backend is wedged (auth prompt, dead socket,
                    # nested-SDK deadlock). Don't pretend to wait the full
                    # STEP_TIMEOUT_S — abort loudly now.
                    if not started and elapsed_s >= STARTUP_GRACE_S:
                        timed_out = True
                        error_text = (
                            f"{step.role}: the {role_cfg.provider} backend "
                            f"produced NO output within {int(STARTUP_GRACE_S)}s "
                            f"— treating it as unresponsive and aborting this "
                            f"step. The backend is likely not authenticated, "
                            f"hung, or unreachable; this is NOT a slow model."
                        )
                        break
                    # Absolute ceiling for a backend that streams but never
                    # finishes.
                    if elapsed_s >= STEP_TIMEOUT_S:
                        timed_out = True
                        error_text = (
                            f"{step.role} step exceeded {int(STEP_TIMEOUT_S)}s "
                            f"budget — aborting. Check that the "
                            f"{role_cfg.provider} backend is healthy."
                        )
                        break
                    # Honest heartbeat: don't say "still working" when we've
                    # heard nothing at all.
                    if started:
                        msg = f"_…{step.role} still working ({elapsed_s}s)…_\n"
                    else:
                        msg = (
                            f"_…waiting for the {role_cfg.provider} backend — "
                            f"no response yet ({elapsed_s}s)…_\n"
                        )
                    yield Event(
                        type="assistant.text",
                        data={"text": msg, "heartbeat": True},
                    )
        except Exception as exc:
            # Sub-provider raised in the child (propagated through result).
            error_text = str(exc) or repr(exc)
        finally:
            # True cancellation: kill the child process. Reclaims a wedged
            # `claude` CLI immediately (no daemon-thread leak, no hung
            # session-delete). Safe on every exit path — normal completion,
            # timeout, fast-fail, or generator aclose() on WS disconnect.
            handle.kill()
            if not collect.done():
                collect.cancel()

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
