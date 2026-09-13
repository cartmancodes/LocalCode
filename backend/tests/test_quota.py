"""The quota governor: remaining subscription headroom as the steering number.

Two subscriptions are two independently exhaustible budgets on different
clocks, and the per-turn ``cost_usd`` figure bills nobody. These tests pin the
three things that has to be true for headroom to be trustworthy:

  * **the arithmetic** — window rollover, a vendor-reported payload beating
    local accumulation, the minimum across a provider's windows, and an
    unknown limit reading ``1.0`` with ``confidence="unknown"`` rather than a
    confidently full bar;
  * **the durability** — one atomic ``quota.json`` that survives a restart and
    recovers from a corrupt file with a warning instead of taking the process
    down with it;
  * **the counting** — exactly ONE record per turn. The governor is called
    from the main process at exactly two sites (``turn.py`` for a direct turn,
    ``dispatch.py`` beside ``budget.spend`` for a fleet sub-step), because
    ``quota.json`` is a read-modify-write file and the fleet's sub-providers
    run in worker PROCESSES. A double count shows inflated use, which is
    exactly the number this task exists to make honest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, get_args

import pytest

from backend.app import quota as quota_mod
from backend.app.config import get_settings
from backend.app.quota import (
    QUEUE_THRESHOLD,
    Governor,
    default_quota_path,
    get_governor,
    governed_providers,
    tokens_from_usage,
)

FIVE_HOURS_S = 5 * 3600.0
ONE_WEEK_S = 7 * 24 * 3600.0


class Clock:
    """A hand-wound clock: window rollover is a statement about time, and a
    test that slept for five hours would not be one."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clear_governor_cache(monkeypatch: pytest.MonkeyPatch):
    """``get_governor`` is ``lru_cache``d like ``get_settings``, so a cached
    instance built under one test's HOME (or one test's ``QUOTA_PATH``) would
    otherwise be handed to the next test — and would keep writing to a
    tmp_path that no longer exists.

    The writer lock is replaced per test for a subtler reason: an
    ``asyncio.Lock`` binds to the loop of the first coroutine that has to WAIT
    on it, and pytest-asyncio gives every test its own loop. The app has one
    loop for its whole life so the module-level lock is right there (it is the
    same idiom as ``storage/sessions.py``'s ``_index_lock``); only the suite
    needs a fresh one, and only because two different tests contend on it.
    """
    get_governor.cache_clear()
    monkeypatch.setattr(quota_mod, "_record_lock", asyncio.Lock())
    yield
    get_governor.cache_clear()


def a_governor(tmp_path: Path, clock: Clock | None = None) -> Governor:
    return Governor(tmp_path / "quota.json", clock=clock or Clock())


# ─────────────────────────────────────────────────────────────────────────────
# Windows and local accumulation
# ─────────────────────────────────────────────────────────────────────────────


class TestWindows:
    def test_a_provider_gets_its_default_windows_on_first_record(
        self, tmp_path: Path
    ) -> None:
        gov = a_governor(tmp_path)

        gov.record("codex", tokens=10)

        windows = {w.key: w for w in gov.snapshot().windows["codex"]}
        # Codex is a 5-hour rolling window AND a weekly cap, per the spec.
        assert set(windows) == {"five_hour", "weekly"}
        assert windows["five_hour"].window_s == FIVE_HOURS_S
        assert windows["weekly"].window_s == ONE_WEEK_S
        assert all(w.used == 10 for w in windows.values())
        assert all(w.source == "local" for w in windows.values())

    def test_local_records_accumulate_within_the_window(self, tmp_path: Path) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)

        gov.record("claude", tokens=100)
        clock.advance(FIVE_HOURS_S - 1)
        gov.record("claude", tokens=50)

        window = gov.snapshot().windows["claude"][0]
        assert window.used == 150

    def test_a_record_after_the_window_length_rolls_it(self, tmp_path: Path) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)
        gov.record("claude", tokens=100)
        opened_at = gov.snapshot().windows["claude"][0].started_at

        clock.advance(FIVE_HOURS_S)
        gov.record("claude", tokens=7)

        window = gov.snapshot().windows["claude"][0]
        assert window.used == 7, "the rolled window kept the old window's spend"
        assert window.started_at == clock.now
        assert window.started_at > opened_at

    def test_the_weekly_window_does_not_roll_when_the_five_hour_one_does(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)
        gov.record("codex", tokens=100)

        clock.advance(FIVE_HOURS_S)
        gov.record("codex", tokens=5)

        windows = {w.key: w for w in gov.snapshot().windows["codex"]}
        assert windows["five_hour"].used == 5
        assert windows["weekly"].used == 105


class TestReportedPayloads:
    def test_a_reported_payload_overrides_local_accumulation_and_flips_source(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)
        gov.record("claude", tokens=100_000)

        gov.record(
            "claude",
            reported={
                "status": "allowed_warning",
                "rate_limit_type": "five_hour",
                "utilization": 0.4,
                "resets_at": clock.now + 900,
            },
        )

        window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
        assert window.source == "provider"
        # utilization IS the fraction: used 0.4 of a limit of 1.0.
        assert window.used == pytest.approx(0.4)
        assert window.limit == pytest.approx(1.0)
        assert window.unit == "fraction"
        assert window.resets_at == pytest.approx(clock.now + 900)
        assert gov.headroom("claude") == pytest.approx(0.6)

    def test_each_rate_limit_type_gets_its_own_window(self, tmp_path: Path) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)

        for kind, util in (("five_hour", 0.2), ("seven_day_opus", 0.9)):
            gov.record(
                "claude",
                reported={
                    "status": "allowed",
                    "rate_limit_type": kind,
                    "utilization": util,
                    "resets_at": clock.now + 3600,
                },
            )

        windows = {w.key: w for w in gov.snapshot().windows["claude"]}
        assert {"five_hour", "seven_day_opus"} <= set(windows)
        # A window that exists only because the vendor named it still knows how
        # long it runs — it is a seven-day cap, not another five-hour one.
        assert windows["seven_day_opus"].window_s == ONE_WEEK_S
        # The minimum across the provider's windows wins.
        assert gov.headroom("claude") == pytest.approx(0.1)

    def test_absence_of_an_event_is_no_change_not_a_reset(self, tmp_path: Path) -> None:
        """The CLI emits a rate-limit event only on a TRANSITION, so between
        events the last-known provider state stands — local tokens must not
        quietly overwrite a measured fraction with an unmeasured count."""
        clock = Clock()
        gov = a_governor(tmp_path, clock)
        gov.record(
            "claude",
            reported={
                "status": "allowed",
                "rate_limit_type": "five_hour",
                "utilization": 0.8,
                "resets_at": clock.now + 7200,
            },
        )

        clock.advance(60)
        gov.record("claude", tokens=50_000)

        window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
        assert window.source == "provider"
        assert window.used == pytest.approx(0.8)
        assert gov.headroom("claude") == pytest.approx(0.2)

    def test_a_provider_window_falls_back_to_local_once_resets_at_passes(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)
        gov.record(
            "claude",
            reported={
                "status": "rejected",
                "rate_limit_type": "five_hour",
                "utilization": 1.0,
                "resets_at": clock.now + 600,
            },
        )
        assert gov.headroom("claude") == pytest.approx(0.0)

        clock.advance(601)
        gov.record("claude", tokens=10)

        window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
        assert window.source == "local"
        assert window.used == 10
        assert window.limit is None
        assert gov.headroom("claude") == 1.0

    def test_a_provider_window_with_no_usable_reset_rolls_on_its_own_length(
        self, tmp_path: Path
    ) -> None:
        """Ruling 32. A payload with a utilization and no reset is exactly what
        the Codex path allows, and without a fallback it froze the window at
        that utilization forever — at 0.95 or above, pinning headroom under the
        threshold so every ``auto`` dispatch refused with no way back."""
        clock = Clock()
        for payload_reset in (
            None,  # absent entirely
            clock.now - 10,  # already in the past
            clock.now * 1000,  # a millisecond-epoch stamp
            clock.now + 400 * 24 * 3600,  # absurdly far out
            "soon",  # not a number at all
        ):
            gov = Governor(tmp_path / f"quota-{payload_reset}.json", clock=clock)
            reported: dict[str, Any] = {
                "rate_limit_type": "five_hour",
                "utilization": 0.99,
            }
            if payload_reset is not None:
                reported["resets_at"] = payload_reset
            gov.record("claude", reported=reported)

            window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
            assert window.source == "provider"
            # Not stored: the snapshot never shows a reset the governor is not
            # using — and a rejected stamp is never CONVERTED into a plausible
            # one, which would be fabricating a measurement.
            assert window.resets_at is None
            assert gov.headroom("claude") == pytest.approx(0.01)

            # …and it rolls at started_at + window_s, like any other window.
            clock.advance(FIVE_HOURS_S)
            assert gov.headroom("claude") == 1.0
            clock.advance(-FIVE_HOURS_S)

    def test_a_sane_reset_is_honoured_verbatim(self, tmp_path: Path) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)

        gov.record(
            "claude",
            reported={
                "rate_limit_type": "five_hour",
                "utilization": 0.5,
                "resets_at": clock.now + 1800,
            },
        )

        window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
        assert window.resets_at == pytest.approx(clock.now + 1800)
        # Before the reset it stands; after it, the window rolls — well before
        # started_at + window_s, because the vendor said so.
        clock.advance(1799)
        assert gov.headroom("claude") == pytest.approx(0.5)
        clock.advance(2)
        assert gov.headroom("claude") == 1.0

    def test_an_unusable_reset_is_warned_about_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)
        payload = {
            "rate_limit_type": "five_hour",
            "utilization": 0.2,
            "resets_at": clock.now * 1000,
        }

        with caplog.at_level(logging.WARNING, logger="backend.app.quota"):
            gov.record("claude", reported=payload)
            gov.record("claude", reported=payload)

        warnings = [r for r in caplog.records if "resets_at" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert str(int(clock.now * 1000)) in warnings[0].getMessage()

    def test_a_malformed_payload_records_locally_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        gov = a_governor(tmp_path)

        for payload in (
            {"utilization": "banana"},
            {"rate_limit_type": "five_hour"},
            "not a mapping",
            None,
            {"utilization": float("nan")},
        ):
            gov.record("claude", tokens=3, reported=payload)

        window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
        assert window.source == "local"
        assert window.used == 15

    def test_a_payload_without_a_window_name_lands_on_the_primary_window(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        gov = a_governor(tmp_path, clock)

        gov.record("claude", reported={"status": "allowed", "utilization": 0.5})

        window = {w.key: w for w in gov.snapshot().windows["claude"]}["five_hour"]
        assert window.source == "provider"
        assert gov.headroom("claude") == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Headroom, choose, should_queue
# ─────────────────────────────────────────────────────────────────────────────


class TestHeadroom:
    def test_an_unknown_limit_is_one_point_zero_with_unknown_confidence(
        self, tmp_path: Path
    ) -> None:
        gov = a_governor(tmp_path)
        gov.record("claude", tokens=10**9)

        assert gov.headroom("claude") == 1.0
        body = gov.to_dict()
        assert body["providers"]["claude"]["confidence"] == "unknown"
        assert body["providers"]["claude"]["windows"][0]["confidence"] == "unknown"

    def test_a_provider_never_seen_is_full_and_unknown(self, tmp_path: Path) -> None:
        gov = a_governor(tmp_path)

        assert gov.headroom("codex") == 1.0

    def test_a_known_limit_is_the_remaining_fraction(self, tmp_path: Path) -> None:
        gov = a_governor(tmp_path)
        gov.record(
            "codex",
            reported={"rate_limit_type": "weekly", "utilization": 0.25},
        )

        assert gov.headroom("codex") == pytest.approx(0.75)
        body = gov.to_dict()
        assert body["providers"]["codex"]["confidence"] == "reported"

    def test_over_utilization_clamps_to_zero(self, tmp_path: Path) -> None:
        gov = a_governor(tmp_path)
        gov.record("codex", reported={"rate_limit_type": "weekly", "utilization": 1.4})

        assert gov.headroom("codex") == 0.0


class TestChoose:
    def _two(self, tmp_path: Path, claude_util: float, codex_util: float) -> Governor:
        gov = a_governor(tmp_path)
        gov.record(
            "claude", reported={"rate_limit_type": "five_hour", "utilization": claude_util}
        )
        gov.record(
            "codex", reported={"rate_limit_type": "five_hour", "utilization": codex_util}
        )
        return gov

    def test_the_candidate_with_the_most_headroom_wins(self, tmp_path: Path) -> None:
        gov = self._two(tmp_path, claude_util=0.9, codex_util=0.1)

        assert gov.choose(["claude", "codex"]) == "codex"

    def test_a_tie_resolves_to_the_first_candidate(self, tmp_path: Path) -> None:
        gov = self._two(tmp_path, claude_util=0.5, codex_util=0.5)

        assert gov.choose(["claude", "codex"]) == "claude"
        assert gov.choose(["codex", "claude"]) == "codex"

    def test_every_candidate_exhausted_returns_none(self, tmp_path: Path) -> None:
        gov = self._two(tmp_path, claude_util=0.99, codex_util=0.98)

        assert gov.choose(["claude", "codex"]) is None

    def test_no_candidates_chooses_nothing(self, tmp_path: Path) -> None:
        assert a_governor(tmp_path).choose([]) is None

    def test_should_queue_at_the_threshold_boundary(self, tmp_path: Path) -> None:
        exactly = self._two(
            tmp_path, claude_util=1.0 - QUEUE_THRESHOLD, codex_util=1.0 - QUEUE_THRESHOLD
        )
        assert exactly.headroom("claude") == pytest.approx(QUEUE_THRESHOLD)
        # Exactly at the threshold does NOT queue.
        assert exactly.should_queue(["claude", "codex"]) is False
        assert exactly.choose(["claude", "codex"]) == "claude"

    def test_should_queue_below_the_threshold(self, tmp_path: Path) -> None:
        below = self._two(tmp_path, claude_util=0.96, codex_util=0.97)

        assert below.should_queue(["claude", "codex"]) is True

    def test_nothing_to_queue_when_there_are_no_candidates(self, tmp_path: Path) -> None:
        assert a_governor(tmp_path).should_queue([]) is False

    def test_the_refusal_names_every_candidate_its_headroom_and_its_reset(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        gov = Governor(tmp_path / "quota.json", clock=clock)
        for name, util in (("claude", 0.99), ("codex", 0.98)):
            gov.record(
                name,
                reported={
                    "rate_limit_type": "five_hour",
                    "utilization": util,
                    "resets_at": clock.now + 3600,
                },
            )

        message = gov.refusal(["claude", "codex"])

        assert "claude" in message and "codex" in message
        assert "1%" in message and "2%" in message
        # A human time, not a unix float, and an explicit "it can be queued".
        assert "queue" in message.lower()
        assert str(int(clock.now + 3600)) not in message


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistence:
    def test_state_round_trips_through_the_file(self, tmp_path: Path) -> None:
        clock = Clock()
        first = Governor(tmp_path / "quota.json", clock=clock)
        first.record("claude", tokens=42)
        first.record(
            "codex", reported={"rate_limit_type": "weekly", "utilization": 0.3}
        )

        second = Governor(tmp_path / "quota.json", clock=clock)

        assert {w.key: w.used for w in second.snapshot().windows["claude"]} == {
            "five_hour": 42
        }
        assert second.headroom("codex") == pytest.approx(0.7)

    def test_a_corrupt_file_starts_fresh_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "quota.json"
        path.write_text("{not json at all", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="backend.app.quota"):
            gov = Governor(path, clock=Clock())

        assert gov.snapshot().windows == {}
        assert any("quota" in r.message.lower() for r in caplog.records)
        # And it is usable afterwards, not poisoned.
        gov.record("claude", tokens=1)
        assert gov.headroom("claude") == 1.0

    def test_a_wrongly_shaped_file_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "quota.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        gov = Governor(path, clock=Clock())

        assert gov.snapshot().windows == {}

    def test_a_garbage_window_entry_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "quota.json"
        path.write_text(
            json.dumps(
                {
                    "windows": {
                        "claude": [
                            {"key": "five_hour", "window_s": "nonsense"},
                            {
                                "key": "seven_day",
                                "window_s": ONE_WEEK_S,
                                "started_at": 1.0,
                                "used": 0.5,
                                "limit": 1.0,
                                "source": "provider",
                                "resets_at": None,
                                "unit": "fraction",
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        gov = Governor(path, clock=Clock())

        keys = [w.key for w in gov.snapshot().windows.get("claude", [])]
        assert keys == ["seven_day"]

    def test_the_atomic_write_leaves_no_tmp_file_behind(self, tmp_path: Path) -> None:
        gov = a_governor(tmp_path)

        gov.record("claude", tokens=1)

        assert (tmp_path / "quota.json").exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_write_failure_does_not_take_the_turn_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recording is telemetry hanging off a turn's terminal event. A full
        disk must cost the quota number, never the turn."""
        gov = a_governor(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(quota_mod, "_atomic_write_text", boom)
        gov.record("claude", tokens=5)

        assert gov.headroom("claude") == 1.0


class TestOneWriterAtATime:
    """``quota.json`` is a read-modify-write file and the process has exactly
    one cached Governor. Two turns finishing at once — two sessions, or two
    ``dispatch_subagent`` calls in flight in one fleet turn — must not lose an
    increment or publish a half-written ledger."""

    async def test_records_are_serialized_across_the_thread_offload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Overlapping:
            """Reports the widest overlap it ever saw. Without the lock held
            ACROSS ``asyncio.to_thread``, eight of these run at once."""

            def __init__(self) -> None:
                self.live = 0
                self.widest = 0
                self.total = 0

            def record(self, _provider: str, *, tokens: int = 0, reported: Any = None) -> None:
                self.live += 1
                self.widest = max(self.widest, self.live)
                # A real read-modify-write window, not an instant one.
                time.sleep(0.01)
                self.total += tokens
                self.live -= 1

        governor = Overlapping()
        monkeypatch.setattr(quota_mod, "get_governor", lambda: governor)

        await asyncio.gather(
            *(quota_mod.record_turn("claude", tokens=1) for _ in range(8))
        )

        assert governor.widest == 1, "two writers were inside the ledger at once"
        assert governor.total == 8

    async def test_concurrent_records_keep_every_token_and_a_parseable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        governor = a_governor(tmp_path)
        monkeypatch.setattr(quota_mod, "get_governor", lambda: governor)

        await asyncio.gather(
            *(quota_mod.record_turn("claude", tokens=10) for _ in range(20))
        )

        assert governor.snapshot().windows["claude"][0].used == 200
        on_disk = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
        assert on_disk["windows"]["claude"][0]["used"] == 200

    def test_the_temp_file_is_per_process(self, tmp_path: Path) -> None:
        """A second LocalCode process shares only the file. On one fixed
        ``quota.json.tmp`` its truncating open, landing between our write and
        our rename, publishes a truncated ledger that the next start
        "recovers" by discarding every window."""
        import os

        seen: list[Path] = []
        real_replace = os.replace

        def spy(src: Any, dst: Any) -> None:
            seen.append(Path(src))
            real_replace(src, dst)

        gov = a_governor(tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(quota_mod.os, "replace", spy)
            gov.record("claude", tokens=1)

        assert seen and seen[0].name == f"quota.json.{os.getpid()}.tmp"


class TestSettingsDrivenPath:
    def test_the_default_path_resolves_under_a_redirected_home(
        self, tmp_localcode: Path
    ) -> None:
        """Same rule as the usage log: resolved when a Governor is built, never
        at import time, or a test that redirects HOME still writes to the
        developer's real ``~/.localcode``."""
        assert default_quota_path() == tmp_localcode / ".localcode" / "quota.json"
        assert Governor().path == tmp_localcode / ".localcode" / "quota.json"

    def test_get_governor_resolves_the_default(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        assert get_governor().path == tmp_localcode / ".localcode" / "quota.json"

    def test_get_governor_resolves_the_settings_override(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "elsewhere" / "quota.json"
        monkeypatch.setenv("QUOTA_PATH", str(custom))

        assert get_settings().quota_path == str(custom)
        assert get_governor().path == custom.resolve()

    def test_get_governor_is_cached(self, tmp_localcode: Path, fresh_settings) -> None:
        assert get_governor() is get_governor()


class TestTokensFromUsage:
    def test_every_token_key_counts(self) -> None:
        assert (
            tokens_from_usage(
                {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_tokens": 4,
                    "cache_creation_tokens": 8,
                    "provider": "claude",
                    "cost_usd": 0.5,
                }
            )
            == 15
        )

    def test_a_missing_or_malformed_usage_costs_nothing(self) -> None:
        assert tokens_from_usage(None) == 0
        assert tokens_from_usage({}) == 0
        assert tokens_from_usage("nope") == 0
        assert tokens_from_usage({"input_tokens": "many"}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Providers report in — an EVENT, never a governor call from a worker process
# ─────────────────────────────────────────────────────────────────────────────


class TestClaudeReportsIn:
    async def _translate(self, message: Any) -> list[Any]:
        from backend.app.orchestrator import claude as claude_mod

        return [ev async for ev in claude_mod._translate(message)]

    async def test_a_rate_limit_event_becomes_a_quota_limit_event(self) -> None:
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo

        events = await self._translate(
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    resets_at=1_700_000_000,
                    rate_limit_type="seven_day_opus",
                    utilization=0.85,
                ),
                uuid="u1",
                session_id="s1",
            )
        )

        assert [ev.type for ev in events] == ["quota.limit"]
        assert events[0].data == {
            "provider": "claude",
            "status": "allowed_warning",
            "resets_at": 1_700_000_000,
            "rate_limit_type": "seven_day_opus",
            "utilization": 0.85,
        }

    async def test_a_missing_info_does_not_raise(self) -> None:
        from claude_agent_sdk import RateLimitEvent

        event = RateLimitEvent(rate_limit_info=None, uuid="u", session_id="s")  # type: ignore[arg-type]

        events = await self._translate(event)

        assert [ev.type for ev in events] == ["quota.limit"]
        assert events[0].data["provider"] == "claude"
        assert events[0].data["utilization"] is None

    async def test_a_shape_change_does_not_raise(self) -> None:
        """A future SDK that renames every field must cost the quota number,
        never the turn."""
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo

        drifted = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed"), uuid="u", session_id="s"
        )
        drifted.rate_limit_info = object()  # type: ignore[assignment]

        events = await self._translate(drifted)

        assert [ev.type for ev in events] == ["quota.limit"]


class TestFleetOrchestratorReportsIn:
    """The orchestrator's own model loop is a claude-agent-sdk session too, and
    for a user who works only in fleet sessions it is the ONLY place Claude's
    headroom is ever measured. Its translator dropped ``RateLimitEvent``
    entirely, so ``auto`` routed on an unmeasured local estimate forever."""

    async def _translate(self, message: Any) -> list[Any]:
        from backend.app.orchestrator.orchestrator import (
            _translate_orchestrator_message,
        )

        return [
            ev
            async for ev in _translate_orchestrator_message(
                message, suppressed_tool_names={"dispatch_subagent"}
            )
        ]

    async def test_a_rate_limit_event_becomes_a_quota_limit_event(self) -> None:
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo

        events = await self._translate(
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    resets_at=1_700_000_000,
                    rate_limit_type="five_hour",
                    utilization=0.6,
                ),
                uuid="u1",
                session_id="s1",
            )
        )

        assert [ev.type for ev in events] == ["quota.limit"]
        assert events[0].data["provider"] == "claude"
        assert events[0].data["utilization"] == 0.6

    async def test_a_shape_change_does_not_raise(self) -> None:
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo

        drifted = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed"), uuid="u", session_id="s"
        )
        drifted.rate_limit_info = object()  # type: ignore[assignment]

        events = await self._translate(drifted)

        assert [ev.type for ev in events] == ["quota.limit"]

    def test_both_translators_share_one_reader(self) -> None:
        """Two copies of the ``getattr`` chain is two places to fix when the
        SDK's shape moves — and one of them will be missed."""
        from backend.app.orchestrator import claude as claude_mod
        from backend.app.orchestrator import orchestrator as orch_mod

        assert orch_mod.rate_limit_event is claude_mod.rate_limit_event


class TestCodexReportsIn:
    def _completed(self, params: dict[str, Any]) -> list[Any]:
        from backend.app.orchestrator.codex.provider import _Translator

        translator = _Translator(model="m", session_id="s", thread_id="t")
        return list(translator._handle_completed(params))

    def test_no_rate_limit_field_emits_nothing_extra(self) -> None:
        events = self._completed({"usage": {"input_tokens": 3}})

        assert [ev.type for ev in events] == ["assistant.done"]

    def test_a_reported_rate_limit_becomes_a_quota_limit_event(self) -> None:
        events = self._completed(
            {
                "usage": {"input_tokens": 3},
                "rate_limits": {
                    "five_hour": {"utilization": 0.42, "resets_at": 1_700_000_000}
                },
            }
        )

        assert [ev.type for ev in events] == ["quota.limit", "assistant.done"]
        assert events[0].data["provider"] == "codex"
        assert events[0].data["rate_limit_type"] == "five_hour"
        assert events[0].data["utilization"] == 0.42

    def test_a_list_shaped_payload_is_read_too(self) -> None:
        events = self._completed(
            {
                "rateLimits": [
                    {"type": "five_hour", "utilization": 0.1},
                    {"type": "weekly", "utilization": 0.9},
                ]
            }
        )

        assert [ev.type for ev in events] == ["quota.limit", "quota.limit", "assistant.done"]
        assert [ev.data["rate_limit_type"] for ev in events[:2]] == ["five_hour", "weekly"]

    def test_a_nested_usage_limits_payload_is_read_too(self) -> None:
        events = self._completed(
            {"usage": {"input_tokens": 1, "limits": {"weekly": {"utilization": 0.5}}}}
        )

        assert [ev.type for ev in events] == ["quota.limit", "assistant.done"]
        assert events[0].data["rate_limit_type"] == "weekly"

    def test_a_junk_payload_is_ignored_rather_than_raising(self) -> None:
        events = self._completed({"rate_limits": "soon"})

        assert [ev.type for ev in events] == ["assistant.done"]

    def test_no_codex_string_literal_leaked_out_of_protocol(self) -> None:
        """Task 10's rule, still true: every wire spelling lives in
        ``protocol.py``."""
        from backend.app.orchestrator.codex import protocol

        assert "rate_limits" in protocol.RATE_LIMIT_FIELDS
        assert "rateLimits" in protocol.RATE_LIMIT_FIELDS


class TestEventTypeIsDeclaredEverywhere:
    def test_the_event_union_names_quota_limit(self) -> None:
        """A new event type goes in three places; two of them are Python."""
        from backend.app.orchestrator.base import EventType
        from backend.app.schemas import StreamEvent

        assert "quota.limit" in get_args(EventType)
        assert "quota.limit" in get_args(StreamEvent.model_fields["type"].annotation)

    def test_the_frontend_union_names_quota_limit(self, repo_root: Path) -> None:
        types_ts = (repo_root / "frontend" / "src" / "types.ts").read_text()
        assert '"quota.limit"' in types_ts


# ─────────────────────────────────────────────────────────────────────────────
# Ruling 31 — recorded in the main process, exactly once per turn
# ─────────────────────────────────────────────────────────────────────────────


class _RecordingGovernor:
    """Captures calls without touching the disk."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict[str, Any] | None]] = []

    def record(
        self, provider: str, *, tokens: int = 0, reported: Any = None
    ) -> None:
        self.calls.append((provider, tokens, reported))

    @property
    def token_calls(self) -> list[tuple[str, int]]:
        return [(p, t) for p, t, r in self.calls if r is None]


@pytest.fixture
def recording_governor(monkeypatch: pytest.MonkeyPatch) -> _RecordingGovernor:
    gov = _RecordingGovernor()
    monkeypatch.setattr(quota_mod, "get_governor", lambda: gov)
    return gov


class TestDirectTurnRecordsOnce:
    async def test_a_claude_turn_through_the_fake_client_records_exactly_once(
        self, isolated_store: Path, recording_governor: _RecordingGovernor
    ) -> None:
        from backend.app.orchestrator import claude as claude_mod
        from backend.app.session_runner.turn import execute_turn
        from backend.app.storage.sessions import store as session_store
        from backend.tests.fakes.claude_client import FakeClientFactory

        session = await session_store.create_session(
            provider="claude", model="m", title="t"
        )
        provider = claude_mod.ClaudeProvider()
        provider._factory = FakeClientFactory()
        bus = _CollectingBus()

        await execute_turn(
            session_id=session["id"],
            bus=bus,  # type: ignore[arg-type]
            approval_q=asyncio.Queue(),
            provider=provider,  # type: ignore[arg-type]
            provider_name="claude",
            model="m",
            cwd=None,
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt="hi",
        )

        assert recording_governor.token_calls == [("claude", 0)], (
            "one usage record per turn — the fake turn reports zero tokens"
        )

    async def test_a_quota_limit_event_on_a_direct_turn_is_recorded_as_reported(
        self, isolated_store: Path, recording_governor: _RecordingGovernor
    ) -> None:
        from backend.app.orchestrator.base import Event
        from backend.app.session_runner.turn import execute_turn
        from backend.app.storage.sessions import store as session_store

        session = await session_store.create_session(
            provider="claude", model="m", title="t"
        )
        provider = _StubProvider(
            [
                Event(
                    type="quota.limit",
                    data={
                        "provider": "claude",
                        "status": "allowed_warning",
                        "rate_limit_type": "five_hour",
                        "utilization": 0.7,
                        "resets_at": 1_700_000_000,
                    },
                ),
                Event(
                    type="assistant.done",
                    data={"usage": {"input_tokens": 10, "output_tokens": 5}},
                ),
            ]
        )
        bus = _CollectingBus()

        await execute_turn(
            session_id=session["id"],
            bus=bus,  # type: ignore[arg-type]
            approval_q=asyncio.Queue(),
            provider=provider,  # type: ignore[arg-type]
            provider_name="claude",
            model="m",
            cwd=None,
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt="hi",
        )

        assert recording_governor.token_calls == [("claude", 15)]
        reported = [r for _p, _t, r in recording_governor.calls if r is not None]
        assert len(reported) == 1
        assert reported[0]["utilization"] == 0.7
        # And the event still reaches subscribers.
        assert "quota.limit" in [ev["type"] for ev in bus.events]

    async def test_a_turn_records_under_the_name_the_site_already_holds(
        self, isolated_store: Path, recording_governor: _RecordingGovernor
    ) -> None:
        """No call site maps one provider name onto another — the fleet's own
        orchestrator turn is recorded as ``fleet`` (its sub-steps are recorded
        separately, by dispatch.py, against the provider that served them), and
        a terminal event carrying NO usage is charged nothing at all. Today
        that is every fleet turn: ``orchestrator.py``'s ``assistant.done`` has
        no ``usage`` key, so the orchestrator's own model loop is unmetered."""
        from backend.app.orchestrator.base import Event
        from backend.app.session_runner.turn import execute_turn
        from backend.app.storage.sessions import store as session_store

        session = await session_store.create_session(
            provider="fleet", model="m", title="t"
        )

        async def run(provider_name: str, events: list[Any]) -> None:
            await execute_turn(
                session_id=session["id"],
                bus=_CollectingBus(),  # type: ignore[arg-type]
                approval_q=asyncio.Queue(),
                provider=_StubProvider(events),  # type: ignore[arg-type]
                provider_name=provider_name,
                model="m",
                cwd=None,
                additional_dirs=[],
                upstream_id=None,
                fleet_override=None,
                permission_mode=None,
                prompt="hi",
            )

        # What the fleet emits today: a done with no usage at all.
        await run("fleet", [Event(type="assistant.done", data={"cost_usd": 1.0})])
        assert recording_governor.token_calls == []

        await run(
            "fleet",
            [Event(type="assistant.done", data={"usage": {"input_tokens": 4}})],
        )
        assert recording_governor.token_calls == [("fleet", 4)]

    async def test_a_governor_failure_does_not_break_the_turn(
        self, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.app.orchestrator.base import Event
        from backend.app.session_runner.turn import execute_turn
        from backend.app.storage.sessions import store as session_store

        class Exploding:
            def record(self, *_a: Any, **_k: Any) -> None:
                raise RuntimeError("governor is on fire")

        monkeypatch.setattr(quota_mod, "get_governor", Exploding)
        session = await session_store.create_session(
            provider="claude", model="m", title="t"
        )
        provider = _StubProvider(
            [Event(type="assistant.done", data={"usage": {"input_tokens": 1}})]
        )
        bus = _CollectingBus()

        await execute_turn(
            session_id=session["id"],
            bus=bus,  # type: ignore[arg-type]
            approval_q=asyncio.Queue(),
            provider=provider,  # type: ignore[arg-type]
            provider_name="claude",
            model="m",
            cwd=None,
            additional_dirs=[],
            upstream_id=None,
            fleet_override=None,
            permission_mode=None,
            prompt="hi",
        )

        assert [ev["type"] for ev in bus.events].count("assistant.done") == 1
        assert not [ev for ev in bus.events if ev["type"] == "error"]


class _CollectingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def broadcast(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


class _StubProvider:
    name = "stub"

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def open_session(self, ctx: Any) -> str | None:
        return None

    async def run(self, ctx: Any):
        for ev in self._events:
            yield ev


# ─────────────────────────────────────────────────────────────────────────────
# Fleet sub-steps: recorded once, for the role's resolved provider
# ─────────────────────────────────────────────────────────────────────────────


def _dispatch_tool(
    monkeypatch: pytest.MonkeyPatch,
    run_step_fn: Any,
    *,
    provider: str = "claude",
    metadata: dict[str, Any] | None = None,
):
    """The real ``dispatch_subagent``, captured out of the MCP server build —
    the per-turn ledgers are closure locals, so there is no other way in."""
    from backend.app.orchestrator import dispatch as dispatch_mod
    from backend.app.orchestrator.agent_def import AgentDef
    from backend.app.orchestrator.approvals import EventSink
    from backend.app.orchestrator.base import RunContext

    captured: dict[str, Any] = {}

    def _fake_create(*, name: str, version: str, tools: list):
        captured.update({t.name: t for t in tools})
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(dispatch_mod, "create_sdk_mcp_server", _fake_create)
    registry = {
        "coder": AgentDef(
            name="coder",
            description="d",
            provider=provider,
            model="m",
            system_prompt="s",
            metadata=metadata or {},
        )
    }
    dispatch_mod.build_dispatch_mcp(
        registry=registry,
        ctx=RunContext(model="m", prompt="p", cwd=None),
        sink=EventSink(),
        run_step_fn=run_step_fn,
    )
    return captured["dispatch_subagent"].handler


class _Recorder:
    """Stands in for ``FleetProvider._run_step_with_role``."""

    def __init__(self, envelope: Any) -> None:
        self.envelope = envelope
        self.role_cfgs: list[Any] = []

    async def __call__(self, step, role_cfg, ctx, outputs):
        self.role_cfgs.append(role_cfg)
        outputs[step.id] = self.envelope
        if False:  # pragma: no cover — makes this an async generator
            yield None


def _envelope(usage: dict[str, int] | None = None) -> Any:
    from backend.app.orchestrator.fleet.envelope import StepResult

    return StepResult("done", None, None, None, "", 4, usage=usage)


class TestFleetStepRecordsOnce:
    async def test_a_sub_step_records_once_for_its_own_provider(
        self, monkeypatch: pytest.MonkeyPatch, recording_governor: _RecordingGovernor
    ) -> None:
        tool = _dispatch_tool(
            monkeypatch,
            _Recorder(_envelope({"input_tokens": 30, "output_tokens": 12})),
            provider="codex",
        )

        out = await tool({"name": "coder", "prompt": "work"})

        assert not out.get("is_error"), out
        assert recording_governor.token_calls == [("codex", 42)]

    async def test_a_step_that_reported_no_usage_is_not_charged_a_guess(
        self, monkeypatch: pytest.MonkeyPatch, recording_governor: _RecordingGovernor
    ) -> None:
        tool = _dispatch_tool(monkeypatch, _Recorder(_envelope(None)))

        await tool({"name": "coder", "prompt": "work"})

        assert recording_governor.token_calls == []


class TestAutoRouting:
    async def test_auto_resolves_to_the_provider_with_the_most_headroom(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gov = a_governor(tmp_path)
        gov.record("claude", reported={"rate_limit_type": "five_hour", "utilization": 0.9})
        gov.record("codex", reported={"rate_limit_type": "five_hour", "utilization": 0.1})
        monkeypatch.setattr(quota_mod, "get_governor", lambda: gov)
        recorder = _Recorder(_envelope(None))
        tool = _dispatch_tool(monkeypatch, recorder, provider="auto")

        out = await tool({"name": "coder", "prompt": "work"})

        assert not out.get("is_error"), out
        assert [cfg.provider for cfg in recorder.role_cfgs] == ["codex"]

    async def test_auto_honours_an_explicit_candidate_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gov = a_governor(tmp_path)
        gov.record("claude", reported={"rate_limit_type": "five_hour", "utilization": 0.9})
        gov.record("codex", reported={"rate_limit_type": "five_hour", "utilization": 0.1})
        monkeypatch.setattr(quota_mod, "get_governor", lambda: gov)
        recorder = _Recorder(_envelope(None))
        tool = _dispatch_tool(
            monkeypatch,
            recorder,
            provider="auto",
            metadata={"providers": ["claude", "nonsense"]},
        )

        await tool({"name": "coder", "prompt": "work"})

        assert [cfg.provider for cfg in recorder.role_cfgs] == ["claude"]

    async def test_a_non_auto_provider_is_never_rerouted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gov = a_governor(tmp_path)
        gov.record("claude", reported={"rate_limit_type": "five_hour", "utilization": 0.99})
        monkeypatch.setattr(quota_mod, "get_governor", lambda: gov)
        recorder = _Recorder(_envelope(None))
        tool = _dispatch_tool(monkeypatch, recorder, provider="claude")

        out = await tool({"name": "coder", "prompt": "work"})

        assert not out.get("is_error"), out
        assert [cfg.provider for cfg in recorder.role_cfgs] == ["claude"]

    async def test_everything_exhausted_refuses_honestly_and_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        clock = Clock()
        gov = Governor(tmp_path / "quota.json", clock=clock)
        for name in ("claude", "codex"):
            gov.record(
                name,
                reported={
                    "rate_limit_type": "five_hour",
                    "utilization": 0.99,
                    "resets_at": clock.now + 1800,
                },
            )
        monkeypatch.setattr(quota_mod, "get_governor", lambda: gov)
        recorder = _Recorder(_envelope(None))
        tool = _dispatch_tool(monkeypatch, recorder, provider="auto")

        out = await tool({"name": "coder", "prompt": "work"})

        assert out["is_error"] is True
        text = out["content"][0]["text"]
        assert "claude" in text and "codex" in text
        assert "queue" in text.lower()
        assert recorder.role_cfgs == [], "a refused dispatch still ran the step"


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/system/quota
# ─────────────────────────────────────────────────────────────────────────────


class TestQuotaRoute:
    async def test_the_route_returns_the_snapshot_plus_queue_suggested(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        from backend.app.routes import system as system_routes

        get_governor().record("claude", tokens=100)

        body = await system_routes.get_system_quota()

        assert body["queue_suggested"] is False
        assert body["queue_threshold"] == QUEUE_THRESHOLD
        assert body["providers"]["claude"]["headroom"] == 1.0
        assert body["providers"]["claude"]["confidence"] == "unknown"
        assert "generated_at" in body

    async def test_an_unrecorded_subscription_still_appears_as_unknown(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        """A provider missing from the meter reads as "no such subscription";
        the truth is "nothing has measured it yet"."""
        from backend.app.routes import system as system_routes

        body = await system_routes.get_system_quota()

        assert set(body["providers"]) == set(governed_providers())
        for info in body["providers"].values():
            assert info["headroom"] == 1.0
            assert info["confidence"] == "unknown"
            assert info["windows"] == []

    async def test_queue_suggested_flips_when_every_provider_is_spent(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        from backend.app.routes import system as system_routes

        gov = get_governor()
        for name in governed_providers():
            gov.record(
                name, reported={"rate_limit_type": "five_hour", "utilization": 0.99}
            )

        body = await system_routes.get_system_quota()

        assert body["queue_suggested"] is True

    async def test_the_route_reads_the_file_the_governor_writes(
        self, tmp_path: Path, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same split the usage log had to be protected from: a route that
        resolved its own path would report a quota nobody is recording into."""
        from backend.app.routes import system as system_routes

        custom = tmp_path / "shared-quota.json"
        monkeypatch.setenv("QUOTA_PATH", str(custom))

        get_governor().record(
            "codex", reported={"rate_limit_type": "weekly", "utilization": 0.5}
        )
        body = await system_routes.get_system_quota()

        assert custom.exists()
        assert body["providers"]["codex"]["headroom"] == pytest.approx(0.5)
