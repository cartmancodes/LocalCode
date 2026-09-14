"""Gate verdicts — structured, with the old string form as a fallback.

The previous fleet asked reviewers to end with a bare ``LGTM`` or
``NACK: reason`` line and parsed the last line of the reply. A reviewer that
added a closing pleasantry silently became a NACK, and one that wrote
``LGTM, but consider…`` silently became a pass. Now the gate asks for a
fenced JSON object; the line form is still accepted so an older prompt or a
model that ignores the schema does not break the workflow.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

Outcome = Literal["pass", "fail", "unknown"]
Blame = Literal["code", "tests"] | None

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_BARE_OBJECT = re.compile(r"(\{[^{}]*\"verdict\"[^{}]*\})", re.S)
_CLASSIFIER = re.compile(
    r"^\s*[`*_#\s]*(LGTM|TESTS_OK|NACK_TESTS|NACK_CODE|NACK)\b[:\s]*(.*)$", re.I
)

# The old fleet appended a tool-activity digest under this marker; anything
# from here on is log output, not the model's verdict.
DIGEST_MARKER = "\n---\n(tool activity from "


@dataclass
class Verdict:
    verdict: Outcome
    reason: str = ""
    blame: Blame = None
    source: Literal["json", "classifier", "missing"] = "missing"

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "blame": self.blame,
            "source": self.source,
        }


def parse_verdict(text: str) -> Verdict:
    """Prefer the JSON object; fall back to a classifier line; fail safe.

    Fail-safe means: no recognisable verdict is a ``fail``, not a pass. A gate
    that cannot be read has not been passed.
    """
    body = (text or "").split(DIGEST_MARKER, 1)[0]

    parsed = _from_json(body)
    if parsed is not None:
        return parsed

    parsed = _from_classifier(body)
    if parsed is not None:
        return parsed

    return Verdict("fail", "no verdict found in the reply", None, "missing")


def _from_json(body: str) -> Verdict | None:
    candidates = _FENCE.findall(body) or _BARE_OBJECT.findall(body)
    for raw in reversed(candidates):  # the last verdict in the reply wins
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "verdict" not in data:
            continue
        value = str(data.get("verdict", "")).strip().lower()
        outcome: Outcome
        if value in ("pass", "passed", "lgtm", "ok", "true"):
            outcome = "pass"
        elif value in ("fail", "failed", "nack", "false"):
            outcome = "fail"
        else:
            continue
        blame_raw = str(data.get("blame") or "").strip().lower()
        blame: Blame = blame_raw if blame_raw in ("code", "tests") else None  # type: ignore[assignment]
        return Verdict(outcome, str(data.get("reason") or "").strip(), blame, "json")
    return None


def _from_classifier(body: str) -> Verdict | None:
    lines = [line for line in body.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        match = _CLASSIFIER.match(line)
        if match is None:
            continue
        token = match.group(1).upper()
        reason = match.group(2).strip()
        if token in ("LGTM", "TESTS_OK"):
            return Verdict("pass", reason, None, "classifier")
        if token == "NACK_TESTS":
            return Verdict("fail", reason, "tests", "classifier")
        return Verdict("fail", reason, "code", "classifier")
    return None
