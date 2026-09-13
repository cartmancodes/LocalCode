"""Usage instrumentation: the number that proves Task 4's persistent-client
prompt cache is actually doing something.

``parse_claude_usage`` has to survive whatever shape ``ResultMessage.usage``
shows up in (missing entirely, snake_case, or the Anthropic API's camelCase),
and ``UsageLog`` has to survive a truncated last line from a process killed
mid-write — neither is optional, since a usage-parsing exception must never
take down the ``assistant.done`` event it rides on.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.usage import (
    TurnUsage,
    UsageLog,
    cache_hit_rate,
    default_usage_log_path,
    parse_claude_usage,
    uncached_share,
    usage_log_from_settings,
)


def _entry(**overrides: object) -> TurnUsage:
    base = dict(
        provider="claude",
        model="claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=None,
        session_id="s1",
        ts=time.time(),
    )
    base.update(overrides)
    return TurnUsage(**base)  # type: ignore[arg-type]


class TestParseClaudeUsage:
    def test_snake_case(self) -> None:
        result = SimpleNamespace(
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
            },
            total_cost_usd=0.5,
        )

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.provider == "claude"
        assert usage.model == "m1"
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.cache_read_tokens == 100
        assert usage.cache_creation_tokens == 20
        assert usage.cost_usd == 0.5
        assert usage.session_id == "s1"
        assert isinstance(usage.ts, float)

    def test_camel_case(self) -> None:
        result = SimpleNamespace(
            usage={
                "inputTokens": 11,
                "outputTokens": 6,
                "cacheReadInputTokens": 101,
                "cacheCreationInputTokens": 21,
            },
            total_cost_usd=None,
        )

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id=None
        )

        assert usage.input_tokens == 11
        assert usage.output_tokens == 6
        assert usage.cache_read_tokens == 101
        assert usage.cache_creation_tokens == 21
        assert usage.cost_usd is None
        assert usage.session_id is None

    def test_usage_none_does_not_raise(self) -> None:
        result = SimpleNamespace(usage=None, total_cost_usd=None)

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cache_creation_tokens == 0

    def test_missing_usage_attribute_does_not_raise(self) -> None:
        result = SimpleNamespace(total_cost_usd=None)  # no .usage at all

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0

    def test_a_usage_property_that_raises_does_not_raise_here(self) -> None:
        """``getattr(x, "usage", None)`` swallows a MISSING attribute, not a
        raising one. This function sits inside the per-turn loop with no local
        guard, so an exception escaping it turns a successful turn into an
        ``error`` with no ``assistant.done`` — the turn is lost to protect a
        telemetry field."""

        class RaisingUsage:
            total_cost_usd = 0.5

            @property
            def usage(self) -> dict[str, int]:
                raise RuntimeError("usage is unavailable on this result")

        usage = parse_claude_usage(
            RaisingUsage(), provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        # The fields that did not raise are still reported.
        assert usage.cost_usd == 0.5

    def test_a_cost_property_that_raises_does_not_raise_here_either(self) -> None:
        """``total_cost_usd`` sat outside every guard, on the last line of the
        function — the same defect as ``usage``, one field along, in a function
        whose whole contract is that it cannot take a turn down."""

        class RaisingCost:
            usage = {"input_tokens": 7}

            @property
            def total_cost_usd(self) -> float:
                raise RuntimeError("cost is computed lazily and the call failed")

        usage = parse_claude_usage(
            RaisingCost(), provider="claude", model="m1", session_id="s1"
        )

        assert usage.cost_usd is None
        # The fields that did not raise are still reported.
        assert usage.input_tokens == 7

    def test_unparseable_value_becomes_zero(self) -> None:
        result = SimpleNamespace(usage={"input_tokens": "not-a-number"}, total_cost_usd=None)

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0

    def test_usage_as_a_list_does_not_raise(self) -> None:
        """A malformed SDK response could hand back anything truthy in
        ``.usage``. The module's own docstring says a usage-parsing
        exception must never take down the assistant.done event it rides
        on — a non-dict shape must degrade to zeros, not raise."""
        result = SimpleNamespace(usage=[1, 2, 3], total_cost_usd=None)

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0
        assert usage.cache_read_tokens == 0

    def test_usage_as_a_string_does_not_raise(self) -> None:
        result = SimpleNamespace(usage="not-a-usage-dict", total_cost_usd=None)

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0

    def test_usage_as_an_int_does_not_raise(self) -> None:
        result = SimpleNamespace(usage=42, total_cost_usd=None)

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0

    def test_usage_whose_get_raises_does_not_raise(self) -> None:
        """A dict-*like* usage (passes any duck-typed dict check) whose own
        ``.get`` blows up must still not take the turn down with it."""

        class RaisingUsage(dict):
            def get(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("boom")

        result = SimpleNamespace(usage=RaisingUsage(input_tokens=5), total_cost_usd=None)

        usage = parse_claude_usage(
            result, provider="claude", model="m1", session_id="s1"
        )

        assert usage.input_tokens == 0


class TestUsageLog:
    def test_append_and_recent_round_trip(self, tmp_path: Path) -> None:
        log = UsageLog(path=tmp_path / "usage.jsonl")
        log.append(_entry(input_tokens=1))
        log.append(_entry(input_tokens=2))

        recent = log.recent(window_s=10_000)

        assert [e.input_tokens for e in recent] == [1, 2]

    def test_recent_excludes_entries_outside_the_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time as time_mod

        log = UsageLog(path=tmp_path / "usage.jsonl")
        now = 100_000.0
        log.append(_entry(ts=now - 10_000, input_tokens=1))  # too old
        log.append(_entry(ts=now - 10, input_tokens=2))  # recent

        monkeypatch.setattr(time_mod, "time", lambda: now)
        recent = log.recent(window_s=60)

        assert [e.input_tokens for e in recent] == [2]

    def test_recent_on_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        log = UsageLog(path=tmp_path / "does-not-exist.jsonl")
        assert log.recent(window_s=3600) == []

    def test_a_truncated_last_line_is_skipped_not_raised(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.jsonl"
        log = UsageLog(path=path)
        # A real "now" timestamp — this test is about surviving a truncated
        # line, not about window filtering, so the good entry must fall
        # inside any window `recent()` is asked for.
        log.append(_entry(ts=time.time(), input_tokens=1))
        # Simulate a crash mid-write: an unterminated, truncated JSON line.
        with path.open("a", encoding="utf-8") as f:
            f.write(f'{{"provider": "claude", "ts": {time.time()}, "input_to')

        recent = log.recent(window_s=10_000)

        assert [e.input_tokens for e in recent] == [1]

    def test_rotation_when_the_file_exceeds_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import backend.app.usage as usage_mod

        monkeypatch.setattr(usage_mod, "_MAX_LOG_BYTES", 100)
        path = tmp_path / "usage.jsonl"
        log = UsageLog(path=path)

        # Real "now" timestamps — this test is about rotation, not window
        # filtering, so both entries must fall inside the window below.
        log.append(_entry(ts=time.time()))
        # This append sees the file already over the (lowered) cap and
        # rotates before writing its own line.
        log.append(_entry(ts=time.time()))

        assert (tmp_path / "usage.jsonl.1").exists()
        # The fresh file holds only what was written after rotation.
        fresh = log.recent(window_s=10_000)
        assert len(fresh) == 1

    def test_default_path_resolves_under_a_redirected_home(
        self, tmp_localcode: Path
    ) -> None:
        """Same bug class as the artifact store: the default must be computed
        when a UsageLog is actually constructed, not at import time."""
        assert default_usage_log_path() == tmp_localcode / ".localcode" / "usage.jsonl"

        log = UsageLog()
        assert log.path == tmp_localcode / ".localcode" / "usage.jsonl"


class TestCacheHitRate:
    def test_basic_math(self) -> None:
        entries = [
            _entry(cache_read_tokens=80, input_tokens=20),
            _entry(cache_read_tokens=0, input_tokens=100),
        ]

        assert cache_hit_rate(entries) == pytest.approx(80 / 200)
        assert uncached_share(entries) == pytest.approx(120 / 200)

    def test_zero_denominator_is_zero_not_a_crash(self) -> None:
        entries = [_entry(cache_read_tokens=0, input_tokens=0)]

        assert cache_hit_rate(entries) == 0.0
        assert uncached_share(entries) == 0.0

    def test_empty_entries_is_zero(self) -> None:
        assert cache_hit_rate([]) == 0.0
        assert uncached_share([]) == 0.0

    def test_fully_cached(self) -> None:
        entries = [_entry(cache_read_tokens=50, input_tokens=0)]

        assert cache_hit_rate(entries) == 1.0
        assert uncached_share(entries) == 0.0


class TestUsageLogFromSettings:
    """The one thing that keeps the provider (writer) and the /usage
    endpoint (reader) pointed at the same file: both must resolve through
    this function, never independently."""

    def test_resolves_the_default_path_when_unset(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        log = usage_log_from_settings()
        assert log.path == tmp_localcode / ".localcode" / "usage.jsonl"

    def test_resolves_the_override_from_settings(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "custom-usage.jsonl"
        monkeypatch.setenv("USAGE_LOG_PATH", str(custom))

        log = usage_log_from_settings()

        assert log.path == custom.resolve()

    async def test_the_provider_and_the_route_resolve_to_the_same_overridden_file(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression this whole class exists to catch: if the provider
        and the endpoint ever again resolve the usage-log path separately,
        setting USAGE_LOG_PATH would split them onto two different files —
        the provider logs turns nobody reads, the endpoint reports zero
        usage forever. Driving both the provider's own resolution *and* the
        route through one override is what makes that impossible."""
        from backend.app.orchestrator import claude as claude_mod
        from backend.app.routes import system as system_routes

        custom = tmp_path / "shared-usage.jsonl"
        monkeypatch.setenv("USAGE_LOG_PATH", str(custom))

        provider = claude_mod.ClaudeProvider()
        assert provider._get_usage_log().path == custom.resolve()

        provider._get_usage_log().append(_entry(provider="claude", input_tokens=7))

        body = await system_routes.get_system_usage()

        assert body["turns"] == 1
        assert body["input_tokens"] == 7


class TestSystemUsageRoute:
    async def test_reports_stats_over_the_window(
        self, tmp_localcode: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.app.routes import system as system_routes

        path = tmp_localcode / ".localcode" / "usage.jsonl"
        log = UsageLog(path=path)
        log.append(_entry(provider="claude", cache_read_tokens=80, input_tokens=20))
        log.append(_entry(provider="opencode", cache_read_tokens=0, input_tokens=10))

        body = await system_routes.get_system_usage()

        assert body["window_s"] == 3600
        assert body["turns"] == 2
        assert body["input_tokens"] == 30
        assert body["cache_read_tokens"] == 80
        assert set(body["by_provider"]) == {"claude", "opencode"}
        assert body["by_provider"]["claude"]["turns"] == 1

    async def test_empty_log_reports_zeros(self, tmp_localcode: Path) -> None:
        from backend.app.routes import system as system_routes

        body = await system_routes.get_system_usage()

        assert body["turns"] == 0
        assert body["cache_hit_rate"] == 0.0
        assert body["by_provider"] == {}
