"""Tests for the pure permission-policy layer (`backend/app/orchestrator/permissions.py`).

`decide`'s branch order is security-critical: branches 1-5 (role/allow-list/
write/exec/path gates) run before branch 6 (`bypassPermissions`), so a mode
can never override a role's structural limits. Several rows below exist
specifically to pin that ordering rather than just the individual branches.
"""
from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend.app.config import Settings, get_settings
from backend.app.orchestrator.permissions import (
    Decision,
    ToolPolicy,
    decide,
    extract_paths,
    is_within,
    normalize_permission_mode,
    policy_extras,
    policy_for_role,
    resolve_roots,
)


def mk_policy(**overrides) -> ToolPolicy:
    """Build a ToolPolicy for isolating one `decide` branch at a time,
    independent of the fixed role table (which is tested separately via
    `policy_for_role`)."""
    defaults = dict(
        name="custom",
        writable=True,
        exec_allowed=True,
        roots=(),
        denied=(),
        allow_tools=None,
        deny_tools=(),
    )
    defaults.update(overrides)
    return ToolPolicy(**defaults)


# ── normalize_permission_mode ────────────────────────────────────────────


class TestNormalizePermissionMode:
    @pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan"])
    def test_valid_modes_round_trip(self, mode: str) -> None:
        assert normalize_permission_mode(mode, allow_bypass=False) == mode
        assert normalize_permission_mode(mode, allow_bypass=True) == mode

    def test_none_falls_back_to_default(self) -> None:
        assert normalize_permission_mode(None, allow_bypass=False) == "default"

    def test_trailing_whitespace_is_not_matched(self) -> None:
        assert normalize_permission_mode("acceptEdits ", allow_bypass=False) == "default"

    def test_wrong_case_is_not_matched(self) -> None:
        assert normalize_permission_mode("AcceptEdits", allow_bypass=False) == "default"

    def test_bypass_denied_without_the_flag(self) -> None:
        assert normalize_permission_mode("bypassPermissions", allow_bypass=False) == "default"

    def test_bypass_allowed_with_the_flag(self) -> None:
        result = normalize_permission_mode("bypassPermissions", allow_bypass=True)
        assert result == "bypassPermissions"

    def test_unknown_value_never_escalates_to_accept_edits(self) -> None:
        # The defect this module removes: an unrecognized mode must never
        # silently become the mode that skips write approval.
        assert normalize_permission_mode("bogus-mode", allow_bypass=True) == "default"

    def test_unknown_value_logs_warning_naming_value_and_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            normalize_permission_mode("bogus-mode", allow_bypass=False)
        assert any(
            "bogus-mode" in rec.message and "default" in rec.message for rec in caplog.records
        )

    def test_denied_bypass_logs_warning_naming_the_setting(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            normalize_permission_mode("bypassPermissions", allow_bypass=False)
        assert any("allow_bypass_permissions" in rec.message for rec in caplog.records)


# ── is_within ─────────────────────────────────────────────────────────────


class TestIsWithin:
    def test_inside(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "sub" / "file.txt"
        assert is_within(target, [root]) is True

    def test_equal_to_root(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        assert is_within(root, [root]) is True

    def test_outside(self, tmp_path) -> None:
        root = tmp_path / "root"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        assert is_within(other / "file.txt", [root]) is False

    def test_parent_of_root_is_not_within(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        # tmp_path is a parent of root, not a descendant — must not match.
        assert is_within(tmp_path, [root]) is False

    def test_symlink_escaping_root_is_caught(self, tmp_path) -> None:
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")
        escape = root / "escape"
        escape.symlink_to(outside)
        # escape/secret.txt LOOKS like it's under root, but resolve()
        # follows the symlink out to `outside` — must be rejected.
        assert is_within(escape / "secret.txt", [root]) is False

    def test_relative_path_resolves_against_cwd(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "file.txt").write_text("x")
        assert is_within(Path("file.txt"), [tmp_path]) is True


# ── decide ────────────────────────────────────────────────────────────────

DENY = "deny"
ALLOW = "allow"
ASK = "ask"

DECIDE_ROWS = [
    # (id, policy_kwargs, tool_name, tool_input, mode, expected_outcome, reason_substr)
    (
        "branch1-explicit-deny-tool",
        dict(deny_tools=("Write",)),
        "Write",
        {},
        "default",
        DENY,
        "may not use Write",
    ),
    (
        "branch1-reviewer-denies-write-via-role-table",
        "reviewer",
        "Write",
        {},
        "default",
        DENY,
        "reviewer",
    ),
    (
        "branch2-allow-list-exhaustive",
        dict(allow_tools=("Read",)),
        "Grep",
        {},
        "default",
        DENY,
        "does not allow Grep",
    ),
    (
        "branch2-planner-denies-unlisted-network-tool",
        "planner",
        "WebFetch",
        {},
        "default",
        DENY,
        "planner",
    ),
    (
        "branch3-write-tool-on-read-only-policy",
        dict(writable=False),
        "Edit",
        {},
        "default",
        DENY,
        "read-only",
    ),
    (
        "branch4-exec-tool-when-exec-not-allowed",
        dict(exec_allowed=False),
        "Bash",
        {},
        "default",
        DENY,
        "may not execute",
    ),
    (
        "branch6-bypass-does-not-override-role-deny",
        "planner",
        "Write",
        {},
        "bypassPermissions",
        DENY,
        "planner",
    ),
    (
        "branch7-ask-tool-outranks-acceptEdits",
        dict(ask_tools=frozenset({"Write"})),
        "Write",
        {},
        "acceptEdits",
        ASK,
        "always asks",
    ),
    (
        "branch8-plan-mode-denies-write",
        {},
        "Edit",
        {},
        "plan",
        DENY,
        "plan mode",
    ),
    (
        "branch8-plan-mode-denies-exec",
        {},
        "Bash",
        {},
        "plan",
        DENY,
        "plan mode",
    ),
    (
        "branch9-acceptEdits-allows-write",
        {},
        "Write",
        {},
        "acceptEdits",
        ALLOW,
        "acceptEdits",
    ),
    (
        # acceptEdits auto-approves writes only, never exec — see the comment
        # on branch 9. An interactive session's default mode is acceptEdits,
        # and `ctx.role` is unset everywhere, so widening this to exec would
        # silently auto-approve every shell command with no card.
        "branch9-acceptEdits-does-not-allow-exec",
        {},
        "Bash",
        {},
        "acceptEdits",
        ASK,
        "confirmation",
    ),
    (
        "branch10-default-mode-asks-for-write",
        {},
        "Write",
        {},
        "default",
        ASK,
        "confirmation",
    ),
    (
        "branch10-default-mode-asks-for-exec",
        {},
        "Bash",
        {},
        "default",
        ASK,
        "confirmation",
    ),
    (
        "branch11-read-tool-always-allowed",
        {},
        "Read",
        {},
        "default",
        ALLOW,
        "no restriction",
    ),
    (
        "branch11-network-tool-allowed-in-plan-mode",
        {},
        "WebFetch",
        {},
        "plan",
        ALLOW,
        "no restriction",
    ),
]


class TestDecideTable:
    @pytest.mark.parametrize(
        "case_id,policy_arg,tool_name,tool_input,mode,expected_outcome,reason_substr",
        DECIDE_ROWS,
        ids=[row[0] for row in DECIDE_ROWS],
    )
    def test_row(
        self, case_id, policy_arg, tool_name, tool_input, mode, expected_outcome, reason_substr
    ) -> None:
        if isinstance(policy_arg, str):
            policy = policy_for_role(policy_arg, roots=(), denied=())
        else:
            policy = mk_policy(**policy_arg)
        result = decide(tool_name, tool_input, policy, mode=mode)
        assert result.outcome == expected_outcome, f"{case_id}: {result}"
        assert reason_substr in result.reason, f"{case_id}: {result.reason!r}"

    def test_branch5_path_outside_roots_denies(self, tmp_path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        policy = mk_policy(roots=(proj,))
        result = decide(
            "Write",
            {"file_path": str(outside / "f.txt")},
            policy,
            mode="acceptEdits",
        )
        assert result.outcome == "deny"
        assert "outside all allowed roots" in result.reason

    def test_branch5_denied_directory_wins_over_root(self, tmp_path) -> None:
        secret = tmp_path / "secret"
        secret.mkdir()
        policy = mk_policy(roots=(tmp_path,), denied=(secret,))
        result = decide(
            "Write",
            {"file_path": str(secret / "f.txt")},
            policy,
            mode="acceptEdits",
        )
        assert result.outcome == "deny"
        assert "denied directory" in result.reason

    def test_branch5_multiedit_checks_every_edit_path(self, tmp_path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        policy = mk_policy(roots=(proj,))
        tool_input = {
            "edits": [
                {"file_path": str(proj / "a.txt")},
                {"file_path": str(outside / "b.txt")},
            ]
        }
        result = decide("MultiEdit", tool_input, policy, mode="acceptEdits")
        assert result.outcome == "deny"
        assert "outside all allowed roots" in result.reason

    def test_branch5_does_not_apply_to_bash(self, tmp_path) -> None:
        # Bash's tool_input carries a shell command, not a path. Even a
        # path-shaped `path` key must be ignored — extract_paths returns []
        # for Bash — so this falls through to branch 10 (ask), not deny.
        proj = tmp_path / "proj"
        proj.mkdir()
        policy = mk_policy(roots=(proj,), exec_allowed=True)
        result = decide(
            "Bash",
            {"command": "rm -rf /", "path": "/etc"},
            policy,
            mode="default",
        )
        assert result.outcome == "ask"

    def test_branch6_bypass_allows_for_unrestricted_policy(self) -> None:
        policy = mk_policy()
        result = decide("Bash", {}, policy, mode="bypassPermissions")
        assert result.outcome == "allow"
        assert "bypassPermissions" in result.reason

    @pytest.mark.parametrize(
        "mode", ["default", "acceptEdits", "plan", "bypassPermissions"]
    )
    def test_exec_stays_denied_for_a_role_without_exec_in_every_mode(
        self, mode: str
    ) -> None:
        # The acceptEdits exec allowance is the role's grant, not the mode's:
        # branches 1-5 run first, so no mode — bypassPermissions included —
        # hands exec to a role that does not have it.
        policy = mk_policy(exec_allowed=False)
        result = decide("Bash", {"command": "ls"}, policy, mode=mode)
        assert result.outcome == "deny", f"{mode}: {result}"
        assert "may not execute" in result.reason

    @pytest.mark.parametrize("role", ["planner", "developer"])
    def test_read_only_roles_still_cannot_exec_under_accept_edits(
        self, role: str, tmp_path
    ) -> None:
        policy = policy_for_role(role, roots=(tmp_path,), denied=())
        assert decide("Bash", {"command": "ls"}, policy, mode="acceptEdits").outcome == (
            "deny"
        )

    def test_branch10_reason_names_the_live_mode(self) -> None:
        # `decide` takes the mode as a plain string and does not assume it was
        # normalized, so the reason must interpolate it. It used to say
        # "default mode requires confirmation" whatever the mode actually was,
        # which points whoever reads the audit line at the wrong setting.
        result = decide("Write", {}, mk_policy(), mode="dontAsk")
        assert result.outcome == "ask"
        assert result.reason.startswith("dontAsk mode requires confirmation")
        assert "Write" in result.reason

    def test_a_policy_with_no_roots_says_the_session_has_no_cwd(self, tmp_path) -> None:
        # "outside all allowed roots []" reads as a bug in the gate; the real
        # cause is a session created without a working directory.
        result = decide(
            "Read", {"file_path": str(tmp_path / "f.py")}, mk_policy(roots=()), mode="default"
        )
        assert result.outcome == "deny"
        assert "no working directory" in result.reason


# ── policy_for_role ───────────────────────────────────────────────────────


class TestPolicyForRole:
    def test_reviewer_denies_write_permits_bash(self) -> None:
        policy = policy_for_role("reviewer", roots=(), denied=())
        assert "Write" in policy.deny_tools
        assert policy.exec_allowed is True
        assert decide("Bash", {}, policy, mode="default").outcome == "ask"
        assert decide("Write", {}, policy, mode="acceptEdits").outcome == "deny"

    def test_planner_denies_bash(self) -> None:
        policy = policy_for_role("planner", roots=(), denied=())
        assert "Bash" in policy.deny_tools
        assert policy.exec_allowed is False

    def test_orchestrator_allow_tools_is_empty(self) -> None:
        policy = policy_for_role("orchestrator", roots=(), denied=())
        assert policy.allow_tools == ()

    def test_unknown_role_gets_default_session_policy(self) -> None:
        policy = policy_for_role("not-a-real-role", roots=(), denied=())
        assert policy.name == "session"
        assert policy.allow_tools is None
        assert policy.deny_tools == ()

    def test_none_role_gets_default_session_policy(self) -> None:
        policy = policy_for_role(None, roots=(), denied=())
        assert policy.name == "session"

    def test_writable_default_controls_session_policy(self) -> None:
        writable = policy_for_role("unknown", roots=(), denied=(), writable_default=True)
        readonly = policy_for_role("unknown", roots=(), denied=(), writable_default=False)
        assert writable.writable is True
        assert readonly.writable is False

    def test_tester_and_coder_are_fully_writable_and_executable(self) -> None:
        for role in ("tester", "coder"):
            policy = policy_for_role(role, roots=(), denied=())
            assert policy.writable is True
            assert policy.exec_allowed is True
            assert "Agent" in policy.deny_tools and "Task" in policy.deny_tools

    def test_developer_is_read_only_with_a_narrow_allow_list(self) -> None:
        policy = policy_for_role("developer", roots=(), denied=())
        assert policy.writable is False
        assert policy.exec_allowed is False
        assert policy.allow_tools == ("Read", "Glob", "Grep", "LS")


# ── policy_extras ─────────────────────────────────────────────────────────


class TestPolicyExtras:
    def test_read_only_policy_disables_settings_and_skills(self) -> None:
        policy = policy_for_role("planner", roots=(), denied=())
        extras = policy_extras(policy)
        assert extras["claude_disable_settings"] is True
        assert extras["claude_disable_skills"] is True

    def test_writable_policy_does_not_set_allowed_tools(self) -> None:
        policy = policy_for_role("coder", roots=(), denied=())
        extras = policy_extras(policy)
        assert "claude_allowed_tools" not in extras
        assert "claude_disable_settings" not in extras
        assert "claude_disable_skills" not in extras

    def test_allow_tools_renders_when_present(self) -> None:
        policy = policy_for_role("planner", roots=(), denied=())
        extras = policy_extras(policy)
        assert extras["claude_allowed_tools"] == list(policy.allow_tools)

    def test_deny_tools_renders_when_present(self) -> None:
        policy = policy_for_role("reviewer", roots=(), denied=())
        extras = policy_extras(policy)
        assert set(extras["claude_disallowed_tools"]) == set(policy.deny_tools)

    def test_unrestricted_session_policy_renders_no_extras(self) -> None:
        policy = policy_for_role(None, roots=(), denied=(), writable_default=True)
        assert policy_extras(policy) == {}


# ── extract_paths ─────────────────────────────────────────────────────────


class TestExtractPaths:
    def test_bash_never_yields_a_path(self) -> None:
        assert extract_paths("Bash", {"command": "cat /etc/passwd"}) == []

    def test_edit_reads_file_path(self) -> None:

        assert extract_paths("Edit", {"file_path": "/a/b.py"}) == [Path("/a/b.py")]

    def test_notebook_edit_reads_notebook_path(self) -> None:

        result = extract_paths("NotebookEdit", {"notebook_path": "/a/b.ipynb"})
        assert result == [Path("/a/b.ipynb")]

    def test_multiedit_combines_top_level_and_each_edit(self) -> None:

        tool_input = {
            "file_path": "/a/base.py",
            "edits": [{"file_path": "/a/one.py"}, {"file_path": "/a/two.py"}],
        }
        result = extract_paths("MultiEdit", tool_input)
        assert result == [Path("/a/base.py"), Path("/a/one.py"), Path("/a/two.py")]

    def test_no_path_key_present_yields_empty_list(self) -> None:
        assert extract_paths("Edit", {"content": "no path here"}) == []


# ── resolve_roots ─────────────────────────────────────────────────────────


class TestResolveRoots:
    def test_dedup_preserves_first_occurrence_order(self, tmp_path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        result = resolve_roots(str(a), [str(b), str(a)])
        assert result == (a.resolve(), b.resolve())

    def test_empties_are_dropped(self, tmp_path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        result = resolve_roots(str(a), ["", "   ", None])
        assert result == (a.resolve(),)

    def test_none_cwd_and_no_additional_dirs_is_empty(self) -> None:
        assert resolve_roots(None, None) == ()

    def test_expanduser_is_applied(self) -> None:
        result = resolve_roots("~", None)
        assert result[0].is_absolute()
        assert "~" not in str(result[0])


# ── Decision / ToolPolicy dataclasses ────────────────────────────────────


class TestDataclasses:
    def test_decision_is_frozen(self) -> None:
        d = Decision(outcome="allow", reason="x")
        with pytest.raises(FrozenInstanceError):
            d.outcome = "deny"  # type: ignore[misc]

    def test_toolpolicy_is_frozen(self) -> None:
        p = mk_policy()
        with pytest.raises(FrozenInstanceError):
            p.writable = False  # type: ignore[misc]


# ── Settings: allowed_cwd_roots / denied_cwd_paths ───────────────────────


class TestCwdSettings:
    @pytest.fixture(autouse=True)
    def _no_ambient_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate default-value assertions from whatever the developer's own
        # shell (or CI) happens to export for these names.
        for var in ("ALLOWED_CWD_ROOTS", "DENIED_CWD_PATHS", "ALLOW_BYPASS_PERMISSIONS"):
            monkeypatch.delenv(var, raising=False)

    def test_default_allowed_cwd_roots_resolves_to_home(self) -> None:
        s = Settings(_env_file=None)

        assert s.cwd_allowlist() == [Path.home().resolve()]

    def test_default_is_not_permissive(self) -> None:
        s = Settings(_env_file=None)
        assert len(s.cwd_allowlist()) > 0

    def test_denied_path_list_resolves_every_configured_entry(self) -> None:
        s = Settings(_env_file=None)
        denied = s.denied_path_list()
        assert len(denied) == 8
        names = {p.name for p in denied}
        assert {".ssh", ".aws", ".gnupg", ".claude", ".codex"} <= names

    def test_allow_bypass_permissions_env_var(self, fresh_settings, monkeypatch) -> None:
        monkeypatch.setenv("ALLOW_BYPASS_PERMISSIONS", "true")
        assert get_settings().allow_bypass_permissions is True
