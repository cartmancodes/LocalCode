from __future__ import annotations

import time

import pytest

from backend.app.core.quota import (
    EXHAUSTED_AT,
    QuotaGovernor,
    QuotaSnapshot,
    QuotaWindow,
    parse_rate_limit,
)


def test_parse_codex_windows() -> None:
    now = time.time()
    snap = parse_rate_limit(
        "codex",
        {
            "plan": "plus",
            "primary": {"usedPercent": 42, "windowMinutes": 300, "resetsAt": now + 1800},
            "secondary": {"usedPercent": 10, "windowMinutes": 10080, "resetsAt": now + 86400},
            "reached": None,
        },
    )
    assert snap.plan == "plus" and snap.engine == "codex"
    assert [w.label for w in snap.windows] == ["primary", "secondary"]
    assert snap.windows[0].used_fraction == 0.42
    assert snap.windows[0].duration_minutes == 300
    assert snap.headroom == pytest.approx(0.58)  # the tightest window wins
    assert snap.status == "ok"
    assert 1700 < snap.resets_in_seconds < 1900


def test_parse_codex_reached_is_exhausted() -> None:
    snap = parse_rate_limit(
        "codex", {"plan": "plus", "primary": {"usedPercent": 100}, "reached": "primary"}
    )
    assert snap.headroom == 0.0 and snap.status == "exhausted" and snap.reached == "primary"


def test_parse_claude_shapes() -> None:
    now = time.time()
    ok = parse_rate_limit(
        "claude",
        {"status": "allowed", "utilization": 0.15, "resetsAt": now + 600, "type": "five_hour"},
    )
    assert ok.headroom == 0.85 and ok.status == "ok" and ok.windows[0].label == "five_hour"
    warn = parse_rate_limit("claude", {"status": "allowed_warning"})
    assert warn.status == "warning"  # status alone implies the warning line
    rejected = parse_rate_limit("claude", {"status": "rejected", "type": "weekly"})
    assert (
        rejected.headroom == 0.0 and rejected.status == "exhausted" and rejected.reached == "weekly"
    )


def test_percent_and_millisecond_tolerance() -> None:
    ms = parse_rate_limit(
        "codex", {"primary": {"usedPercent": 50, "resetsAt": (time.time() + 60) * 1000}}
    )
    assert 55 < ms.resets_in_seconds <= 60
    fraction = parse_rate_limit("codex", {"primary": {"usedPercent": 0.5}})
    assert fraction.headroom == 0.5  # a 0–1 value is read as a fraction, not 0.5%
    junk = parse_rate_limit("codex", {"primary": {"usedPercent": "nonsense"}})
    assert junk.windows == [] and junk.status == "unknown"


def test_unknown_engine_is_available_not_blocked() -> None:
    gov = QuotaGovernor()
    assert gov.headroom("claude") == 1.0 and gov.status("claude") == "unknown"
    assert gov.is_exhausted("claude") is False
    assert gov.describe() == "no quota reported yet"


def test_governor_ingests_session_events() -> None:
    gov = QuotaGovernor()
    listener = gov.observe("codex")
    listener({"type": "agent_start"})
    listener({"type": "rate_limit", "info": {"plan": "plus", "primary": {"usedPercent": 80}}})
    assert gov.status("codex") == "warning" and gov.headroom("codex") == pytest.approx(0.2)
    listener({"type": "rate_limit", "info": {"plan": "plus", "primary": {"usedPercent": 99}}})
    assert gov.status("codex") == "exhausted"
    assert "codex: 1% left (exhausted" in gov.describe()
    assert gov.to_dict()["codex"]["plan"] == "plus"


def test_choose_prefers_then_falls_back() -> None:
    gov = QuotaGovernor()
    gov.record("claude", {"status": "allowed", "utilization": 0.1})
    gov.record("codex", {"primary": {"usedPercent": 60}})
    engine, reason = gov.choose(["claude", "codex"], prefer="claude")
    assert engine == "claude" and "90% headroom" in reason

    gov.record("claude", {"status": "allowed", "utilization": 0.95})
    engine, reason = gov.choose(["claude", "codex"], prefer="claude")
    assert engine == "codex" and "routing to codex" in reason and "5%" in reason

    engine, reason = gov.choose(["claude", "codex"])
    assert engine == "codex"  # no preference → most headroom


def test_choose_when_everything_is_spent() -> None:
    gov = QuotaGovernor()
    gov.record("claude", {"status": "rejected"})
    gov.record(
        "codex",
        {"primary": {"usedPercent": 100, "resetsAt": time.time() + 3600}, "reached": "primary"},
    )
    engine, reason = gov.choose(["claude", "codex"])
    assert engine is None and "every engine is at its limit" in reason
    # tie on headroom → the one with a known, sooner reset is named
    assert "codex is closest" in reason and "resets in 59 min" in reason
    assert gov.choose([]) == (None, "no candidate engines")


def test_exhausted_threshold_boundary() -> None:
    gov = QuotaGovernor()
    gov.record("codex", {"primary": {"usedPercent": (1 - EXHAUSTED_AT) * 100}})
    assert gov.is_exhausted("codex") is True
    gov.record("codex", {"primary": {"usedPercent": 90}})
    assert gov.is_exhausted("codex") is False and gov.status("codex") == "warning"


def test_snapshot_serialisation_round_trips() -> None:
    snap = QuotaSnapshot(
        "codex", [QuotaWindow(0.25, 300, time.time() + 900, "primary")], plan="pro"
    )
    data = snap.to_dict()
    assert data["engine"] == "codex" and data["plan"] == "pro" and data["status"] == "ok"
    assert data["headroom"] == 0.75
    assert data["windows"][0]["durationMinutes"] == 300
    assert 800 < data["windows"][0]["resetsInSeconds"] <= 900
