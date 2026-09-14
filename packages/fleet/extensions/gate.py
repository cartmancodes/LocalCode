"""The complexity gate — deciding whether a turn is worth a crew.

Anthropic's own measurements put a multi-agent run at roughly fifteen times
the tokens of a single-agent chat. On a subscription that multiplier is the
binding constraint, so the question "does this turn need a crew?" has to be
asked before the crew convenes. The old fleet never asked it: every turn,
including "how many files are in this repo?", ran planner then coder then
reviewer.

This is deliberately a cheap lexical classifier rather than a model call —
spending a model call to decide whether to spend model calls is its own tax,
and the failure mode is mild in both directions: a misjudged SIMPLE turn just
means the model answers directly (and may still dispatch if it disagrees),
while a misjudged COMPLEX turn costs one unnecessary planning pass.
"""

from __future__ import annotations

import enum
import re


class Complexity(enum.Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"
    UNKNOWN = "unknown"


# Asking about the code rather than changing it.
_READ_ONLY = re.compile(
    r"\b(what|which|where|who|when|why|how many|how much|list|show|explain|describe|"
    r"summari[sz]e|find|search|look up|read|print|display|count|does|is there|are there)\b",
    re.I,
)

# Work that produces or changes artifacts.
_MUTATING = re.compile(
    r"\b(implement|build|create|add|write|refactor|migrate|rewrite|port|fix|repair|"
    r"remove|delete|rename|move|update|change|modify|extend|integrate|wire|design|"
    r"set up|scaffold|generate|introduce|replace|upgrade|optimi[sz]e|harden|test)\b",
    re.I,
)

# Signals that the work spans more than one obvious step.
_MULTI_STEP = re.compile(
    r"\b(and then|after that|first.*then|step \d|phase \d|end[- ]to[- ]end|across|"
    r"throughout|entire|whole|as well as|followed by)\b",
    re.I,
)

_EXPLICIT_CREW = re.compile(r"\b(plan|review|test|crew|fleet|subagent|orchestrat)\w*\b", re.I)

# A single short read-only question, with no conjunction, is the clearest
# SIMPLE case there is.
SHORT_PROMPT_WORDS = 12
LONG_PROMPT_WORDS = 60


def classify_complexity(text: str) -> Complexity:
    """SIMPLE when one agent should just answer; COMPLEX when a crew earns its
    cost; UNKNOWN when the signals disagree and the model should decide."""
    prompt = (text or "").strip()
    if not prompt:
        return Complexity.UNKNOWN

    words = prompt.split()
    word_count = len(words)
    mutating = bool(_MUTATING.search(prompt))
    read_only = bool(_READ_ONLY.search(prompt))
    multi_step = bool(_MULTI_STEP.search(prompt))
    explicit = bool(_EXPLICIT_CREW.search(prompt))

    if explicit or multi_step:
        return Complexity.COMPLEX
    if word_count >= LONG_PROMPT_WORDS and mutating:
        return Complexity.COMPLEX
    if mutating:
        # A short mutating request ("fix the typo in README") is still one
        # edit; a longer one probably is not.
        return Complexity.SIMPLE if word_count <= SHORT_PROMPT_WORDS else Complexity.UNKNOWN
    if read_only:
        return Complexity.SIMPLE
    if word_count <= SHORT_PROMPT_WORDS:
        return Complexity.SIMPLE
    return Complexity.UNKNOWN
