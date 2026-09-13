"""Conditional routing: the crew a prompt gets, and the prompt that says so.

The defect these tests pin down is the one the roadmap names: every fleet turn
bought planner → coder → reviewer, so "how many files are in this repo?" cost
three model sessions. The classification table below is the heart of it — each
row states the class *and the reason*, because a classifier whose rows can only
be explained by re-reading the regex is a classifier nobody will dare change.

Two properties matter more than any single row:

  * ambiguity resolves upward (an empty prompt, a bare URL, a long one, an
    ask-plus-change) lands on ``standard``, never on a cheaper class;
  * ``ORCHESTRATOR_SYSTEM`` still formats. A new ``{routing_block}``
    placeholder with no matching keyword argument raises ``KeyError`` at the
    top of every fleet turn — the classic way this change breaks production
    while every routing unit test stays green.
"""
from __future__ import annotations

import pytest

from backend.app.orchestrator.agent_def import AgentDef
from backend.app.orchestrator.fleet import (
    DEFAULT_FLEET_CONFIG,
    RouteDecision,
    _merge_config,
    classify,
    config_to_dict,
    decide,
    render_routing_block,
)
from backend.app.orchestrator.fleet.router import FULL_CREW_BLOCK, FULL_CREW_RATIONALE
from backend.app.orchestrator.orchestrator import (
    DEFAULT_ORCHESTRATOR_MAX_TURNS,
    OrchestratorAgent,
)

FULL_CREW = ["planner", "developer", "coder", "reviewer", "tester"]
CORE_CREW = ["planner", "coder", "reviewer"]

# A question long enough that its length alone disqualifies it: nobody writes
# 400+ characters to ask a one-line question, and the ones who do are
# describing a problem, not asking a lookup.
LONG_LOOKUP = (
    "can you tell me which of the worker pool's invariants actually hold when "
    "the sweep runs concurrently with a spawn, because I have been reading the "
    "pidfile logic and the heartbeat loop and I cannot work out whether a "
    "worker that dies between writing its pidfile and answering its first "
    "request is reaped by the sweep or leaks until the next process start, and "
    "the log lines do not distinguish those two cases at all"
)

FENCED_DIFF_QUESTION = """\
why is this failing?

```diff
- return route(x)
+ add the y argument and return route(x, y)
```
"""

UNFENCED_DIFF = (
    "diff --git a/backend/app/orchestrator/fleet/pool.py b/backend/app/"
    "orchestrator/fleet/pool.py\n"
    "@@ -120,7 +120,9 @@ class WorkerPool:\n"
    "-        self._procs[key] = proc\n"
    "+        self._procs[key] = proc\n"
    "+        # add the pidfile before the first request so the sweep can see it\n"
    "+        write_worker_pidfile(key, proc.pid)\n"
    "@@ -built from a real paste, 400+ chars of context follows -----------\n"
    "         async with self._lock:\n"
    "             proc = self._procs.get(key)\n"
)

NUMBERED_LIST = "1. read the loader\n2. fix the merge\n3. run the tests\n"


# Every row: prompt → expected class → why that class and not its neighbours.
CLASSIFY_TABLE: list[tuple[str, str, str]] = [
    # ── lookup: question-shaped, no mutation verb, short ────────────────────
    ("how many files are in this repo?", "lookup",
     "'how many' marker, no mutation verb — the exact trivia that used to cost 3 agents"),
    ("explain the fleet config", "lookup",
     "'explain' marker, nothing to change"),
    ("what is the default entry_role?", "lookup",
     "'what is' marker; 'entry_role' is not a verb"),
    ("which roles are registered?", "lookup",
     "'which' marker, read-only"),
    ("where does the worker pidfile get written?", "lookup",
     "'where'/'does'; 'written' is not the whole word 'write'"),
    ("show me the last 20 lines of the worker log", "lookup",
     "'show' marker, no mutation verb"),
    ("count the python modules under backend/app", "lookup",
     "'count' marker — an inspection, not a change"),
    ("summarize the changes in the last commit", "lookup",
     "'summarize'; 'changes' is not the whole word 'change'"),
    ("is there a test for the gate classifier?", "lookup",
     "'is there' marker, read-only"),
    ("review this PR", "lookup",
     "'review' is the read-only marker — reviewing is not changing"),
    ("what does the `fix` command do?", "lookup",
     "adversarial: 'fix' is a noun inside an inline code span, stripped before matching"),
    (FENCED_DIFF_QUESTION, "lookup",
     "adversarial: the pasted diff says 'add', but a fenced block is evidence, not a request"),
    # ── simple: exactly one ordinary mutation verb, short, single-step ──────
    ("fix the typo in README.md", "simple",
     "one verb, one file; 'README.md' must not read as two sentences"),
    ("bump the ruff version in pyproject.toml", "simple",
     "one verb, one line — a plan for this is the tax being removed"),
    ("add a docstring to worker_key", "simple",
     "one verb, no second unit of work"),
    ("rename STEP_TIMEOUT_S to STEP_BUDGET_S", "simple",
     "one verb, mechanical"),
    ("write a test for the gate classifier", "simple",
     "one verb; 'test' is a noun here, not the tester role"),
    # ── standard: everything else, because ambiguity resolves upward ───────
    ("implement a quota governor", "standard",
     "feature-scale verb: grammatically identical to a typo fix, nothing like it in size"),
    ("refactor collect.py", "standard",
     "feature-scale verb — a refactor is never one line"),
    ("add the routing table then refactor the loader", "standard",
     "'then' plus two verbs: two units of work"),
    ("review and fix the flaky worker test", "standard",
     "an ask AND a change — collapsing that to the coder pair under-plans half of it"),
    ("also fix the lint error", "standard",
     "'also' betrays a step that came before this one"),
    ("update the changelog. add the new flag. bump the version.", "standard",
     "three sentences, three verbs — a list wearing prose"),
    (NUMBERED_LIST, "standard",
     "a numbered list is an enumerated plan by definition"),
    (LONG_LOOKUP, "standard",
     "question-shaped but 400+ chars: length alone says specification, not trivia"),
    (UNFENCED_DIFF, "standard",
     "adversarial: a raw paste mentioning 'add' is long and ambiguous — resolve upward"),
    ("", "standard",
     "an empty prompt tells us nothing, and nothing must never justify spending less"),
    ("https://github.com/anthropics/claude-code/pull/1234", "standard",
     "a bare URL carries no marker and no verb — ambiguous, so full crew"),
]


class TestClassify:
    @pytest.mark.parametrize("prompt,expected,reason", CLASSIFY_TABLE)
    def test_table(self, prompt: str, expected: str, reason: str) -> None:
        assert classify(prompt) == expected, reason

    def test_table_covers_every_class(self) -> None:
        """A table that drifted to all-``standard`` would still pass every row
        above while saving nothing. Pin that all three classes are exercised."""
        seen = {expected for _, expected, _ in CLASSIFY_TABLE}
        assert seen == {"lookup", "simple", "standard"}

    def test_no_model_call(self) -> None:
        """The classifier must stay pure regex: a classifier that spends a
        model call to decide whether to spend model calls defeats itself."""
        import backend.app.orchestrator.fleet.router as router_mod

        source = router_mod.__file__
        assert source is not None
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        for forbidden in ("import anthropic", "claude_agent_sdk", "httpx", "requests"):
            assert forbidden not in text


class TestDecideRoleSelection:
    def test_lookup_picks_the_read_only_role(self) -> None:
        d = decide("how many files are in this repo?", FULL_CREW)

        assert d.task_class == "lookup"
        assert d.agents == ("reviewer",)

    def test_lookup_without_a_reviewer_falls_to_the_coder(self) -> None:
        d = decide("explain the fleet config", ["planner", "coder", "tester"])

        assert d.agents == ("coder",)

    def test_lookup_with_only_a_planner(self) -> None:
        d = decide("explain the fleet config", ["planner"])

        assert d.agents == ("planner",)

    def test_single_role_registry_always_yields_that_role(self) -> None:
        """A tester-only fleet has to do the work with the tester. Preference
        order must never route to an agent that isn't registered."""
        for prompt in ("how many files are here?", "fix the typo", "implement a governor"):
            d = decide(prompt, ["tester"])
            assert d.agents == ("tester",), prompt

    def test_simple_is_coder_then_reviewer(self) -> None:
        d = decide("fix the typo in README.md", FULL_CREW)

        assert d.task_class == "simple"
        assert d.agents == ("coder", "reviewer")

    def test_standard_is_every_registered_role_in_canonical_order(self) -> None:
        d = decide("implement a quota governor", ["reviewer", "planner", "coder"])

        assert d.task_class == "standard"
        assert d.agents == ("planner", "coder", "reviewer")

    def test_unknown_roles_are_dropped(self) -> None:
        d = decide("implement a quota governor", ["coder", "wizard", "PLANNER"])

        assert d.agents == ("planner", "coder")

    def test_empty_registry_returns_a_decision_not_an_exception(self) -> None:
        """``run()`` errors out before this, but a unit caller must get a
        decision back rather than an IndexError from ``available[0]``."""
        d = decide("anything at all", [])

        assert d.agents == ()
        assert d.rationale

    def test_lookup_saves_agents_against_the_registry(self) -> None:
        d = decide("how many files are in this repo?", FULL_CREW)

        assert len(d.agents) < len(FULL_CREW)


class TestDecideRationale:
    @pytest.mark.parametrize(
        "prompt,expected",
        [
            ("how many files are in this repo?", "lookup"),
            ("fix the typo in README.md", "simple"),
            ("implement a quota governor", "standard"),
        ],
    )
    def test_rationale_names_the_class_and_the_trigger(
        self, prompt: str, expected: str
    ) -> None:
        d = decide(prompt, FULL_CREW)

        assert d.rationale.strip()
        assert d.rationale.startswith(expected)
        # The saving (or the spend) is stated, so a log line is arguable.
        assert "agent" in d.rationale


class TestAlwaysFullCrew:
    @pytest.mark.parametrize(
        "prompt", ["how many files are in this repo?", "fix the typo", "implement it all"]
    )
    def test_flag_overrides_every_class(self, prompt: str) -> None:
        d = decide(prompt, FULL_CREW, always_full_crew=True)

        assert d.agents == tuple(FULL_CREW)
        assert d.rationale == FULL_CREW_RATIONALE

    def test_flag_still_reports_the_class_it_overrode(self) -> None:
        """The class stays inspectable — an operator has to be able to see
        that the flag, not the classifier, is what bought four agents."""
        d = decide("how many files are in this repo?", FULL_CREW, always_full_crew=True)

        assert d.task_class == "lookup"

    def test_flag_renders_the_old_wording_verbatim(self) -> None:
        d = decide("how many files are in this repo?", FULL_CREW, always_full_crew=True)

        assert render_routing_block(d) == FULL_CREW_BLOCK
        assert "The user's preference is to see all three" in FULL_CREW_BLOCK


class TestRenderRoutingBlock:
    def test_names_every_chosen_agent(self) -> None:
        d = decide("implement a quota governor", CORE_CREW)
        block = render_routing_block(d)

        for agent in d.agents:
            assert agent in block

    def test_lookup_block_names_the_one_agent_and_the_class(self) -> None:
        d = decide("how many files are in this repo?", FULL_CREW)
        block = render_routing_block(d)

        assert "reviewer" in block
        assert "lookup" in block
        assert d.rationale in block
        # The agents it deliberately skipped must not be named as a set to run.
        assert "`planner`" not in block

    def test_escalation_allowed_de_escalation_forbidden(self) -> None:
        """A block the model reads as advice saves nothing and risks a dropped
        reviewer. Both directions are stated explicitly."""
        block = render_routing_block(decide("fix the typo", FULL_CREW))

        assert "MAY escalate" in block
        assert "MUST NOT de-escalate" in block

    def test_none_renders_the_full_crew_block(self) -> None:
        """Direct/unit construction routes nothing; the safe default is the
        old behaviour, not a guess."""
        assert render_routing_block(None) == FULL_CREW_BLOCK

    def test_empty_agent_set_renders_the_full_crew_block(self) -> None:
        assert render_routing_block(RouteDecision("lookup", (), "n/a")) == FULL_CREW_BLOCK


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name,
        description=f"the {name}",
        provider="claude",
        model="claude-test",
        system_prompt=f"you are the {name}",
    )


REGISTRY = {name: _agent(name) for name in ("planner", "coder", "reviewer")}


def _render(route: RouteDecision | None, *, hitl: bool = False) -> str:
    """The REAL call site, not a re-implementation of it. A test that formats
    ``ORCHESTRATOR_SYSTEM`` with its own kwargs passes happily while the
    orchestrator itself raises ``KeyError`` on the new placeholder at the top
    of every fleet turn — so go through the agent."""
    agent = OrchestratorAgent(
        registry=REGISTRY,
        run_step_fn=None,
        require_plan_approval=hitl,
        route=route,
    )
    assert agent.max_turns == DEFAULT_ORCHESTRATOR_MAX_TURNS
    return agent.build_system_prompt()


class TestSystemPromptStillFormats:
    def test_routed_path_formats_and_carries_the_decision(self) -> None:
        route = decide("how many files are in this repo?", list(REGISTRY))

        prompt = _render(route)

        assert "{routing_block}" not in prompt
        assert "{hitl_block}" not in prompt
        assert "lookup" in prompt
        assert route.rationale in prompt
        # The mandatory paragraph is gone on a routed turn — that removal is
        # the entire behaviour change.
        assert "Do NOT skip planner, coder, or reviewer" not in prompt

    def test_always_full_crew_path_restores_the_old_prompt_exactly(self) -> None:
        route = decide("how many files?", list(REGISTRY), always_full_crew=True)

        prompt = _render(route)

        # Verbatim, in place, with the numbered pipeline still following it.
        assert FULL_CREW_BLOCK + "\n\n  1. Dispatch `planner` first." in prompt

    def test_unrouted_orchestrator_defaults_to_the_full_crew(self) -> None:
        agent = OrchestratorAgent(registry=REGISTRY, run_step_fn=None)

        assert agent.route is None
        assert render_routing_block(agent.route) == FULL_CREW_BLOCK

    def test_orchestrator_keeps_the_route_it_was_given(self) -> None:
        route = decide("how many files?", list(REGISTRY))

        agent = OrchestratorAgent(registry=REGISTRY, run_step_fn=None, route=route)

        assert agent.route is route

    def test_hitl_block_survives_the_new_placeholder(self) -> None:
        """Two placeholders live in one string; adding the second must not
        break the first."""
        prompt = _render(decide("fix the typo", list(REGISTRY)), hitl=True)

        assert "request_plan_approval" in prompt
        assert "HITL plan approval is required" in prompt


class TestConfigFlag:
    def test_defaults_off(self) -> None:
        assert DEFAULT_FLEET_CONFIG.always_full_crew is False

    def test_override_parses(self) -> None:
        cfg = _merge_config(DEFAULT_FLEET_CONFIG, {"always_full_crew": True})

        assert cfg.always_full_crew is True

    def test_absent_override_inherits_the_base(self) -> None:
        base = _merge_config(DEFAULT_FLEET_CONFIG, {"always_full_crew": True})

        cfg = _merge_config(base, {"max_steps": 4})

        assert cfg.always_full_crew is True

    def test_serialized_for_the_api(self) -> None:
        """The UI toggle can only round-trip if the field is in the payload."""
        d = config_to_dict(_merge_config(DEFAULT_FLEET_CONFIG, {"always_full_crew": True}))

        assert d["always_full_crew"] is True
