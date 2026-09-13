"""A sub-agent's role is a limit the runtime applies, not a paragraph.

Task 2 built the role policy table and Task 3 wired a ``can_use_tool`` callback
into the Claude provider — and ``RunContext.role`` was still set by nobody, so
every fleet sub-agent reached ``policy_for_role(None, ...)`` and ran under the
permissive "session" policy. The table existed, was tested, and enforced
nothing. These tests are about the wiring that closes that gap, and about the
two ways it could be re-opened:

  * **A mode is not an override.** ``bypassPermissions`` says "stop asking the
    human", not "ignore what this role may touch". A reviewer's ``Write`` is
    refused in every mode, including that one.
  * **Extras narrow, never widen.** ``ctx.extras`` used to *replace* the
    allowed-tools list the provider sent, so any caller that put a tool in
    ``claude_allowed_tools`` granted itself that tool whatever the role said.

The planner's grants get their own test, character for character: the planner
was the one role with a hand-written extras dict before this task, and the
whole point of deleting that dict is that the table says exactly the same
thing. A table that says *nearly* the same thing is a silent permission change.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.app.orchestrator import claude as claude_mod
from backend.app.orchestrator import registry as registry_mod
from backend.app.orchestrator.approvals import build_can_use_tool
from backend.app.orchestrator.base import Event, RunContext
from backend.app.orchestrator.claude import _narrowed_allowed_tools
from backend.app.orchestrator.fleet import subproc as subproc_mod
from backend.app.orchestrator.fleet.collect import collect_step
from backend.app.orchestrator.fleet.models import RoleConfig
from backend.app.orchestrator.permissions import (
    EXEC_TOOLS,
    VALID_PERMISSION_MODES,
    WRITE_TOOLS,
    ToolPolicy,
    decide,
    policy_extras,
    policy_for_role,
)
from backend.tests.fakes.claude_client import FakeClientFactory

ROLE = RoleConfig(provider="claude", model="sonnet", system_prompt="be terse")

ALL_MODES = sorted(VALID_PERMISSION_MODES)


class _StubProvider:
    """A sub-provider that records the ``RunContext`` it was handed."""

    def __init__(self) -> None:
        self.ctx: RunContext | None = None
        self.closed = False

    async def run(self, ctx):  # noqa: ANN001 - mirrors the Provider protocol
        self.ctx = ctx
        yield Event(type="assistant.text", data={"text": "ok"})

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubProvider:
    """``collect_step`` imports ``_build_provider`` from the registry at call
    time, so patching the module attribute is enough."""
    provider = _StubProvider()
    monkeypatch.setattr(registry_mod, "_build_provider", lambda name: provider)
    return provider


# ── the mode is not an override ───────────────────────────────────────────


class TestReadOnlyRolesSurviveEveryMode:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_reviewer_write_is_denied_in_every_mode(self, mode: str, tmp_path: Path) -> None:
        # bypassPermissions in particular: a permission mode is a
        # human-in-the-loop preference, and if it were checked before the role
        # gates an operator who turned it on would hand every read-only role
        # write access to the whole repo.
        policy = policy_for_role("reviewer", roots=(tmp_path,), denied=())
        result = decide("Write", {"file_path": str(tmp_path / "f.py")}, policy, mode=mode)
        assert result.outcome == "deny", f"{mode}: {result}"
        assert "reviewer" in result.reason

    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_planner_write_is_denied_in_every_mode(self, mode: str, tmp_path: Path) -> None:
        policy = policy_for_role("planner", roots=(tmp_path,), denied=())
        result = decide("Edit", {"file_path": str(tmp_path / "f.py")}, policy, mode=mode)
        assert result.outcome == "deny", f"{mode}: {result}"

    async def test_the_callback_refuses_the_reviewer_even_under_bypass(
        self, tmp_path: Path
    ) -> None:
        # Enforced twice on purpose. `disallowed_tools` stops the tool being
        # offered; this is the other half — if a settings file or a future SDK
        # default offers `Write` anyway, the gate the provider installs still
        # says no, with a reason the model can read.
        gate = build_can_use_tool(
            policy=policy_for_role("reviewer", roots=(tmp_path,), denied=()),
            mode="bypassPermissions",
            sink=None,
            approval_channel=None,
            timeout_s=1.0,
        )
        result = await gate("Write", {"file_path": str(tmp_path / "f.py")}, None)
        assert type(result).__name__ == "PermissionResultDeny"
        assert "reviewer" in result.message


class TestCoderIsBoundedByItsRoots:
    def test_write_inside_a_root_is_not_refused(self, tmp_path: Path) -> None:
        policy = policy_for_role("coder", roots=(tmp_path,), denied=())
        result = decide(
            "Write", {"file_path": str(tmp_path / "src" / "a.py")}, policy, mode="acceptEdits"
        )
        assert result.outcome == "allow"

    def test_write_outside_every_root_is_denied(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "a.py"
        policy = policy_for_role("coder", roots=(root,), denied=())
        result = decide("Write", {"file_path": str(outside)}, policy, mode="bypassPermissions")
        assert result.outcome == "deny"
        assert "outside all allowed roots" in result.reason

    def test_a_denied_subdirectory_beats_an_allowed_root(self, tmp_path: Path) -> None:
        # The .ssh-inside-home case: a path under both an allowed root and a
        # denied directory is refused, or "roots" would silently re-grant every
        # secret the deny list exists to protect.
        secret = tmp_path / ".ssh"
        secret.mkdir()
        policy = policy_for_role("coder", roots=(tmp_path,), denied=(secret,))
        result = decide("Write", {"file_path": str(secret / "id_rsa")}, policy, mode="default")
        assert result.outcome == "deny"
        assert "denied directory" in result.reason


# ── the wiring: a fleet step runs as its role ─────────────────────────────


class TestCollectStepSetsTheRole:
    async def test_the_planner_step_runs_as_the_planner(
        self, stub: _StubProvider, tmp_path: Path
    ) -> None:
        await collect_step(ROLE, "plan it", str(tmp_path), role_name="planner")

        assert stub.ctx is not None
        assert stub.ctx.role == "planner"
        disallowed = stub.ctx.extras["claude_disallowed_tools"]
        for tool in ("Edit", "Write", "Bash"):
            assert tool in disallowed

    async def test_the_reviewer_step_is_read_only_without_a_hand_written_dict(
        self, stub: _StubProvider, tmp_path: Path
    ) -> None:
        # Before Task 9 only the planner had extras at all, so a reviewer was
        # handed the full tool catalogue and the "read-only" guarantee was a
        # sentence in the orchestrator's system prompt.
        await collect_step(ROLE, "review it", str(tmp_path), role_name="reviewer")

        assert stub.ctx is not None
        assert stub.ctx.role == "reviewer"
        assert "Write" in stub.ctx.extras["claude_disallowed_tools"]
        assert stub.ctx.extras["claude_disable_settings"] is True

    async def test_an_unknown_role_still_gets_no_extras(
        self, stub: _StubProvider, tmp_path: Path
    ) -> None:
        # The session policy is unrestricted by design; rendering extras for it
        # would quietly pin a tool list onto every non-fleet caller.
        await collect_step(ROLE, "go", str(tmp_path), role_name=None)

        assert stub.ctx is not None
        assert stub.ctx.extras == {}

    async def test_the_role_reaches_the_context_built_inside_a_worker(
        self, stub: _StubProvider, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The pool hosts `collect_step` in a child process, so the sub-context
        # is built there, from the request's `role_name`. If that hop dropped
        # the role, every real fleet step would run unroled while the in-process
        # tests above stayed green.
        written: list[bytes] = []
        monkeypatch.setattr(subproc_mod, "_write", written.append)

        await subproc_mod.handle_request(
            json.dumps(
                {
                    "id": "r1",
                    "provider": "claude",
                    "model": "sonnet",
                    "prompt": "review it",
                    "cwd": str(tmp_path),
                    "role_name": "reviewer",
                }
            )
        )

        assert stub.ctx is not None
        assert stub.ctx.role == "reviewer"
        # The worker's stdout is markers plus one JSON body; the body is the
        # only line that starts with "{".
        bodies = [
            line for line in b"".join(written).split(b"\n") if line.startswith(b"{")
        ]
        assert json.loads(bodies[0])["ok"] is True


# ── the planner's grants did not change ───────────────────────────────────


class TestPlannerGrantsAreUnchanged:
    def test_the_table_renders_exactly_the_dict_it_replaced(self) -> None:
        # Verbatim from the `_role_extras` dict Task 9 deleted from
        # fleet/collect.py. The planner produces a plan artifact: implementing
        # belongs to the coder and reviewing to the reviewer, and every entry
        # below is one of the ways it could do those instead.
        legacy = {
            "claude_allowed_tools": ["Read", "Glob", "Grep", "LS"],
            "claude_disable_settings": True,
            "claude_disable_skills": True,
            "claude_disallowed_tools": [
                "Edit",
                "Write",
                "MultiEdit",
                "NotebookEdit",
                "Bash",
                "BashOutput",
                "KillBash",
                "Agent",
                "Task",
                "Skill",
                "ToolSearch",
                "Monitor",
                "RemoteTrigger",
                "TaskStop",
            ],
        }
        extras = policy_extras(policy_for_role("planner", roots=(), denied=()))

        assert extras["claude_allowed_tools"] == legacy["claude_allowed_tools"]
        assert set(extras["claude_disallowed_tools"]) == set(legacy["claude_disallowed_tools"])
        assert extras["claude_disable_settings"] is True
        assert extras["claude_disable_skills"] is True


# ── extras narrow, never widen ────────────────────────────────────────────


async def _drain(ctx: RunContext) -> Any:
    """Run one turn against a fake client and return the options it was built
    with — what the spawned CLI would actually have been told. Asserting on the
    options rather than on a helper's return value is the point: this is the
    boundary where a widened permission would leave the process."""
    factory = FakeClientFactory()
    provider = claude_mod.ClaudeProvider()
    provider._factory = factory
    try:
        async for _ in provider.run(ctx):
            pass
    finally:
        await provider.aclose()
    assert factory.clients, "no client was built"
    return factory.clients[0].options


def a_ctx(tmp_path: Path, **overrides: Any) -> RunContext:
    base: dict[str, Any] = dict(model="m", prompt="hi", cwd=str(tmp_path))
    base.update(overrides)
    return RunContext(**base)


class TestExtrasCannotWiden:
    async def test_a_reviewer_asking_for_write_does_not_get_it(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # The failure this prevents: extras used to REPLACE the allowed list,
        # so this context handed the reviewer `Write` — and a whole-tool entry
        # in `allowed_tools` auto-approves before `can_use_tool` is consulted,
        # so the callback would never even have been asked.
        options = await _drain(
            a_ctx(tmp_path, role="reviewer", extras={"claude_allowed_tools": ["Write"]})
        )

        assert "Write" not in (options.allowed_tools or [])
        assert "Write" in options.disallowed_tools

    async def test_extras_intersect_the_roles_allow_list(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        options = await _drain(
            a_ctx(
                tmp_path,
                role="planner",
                extras={"claude_allowed_tools": ["Read", "WebFetch"]},
            )
        )

        # Read survives (both sides name it); WebFetch does not (the planner's
        # allow-list never had it); Glob is gone (the extras narrowed it away).
        assert options.allowed_tools == ["Read"]

    async def test_extras_can_still_add_denials(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        options = await _drain(
            a_ctx(tmp_path, role="coder", extras={"claude_disallowed_tools": ["WebFetch"]})
        )

        assert "WebFetch" in options.disallowed_tools
        assert "Agent" in options.disallowed_tools  # the coder role's own denial
        # And the other direction, which is just as much a bug: narrowing must
        # not stop a coder doing the job it exists for.
        assert "Write" not in options.disallowed_tools
        assert "Bash" not in options.disallowed_tools

    async def test_no_write_or_exec_tool_is_ever_auto_approved(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # A whole-tool `allowed_tools` entry shadows `can_use_tool` entirely,
        # so a coder with `Write` there could write outside its roots without
        # the path check ever running. Writes are gated by the callback, never
        # pre-approved by name — whatever the role, whatever the extras.
        options = await _drain(
            a_ctx(
                tmp_path,
                role="coder",
                extras={"claude_allowed_tools": ["Write", "Bash", "Read"]},
            )
        )

        # Empty, not ["Read"]: the coder role has no allow-list of its own, and
        # extras may not create one (see the next test).
        assert options.allowed_tools == []

    async def test_scoped_rule_syntax_cannot_smuggle_a_write_tool(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # `Write(*)` and `Write()` resolve to a WHOLE-tool allow in the SDK
        # (`types._whole_tool_allowed`), so a bar that matches the raw string
        # filters "Write" and waves "Write(*)" through — the callback is then
        # just as shadowed, by a spelling.
        options = await _drain(
            a_ctx(
                tmp_path,
                role="coder",
                extras={"claude_allowed_tools": ["Write(*)", "Bash(rm -rf:*)"]},
            )
        )

        assert options.allowed_tools == []

    async def test_extras_cannot_create_an_allow_list_for_a_role_without_one(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # Adding "just Read" is not a narrowing: an allow-list entry
        # auto-approves before `can_use_tool` runs, so this would take Read out
        # of the gate's hands entirely — `denied_cwd_paths` included. A role
        # with no allow-list keeps having no allow-list.
        policy = policy_for_role("coder", roots=(tmp_path,), denied=())
        assert _narrowed_allowed_tools(policy, {"claude_allowed_tools": ["Read"]}) is None

        options = await _drain(
            a_ctx(tmp_path, role="coder", extras={"claude_allowed_tools": ["Read"]})
        )
        assert options.allowed_tools == []

    async def test_the_bar_holds_against_a_table_that_names_a_write_tool(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The bar must not depend on the role table being right. A row that
        # allow-listed a write or exec tool — by plain name or scoped — would
        # otherwise hand it over pre-approved.
        rogue = ToolPolicy(
            name="rogue",
            writable=True,
            exec_allowed=True,
            roots=(tmp_path,),
            denied=(),
            allow_tools=("Read", "Write", "Write(*)", "Bash(git diff:*)", "Glob"),
        )
        monkeypatch.setattr(claude_mod, "policy_for_role", lambda *a, **k: rogue)

        options = await _drain(a_ctx(tmp_path, role="rogue"))

        assert options.allowed_tools == ["Read", "Glob"]

    async def test_disallowed_tools_derive_from_the_policy_not_from_the_table(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Under bypassPermissions the callback is never invoked, so
        # `disallowed_tools` is the ONLY enforcement left. A future read-only
        # row whose `deny_tools` forgot a write tool would lose its guarantee in
        # silence; deriving the families from `writable`/`exec_allowed` means
        # the policy's structure and the tools the CLI is offered cannot drift.
        incomplete = ToolPolicy(
            name="forgetful",
            writable=False,
            exec_allowed=False,
            roots=(tmp_path,),
            denied=(),
            deny_tools=(),
        )
        monkeypatch.setattr(claude_mod, "policy_for_role", lambda *a, **k: incomplete)

        options = await _drain(a_ctx(tmp_path, role="forgetful"))

        for tool in WRITE_TOOLS | EXEC_TOOLS:
            assert tool in options.disallowed_tools, tool

    async def test_a_read_only_role_never_loads_settings_or_skills(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # Allow rules in a settings file shadow `can_use_tool` the same way an
        # allow-list entry does, so a read-only role that loaded the user's own
        # settings would be handed back what the policy took away. Derived from
        # the role, not only from extras a caller may forget to pass.
        options = await _drain(a_ctx(tmp_path, role="reviewer"))

        assert options.setting_sources == []
        assert options.skills == []

    async def test_an_unroled_session_keeps_the_vendor_default_surface(
        self, tmp_path: Path, fresh_settings
    ) -> None:
        # Narrowing must not leak into ordinary chat: a session with no role
        # gets no allow-list at all, so `can_use_tool` sees every call.
        #
        # `is None` at the source, not `falsy` at the options layer: the
        # provider sends no `allowed_tools` key at all, and asserting only
        # `not options.allowed_tools` would still pass if a regression started
        # sending an explicit `[]` — which reads as a deliberate empty list to
        # anything that inspects the options.
        session = policy_for_role(None, roots=(tmp_path,), denied=())
        assert _narrowed_allowed_tools(session, {}) is None

        options = await _drain(a_ctx(tmp_path))

        assert options.allowed_tools == []  # the SDK's own default, untouched
        assert options.disallowed_tools == []
