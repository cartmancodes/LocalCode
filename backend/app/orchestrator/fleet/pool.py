"""A pool of long-lived sub-provider worker processes.

**Why the process boundary stays.** The orchestrator is itself a
``claude_agent_sdk`` session. Driving a second ``query()`` from inside its MCP
tool callback deadlocks the SDK — its ``_process_query_inner`` async generator
cannot be closed while another is interleaved on the same process
(``RuntimeError: aclose(): asynchronous generator is already running``), and the
state is process-global so a thread with its own loop does not help. A separate
OS process is fully isolated, and it gives the parent true cancellation: killing
the process reclaims a wedged vendor CLI.

**Why the process is now long-lived.** One process *per dispatch* paid
interpreter start + SDK import + CLI spawn every time — 0.7 to 1.0 s before any
model token, 3 to 4 s on a four-role turn. That cost is fixed, so it belongs
paid once. This module keeps the isolation and removes the repetition: a worker
is spawned on the first step for a key and reused by every later step on that
key.

**What is NOT reused: the conversation.** The worker builds a fresh
sub-provider per request (``collect_step``'s own design), so reusing the process
cannot leak one role's context into the next role's. The win here is the
process, not the client.

Three failures this module is shaped by:

* **A result larger than the stdout line limit wedged the parent.** The old
  framing wrote the whole result on one newline-terminated line with no
  ``limit=`` on the reader, so a 512 KiB plan made ``readline()`` raise at
  64 KiB — and then the parent's ``proc.wait()`` blocked forever, because the
  child was still trying to write the rest into a pipe nobody was draining.
  Results are length-prefixed now (see ``constants.RESULT_MARKER``) *and* the
  reader gets 8 MiB, because a protocol whose correctness rests on an
  undocumented reader limit is itself the defect.
* **A killed worker left the vendor CLI running.** The CLI is the worker's
  child, so the backend's grandchild. Killing the worker alone re-parents the
  CLI to ``launchd`` and it keeps burning the user's paid subscription with no
  interface attached. Every worker is spawned with ``start_new_session=True``
  and reclaimed with ``killpg`` on the pgid captured at spawn — captured then,
  because once the worker is gone its pid can no longer be translated to a
  group.
* **A SIGKILLed backend left workers running forever.** Nothing gets to run a
  shutdown handler, and the workers are in their own sessions so they do not
  even see the terminal's signals. Each worker therefore records a pidfile in
  a pool-owned directory, and ``sweep_stale_workers`` — run once when the pool
  first starts — reclaims the ones whose backend is gone. It refuses to signal
  anything it cannot prove is ours; see that function for exactly what it
  checks and what it leaves alone.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    FIRST_MARKER,
    RESULT_MARKER,
    WORKER_PID_DIR_ENV,
    WORKER_STDOUT_LIMIT,
)
from .envelope import StepResult

logger = logging.getLogger(__name__)

# Prefixes as they appear on the wire, with the separating space, so the reader
# never re-derives them (and never accidentally matches a diagnostic line that
# merely starts with the marker).
_FIRST_PREFIX = FIRST_MARKER + " "
_RESULT_PREFIX = RESULT_MARKER + " "

# How many stderr lines to keep per worker. When a worker dies without a result
# this ring is the ONLY window into why — discarding stderr (the earlier
# mistake) made that failure undiagnosable. Bounded because a worker now lives
# for many requests and an unbounded buffer would be a leak.
_STDERR_TAIL_LINES = 40

# Request ids only have to be unique among one pool's in-flight requests; a
# monotonic counter is enough and makes a log line readable.
_REQUEST_IDS = itertools.count(1)


def worker_key(
    session_id: str | None, provider: str, model: str, cwd: str | None
) -> str:
    """The pool key for one step.

    The session id is first and is never omitted: a worker holds a vendor CLI
    with a working directory and (for providers that keep one) a per-session
    client, so sharing one across sessions would let one user's session reach
    another's. ``provider`` and ``model`` are part of the key because a worker
    is a *process*, and the process is what pins the interpreter's imported
    SDK; ``cwd`` because the CLI is spawned against it.

    Keyed by provider *name*, never branched on: a new provider (Task 10's
    Codex) gets its own workers here without editing this module.
    """
    return f"{session_id or 'anon'}|{provider}|{model}|{cwd}"


@dataclass
class _Pending:
    """The two signals one in-flight request exposes to its caller.

    Deliberately the same contract the per-dispatch handle exposed, so the
    provider's heartbeat / fast-fail / ceiling logic is reused verbatim rather
    than rewritten against a new shape.
    """

    # Set the instant the worker reports its first sub-provider event. Drives
    # the caller's honest heartbeats ("no response yet" vs "still working") and
    # its fast-fail on a backend that never says anything.
    first: asyncio.Event
    result: asyncio.Future[StepResult]


@dataclass(eq=False)
class _Worker:
    """One long-lived worker process and everything needed to reclaim it.

    ``eq=False`` so identity is the identity: the pool keeps workers in a set
    and asks "is the worker registered under this key still *this* object?"
    after an await, which field equality would answer wrong.
    """

    proc: asyncio.subprocess.Process
    key: str
    # Captured at spawn, because the group outlives the worker: once the worker
    # is gone its pid no longer translates to a pgid and the vendor CLI still
    # in that group would be unreachable. See ``WorkerPool._terminate``.
    pgid: int | None
    pending: dict[str, _Pending] = field(default_factory=dict)
    last_used: float = 0.0
    requests_served: int = 0
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    reader: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    pidfile: Path | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def busy(self) -> bool:
        return bool(self.pending)

    def stderr_detail(self) -> str:
        tail = [line for line in self.stderr_tail if line.strip()][-6:]
        return (" | stderr: " + " ⏎ ".join(tail)) if tail else ""


class WorkerPool:
    """Long-lived sub-provider workers, keyed by :func:`worker_key`.

    One worker serves its requests strictly one at a time — it reads the next
    request line only after the previous result is written — so a second submit
    on a busy key queues behind the first. Concurrency comes from running
    several workers, bounded by ``max_workers``.
    """

    def __init__(
        self,
        *,
        max_workers: int = 4,
        idle_timeout_s: float = 300.0,
        worker_module: str,
        repo_root: str,
        pid_dir: Path | None = None,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._idle_timeout_s = max(0.01, float(idle_timeout_s))
        self._worker_module = worker_module
        self._repo_root = repo_root
        # ``None`` means "under the user's home, resolved at call time" — a
        # module- or constructor-level ``Path.home()`` would freeze the
        # developer's real home into every test run, and the suite redirects
        # HOME per test.
        self._pid_dir_override = pid_dir
        self._workers: dict[str, _Worker] = {}
        # Every worker whose reader task is still running, registered or not.
        # ``aclose`` waits on these so no reader outlives the pool (an orphaned
        # task is both a leak and a "Task was destroyed" warning at teardown).
        self._live: set[_Worker] = set()
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    # ── public surface ───────────────────────────────────────────────────────

    async def submit(
        self, key: str, request: dict[str, Any]
    ) -> tuple[asyncio.Event, asyncio.Future[StepResult]]:
        """Queue one step on ``key``'s worker, spawning it if needed.

        Returns ``(first, result)`` — the same two signals the per-dispatch
        handle exposed. Neither is resolved here; the worker's reader task
        resolves them.
        """
        if self._closed:
            raise RuntimeError("worker pool is closed")
        loop = asyncio.get_running_loop()
        pending = _Pending(first=asyncio.Event(), result=loop.create_future())
        request_id = f"r{next(_REQUEST_IDS)}"

        async with self._lock:
            await self._ensure_started()
            worker = await self._worker_for(key)
            worker.pending[request_id] = pending
            worker.last_used = time.monotonic()
            worker.requests_served += 1
            # Eviction runs AFTER this request is registered, never before. A
            # freshly spawned worker with nothing pending yet looks idle — and
            # being the only idle worker, it is the one eviction picks. It
            # killed itself before it read its first request.
            self._evict()
            stdin = worker.proc.stdin

        # Written OUTSIDE the lock on purpose. A step's prompt can be hundreds
        # of KiB (a stitched plan), and a worker that is still finishing its
        # previous request is not reading stdin — so ``drain()`` can block until
        # it does. Holding the pool lock across that would stall every other
        # key's spawn behind one busy worker.
        assert stdin is not None
        try:
            stdin.write((json.dumps({**request, "id": request_id}) + "\n").encode())
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as exc:
            # The worker died between spawn and write. Fail this request with
            # the real reason rather than letting the caller wait out its whole
            # step budget on a request nothing will ever read.
            worker.pending.pop(request_id, None)
            self._close_worker(
                key, f"worker stdin is gone: {type(exc).__name__}: {exc}", worker=worker
            )
            if not pending.result.done():
                pending.result.set_exception(
                    RuntimeError(
                        f"sub-provider worker could not accept the request "
                        f"({type(exc).__name__}: {exc}){worker.stderr_detail()}"
                    )
                )
        return pending.first, pending.result

    def kill(self, key: str) -> None:
        """SIGKILL ``key``'s worker *and everything it spawned*, failing its
        pending requests.

        Synchronous because the caller invokes it from an ``async`` generator's
        ``finally`` — which also runs during ``aclose()`` on a WS disconnect,
        where awaiting is not reliably possible.

        The group, not the process: the worker starts the vendor CLI as its own
        child, so killing only the worker re-parents the CLI and it keeps
        running on the user's paid subscription with no interface attached.
        That orphan, not the worker, was the leak.
        """
        self._close_worker(key, "killed by the pool (step abandoned or timed out)")

    async def aclose(self) -> None:
        """Kill every worker and wait for its reader to finish."""
        self._closed = True
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sweeper
        for key in list(self._workers):
            self._close_worker(key, "pool closed")
        tasks = [
            task
            for worker in list(self._live)
            for task in (worker.reader, worker.stderr_task)
            if task is not None and not task.done()
        ]
        if tasks:
            # Bounded: a reader blocked on a pipe that SIGKILL somehow did not
            # close must not hold up backend shutdown.
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=10.0)
        self._workers.clear()
        self._live.clear()

    # ── introspection (tests and logging) ────────────────────────────────────

    def worker_pid(self, key: str) -> int | None:
        worker = self._workers.get(key)
        return None if worker is None else worker.pid

    def requests_served(self, key: str) -> int:
        worker = self._workers.get(key)
        return 0 if worker is None else worker.requests_served

    @property
    def keys(self) -> list[str]:
        return list(self._workers)

    def pid_dir(self) -> Path:
        """The pool-owned pidfile directory, resolved now rather than at
        construction so a redirected ``HOME`` is honoured."""
        if self._pid_dir_override is not None:
            return self._pid_dir_override
        return Path.home() / ".localcode" / "workers"

    # ── startup ──────────────────────────────────────────────────────────────

    async def _ensure_started(self) -> None:
        """One-time startup: reclaim a previous backend's workers, then start
        the idle sweeper. Called under ``self._lock`` so the sweep completes
        before this pool spawns anything."""
        if self._started:
            return
        self._started = True
        pid_dir = self.pid_dir()
        try:
            # In a thread: the sweep stats files and shells out to ``ps``, and
            # this runs on the turn's event loop.
            killed = await asyncio.to_thread(
                sweep_stale_workers, pid_dir, self._worker_module
            )
        except Exception as exc:  # noqa: BLE001 - a failed sweep must not fail a turn
            logger.warning("stale-worker sweep failed: %s", exc)
        else:
            if killed:
                logger.info("reclaimed %d worker(s) from a previous backend", killed)
        self._sweeper = asyncio.create_task(self._sweep_idle())

    # ── worker lifecycle ─────────────────────────────────────────────────────

    async def _worker_for(self, key: str) -> _Worker:
        """The live worker for ``key``, spawning one if there is none. Caller
        holds ``self._lock``."""
        worker = self._workers.get(key)
        if worker is not None:
            if worker.proc.returncode is None:
                return worker
            # Exited since its last request. Its reader has already failed any
            # pending requests; drop the registration and start fresh.
            self._workers.pop(key, None)
        worker = await self._spawn(key)
        self._workers[key] = worker
        return worker

    async def _spawn(self, key: str) -> _Worker:
        env = dict(os.environ)
        # The pidfile directory is PASSED, not derived in the child: the child
        # would otherwise resolve its own ``Path.home()`` and the two sides
        # could disagree about where the file is.
        env[WORKER_PID_DIR_ENV] = str(self.pid_dir())
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            self._worker_module,
            cwd=self._repo_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Captured, never discarded — see ``_STDERR_TAIL_LINES``.
            stderr=asyncio.subprocess.PIPE,
            # Own session/process group so one killpg reaps the vendor CLI the
            # worker spawns. The trade-off is deliberate: the worker no longer
            # receives the terminal's SIGINT, so the backend MUST cancel turns
            # on shutdown — which ``drop_all_runners()`` guarantees — and the
            # startup sweep covers the case where the backend never got to.
            start_new_session=True,
            # Generous, so one oversize diagnostic line is a warning instead of
            # a dead worker. The framed result does not depend on it.
            limit=WORKER_STDOUT_LIMIT,
            env=env,
        )
        try:
            pgid: int | None = os.getpgid(proc.pid)
        except OSError:
            # Already gone. ``start_new_session`` made it the group leader, so
            # its pid is the group id either way.
            pgid = proc.pid
        worker = _Worker(
            proc=proc,
            key=key,
            pgid=pgid,
            last_used=time.monotonic(),
            pidfile=self.pid_dir() / f"{proc.pid}.json",
        )
        self._live.add(worker)
        worker.reader = asyncio.create_task(self._read_loop(worker))
        worker.stderr_task = asyncio.create_task(self._drain_stderr(worker))
        logger.debug("fleet worker %d spawned for key %s", proc.pid, key)
        return worker

    def _evict(self) -> None:
        """Close least-recently-used IDLE workers until we are back at the cap.

        A worker with pending requests is never evicted — evicting it would
        fail a step that is mid-flight to save a process. Over the cap with
        every worker busy we simply stay over it; the cap bounds steady-state
        memory, not a momentary burst.
        """
        while len(self._workers) > self._max_workers:
            idle = [(w.last_used, k) for k, w in self._workers.items() if not w.busy]
            if not idle:
                logger.debug(
                    "fleet worker pool over cap (%d > %d) with every worker busy",
                    len(self._workers),
                    self._max_workers,
                )
                return
            _, victim = min(idle)
            self._close_worker(victim, "evicted: least recently used over the cap")

    async def _sweep_idle(self) -> None:
        """Reap workers idle past ``idle_timeout_s``.

        Lazily started, because a pool that never serves a step should not hold
        a task. Each pass is guarded: a sweeper that dies on one bad worker
        stops reaping every later one, and the leak it was preventing comes
        straight back.
        """
        interval = max(0.05, min(self._idle_timeout_s, 30.0) / 2)
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            try:
                for key, worker in list(self._workers.items()):
                    if worker.busy or now - worker.last_used <= self._idle_timeout_s:
                        continue
                    self._close_worker(
                        key, f"reaped: idle for more than {self._idle_timeout_s:g}s"
                    )
            except Exception:  # noqa: BLE001
                logger.exception("idle worker sweep raised; continuing")

    def _close_worker(
        self, key: str, reason: str, worker: _Worker | None = None
    ) -> None:
        """Deregister and SIGKILL ``key``'s worker, failing its pending work."""
        registered = self._workers.get(key)
        target = worker or registered
        if target is None:
            return
        if registered is target:
            self._workers.pop(key, None)
        self._terminate(target, reason)

    def _terminate(self, worker: _Worker, reason: str) -> None:
        logger.debug("fleet worker %d (%s): %s", worker.pid, worker.key, reason)
        if worker.pgid is not None:
            try:
                os.killpg(worker.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # whole group already gone
            except OSError:
                logger.warning(
                    "killpg(%d) failed; falling back to killing the worker only",
                    worker.pgid,
                )
                self._kill_proc_only(worker)
        else:
            self._kill_proc_only(worker)
        self._remove_pidfile(worker)
        # Fail pending work NOW rather than waiting for the reader to notice
        # EOF: ``kill`` is called from a caller that is about to stop awaiting,
        # and a future resolved late is a future nobody reads.
        self._fail_pending(worker, f"sub-provider worker {reason}")

    @staticmethod
    def _kill_proc_only(worker: _Worker) -> None:
        if worker.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                worker.proc.kill()

    @staticmethod
    def _remove_pidfile(worker: _Worker) -> None:
        if worker.pidfile is not None:
            with contextlib.suppress(OSError):
                worker.pidfile.unlink()

    def _fail_pending(self, worker: _Worker, message: str) -> None:
        detail = worker.stderr_detail()
        while worker.pending:
            _, pending = worker.pending.popitem()
            # Unblock anyone waiting on the first signal too — a caller polling
            # ``first`` in a heartbeat loop should not keep waiting on a
            # request that is already decided.
            pending.first.set()
            if not pending.result.done():
                pending.result.set_exception(RuntimeError(message + detail))

    # ── stdout demultiplexing ────────────────────────────────────────────────

    async def _read_loop(self, worker: _Worker) -> None:
        """Demultiplex one worker's stdout onto its pending requests."""
        stdout = worker.proc.stdout
        assert stdout is not None
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except ValueError:
                    # One line exceeded ``WORKER_STDOUT_LIMIT``. asyncio has
                    # already discarded it, so the stream is usable again —
                    # log and keep this worker (and its in-flight request)
                    # alive rather than failing a step over a noisy log line.
                    logger.warning(
                        "fleet worker %d wrote a stdout line over %d bytes; dropped it",
                        worker.pid,
                        WORKER_STDOUT_LIMIT,
                    )
                    continue
                if not raw:
                    break  # EOF — the worker exited
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line:
                    continue  # the newline that follows a framed payload
                if line.startswith(_FIRST_PREFIX):
                    self._on_first(worker, line[len(_FIRST_PREFIX) :].strip())
                elif line.startswith(_RESULT_PREFIX):
                    if not await self._on_result(
                        worker, stdout, line[len(_RESULT_PREFIX) :].strip()
                    ):
                        break  # truncated payload: the stream is unusable
                # Anything else is diagnostic noise and ignored by design.
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("fleet worker %d reader failed", worker.pid)
        finally:
            await self._reap(worker)

    def _on_first(self, worker: _Worker, request_id: str) -> None:
        pending = worker.pending.get(request_id)
        if pending is None:
            # A result for a request we already abandoned (timed out, killed).
            # Dropping it is correct; logging it is how a protocol skew between
            # parent and worker becomes visible instead of silent.
            logger.debug(
                "fleet worker %d signalled first for unknown request %s",
                worker.pid,
                request_id,
            )
            return
        pending.first.set()

    async def _on_result(
        self, worker: _Worker, stdout: asyncio.StreamReader, header: str
    ) -> bool:
        """Read one length-prefixed result. ``False`` means the stream died
        mid-payload and this worker's reader must stop."""
        request_id, _, size_text = header.partition(" ")
        try:
            size = int(size_text)
        except ValueError:
            logger.warning(
                "fleet worker %d wrote an unparseable result header %r",
                worker.pid,
                header,
            )
            return True
        try:
            body = await stdout.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            logger.warning(
                "fleet worker %d died %d bytes into a %d-byte result",
                worker.pid,
                len(exc.partial),
                size,
            )
            return False
        pending = worker.pending.pop(request_id, None)
        if pending is None:
            logger.debug(
                "fleet worker %d returned a result for unknown request %s",
                worker.pid,
                request_id,
            )
            return True
        worker.last_used = time.monotonic()
        pending.first.set()
        self._resolve(pending, worker, body)
        return True

    @staticmethod
    def _resolve(pending: _Pending, worker: _Worker, body: bytes) -> None:
        if pending.result.done():
            return  # the caller gave up and we were killed; nothing to resolve
        try:
            payload = json.loads(body)
        except ValueError as exc:
            pending.result.set_exception(
                RuntimeError(
                    f"sub-provider worker wrote an unparseable result payload "
                    f"({exc}){worker.stderr_detail()}"
                )
            )
            return
        wire = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("ok") and isinstance(wire, dict):
            # ``from_wire`` is tolerant by design: a partially-written envelope
            # must arrive as a degraded result the caller can still report, not
            # as an exception in this reader (which the caller would then
            # describe as "exited without a result" — the least useful message
            # available).
            pending.result.set_result(StepResult.from_wire(wire))
            return
        error = payload.get("error") if isinstance(payload, dict) else None
        if not error and isinstance(payload, dict) and payload.get("ok"):
            error = (
                "sub-provider worker reported success without a result "
                "envelope — its stdout protocol is out of sync with this "
                "process"
            )
        pending.result.set_exception(
            RuntimeError(str(error or "sub-provider worker returned no result"))
        )

    async def _drain_stderr(self, worker: _Worker) -> None:
        """Keep stderr flowing into a bounded ring.

        Not optional for a long-lived worker: an undrained stderr pipe fills
        and then every later ``print`` in the worker blocks — the worker would
        wedge on its own diagnostics.
        """
        stream = worker.proc.stderr
        if stream is None:
            return
        try:
            while True:
                try:
                    raw = await stream.readline()
                except ValueError:
                    continue  # oversize line, already discarded
                if not raw:
                    return
                worker.stderr_tail.append(raw.decode(errors="replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def _reap(self, worker: _Worker) -> None:
        """Called once per worker, when its stdout reaches EOF."""
        try:
            with contextlib.suppress(Exception):
                await worker.proc.wait()
            if self._workers.get(worker.key) is worker:
                self._workers.pop(worker.key, None)
            self._remove_pidfile(worker)
            if worker.pending:
                # Keep this diagnostic — the exit code plus the stderr tail is
                # the whole reason stderr is captured, and it was hard-won.
                self._fail_pending(
                    worker,
                    f"sub-provider worker exited without a result "
                    f"(exit={worker.proc.returncode})",
                )
        finally:
            self._live.discard(worker)


# ─────────────────────────────────────────────────────────────────────────────
# Pidfiles — reclaiming workers whose backend died without running a handler
# ─────────────────────────────────────────────────────────────────────────────


def write_worker_pidfile(worker_module: str) -> Path | None:
    """Record this worker in the pool-owned directory, for the startup sweep.

    Called by the worker itself, so the file exists even if the backend is
    SIGKILLed microseconds after the spawn. Written to a temporary name and
    renamed, so a sweep never reads a half-written record.

    Returns the path, or ``None`` when no directory was handed to us (a worker
    run by hand) or the write failed — a worker must never fail to start
    because bookkeeping did.
    """
    raw = os.environ.get(WORKER_PID_DIR_ENV)
    if not raw:
        return None
    pid = os.getpid()
    directory = Path(raw)
    path = directory / f"{pid}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": pid,
            "pgid": os.getpgid(pid),
            # The backend that spawned us. The sweep skips records whose owner
            # is still alive, which is what keeps one backend from reclaiming a
            # concurrently-running backend's workers.
            "owner_pid": os.getppid(),
            "worker_module": worker_module,
            "started": time.time(),
        }
        tmp = directory / f"{pid}.json.tmp"
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write worker pidfile %s: %s", path, exc)
        return None
    return path


def remove_worker_pidfile(path: Path | None) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def sweep_stale_workers(pid_dir: Path, worker_module: str) -> int:
    """Reclaim workers left behind by a backend that died without cleaning up.

    With ``start_new_session=True`` a backend killed with SIGKILL leaves its
    workers running: nothing gets to run a handler, and the workers are not in
    the terminal's process group either. Returns the number of groups killed.

    Four checks, and each one exists to stop this function from signalling
    something that is not ours:

    1. a record that does not parse, or carries no pid, is deleted unread;
    2. a record whose pid is **dead** is deleted and nothing is signalled — the
       worker is already gone;
    3. a record whose **owner** (the backend that spawned it) is still alive is
       left completely alone. That is the one that matters for a second backend
       running concurrently: its workers are not ours to reclaim. It also means
       a pool never reclaims its own live workers;
    4. a record whose pid is alive but whose **command line does not name our
       worker module** is deleted *without* being signalled. That is pid
       recycling — some unrelated process now holds the number — and killing it
       would be far worse than leaving an orphan.

    Only a record that survives all four is killed, by process *group*, because
    the vendor CLI in that group is the process actually costing the user money.
    """
    try:
        records = sorted(pid_dir.glob("*.json"))
    except OSError:
        return 0

    killed = 0
    for path in records:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _unlink(path)
            continue
        if not isinstance(record, dict):
            _unlink(path)
            continue
        pid = _as_pid(record.get("pid"))
        pgid = _as_pid(record.get("pgid"))
        owner = _as_pid(record.get("owner_pid"))
        if pid is None:
            _unlink(path)
            continue
        if not _pid_alive(pid):
            _unlink(path)
            continue
        if owner is not None and _pid_alive(owner):
            continue
        if not _cmdline_names(pid, worker_module):
            logger.info(
                "worker pidfile %s names a live process that is not one of our "
                "workers (recycled pid); removing the file, not signalling it",
                path.name,
            )
            _unlink(path)
            continue
        target = pgid if pgid is not None else pid
        try:
            os.killpg(target, signal.SIGKILL)
            killed += 1
            logger.info(
                "reclaimed stale worker group %d (pid %d) from a dead backend",
                target,
                pid,
            )
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("could not killpg stale worker group %d: %s", target, exc)
        _unlink(path)
    return killed


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _as_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by somebody else. Definitely not one of our workers,
        # and ``_cmdline_names`` will say so.
        return True
    except OSError:
        return False
    return True


def _cmdline_names(pid: int, worker_module: str) -> bool:
    """Does ``pid``'s command line name our worker module?

    The proof of ownership, deliberately done without a third-party process
    library: ``/proc`` on Linux, ``ps`` everywhere else (macOS has no
    ``/proc``). ``-ww`` defeats ``ps``'s width truncation, which would
    otherwise cut the module name off a long interpreter path and make every
    worker look unrecognised.

    Unreadable or empty output answers ``False`` — we only ever act on a
    positive identification.
    """
    cmdline = _read_proc_cmdline(pid)
    if cmdline is None:
        cmdline = _ps_cmdline(pid)
    return bool(cmdline) and worker_module in cmdline


def _read_proc_cmdline(pid: int) -> str | None:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode(errors="replace")


def _ps_cmdline(pid: int) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.decode(errors="replace")
