"""Fake sub-provider worker that spawns a grandchild and then hangs.

Why it has to spawn something
-----------------------------
The leak D14.1 fixes is not the worker process — it is the vendor CLI the
worker starts. The CLI is a *grandchild* of the backend, so a kill aimed at
the worker alone leaves it running: it is re-parented to ``launchd`` and
keeps consuming the user's paid subscription with no interface attached. A
test that kills a childless worker and finds it gone proves nothing about
that. This script reproduces the real shape — one child of ours that
outlives us unless the whole process group is signalled — so the test can
assert on the grandchild's pid, which is the one that used to survive.

How it reports its pids
-----------------------
Through a file whose path arrives in the request's ``prompt`` field, not
through stdout. The real ``_SubprocHandle`` owns this process's stdout and
stderr: it parses them for the ``@@FIRST@@`` / ``@@RESULT@@`` protocol and
drains stderr to a buffer, so nothing a test does can read pids from there.
The file is written under a temporary name and renamed so a reader never
sees a half-written line.

It never produces a result and never exits on its own — the test always
kills it, and the kill is what is under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Long enough that neither process can exit on its own inside a test run, so
# a pid found dead is proof of the kill rather than of a race with a timeout.
_HANG_S = 600


def main() -> None:
    req = json.loads(sys.stdin.readline() or "{}")
    pid_path = Path(req["prompt"])

    grandchild = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", f"import time; time.sleep({_HANG_S})"]
    )

    tmp_path = pid_path.with_suffix(".tmp")
    tmp_path.write_text(f"{os.getpid()} {grandchild.pid}\n", encoding="utf-8")
    os.replace(tmp_path, pid_path)

    # Also on the wire protocol's own channel: the handle ignores lines it
    # does not recognise, and it makes a hung test debuggable from captured
    # output. @@FIRST@@ is what the real worker emits on its first event.
    print(f"pids {os.getpid()} {grandchild.pid}", flush=True)
    print("@@FIRST@@", flush=True)

    time.sleep(_HANG_S)


if __name__ == "__main__":
    main()
