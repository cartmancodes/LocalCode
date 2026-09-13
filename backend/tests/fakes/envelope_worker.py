"""Fake sub-provider worker that emits one ``StepResult`` envelope and exits.

Exists so the wire format can be tested across a REAL process boundary without
a real provider: the parent (``_SubprocHandle``) only ever sees stdout, so a
test that hands it a ``StepResult`` in-process proves nothing about the framing
or the JSON shape the worker actually writes. Task 7 re-frames this protocol,
which is exactly why the current shape needs a test that fails if it changes by
accident.

Echoes the request's ``role_name`` and ``session_id`` into the summary so the
test can assert the parent really forwarded them.
"""
from __future__ import annotations

import json
import sys

# An artifact id shaped like the real thing (sha256 hex) so the parent's
# ``context_text`` pointer line is representative.
_FAKE_ARTIFACT_ID = "c" * 64


def main() -> None:
    req = json.loads(sys.stdin.readline())
    sys.stdout.write("@@FIRST@@\n")
    sys.stdout.flush()
    result = {
        "summary": (
            f"echo role={req.get('role_name')} session={req.get('session_id')}"
            "\n\nNACK: task 3 missing"
        ),
        "structured": {"value": "nack", "reason": "task 3 missing", "source": "json"},
        "artifact_id": _FAKE_ARTIFACT_ID,
        "artifact_path": f"/artifacts/cc/{_FAKE_ARTIFACT_ID}.txt",
        "tool_digest": "(tool activity from claude:m)\n- Bash input={}",
        "full_bytes": 2_000_000,
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    sys.stdout.write("@@RESULT@@ " + json.dumps({"ok": True, "result": result}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
