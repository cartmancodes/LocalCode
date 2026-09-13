"""Gate-output classifier.

The reviewer and tester end their replies with a single classifier line
(``LGTM`` / ``NACK`` / ``NACK_CODE`` / ``NACK_TESTS``). This module turns that
free-form reply into a normalized verdict, robust to trailing prose, Markdown
decoration, and the tool-activity digest ``collect`` appends.

Task 6 added a second, preferred path: the gates now also emit a fenced JSON
verdict block, and :func:`parse_verdict` reads that when it is present. The
line classifier below is NOT deprecated by it — it is the fallback, and a
trustworthy fallback is what lets the JSON path refuse anything it does not
understand instead of guessing. ``classify_gate`` is therefore left exactly as
it was; everything new sits underneath it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Marker that ``collect._collect_text`` injects when it appends a tool-activity
# digest underneath a sub-provider's narrative. The classifier strips
# everything from this marker onward before parsing — otherwise tool log
# lines would mask the model's actual classifier line.
TOOL_DIGEST_MARKER = "\n---\n(tool activity from "

_CLASSIFIER_PREFIXES = ("LGTM", "TESTS_OK", "NACK_TESTS", "NACK_CODE", "NACK")


def _unclassified(role: str) -> str:
    """Fail-safe verdict when no classifier line is found. Better to retry
    than to silently advance past work the gate didn't actually bless."""
    return "nack_code" if role == "tester" else "nack"


def classify_gate(output: str, role: str) -> str:
    """Parse a gate's output for its classifier line.

    Returns one of:
      - ``"lgtm"``        — explicit pass
      - ``"nack"``        — reviewer NACK (or unclassified reviewer output —
                            fail-safe so we retry rather than silently shipping
                            work the gate didn't bless)
      - ``"nack_code"``   — tester says implementation is buggy
      - ``"nack_tests"``  — tester says tests themselves are buggy

    Robustness:
      - Strips ``collect``'s appended tool-activity digest first. Without
        this, a reviewer that correctly emitted ``LGTM`` followed by the
        digest would be misclassified because the absolute last line of the
        output is now a Bash log line.
      - Walks BACKWARDS for the last classifier-shaped line, so a model
        that adds a friendly trailing sentence after its verdict
        ("LGTM\\nThanks for the review!") still matches.
      - Tolerates Markdown decorations on the classifier line (``**LGTM**``,
        ```LGTM```, etc.) by stripping common decoration characters before
        the prefix check.
      - Fail-safe when no classifier is found: NACK for reviewer, NACK_CODE
        for tester.
    """
    body = output.split(TOOL_DIGEST_MARKER, 1)[0]
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    if not lines:
        return _unclassified(role)

    classifier: str | None = None
    for ln in reversed(lines):
        upper = ln.lstrip("`*_# ").upper()
        if upper.startswith(_CLASSIFIER_PREFIXES):
            classifier = upper
            break

    if classifier is None:
        return _unclassified(role)

    if role == "tester":
        if classifier.startswith("LGTM") or classifier.startswith("TESTS_OK"):
            return "lgtm"
        if classifier.startswith("NACK_TESTS"):
            return "nack_tests"
        # Bare NACK or NACK_CODE → assume implementation bug.
        return "nack_code"

    # Reviewer (or any other gate using the LGTM / NACK protocol).
    if classifier.startswith("LGTM"):
        return "lgtm"
    return "nack"


# ─────────────────────────────────────────────────────────────────────────────
# Schema verdicts — a gate says what it means in JSON, and the line classifier
# above stays as the fallback.
# ─────────────────────────────────────────────────────────────────────────────

# Every verdict value the pipeline knows how to route on. A JSON block whose
# ``verdict`` is outside this set is NOT treated as a verdict at all (see
# ``parse_verdict``): "looks like JSON, says something we don't understand" must
# never resolve to a pass.
VERDICT_VALUES = ("lgtm", "nack", "nack_code", "nack_tests")

# The roles whose output IS a routing decision rather than work product, so
# these are the roles a step envelope carries a parsed verdict for. Named once
# here instead of being spelled ``("reviewer", "tester")`` inline at each of
# the three call sites — a fourth gate role added to one tuple and not the
# others is a gate that silently stops gating.
GATE_ROLES: tuple[str, ...] = ("reviewer", "tester")

# The tester's prompt has always allowed ``TESTS_OK`` as a synonym for a pass,
# so the JSON path accepts it too — a model that learned that spelling from the
# classifier instructions should not produce an unusable JSON verdict.
_VERDICT_SYNONYMS = {"tests_ok": "lgtm"}

# A reason is a human-facing sentence, not a payload. Capping it keeps a model
# that dumps its whole analysis into ``"reason"`` from re-introducing the
# unbounded-step-output problem through the structured field.
_MAX_REASON_CHARS = 500

# The fenced block the reviewer/tester prompts ask for. Case-insensitive on the
# tag because models write ```JSON about as often as ```json.
_JSON_FENCE_RE = re.compile(
    r"```[ \t]*json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE
)

_NO_VERDICT_REASON = "no verdict found — failing safe"


@dataclass(frozen=True)
class Verdict:
    """A gate's decision, plus where it came from.

    ``source`` is not decoration: when a gate's JSON block is missing and we
    fell back to the line classifier, that is worth seeing in the step
    envelope — it is the early warning that a role's prompt has drifted, and
    it distinguishes "the model said nack" from "we could not tell, so nack".
    """

    value: str  # one of VERDICT_VALUES
    reason: str
    source: str  # "json" | "line"

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "reason": self.reason, "source": self.source}


def parse_verdict(output: str, role: str | None) -> Verdict:
    """Parse a gate's output into a normalized verdict.

    Precedence, and the reason for it:

    1. A JSON object carrying a recognized ``"verdict"`` wins. It is
       unambiguous — no backwards walk, no Markdown stripping, no guessing
       which line the model meant as its classifier.
    2. Everything else falls back to :func:`classify_gate`, including a JSON
       block that fails to parse and a JSON block whose ``"verdict"`` names a
       value we don't recognize. That is the whole point: a malformed machine
       verdict must not invent a pass, and it must not throw away a perfectly
       good ``LGTM`` line sitting above it either.
    3. With neither, ``classify_gate``'s fail-safe applies — ``nack`` for a
       reviewer, ``nack_code`` for a tester — so unreviewed work loops back
       instead of shipping.
    """
    # Same first move as ``classify_gate``: the appended tool digest can itself
    # contain JSON (a Write tool's input is a JSON blob), and that blob is
    # emphatically not the gate's verdict.
    body = output.split(TOOL_DIGEST_MARKER, 1)[0]

    for candidate in _verdict_candidates(body):
        parsed = _verdict_from_json(candidate)
        if parsed is not None:
            return parsed

    # The VALUE always comes from the untouched line classifier; only the
    # human-readable reason is recovered separately.
    value = classify_gate(output, role or "")
    line = _classifier_line(body)
    return Verdict(value=value, reason=line or _NO_VERDICT_REASON, source="line")


def _verdict_candidates(body: str) -> list[str]:
    """Candidate JSON texts, best first: the LAST fenced ``json`` block, then
    the LAST balanced ``{...}`` anywhere in the body.

    Last, not first, because the prompts ask the model to *end* with its
    verdict; an earlier block is typically an example, or a payload the gate
    was reviewing.
    """
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.findall(body)
    if fenced:
        candidates.append(fenced[-1])
    braced = _last_balanced_object(body)
    if braced is not None:
        candidates.append(braced)
    return candidates


def _last_balanced_object(text: str) -> str | None:
    """The last top-level ``{...}`` span in ``text``, or ``None``.

    Brace counting is string-aware (quotes and backslash escapes) because a
    reason like ``"missing } in parser.py"`` would otherwise unbalance the
    scan and yield a truncated object that fails to parse — silently demoting
    a valid JSON verdict to the line fallback.
    """
    best: str | None = None
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                best = text[start : i + 1]
    return best


def _verdict_from_json(candidate: str) -> Verdict | None:
    """A ``Verdict`` from one candidate JSON text, or ``None`` when the text is
    not an object, carries no ``"verdict"``, or names a value outside
    ``VERDICT_VALUES``. ``None`` means "keep looking, then fall back" — never
    "pass"."""
    try:
        obj: Any = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "verdict" not in obj:
        return None
    raw = obj.get("verdict")
    if not isinstance(raw, str):
        return None
    value = raw.lower().strip()
    value = _VERDICT_SYNONYMS.get(value, value)
    if value not in VERDICT_VALUES:
        return None
    reason = obj.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = obj.get("summary")
    if not isinstance(reason, str):
        reason = ""
    # Collapse whitespace so a multi-line reason stays one line in the
    # envelope, then cap it (see ``_MAX_REASON_CHARS``).
    return Verdict(
        value=value,
        reason=" ".join(reason.split())[:_MAX_REASON_CHARS],
        source="json",
    )


def _classifier_line(body: str) -> str | None:
    """The classifier line ``classify_gate`` would have matched, used only as
    the verdict's human-readable reason.

    Reporting only, deliberately: the verdict VALUE comes from
    ``classify_gate`` itself, so this helper cannot change how a gate routes
    even if the two ever disagree about which line was the classifier.
    """
    for ln in reversed([x.strip() for x in body.strip().splitlines() if x.strip()]):
        if ln.lstrip("`*_# ").upper().startswith(_CLASSIFIER_PREFIXES):
            return ln
    return None


# Back-compat aliases — the original module exposed these underscore names.
_TOOL_DIGEST_MARKER = TOOL_DIGEST_MARKER
_classify_gate = classify_gate
