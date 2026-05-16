"""Gate-output classifier.

The reviewer and tester end their replies with a single classifier line
(``LGTM`` / ``NACK`` / ``NACK_CODE`` / ``NACK_TESTS``). This module turns that
free-form reply into a normalized verdict, robust to trailing prose, Markdown
decoration, and the tool-activity digest ``collect`` appends.
"""
from __future__ import annotations

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


# Back-compat aliases — the original module exposed these underscore names.
_TOOL_DIGEST_MARKER = TOOL_DIGEST_MARKER
_classify_gate = classify_gate
