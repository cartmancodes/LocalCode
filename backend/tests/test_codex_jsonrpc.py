"""The Codex transport (`backend/app/orchestrator/codex/jsonrpc.py`).

Every test here runs against a real child process — ``fake_codex_app_server.py``
spawned with the ``rpc`` scenario — because the properties under test are
properties of a pipe and a reader task, not of a mock:

  * **correlation.** Two requests in flight, answered backwards, and each
    caller still gets its own answer. A single "last response wins" reader
    passes a one-request test and silently hands turn N's thread id to turn
    N+1's caller.
  * **a server request is always answered.** Codex blocks on
    ``execCommandApproval`` until a response with the matching id arrives, so
    a handler that raises, and a method nobody registered, both have to
    produce an error response. Dropping either wedges the agent for the life
    of the turn.
  * **-32001 is backpressure.** It is retried and then succeeds; exhausting
    the retries surfaces the code rather than looping forever.
  * **the child prints prose on stdout.** A non-JSON line must not kill the
    reader task, because the reader dying takes every pending future with it.
  * **close() reaps the process GROUP.** The app-server spawns helpers; a kill
    aimed at the leader alone re-parents them to ``launchd``, where they keep
    running on the user's subscription with nothing attached.

Ordering is established by a notification handshake, never by sleeping: the
fake acknowledges a parked request with a notification, and the test waits for
that. Every test kills the child in a ``finally`` whatever it asserts.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from backend.app.orchestrator.codex.jsonrpc import JsonRpcError, StdioJsonRpc
from backend.app.orchestrator.codex.protocol import (
    ERR_BUSY,
    ERR_INTERNAL,
    ERR_METHOD_NOT_FOUND,
    R_EXEC_APPROVAL,
)

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_codex_app_server.py"

# Generous enough that a loaded machine does not fail a test, short enough that
# a genuinely stuck request does not hang the suite.
WAIT_S = 10.0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_until_dead(pid: int, label: str, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if not _alive(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{label} (pid {pid}) survived close()")


def _load_records(path: Path, kind: str) -> list[dict]:
    """Every record of ``kind`` the fake has written so far. Sync, and called
    through ``to_thread``: file I/O on the event loop is the house rule."""
    if not path.exists():
        return []
    return [
        entry
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for entry in [json.loads(line)]
        if entry.get("kind") == kind
    ]


async def _read_records(path: Path, kind: str, count: int, timeout_s: float = 15.0) -> list[dict]:
    """Wait for ``count`` records of ``kind`` in the fake's JSONL log."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        found = await asyncio.to_thread(_load_records, path, kind)
        if len(found) >= count:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(f"the fake never recorded {count} {kind!r} entries at {path}")


class _Peer:
    """One spawned fake plus the transport under test, and its pids."""

    def __init__(self, rpc: StdioJsonRpc, proc: Any, pgid: int) -> None:
        self.rpc = rpc
        self.proc = proc
        self.pid = proc.pid
        self.pgid = pgid


@pytest.fixture
async def peer_factory() -> AsyncIterator[Any]:
    """Spawns fakes and guarantees every process group is gone afterwards.

    The teardown assertion is the point: a test that leaves a `codex` behind
    would be invisible here and fatal in production, which is the leak this
    whole task inherited from the fleet pool.
    """
    peers: list[_Peer] = []

    async def factory(scenario: str = "rpc", record: Path | None = None) -> _Peer:
        argv = [sys.executable, str(FAKE), scenario]
        if record is not None:
            argv.append(str(record))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Exactly how client.py spawns the real one: its own session, so
            # one killpg reaches the helpers too.
            start_new_session=True,
        )
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:  # pragma: no cover - the child is the group leader
            pgid = proc.pid
        peer = _Peer(StdioJsonRpc(proc, pgid=pgid), proc, pgid)
        peer.rpc.start()
        peers.append(peer)
        return peer

    try:
        yield factory
    finally:
        for peer in peers:
            with contextlib.suppress(Exception):
                await peer.rpc.close()
            with contextlib.suppress(OSError):
                os.killpg(peer.pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(peer.proc.wait(), timeout=5.0)


class TestCorrelation:
    async def test_two_in_flight_requests_resolve_out_of_order(self, peer_factory) -> None:
        peer = await peer_factory()
        queued: asyncio.Queue[str] = asyncio.Queue()
        peer.rpc.on_notification("test/queued", lambda p: queued.put_nowait(p.get("tag")))

        order: list[str] = []

        async def call(tag: str) -> None:
            result = await peer.rpc.request("test/deferred", {"tag": tag}, timeout_s=WAIT_S)
            # Recorded where the caller resumes, so the list is the order the
            # ANSWERS landed, not the order the requests were sent.
            order.append(str(result["tag"]))

        first = asyncio.create_task(call("a"))
        assert await asyncio.wait_for(queued.get(), WAIT_S) == "a"
        second = asyncio.create_task(call("b"))
        assert await asyncio.wait_for(queued.get(), WAIT_S) == "b"

        # The fake answers the parked requests backwards.
        flushed = await peer.rpc.request("test/flush", {}, timeout_s=WAIT_S)
        assert flushed["flushed"] == 2

        await asyncio.wait_for(asyncio.gather(first, second), WAIT_S)
        assert order == ["b", "a"], "each caller must get ITS answer, in whatever order it lands"

    async def test_a_json_rpc_error_response_raises_with_its_code(self, peer_factory) -> None:
        peer = await peer_factory()
        with pytest.raises(JsonRpcError) as excinfo:
            await peer.rpc.request(
                "test/error", {"code": -32042, "message": "no such thread"}, timeout_s=WAIT_S
            )
        # The code, not just the text: ERR_BUSY is retried and everything else
        # is surfaced, and a caller cannot tell those apart from a string.
        assert excinfo.value.code == -32042
        assert "no such thread" in str(excinfo.value)


class TestBackpressure:
    async def test_busy_is_retried_and_then_succeeds(self, peer_factory, tmp_path: Path) -> None:
        record = tmp_path / "rpc.jsonl"
        peer = await peer_factory(record=record)

        result = await peer.rpc.request("test/busy", {"times": 1}, timeout_s=WAIT_S)

        # Two attempts reached the server and the caller saw neither the error
        # nor the delay — backpressure became a slower call, not a failed one.
        assert result["attempts"] == 2
        requests = await _read_records(record, "request", 2)
        assert [r["method"] for r in requests] == ["test/busy", "test/busy"]

    async def test_exhausting_the_retries_surfaces_the_busy_code(
        self, peer_factory, tmp_path: Path
    ) -> None:
        record = tmp_path / "rpc.jsonl"
        peer = await peer_factory(record=record)

        with pytest.raises(JsonRpcError) as excinfo:
            # More busy replies than the schedule has tries.
            await peer.rpc.request("test/busy", {"times": 99}, timeout_s=WAIT_S)

        assert excinfo.value.code == ERR_BUSY
        # Bounded: three tries and it surfaces. An unbounded retry is a turn
        # that never ends and never says why.
        requests = await _read_records(record, "request", 3)
        assert len(requests) == 3


class TestServerRequests:
    async def test_a_server_request_reaches_its_handler_and_the_result_goes_back(
        self, peer_factory
    ) -> None:
        peer = await peer_factory()
        seen: list[dict[str, Any]] = []

        async def handler(params: dict[str, Any]) -> dict[str, Any]:
            seen.append(params)
            return {"decision": "approved"}

        peer.rpc.on_request(R_EXEC_APPROVAL, handler)

        answer = await peer.rpc.request(
            "test/ask",
            {"method": R_EXEC_APPROVAL, "payload": {"command": ["rm", "-rf", "build"]}},
            timeout_s=WAIT_S,
        )

        assert seen == [{"command": ["rm", "-rf", "build"]}]
        # The fake reports the frame it received back to us: the response
        # carried the handler's value under the ORIGINAL request's id.
        assert answer["response"]["result"] == {"decision": "approved"}

    async def test_a_handler_that_raises_produces_an_error_response_not_a_drop(
        self, peer_factory
    ) -> None:
        peer = await peer_factory()

        def handler(params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("the approval gate blew up")

        peer.rpc.on_request(R_EXEC_APPROVAL, handler)

        answer = await peer.rpc.request(
            "test/ask", {"method": R_EXEC_APPROVAL, "payload": {}}, timeout_s=WAIT_S
        )

        error = answer["response"]["error"]
        assert error["code"] == ERR_INTERNAL
        assert "blew up" in error["message"]

    async def test_an_unregistered_server_request_is_answered_with_method_not_found(
        self, peer_factory
    ) -> None:
        peer = await peer_factory()

        answer = await peer.rpc.request(
            "test/ask", {"method": "codex/somethingNew", "payload": {}}, timeout_s=WAIT_S
        )

        # Answered, not dropped. A newer CLI asking for a callback we do not
        # implement must get a refusal it can act on.
        error = answer["response"]["error"]
        assert error["code"] == ERR_METHOD_NOT_FOUND
        assert "codex/somethingNew" in error["message"]


class TestResilience:
    async def test_non_json_stdout_lines_are_ignored(self, peer_factory) -> None:
        peer = await peer_factory()

        result = await peer.rpc.request("test/noise", {}, timeout_s=WAIT_S)

        # The real binary interleaves diagnostics with frames; the reader has
        # to skip them. If it died on one, this request would time out and
        # every later one with it.
        assert result == {"ok": True}
        assert await peer.rpc.request("test/noise", {}, timeout_s=WAIT_S) == {"ok": True}

    async def test_close_fails_pending_requests_and_leaves_no_child(self, peer_factory) -> None:
        peer = await peer_factory()
        pending = asyncio.create_task(peer.rpc.request("test/hang", {}, timeout_s=WAIT_S))
        # The fake never answers this one; give the write time to land.
        await asyncio.sleep(0.1)

        await peer.rpc.close()

        with pytest.raises(ConnectionError) as excinfo:
            await asyncio.wait_for(pending, WAIT_S)
        # Failed with a reason, not left to time out: a caller parked on a
        # dead pipe for its full timeout reports the wrong failure, minutes
        # late.
        assert "codex app-server" in str(excinfo.value)
        await _wait_until_dead(peer.pid, "the app-server")
        assert peer.proc.returncode is not None, "the child must be reaped, not just signalled"

    async def test_close_reaps_the_whole_process_group(
        self, peer_factory, tmp_path: Path
    ) -> None:
        """The leak that actually bites: the app-server's own helpers.

        A kill aimed at the leader alone re-parents its children to ``launchd``,
        where they keep running against the user's paid subscription with no
        interface attached. So the fake spawns a sleeping grandchild and the
        assertion is on ITS pid.
        """
        record = tmp_path / "child.jsonl"
        peer = await peer_factory(scenario="child", record=record)
        pids = (await _read_records(record, "pids", 1))[0]
        server_pid, child_pid = int(pids["server"]), int(pids["child"])

        try:
            assert _alive(server_pid), "the fake died before we could kill it"
            assert _alive(child_pid), "the grandchild died before the kill"

            await peer.rpc.close()

            await _wait_until_dead(server_pid, "the app-server")
            await _wait_until_dead(child_pid, "the grandchild (the app-server's helper)")
        finally:
            # Never leave real processes behind, however this test ends.
            for pid in (server_pid, child_pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)

    async def test_close_is_idempotent(self, peer_factory) -> None:
        peer = await peer_factory()
        await peer.rpc.close()
        # Several paths reach close (a failed handshake, a session delete, app
        # shutdown); the second must not raise.
        await peer.rpc.close()
        assert peer.rpc.closed
