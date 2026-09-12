"""Tests for the one approval bus (`backend/app/orchestrator/approvals.py`).

Three properties carry the security value of this layer and each has tests
that fail loudly if it regresses:

  * an ``ask`` with no approval channel attached **denies** — the code this
    replaced escalated an unknown mode to ``acceptEdits`` instead, granting
    filesystem writes on every headless step;
  * the decision layer is provider-neutral — every assertion below about
    ``evaluate_tool_request`` is written the way Task 10's Codex handler will
    call it, with no SDK type in sight;
  * a callback that raises denies rather than killing the turn.

The Claude adapter is tested only for the translation it performs, because
translation is all it is allowed to do.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
)
from fastapi import HTTPException

from backend.app.orchestrator import claude as claude_mod
from backend.app.orchestrator.approvals import (
    APPROVAL_ID_PREFIX,
    EventSink,
    build_can_use_tool,
    evaluate_tool_request,
    next_approval_id,
    open_approval_gate,
    summarize_tool_input,
)
from backend.app.orchestrator.base import RunContext
from backend.app.orchestrator.permissions import ToolPolicy, policy_for_role
from backend.app.routes.sessions import _validate_cwd

# Generous enough that a loaded machine doesn't fail the test, short enough
# that a genuinely stuck gate doesn't hang the suite.
WAIT_S = 5.0


def mk_policy(roots: tuple[Path, ...] = (), **overrides: Any) -> ToolPolicy:
    defaults: dict[str, Any] = dict(
        name="session",
        writable=True,
        exec_allowed=True,
        roots=roots,
        denied=(),
        allow_tools=None,
        deny_tools=(),
    )
    defaults.update(overrides)
    return ToolPolicy(**defaults)


def ctx() -> ToolPermissionContext:
    """The SDK's third callback argument. We ignore it; it must still pass."""
    return ToolPermissionContext()


# ── summarize_tool_input ─────────────────────────────────────────────────


class TestSummarizeToolInput:
    def test_long_content_is_truncated_with_a_marker(self) -> None:
        body = "x" * 5000
        out = summarize_tool_input({"content": body})

        assert out["content"].startswith("x" * 200)
        assert "more chars)" in out["content"]
        assert str(5000 - 200) in out["content"]

    def test_file_path_survives_whole(self) -> None:
        path = "/Users/someone/projects/localcode/backend/app/orchestrator/approvals.py"
        out = summarize_tool_input({"file_path": path, "content": "y" * 400})

        assert out["file_path"] == path

    def test_no_value_exceeds_the_cap(self) -> None:
        out = summarize_tool_input(
            {"command": "echo " + "a" * 10_000, "new_string": "b" * 10_000},
            max_chars=600,
        )

        assert out  # the preview is not empty — it is capped, not dropped
        for value in out.values():
            assert len(value) <= 600

    def test_a_command_is_not_cut_to_the_body_preview_length(self) -> None:
        # A shell command is the decision itself: cutting it at 200 chars like
        # a file body would hide the `&& rm -rf` at the end.
        command = "pytest -q " + "x" * 300
        out = summarize_tool_input({"command": command})

        assert out["command"] == command

    def test_non_string_values_are_kept_or_capped(self) -> None:
        out = summarize_tool_input({"limit": 5, "all": True, "edits": [{"x": "z" * 900}]})

        assert out["limit"] == 5
        assert out["all"] is True
        assert len(out["edits"]) <= 600


class TestNextApprovalId:
    def test_ids_are_unique_per_gate(self) -> None:
        ids = {next_approval_id(APPROVAL_ID_PREFIX) for _ in range(5)}

        assert len(ids) == 5
        assert all(i.startswith(f"{APPROVAL_ID_PREFIX}.") for i in ids)


# ── build_can_use_tool: translation only ─────────────────────────────────


class TestCanUseToolAdapter:
    async def test_allow_returns_permission_result_allow(self, tmp_path: Path) -> None:
        fn = build_can_use_tool(
            policy=mk_policy(roots=(tmp_path,)),
            mode="default",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )

        result = await fn("Read", {"file_path": str(tmp_path / "a.py")}, ctx())

        assert isinstance(result, PermissionResultAllow)

    async def test_deny_message_names_the_tool(self, tmp_path: Path) -> None:
        fn = build_can_use_tool(
            policy=mk_policy(roots=(tmp_path,)),
            mode="default",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )

        result = await fn("Write", {"file_path": "/etc/hosts", "content": "x"}, ctx())

        assert isinstance(result, PermissionResultDeny)
        assert "Write" in result.message
        # The model only ever sees this string, so it has to carry the reason.
        assert "/etc/hosts" in result.message

    async def test_ask_through_the_adapter_allows_on_yes(self, tmp_path: Path) -> None:
        sink = EventSink()
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        fn = build_can_use_tool(
            policy=mk_policy(roots=(tmp_path,)),
            mode="default",
            sink=sink,
            approval_channel=channel,
            timeout_s=WAIT_S,
        )
        task = asyncio.create_task(
            fn("Write", {"file_path": str(tmp_path / "a.py"), "content": "x"}, ctx())
        )

        card = await asyncio.wait_for(sink.get(), WAIT_S)
        assert card is not None
        await channel.put({"id": card.data["id"], "value": "yes"})

        assert isinstance(await asyncio.wait_for(task, WAIT_S), PermissionResultAllow)


# ── evaluate_tool_request: the provider-neutral core ─────────────────────


class TestEvaluateToolRequest:
    async def test_ask_emits_a_tool_card_and_allows_on_yes(self, tmp_path: Path) -> None:
        sink = EventSink()
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        task = asyncio.create_task(
            evaluate_tool_request(
                "Write",
                {"file_path": str(tmp_path / "a.py"), "content": "z" * 4000},
                policy=mk_policy(roots=(tmp_path,)),
                mode="default",
                sink=sink,
                approval_channel=channel,
                timeout_s=WAIT_S,
            )
        )

        card = await asyncio.wait_for(sink.get(), WAIT_S)
        assert card is not None
        assert card.type == "pipeline.awaiting_approval"
        assert card.data["kind"] == "tool"
        assert card.data["tool"] == "Write"
        assert card.data["input"]["file_path"] == str(tmp_path / "a.py")
        assert "more chars)" in card.data["input"]["content"]  # preview, not the body
        assert card.data["reason"]
        assert card.data["timeout_s"] == WAIT_S

        await channel.put({"id": card.data["id"], "value": "yes"})
        decision = await asyncio.wait_for(task, WAIT_S)

        assert decision.outcome == "allow"
        received = await asyncio.wait_for(sink.get(), WAIT_S)
        assert received is not None
        assert received.type == "pipeline.approval_received"
        assert received.data["value"] == "yes"
        assert received.data["id"] == card.data["id"]

    async def test_no_carries_the_users_feedback_into_the_deny_reason(
        self, tmp_path: Path
    ) -> None:
        sink = EventSink()
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        task = asyncio.create_task(
            evaluate_tool_request(
                "Bash",
                {"command": "rm -rf build"},
                policy=mk_policy(roots=(tmp_path,)),
                mode="default",
                sink=sink,
                approval_channel=channel,
                timeout_s=WAIT_S,
            )
        )

        card = await asyncio.wait_for(sink.get(), WAIT_S)
        assert card is not None
        await channel.put(
            {"id": card.data["id"], "value": "no", "feedback": "use make clean"}
        )
        decision = await asyncio.wait_for(task, WAIT_S)

        assert decision.outcome == "deny"
        assert "use make clean" in decision.reason
        received = await asyncio.wait_for(sink.get(), WAIT_S)
        assert received is not None
        assert received.type == "pipeline.approval_received"
        assert received.data["value"] == "no"

    async def test_zero_timeout_denies_and_says_so(self, tmp_path: Path) -> None:
        sink = EventSink()
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        decision = await asyncio.wait_for(
            evaluate_tool_request(
                "Write",
                {"file_path": str(tmp_path / "a.py"), "content": "x"},
                policy=mk_policy(roots=(tmp_path,)),
                mode="default",
                sink=sink,
                approval_channel=channel,
                timeout_s=0.0,
            ),
            WAIT_S,
        )

        assert decision.outcome == "deny"
        assert "timeout" in decision.reason
        # Both events still reach the sink: the card is part of the history of
        # what the turn asked for, and the UI clears it on approval_received.
        card = await asyncio.wait_for(sink.get(), WAIT_S)
        received = await asyncio.wait_for(sink.get(), WAIT_S)
        assert card is not None and received is not None
        assert card.type == "pipeline.awaiting_approval"
        assert received.data["value"] == "timeout"

    async def test_interactive_accept_edits_with_a_channel_still_cards_bash(
        self, tmp_path: Path
    ) -> None:
        # Controller Ruling 22, the regression this fix round exists to
        # prevent: `ctx.role` is unset for every interactive session, so a
        # plain chat session in the UI's default mode (acceptEdits) gets the
        # permissive "session" policy (`exec_allowed=True`) — exactly the
        # shape a fleet role also has. With a human actually attached (a real
        # approval_channel), Bash must still raise a card and wait for a
        # decision, never auto-allow the way the reverted branch-9 amendment
        # would have. This is what tells the headless concession (scoped to
        # "no channel") apart from a mode-keyed one (which cannot tell an
        # interactive session from a fleet step).
        sink = EventSink()
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        task = asyncio.create_task(
            evaluate_tool_request(
                "Bash",
                {"command": "rm -rf build"},
                policy=mk_policy(roots=(tmp_path,)),  # the "session" shape: exec_allowed=True
                mode="acceptEdits",
                sink=sink,
                approval_channel=channel,
                timeout_s=WAIT_S,
            )
        )

        card = await asyncio.wait_for(sink.get(), WAIT_S)
        assert card is not None
        assert card.type == "pipeline.awaiting_approval"
        assert card.data["tool"] == "Bash"

        await channel.put({"id": card.data["id"], "value": "yes"})
        decision = await asyncio.wait_for(task, WAIT_S)
        assert decision.outcome == "allow"

        received = await asyncio.wait_for(sink.get(), WAIT_S)
        assert received is not None and received.type == "pipeline.approval_received"

    async def test_ask_without_an_approval_channel_denies(self, tmp_path: Path) -> None:
        # THE headless decision: the replaced code escalated to acceptEdits
        # here, which granted every write on every fleet step.
        decision = await evaluate_tool_request(
            "Write",
            {"file_path": str(tmp_path / "a.py"), "content": "x"},
            policy=mk_policy(roots=(tmp_path,)),
            mode="default",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )

        assert decision.outcome == "deny"
        assert decision.outcome != "allow"
        assert "no operator" in decision.reason

    async def test_outcome_is_never_ask(self, tmp_path: Path) -> None:
        # The contract Task 10 relies on: the caller gets a yes or a no and
        # never has to know how to resolve an ask itself.
        for mode in ("default", "acceptEdits", "plan"):
            for tool, args in (
                ("Write", {"file_path": str(tmp_path / "a.py"), "content": "x"}),
                ("Bash", {"command": "ls"}),
                ("Read", {"file_path": str(tmp_path / "a.py")}),
            ):
                decision = await evaluate_tool_request(
                    tool,
                    args,
                    policy=mk_policy(roots=(tmp_path,), ask_tools=frozenset({"Read"})),
                    mode=mode,
                    sink=None,
                    approval_channel=None,
                    timeout_s=WAIT_S,
                )
                assert decision.outcome in ("allow", "deny"), (mode, tool)

    async def test_a_raising_policy_denies_instead_of_exploding(
        self, tmp_path: Path
    ) -> None:
        class ExplodingPolicy:
            """`decide` reads `deny_tools` first, so this raises inside it.

            A callback that propagates an exception takes the turn down with
            it: the SDK has no tool result to report and the WS sees a dead
            stream instead of a refusal.
            """

            name = "exploding"

            @property
            def deny_tools(self) -> tuple[str, ...]:
                raise RuntimeError("policy table is corrupt")

        decision = await evaluate_tool_request(
            "Write",
            {"file_path": str(tmp_path / "a.py")},
            policy=ExplodingPolicy(),  # type: ignore[arg-type]
            mode="default",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )

        assert decision.outcome == "deny"
        assert "policy table is corrupt" in decision.reason

    async def test_a_headless_fleet_coder_can_still_edit_in_its_roots(
        self, tmp_path: Path
    ) -> None:
        # Regression guard for the headless deny: a fleet step runs in a child
        # process with no approval channel, and its allowance comes from the
        # role policy plus acceptEdits — not from any headless fallback. If
        # this ever denies, the fleet stops being able to write code.
        decision = await evaluate_tool_request(
            "Write",
            {"file_path": str(tmp_path / "src" / "main.py"), "content": "print(1)"},
            policy=mk_policy(roots=(tmp_path,), name="coder"),
            mode="acceptEdits",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )

        assert decision.outcome == "allow"

    @pytest.mark.parametrize("role", ["coder", "tester", "reviewer"])
    @pytest.mark.parametrize("tool", ["Bash", "BashOutput", "KillBash"])
    async def test_branch9_every_exec_role_can_run_commands_headless(
        self, role: str, tool: str, tmp_path: Path
    ) -> None:
        # Moved from test_permissions.py (controller Ruling 22): the previous
        # round pinned this premise against `policy_for_role` alone, never
        # against the real headless path. The grant now lives in
        # `evaluate_tool_request`'s no-channel branch, not in `decide`'s
        # acceptEdits branch, so this exercises that live path directly — a
        # fleet step really does call with `approval_channel=None`.
        policy = policy_for_role(role, roots=(tmp_path,), denied=())
        decision = await evaluate_tool_request(
            tool,
            {"command": "pytest -q"},
            policy=policy,
            mode="acceptEdits",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )
        assert decision.outcome == "allow", f"{role}/{tool}: {decision}"

    async def test_headless_exec_still_denies_a_role_without_exec(
        self, tmp_path: Path
    ) -> None:
        # The exec concession is scoped to `policy.exec_allowed`; a headless
        # `ask` for anything else — including exec a role does not have —
        # still denies rather than becoming a second silent allow.
        policy = policy_for_role("planner", roots=(tmp_path,), denied=())
        decision = await evaluate_tool_request(
            "Bash",
            {"command": "ls"},
            policy=policy,
            mode="acceptEdits",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )
        assert decision.outcome == "deny"
        assert decision.outcome != "allow"

    async def test_headless_ask_for_a_non_exec_tool_still_denies(
        self, tmp_path: Path
    ) -> None:
        # The exec concession must not widen into a general headless allow:
        # a role-gated ask_tools entry with no channel still refuses.
        policy = mk_policy(roots=(tmp_path,), ask_tools=frozenset({"Read"}))
        decision = await evaluate_tool_request(
            "Read",
            {"file_path": str(tmp_path / "a.py")},
            policy=policy,
            mode="default",
            sink=None,
            approval_channel=None,
            timeout_s=WAIT_S,
        )
        assert decision.outcome == "deny"
        assert "no operator" in decision.reason


# ── answer routing: many gates, one channel ──────────────────────────────


class TestApprovalRouting:
    """A turn can have several gates open at once.

    claude-agent-sdk handles each permission request in its own task
    (``_internal/query.py``: ``_spawn_control_request_handler``), so two
    parallel tool calls open two gates on one approval channel. Before the
    router, each waiter read the channel itself and discarded any message whose
    id was not its own — so gate B destroyed gate A's answer and A waited out
    its whole timeout for a decision the user had already made.
    """

    async def test_two_open_gates_answered_out_of_order_both_resolve(
        self, tmp_path: Path
    ) -> None:
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        gate_a = open_approval_gate(channel, "approval.tool.a")
        gate_b = open_approval_gate(channel, "approval.tool.b")

        a = asyncio.create_task(gate_a.answer(WAIT_S))
        b = asyncio.create_task(gate_b.answer(WAIT_S))
        # Answered B first, then A: the out-of-order case is the one that used
        # to lose an answer.
        await channel.put({"id": "approval.tool.b", "value": "no", "feedback": "nope"})
        await channel.put({"id": "approval.tool.a", "value": "yes"})

        try:
            answer_a = await asyncio.wait_for(a, WAIT_S)
            answer_b = await asyncio.wait_for(b, WAIT_S)
        finally:
            gate_a.close()
            gate_b.close()

        assert answer_a == {"id": "approval.tool.a", "value": "yes", "feedback": None}
        assert answer_b == {"id": "approval.tool.b", "value": "no", "feedback": "nope"}

    async def test_two_tool_gates_through_the_core_both_resolve(
        self, tmp_path: Path
    ) -> None:
        # The same thing end-to-end: two concurrent `evaluate_tool_request`
        # calls, as two parallel tool calls would produce.
        sink = EventSink()
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def ask(tool: str, path: str) -> asyncio.Task[Any]:
            return asyncio.create_task(
                evaluate_tool_request(
                    tool,
                    {"file_path": path, "content": "x"},
                    policy=mk_policy(roots=(tmp_path,)),
                    mode="default",
                    sink=sink,
                    approval_channel=channel,
                    timeout_s=WAIT_S,
                )
            )

        first = ask("Write", str(tmp_path / "one.py"))
        second = ask("Edit", str(tmp_path / "two.py"))
        cards = [await asyncio.wait_for(sink.get(), WAIT_S) for _ in range(2)]
        by_tool = {c.data["tool"]: c.data["id"] for c in cards if c is not None}
        assert set(by_tool) == {"Write", "Edit"}

        # Answer the second card first.
        await channel.put({"id": by_tool["Edit"], "value": "yes"})
        await channel.put({"id": by_tool["Write"], "value": "no", "feedback": "not that one"})

        assert (await asyncio.wait_for(second, WAIT_S)).outcome == "allow"
        denied = await asyncio.wait_for(first, WAIT_S)
        assert denied.outcome == "deny"
        assert "not that one" in denied.reason

    async def test_an_answer_for_no_open_gate_is_dropped_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        gate = open_approval_gate(channel, "approval.tool.real")
        task = asyncio.create_task(gate.answer(WAIT_S))
        try:
            with caplog.at_level(logging.INFO, logger="backend.app.orchestrator.approvals"):
                await channel.put({"id": "approval.tool.ghost", "value": "yes"})
                await channel.put({"id": "approval.tool.real", "value": "yes"})
                answer = await asyncio.wait_for(task, WAIT_S)
        finally:
            gate.close()

        # Dropped deliberately — not left in the queue where it would satisfy
        # whatever gate opens next.
        assert answer["value"] == "yes"
        assert any(
            "matches no open gate" in r.getMessage() and "ghost" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_timeout_on_one_gate_leaves_another_waiting(self) -> None:
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        short = open_approval_gate(channel, "approval.tool.short")
        long = open_approval_gate(channel, "approval.tool.long")
        long_task = asyncio.create_task(long.answer(WAIT_S))

        timed_out = await asyncio.wait_for(short.answer(0.0), WAIT_S)
        short.close()
        assert timed_out["value"] == "timeout"

        # The surviving gate still gets its answer: the timed-out gate took
        # neither the reader nor the message with it.
        await channel.put({"id": "approval.tool.long", "value": "yes"})
        try:
            assert (await asyncio.wait_for(long_task, WAIT_S))["value"] == "yes"
        finally:
            long.close()

    async def test_an_answer_with_no_id_reaches_the_only_open_gate(self) -> None:
        # Backwards compatibility with a client that omits the id. Attributable
        # only while exactly one gate is open.
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        gate = open_approval_gate(channel, "approval.tool.only")
        task = asyncio.create_task(gate.answer(WAIT_S))
        try:
            await channel.put({"value": "yes"})
            assert (await asyncio.wait_for(task, WAIT_S))["value"] == "yes"
        finally:
            gate.close()

    async def test_a_reused_approval_id_is_refused(self) -> None:
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        gate = open_approval_gate(channel, "approval.tool.dup")
        try:
            with pytest.raises(ValueError):
                open_approval_gate(channel, "approval.tool.dup")
        finally:
            gate.close()

    async def test_the_channel_reader_stops_when_the_last_gate_closes(self) -> None:
        # Otherwise every turn leaves a task parked on its queue forever.
        channel: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        before = len(asyncio.all_tasks())
        gate = open_approval_gate(channel, "approval.tool.solo")
        assert len(asyncio.all_tasks()) == before + 1
        gate.close()
        # Poll rather than assume one loop iteration is enough to retire a
        # cancelled task — how many it takes is not part of the contract.
        for _ in range(100):
            if len(asyncio.all_tasks()) == before:
                break
            await asyncio.sleep(0)

        assert len(asyncio.all_tasks()) == before


# ── claude.py wiring ─────────────────────────────────────────────────────


def _fake_result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=12,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id="upstream-1",
        total_cost_usd=0.25,
        result="done",
    )


class TestClaudeProviderWiring:
    async def test_run_builds_a_callback_and_does_not_escalate_the_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_query(*, prompt: str, options: Any):
            captured["prompt"] = prompt
            captured["options"] = options
            yield _fake_result_message()

        monkeypatch.setattr(claude_mod, "query", fake_query)

        events = [
            ev
            async for ev in claude_mod.ClaudeProvider().run(
                RunContext(model="m", prompt="hello", cwd=str(tmp_path))
            )
        ]

        assert [ev.type for ev in events] == ["assistant.done"]
        assert events[0].data["upstream_session_id"] == "upstream-1"
        options = captured["options"]
        # None → "default" (ask), never "acceptEdits": that escalation is the
        # defect this task removes.
        assert options.permission_mode == "default"
        assert options.can_use_tool is not None

    async def test_run_unions_policy_denials_with_the_extras_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_query(*, prompt: str, options: Any):
            captured["options"] = options
            yield _fake_result_message()

        monkeypatch.setattr(claude_mod, "query", fake_query)

        events = [
            ev
            async for ev in claude_mod.ClaudeProvider().run(
                RunContext(
                    model="m",
                    prompt="hello",
                    cwd=str(tmp_path),
                    role="planner",
                    extras={"claude_disallowed_tools": ["WebFetch"]},
                )
            )
        ]

        assert [ev.type for ev in events] == ["assistant.done"]
        disallowed = captured["options"].disallowed_tools
        # Both sources survive: dropping either re-grants a tool someone
        # deliberately took away.
        assert "WebFetch" in disallowed
        assert "Write" in disallowed  # from the planner role policy
        assert len(disallowed) == len(set(disallowed))

    async def test_a_raising_query_still_yields_an_error_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings
    ) -> None:
        async def boom(*, prompt: str, options: Any):
            raise RuntimeError("cli vanished")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(claude_mod, "query", boom)

        events = [
            ev
            async for ev in claude_mod.ClaudeProvider().run(
                RunContext(model="m", prompt="hello", cwd=str(tmp_path))
            )
        ]

        assert [ev.type for ev in events] == ["error"]
        assert "cli vanished" in events[0].data["message"]

    async def test_callback_events_reach_the_consumer_while_the_turn_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_settings
    ) -> None:
        """The reason run() is a two-producer merge.

        The permission callback emits its card from inside the SDK's message
        loop and then blocks there. A single-iterator run() would hold that
        card in a frame nobody drains, so the user would be asked a question
        they never see.
        """
        approval_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def fake_query(*, prompt: str, options: Any):
            # Stand in for the CLI asking for permission mid-stream.
            result = await options.can_use_tool(
                "Write",
                {"file_path": str(tmp_path / "a.py"), "content": "x"},
                ctx(),
            )
            assert isinstance(result, PermissionResultAllow)
            yield _fake_result_message()

        monkeypatch.setattr(claude_mod, "query", fake_query)

        seen: list[str] = []
        run = claude_mod.ClaudeProvider().run(
            RunContext(
                model="m",
                prompt="hello",
                cwd=str(tmp_path),
                approval_channel=approval_q,
            )
        )
        agen = run.__aiter__()
        card = await asyncio.wait_for(agen.__anext__(), WAIT_S)
        seen.append(card.type)
        assert card.type == "pipeline.awaiting_approval"

        await approval_q.put({"id": card.data["id"], "value": "yes"})
        async for ev in agen:
            seen.append(ev.type)

        # The card comes out first — that is the whole point; the two events
        # after it are not ordered relative to each other (the sink pump and
        # the message pump are separate tasks) and nothing depends on which
        # lands first, so asserting an order here would only add a flake.
        assert seen[0] == "pipeline.awaiting_approval"
        assert sorted(seen[1:]) == ["assistant.done", "pipeline.approval_received"]


# ── denied_cwd_paths at the API boundary ─────────────────────────────────


class TestDeniedCwdAtTheApiBoundary:
    """`~` is an allowed root, so without this check a session could be rooted
    inside a credential store (`~/.ssh`, `~/.claude`) and every tool the
    spawned CLI runs would start there."""

    @pytest.fixture(autouse=True)
    def _no_ambient_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate from whatever the developer's shell (or CI) exports for these
        # names; the defaults are what these assertions are about.
        for var in ("ALLOWED_CWD_ROOTS", "DENIED_CWD_PATHS"):
            monkeypatch.delenv(var, raising=False)

    def test_a_denied_root_is_rejected_with_400_naming_it(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        with pytest.raises(HTTPException) as excinfo:
            _validate_cwd(str(tmp_localcode / ".ssh"))

        assert excinfo.value.status_code == 400
        assert ".ssh" in str(excinfo.value.detail)
        assert "denied" in str(excinfo.value.detail)

    def test_a_path_under_a_denied_root_is_rejected(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        with pytest.raises(HTTPException) as excinfo:
            _validate_cwd(str(tmp_localcode / ".claude" / "projects" / "x"))

        assert excinfo.value.status_code == 400

    def test_additional_dirs_are_rejected_too(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        from backend.app.routes.sessions import _validate_additional_dirs

        ok = str(tmp_localcode / "proj")
        with pytest.raises(HTTPException):
            _validate_additional_dirs([ok, str(tmp_localcode / ".aws")])

    def test_an_ordinary_project_dir_is_still_accepted(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        project = tmp_localcode / "proj"
        project.mkdir()

        assert _validate_cwd(str(project)) == str(project.resolve())

    def test_denied_wins_over_a_permissive_empty_allowlist(
        self, tmp_localcode: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty allowlist is the documented "single-user dev mode" escape
        # hatch; it must not also open the credential stores.
        monkeypatch.setenv("ALLOWED_CWD_ROOTS", "")

        with pytest.raises(HTTPException):
            _validate_cwd(str(tmp_localcode / ".ssh"))
