"""Golden traces: a fleet turn's whole shape, committed to a file.

A fleet turn is the hardest thing in this repo to hold still. Routing decides
how many agents run, dispatch decides what each is told, the role table decides
what each may touch, the gate decides whether a human is asked, and the steps
leave files behind. Any one of those can change without a single unit test
noticing, because each unit test only knows its own layer. A golden is the
cross-layer assertion: run the turn, write down everything observable about it,
and compare that to a file a human reviewed.

**Two traces, and together they are the regression net for Task 8's routing.**
``fleet_standard`` is a feature request that must buy the full crew;
``fleet_lookup`` is a question that must buy exactly one read-only agent. Task 8
needed two fix rounds, and both of them are pinned here: a question is never a
simple change (the lookup trace's prompt carries a mutation verb as a noun and
still routes to one agent), and a narrow lookup must not quietly become a broad
one (its ``route.agents`` is one name, from a registry of three).

**What is recorded, and what is deliberately not.** Per step: the ordered tool
calls with a stable hash of each call's arguments, and the gate's verdict on
each. Per turn: the routing decision, the approval cards with their decisions,
and the files that appeared under the temp cwd. NOT recorded: prose, timings,
absolute paths, counters — anything that changes for reasons that are not
behaviour. The argument hash is ``sha256`` of the arguments as sorted, compact
JSON, first 12 hex characters: enough to catch "the coder is being handed a
different path now", short enough to read in a diff.

**Refreshing a golden.** When a trace changes because the behaviour changed on
purpose::

    UPDATE_GOLDEN=1 .venv/bin/pytest backend/tests/test_golden_traces.py

then READ the diff before committing it. A golden that is regenerated without
being read is a file that agrees with whatever the code now does, which is the
one thing a golden must never be.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from backend.app.orchestrator.fleet.router import decide
from backend.app.session_runner.bus import EventBus
from backend.app.session_runner.turn import execute_turn
from backend.app.storage.sessions import store as session_store
from backend.tests.fakes.providers import (
    GOLDEN_DIR,
    FakeOrchestratorModel,
    FakeWorkerPool,
    GatedSubProviders,
    ToolCall,
    fleet_provider_with,
)

WAIT_S = 20.0
DISPATCH = "dispatch_subagent"
PLAN_APPROVAL = "request_plan_approval"

# A plan timestamp and an approval counter are both real, both unstable, and
# neither is behaviour. Normalized rather than dropped, so the trace still says
# "a plan file was written" and "one plan gate was raised".
_PLAN_FILE_RE = re.compile(r"^(\.localcode/plans/)\d{8}-\d{6}-(.*\.md)$")


def scrub(value: Any, cwd: Path) -> Any:
    """Replace the temp working directory with ``<cwd>`` everywhere in a value.

    Every run gets a different ``tmp_path``, so hashing an absolute path would
    make the golden differ from itself. The relative part is what carries the
    behaviour — "the coder was handed *this* file" — and it survives.
    """
    if isinstance(value, str):
        return value.replace(str(cwd), "<cwd>")
    if isinstance(value, dict):
        return {k: scrub(v, cwd) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, cwd) for v in value]
    return value


def args_sha(args: Any) -> str:
    """Stable hash of one call's arguments: sorted, compact JSON; sha256; 12 hex."""
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def normalize_path(rel: str) -> str:
    match = _PLAN_FILE_RE.match(rel)
    return f"{match.group(1)}<ts>-{match.group(2)}" if match else rel


def files_under(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
    }


class Bus:
    """Everything the turn broadcast, in order."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)

    def of(self, ev_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == ev_type]


async def run_traced_turn(
    home: Path,
    *,
    prompt: str,
    roles: tuple[str, ...],
    script: list[tuple[str, dict[str, Any]]],
    subs: GatedSubProviders,
    monkeypatch: pytest.MonkeyPatch,
    require_plan_approval: bool = False,
    approve: str | None = None,
) -> dict[str, Any]:
    """One fleet turn, reduced to its trace."""
    cwd = home / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    before = files_under(cwd)

    meta = await session_store.create_session(provider="fleet", model="m", cwd=str(cwd))
    session_id = str(meta["id"])
    FakeOrchestratorModel(script).install(monkeypatch)
    subs.install(monkeypatch)

    bus_log = Bus()
    approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pool = FakeWorkerPool()
    provider = fleet_provider_with(pool)

    async def answer_gates() -> None:
        answered: set[str] = set()
        while True:
            for card in bus_log.of("pipeline.awaiting_approval"):
                approval_id = str(card["data"]["id"])
                if approval_id not in answered and approve is not None:
                    answered.add(approval_id)
                    await approval_q.put({"id": approval_id, "value": approve})
            await asyncio.sleep(0.01)

    answerer = asyncio.create_task(answer_gates())
    try:
        await asyncio.wait_for(
            execute_turn(
                session_id=session_id,
                bus=EventBus(session_id, on_event=bus_log),
                approval_q=approval_q,
                provider=provider,
                provider_name="fleet",
                model="m",
                cwd=str(cwd),
                additional_dirs=[],
                upstream_id=None,
                fleet_override={
                    "roles": {r: {"provider": "claude", "model": "m"} for r in roles},
                    "require_plan_approval": require_plan_approval,
                },
                permission_mode="acceptEdits",
                prompt=prompt,
            ),
            WAIT_S,
        )
    finally:
        answerer.cancel()
        try:
            await answerer
        except asyncio.CancelledError:
            pass
        await provider.aclose()

    route = decide(prompt, roles)
    # The trace's own claim about the session directory is not interesting, and
    # it is not under the user's tree in production either.
    created = sorted(
        normalize_path(p)
        for p in files_under(cwd) - before
        if not p.startswith(".localcode/sessions/")
    )

    # Step cards carry the id and the resolved "<role> [<provider>:<model>]".
    steps: list[dict[str, Any]] = []
    results = {
        str((e.get("data") or {}).get("tool_use_id")): e["data"]
        for e in bus_log.of("tool.result")
    }
    for card in bus_log.of("assistant.tool_use"):
        data = card["data"]
        role = str(data["name"]).split(" [", 1)[0]
        steps.append(
            {
                "id": data["id"],
                "role": role,
                "backend": str(data["name"]).split(" [", 1)[1].rstrip("]"),
                "tools": [
                    {
                        "name": rec.tool,
                        "args_sha": args_sha(scrub(rec.tool_input, cwd)),
                        "gate": "allow" if rec.allowed else "deny",
                    }
                    for rec in subs.gate
                    if rec.role == role
                ],
                "failed": bool(results.get(str(data["id"]), {}).get("is_error")),
            }
        )

    decisions = {
        str((e.get("data") or {}).get("id")): str((e.get("data") or {}).get("value"))
        for e in bus_log.of("pipeline.approval_received")
    }
    approvals = [
        {
            "id": f"{str(card['data']['id']).rsplit('.', 1)[0]}.#{n}",
            "kind": card["data"].get("kind"),
            "tool": card["data"].get("tool"),
            "decision": decisions.get(str(card["data"]["id"]), "unanswered"),
        }
        for n, card in enumerate(bus_log.of("pipeline.awaiting_approval"), start=1)
    ]

    return {
        "prompt": prompt,
        "registered_roles": list(roles),
        "route": {
            "task_class": route.task_class,
            "agents": list(route.agents),
            "rationale": route.rationale,
        },
        "steps": steps,
        "approvals": approvals,
        "files_created": created,
        "terminal_events": [
            e["type"] for e in bus_log.events if e["type"] in ("error", "assistant.done")
        ],
    }


def compare_to_golden(name: str, trace: dict[str, Any]) -> None:
    """Assert against the committed golden, or rewrite it under UPDATE_GOLDEN=1."""
    path = GOLDEN_DIR / name
    rendered = json.dumps(trace, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"{path} is missing — run UPDATE_GOLDEN=1 pytest {__file__} and READ the result"
    )
    expected = path.read_text(encoding="utf-8")
    if expected != rendered:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                rendered.splitlines(),
                fromfile=f"{name} (committed)",
                tofile=f"{name} (this run)",
                lineterm="",
            )
        )
        raise AssertionError(
            f"the {name} trace changed.\n\n{diff}\n\n"
            "If this change is intended, rerun with UPDATE_GOLDEN=1 and read the diff."
        )


# ─────────────────────────────────────────────────────────────────────────────
# The two turns
# ─────────────────────────────────────────────────────────────────────────────

STANDARD_PROMPT = (
    "Implement retry-with-backoff in the uploader, then review the change "
    "against the plan."
)
LOOKUP_PROMPT = "How many retries does the uploader do before it gives up?"
# A question whose only mutation verb ("handle") is a noun. Task 8's second
# fix round is what keeps this OUT of ``simple``; it lands on ``standard``,
# the recoverable side, and never on the coder pair.
QUESTION_WITH_A_VERB = "How is the retry handle passed to the worker?"

STANDARD_ROLES = ("planner", "coder", "reviewer")
LOOKUP_ROLES = ("planner", "coder", "reviewer")


def standard_subs(cwd: Path) -> GatedSubProviders:
    """planner reads, coder writes, reviewer reads — and reviewer's write is
    refused by the role table, which is the line worth a golden."""
    return GatedSubProviders(
        {
            "planner": (
                (ToolCall("Read", {"file_path": str(cwd / "uploader.py")}),),
                "# Retry the uploader\n\n1. Add a backoff helper.\n2. Cover it with a test.",
            ),
            "coder": (
                (
                    ToolCall(
                        "Write",
                        {"file_path": str(cwd / "uploader.py"), "content": "retry()\n"},
                        effect=lambda: (cwd / "uploader.py").write_text("retry()\n"),
                    ),
                    ToolCall("Bash", {"command": "pytest -q"}, output="1 passed"),
                ),
                "Implemented the backoff helper and its test.",
            ),
            "reviewer": (
                (
                    ToolCall("Read", {"file_path": str(cwd / "uploader.py")}),
                    ToolCall(
                        "Write",
                        {"file_path": str(cwd / "uploader.py"), "content": "tweak\n"},
                        effect=lambda: (cwd / "reviewer-should-not-write.py").write_text("x"),
                    ),
                ),
                "Matches the plan.\n\n"
                '```json\n{"verdict": "lgtm", "reason": "both tasks present"}\n```',
            ),
        }
    )


def lookup_subs(cwd: Path) -> GatedSubProviders:
    return GatedSubProviders(
        {
            "reviewer": (
                (
                    ToolCall("Grep", {"pattern": "retry", "path": str(cwd)}),
                    ToolCall("Read", {"file_path": str(cwd / "uploader.py")}),
                ),
                "The uploader retries three times.\n\n"
                '```json\n{"verdict": "lgtm", "reason": "question answered"}\n```',
            )
        }
    )


class TestGoldenTraces:
    async def test_a_standard_turn_matches_its_golden(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings: Any
    ) -> None:
        cwd = isolated_store / "proj"
        trace = await run_traced_turn(
            isolated_store,
            prompt=STANDARD_PROMPT,
            roles=STANDARD_ROLES,
            script=[
                (DISPATCH, {"name": "planner", "prompt": "plan the retry work"}),
                (PLAN_APPROVAL, {"plan_summary": "Add a backoff helper, then test it."}),
                (DISPATCH, {"name": "coder", "prompt": "implement the plan"}),
                (DISPATCH, {"name": "reviewer", "prompt": "review the change"}),
            ],
            subs=standard_subs(cwd),
            monkeypatch=monkeypatch,
            require_plan_approval=True,
            approve="yes",
        )

        compare_to_golden("fleet_standard.json", trace)
        # Stated here as well as recorded there: the reviewer TRIED to write,
        # the gate refused, and no file appeared. A golden that only recorded
        # the refusal would still pass if the write happened anyway.
        reviewer = next(s for s in trace["steps"] if s["role"] == "reviewer")
        assert [t["gate"] for t in reviewer["tools"]] == ["allow", "deny"]
        assert not (cwd / "reviewer-should-not-write.py").exists()

    async def test_a_lookup_turn_matches_its_golden(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings: Any
    ) -> None:
        cwd = isolated_store / "proj"
        trace = await run_traced_turn(
            isolated_store,
            prompt=LOOKUP_PROMPT,
            roles=LOOKUP_ROLES,
            script=[(DISPATCH, {"name": "reviewer", "prompt": "answer the question"})],
            subs=lookup_subs(cwd),
            monkeypatch=monkeypatch,
        )

        compare_to_golden("fleet_lookup.json", trace)


class TestWhatTheGoldensAreProofOf:
    """The two claims the goldens exist to keep true, stated as assertions so a
    reader does not have to infer them from a JSON file."""

    def test_a_narrow_lookup_stays_narrow(self) -> None:
        """One read-only agent out of a registry of three, and it is the
        read-only one: a lookup must not be handed to an agent whose whole
        prompt tells it to edit."""
        route = decide(LOOKUP_PROMPT, LOOKUP_ROLES)

        assert route.task_class == "lookup"
        assert route.agents == ("reviewer",)

    def test_a_question_is_never_a_simple_change(self) -> None:
        """``handle`` is in ``MUTATION_VERBS`` and is also an ordinary noun.
        Without the question-shape veto this read as "exactly one mutation
        verb, not asking" and was DEMOTED from the full crew to the coder pair
        — no planner, and a rationale telling the model it had an edit to
        make. Ambiguity resolves UPWARD, so it lands on ``standard``."""
        route = decide(QUESTION_WITH_A_VERB, LOOKUP_ROLES)

        assert route.task_class == "standard"
        assert route.agents == LOOKUP_ROLES

    def test_a_feature_request_still_buys_the_whole_crew(self) -> None:
        route = decide(STANDARD_PROMPT, STANDARD_ROLES)

        assert route.task_class == "standard"
        assert route.agents == STANDARD_ROLES

    async def test_the_routing_decision_reaches_the_model(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings: Any
    ) -> None:
        """A route nobody is told about saves nothing. The agent set is
        rendered into the orchestrator's system prompt as an instruction."""
        cwd = isolated_store / "proj"
        model = FakeOrchestratorModel(
            [(DISPATCH, {"name": "reviewer", "prompt": "answer the question"})]
        )
        monkeypatch.setattr(
            "backend.app.orchestrator.dispatch.create_sdk_mcp_server", model._capture
        )
        monkeypatch.setattr("backend.app.orchestrator.orchestrator.query", model._query)
        lookup_subs(cwd).install(monkeypatch)

        meta = await session_store.create_session(
            provider="fleet", model="m", cwd=str(cwd)
        )
        pool = FakeWorkerPool()
        provider = fleet_provider_with(pool)
        try:
            await asyncio.wait_for(
                execute_turn(
                    session_id=str(meta["id"]),
                    bus=EventBus(str(meta["id"])),
                    approval_q=asyncio.Queue(),
                    provider=provider,
                    provider_name="fleet",
                    model="m",
                    cwd=str(cwd),
                    additional_dirs=[],
                    upstream_id=None,
                    fleet_override={
                        "roles": {r: {"provider": "claude", "model": "m"} for r in LOOKUP_ROLES}
                    },
                    permission_mode="acceptEdits",
                    prompt=LOOKUP_PROMPT,
                ),
                WAIT_S,
            )
        finally:
            await provider.aclose()

        assert model.system_prompt is not None
        assert "task class: `lookup`" in model.system_prompt
        assert "Dispatch exactly these agents, in this order: `reviewer`" in model.system_prompt
        assert "MUST NOT de-escalate" in model.system_prompt
