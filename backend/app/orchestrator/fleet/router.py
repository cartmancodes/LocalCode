"""Conditional routing — decide how many agents a prompt actually deserves.

The failure this module exists to prevent: the orchestrator system prompt used
to *mandate* planner → coder → reviewer on every single turn, read-only ones
included. "how many files are in this repo?" therefore bought a Markdown plan,
an implementation pass and a code review — three model sessions, each with the
full registry preamble, to answer one `ls | wc -l`. The roadmap costs that at
roughly a 15× token multiplier applied to trivia.

Three design commitments, each recording a way this could have gone wrong:

  * **No model call.** A classifier that spends a model call to decide whether
    to spend model calls has already lost. Everything here is regex + length,
    so the decision is deterministic, free, unit-testable, and can be printed
    into a log line an operator can argue with.
  * **Ambiguity resolves upward.** Over-spending on a borderline task wastes
    tokens and is recoverable; under-planning a real implementation ships
    broken code and is not. Every "I'm not sure" branch lands on ``standard``.
  * **The flag stays reachable.** ``FleetConfig.always_full_crew`` renders the
    old mandatory paragraph *verbatim* (``FULL_CREW_BLOCK``), because the
    previous wording records a real user preference and the ruling was that it
    stays one config toggle away, not one git revert away.

One rule is stricter than a first reading of the spec suggests, deliberately:
a prompt that carries **both** a lookup marker and a mutation verb ("review
this PR and fix the flaky test") is ``standard``, not ``simple``. It is two
pieces of work — an inspection and a change — and collapsing it to the coder
pair is exactly the under-planning that ambiguity-resolves-upward forbids.
That guard can only fire on a verb ``MUTATION_VERBS`` actually lists, which is
why the comment on that list is load-bearing rather than decorative.

A second rule is stricter for the same reason: a feature-scale verb
("implement", "refactor", "migrate", "port", "upgrade", "build") is ``standard``
even on its own in a six-word prompt. "implement a quota governor" and "fix the
typo in README.md" are grammatically identical and are not the same job.

Code is stripped before matching — fenced blocks *and* inline spans. Without
that, a pasted diff containing "+ add_route(...)" reads as a request to add
something, and the question "what does the `fix` command do?" reads as a
request to fix something. Both would route a piece of trivia to the full crew,
which is the tax this module was written to remove.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .constants import VALID_ROLES

TaskClass = Literal["lookup", "simple", "standard"]


# Verbs that mean "change the repository". Whole-word matched, after code has
# been stripped, so an "add" inside a pasted diff doesn't count.
#
# THIS LIST IS CLOSED, AND THAT IS CONSEQUENTIAL. A prompt is only kept out of
# ``lookup`` by a verb that appears here. A change request phrased with a verb
# that is missing — "review the auth module and make it thread-safe", before
# "make" was on the list — reads as a pure question and is handed to a single
# read-only agent. That is the one failure this module must never cause.
#
# The listed verbs are therefore deliberately broad and include weak, general
# ones ("make", "set", "run", "handle", "ensure") that are also ordinary nouns.
# An entry can only push a prompt UPWARD to a larger crew, the recoverable
# direction — but only because ``simple`` is gated on ``not _is_question``.
# Without that gate a generic verb matched as a noun inside an unmarked
# question ("how is the handle passed to the worker?") turned that question
# into "exactly one mutation verb, not asking" and DEMOTED it from the
# full-crew default to ``simple``: no planner, and a rationale telling the
# model it had a change to make. With the gate, a coincidental match inside a
# question can only cost tokens. Do not remove that gate while this list is
# broad.
#
# To extend the list, add the bare infinitive here — matching is whole-word, so
# "add" never fires inside "address", "fix" never inside "fixture", "build"
# never inside "builder", and "update" never inside "update_at" (an underscore
# is a word character) — then add a row to ``CLASSIFY_TABLE`` in
# ``test_router.py`` and check that no existing ``lookup`` row flips.
MUTATION_VERBS: tuple[str, ...] = (
    "add", "implement", "fix", "refactor", "write", "create", "delete",
    "remove", "rename", "migrate", "build", "update", "change", "bump",
    "install", "wire", "generate", "port", "upgrade", "patch", "revert",
    "merge", "split", "extract", "replace",
    # Round 1: weak/general verbs that carry just as much work as the strong
    # ones. Without these, "list the failing tests and make them pass"
    # classified as a listing and bought one read-only agent.
    "make", "set", "convert", "move", "enable", "disable", "ensure", "handle",
    "support", "stop", "turn", "switch", "run", "apply", "configure",
    "rewrite", "introduce",
)

# The subset of those verbs that names a *feature*, not a line. "fix the typo
# in README.md" is one edit; "implement a quota governor" is a design, a set of
# files and a test plan wearing the same grammatical shape. One mutation verb
# is not enough to tell them apart, so these always take the full crew —
# under-planning a feature is the failure this whole module must not cause.
HEAVY_MUTATION_VERBS: tuple[str, ...] = (
    "implement", "refactor", "migrate", "port", "upgrade", "build",
)

# Shapes that mean "tell me something about the repository".
LOOKUP_MARKERS: tuple[str, ...] = (
    "how many", "what is", "what are", "which", "where", "why", "explain",
    "describe", "summarize", "list", "show", "find", "count", "read",
    "look at", "inspect", "review", "does", "is there", "can you tell",
)

# Words that betray a second unit of work hiding behind the first. "and then"
# is subsumed by "then".
MULTI_STEP_MARKERS: tuple[str, ...] = ("then", "also")

# Openers that make a prompt a question or a request for information, whether
# or not it carries a ``LOOKUP_MARKERS`` phrase. This is a SHAPE test, not a
# vocabulary test, and it exists to keep ``simple`` honest: "how is the handle
# passed to the worker?" is a question whose only mutation verb ("handle") is a
# noun, and without this guard it classified as a one-verb change — dropping
# the planner and telling the model it had an edit to make.
QUESTION_OPENERS: tuple[str, ...] = (
    "how", "what", "when", "where", "why", "which", "who", "whose",
    "is", "are", "does", "do", "did", "can", "could", "should", "would",
    "will", "tell me", "show me", "give me", "explain", "describe",
    "summarize", "list",
)

# Above this, a prompt is a specification, not a one-liner — whatever verbs it
# happens to contain. Measured on the code-stripped text so a short question
# with a long pasted traceback isn't punished for the traceback.
MAX_SHORT_PROMPT_CHARS = 400

# Who answers a question. Reviewer first because it is the read-only role: a
# lookup should not be handed to an agent whose whole prompt tells it to edit.
LOOKUP_ROLE_PREFERENCE: tuple[str, ...] = ("reviewer", "coder", "developer", "planner")

# A one-verb change: make it, then have it checked. No plan for a typo.
SIMPLE_ROLES: tuple[str, ...] = ("coder", "reviewer")

# Sentinel rationale. ``render_routing_block`` keys the verbatim-old-wording
# path off this exact string, so the flag's rendering can't drift from the
# flag's decision — they are produced and consumed in one file.
FULL_CREW_RATIONALE = "always_full_crew is set in the fleet config"

# The pre-Task-8 paragraph, character for character. Setting ``always_full_crew``
# must restore the *previous behaviour*, not a paraphrase of it.
FULL_CREW_BLOCK = """\
For every user task, use the mandatory core sequence below when those agents
are registered. Do NOT skip planner, coder, or reviewer because a task looks
trivial, read-only, or informational. The user's preference is to see all three
core agents participate on every fleet turn."""


def _phrase_re(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """Whole-word alternation. Multi-word markers tolerate any run of
    whitespace so a line-wrapped "how\\nmany" still matches."""
    alts = "|".join(r"\s+".join(re.escape(w) for w in p.split()) for p in phrases)
    return re.compile(rf"\b(?:{alts})\b")


_MUTATION_RE = _phrase_re(MUTATION_VERBS)
_HEAVY_RE = _phrase_re(HEAVY_MUTATION_VERBS)
_LOOKUP_RE = _phrase_re(LOOKUP_MARKERS)
_MULTI_STEP_RE = _phrase_re(MULTI_STEP_MARKERS)
# Anchored: an opener only counts where it opens. "explain" mid-sentence is a
# lookup marker's job; here we are asking what shape the whole prompt has.
_QUESTION_OPENER_RE = re.compile(
    r"^\W*(?:"
    + "|".join(r"\s+".join(re.escape(w) for w in p.split()) for p in QUESTION_OPENERS)
    + r")\b"
)

# Unterminated fences count too — a half-pasted diff is still a paste.
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
# "1. ", "2) " at the start of a line: an enumerated set of steps.
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
# A terminator only ends a sentence when whitespace or the end follows it —
# otherwise "README.md" and "v1.2.3" would each read as several sentences.
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")


@dataclass(frozen=True)
class RouteDecision:
    """What this turn is allowed to cost, and why.

    Frozen: the decision is computed once per turn, logged, and rendered into
    the system prompt. A mutable one invites a caller to "just add the tester"
    after the rationale it no longer matches has already been logged.
    """

    task_class: TaskClass
    agents: tuple[str, ...]  # roles to dispatch, in canonical order
    rationale: str  # one line, shown in the prompt and logged at INFO


def strip_code(prompt: str) -> str:
    """Remove fenced blocks and inline spans. Pasted code is evidence, not
    instruction: matching verbs inside it is how a diff becomes a work order."""
    text = _FENCE_RE.sub(" ", prompt)
    return _INLINE_CODE_RE.sub(" ", text)


def _is_question(text: str) -> bool:
    """Is this prompt shaped like a question or a request for information?

    Deliberately independent of ``LOOKUP_MARKERS``: the marker list is a closed
    vocabulary, and an unmarked question ("what happens if we stop the
    process?") must still be recognised as *not a change request*. Used only to
    veto ``simple``, never to grant ``lookup`` — a question this recognises but
    the markers don't lands on ``standard``, which is the recoverable side.
    """
    return text.endswith("?") or bool(_QUESTION_OPENER_RE.match(text))


def _is_multi_step(text: str) -> bool:
    if _MULTI_STEP_RE.search(text) or _NUMBERED_ITEM_RE.search(text):
        return True
    return len(_SENTENCE_END_RE.findall(text)) > 2


def classify(prompt: str) -> TaskClass:
    """Bucket a prompt without asking a model.

    ``lookup``   a question, no mutation verb, short, single-step.
    ``simple``   exactly one mutation verb — and not a feature-scale one —
                 short, single-step, nothing asked alongside it, and not
                 shaped like a question.
    ``standard`` everything else — including the empty prompt, which tells us
                 nothing and must therefore not be used to justify spending
                 less than the full crew.
    """
    text = strip_code(prompt or "").lower().strip()
    if len(text) > MAX_SHORT_PROMPT_CHARS:
        # Length alone is a real signal: nobody writes 400 characters to ask
        # how many files are in a directory.
        return "standard"

    asks = bool(_LOOKUP_RE.search(text))
    mutations = _MUTATION_RE.findall(text)

    if asks and not mutations and not _is_multi_step(text):
        # The multi-step markers have to bite here too, not only on ``simple``:
        # "explain the loader then make it faster" is a question with a second
        # unit of work bolted on, and one read-only agent cannot do the second.
        return "lookup"
    # Both an ask and a change is two units of work — resolve upward. And a
    # question is never a change request, whatever verb it happens to contain:
    # without the shape test, every generic verb added to MUTATION_VERBS
    # silently DEMOTED unmarked questions from ``standard`` to ``simple``.
    if (
        len(mutations) == 1
        and not asks
        and not _is_question(text)
        and not _HEAVY_RE.search(text)
        and not _is_multi_step(text)
    ):
        return "simple"
    return "standard"


def _canonical(available_roles: object) -> tuple[str, ...]:
    """Filter to registered roles in canonical execution order. Accepts any
    iterable of names so callers can pass ``cfg.role_names()`` or a set."""
    present = {str(r).strip().lower() for r in available_roles or ()}
    return tuple(r for r in VALID_ROLES if r in present)


def _agents_phrase(n: int) -> str:
    return "1 agent" if n == 1 else f"{n} agents"


def decide(
    prompt: str,
    available_roles: object,
    *,
    always_full_crew: bool = False,
) -> RouteDecision:
    """Pick the agent set for one turn.

    ``always_full_crew`` wins over every class — it is the escape hatch that
    restores the pre-Task-8 behaviour, so it cannot be conditional on the
    classifier agreeing with it.
    """
    available = _canonical(available_roles)
    task_class = classify(prompt)

    if always_full_crew:
        return RouteDecision(task_class, available, FULL_CREW_RATIONALE)

    if not available:
        # Never reached from ``run()`` (an empty fleet errors out earlier), but
        # a unit caller must get a decision back, not an IndexError.
        return RouteDecision(
            task_class, (), f"{task_class}: no roles are registered → nothing to dispatch"
        )

    if task_class == "lookup":
        pick = next((r for r in LOOKUP_ROLE_PREFERENCE if r in available), available[0])
        agents = (pick,)
        rationale = (
            "lookup: question-shaped, no mutation verb → "
            f"{_agents_phrase(len(agents))} instead of {len(available)}"
        )
    elif task_class == "simple":
        # Falls back to whatever single role exists — a tester-only fleet still
        # has to be able to do the work.
        agents = tuple(r for r in SIMPLE_ROLES if r in available) or (available[0],)
        rationale = (
            "simple: one mutation verb, single-step → "
            f"{_agents_phrase(len(agents))} instead of {len(available)}"
        )
    else:
        agents = available
        rationale = (
            "standard: multi-step, long, or ambiguous → the full crew "
            f"({_agents_phrase(len(agents))})"
        )

    return RouteDecision(task_class, agents, rationale)


def render_routing_block(decision: RouteDecision | None) -> str:
    """The Markdown paragraph spliced into the orchestrator system prompt.

    A routing block the model reads as advice saves nothing, so the agent set
    is stated as an instruction with both directions pinned: escalation is
    explicitly allowed (the classifier can undershoot), de-escalation is
    explicitly forbidden (otherwise a model that thinks a task is trivial
    silently drops the reviewer).

    ``None`` — direct/unit construction of ``OrchestratorAgent`` — and the
    ``always_full_crew`` flag both render the old mandatory wording, so the
    safe default and the opt-out are the same known-good paragraph.
    """
    if decision is None or not decision.agents or decision.rationale == FULL_CREW_RATIONALE:
        return FULL_CREW_BLOCK

    named = ", ".join(f"`{a}`" for a in decision.agents)
    return (
        f"**Routing for this turn — task class: `{decision.task_class}`.**\n"
        f"Dispatch exactly these agents, in this order: {named}.\n"
        f"Rationale: {decision.rationale}.\n"
        "\n"
        "The numbered pipeline below describes the FULL workflow. For this turn,\n"
        "run only the steps whose agent is named above, keeping their relative\n"
        "order, and skip the rest.\n"
        "\n"
        "You MAY escalate: if the work turns out to be larger than that\n"
        "classification suggested (a question that can only be answered by\n"
        "changing code, a one-line fix that spreads across modules), dispatch\n"
        "the additional registered agents you need and say why in one line.\n"
        "You MUST NOT de-escalate: do not skip an agent named above because the\n"
        "task looks trivial. The set above is the floor, not a suggestion."
    )
