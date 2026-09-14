"""Quota governor — what actually runs out on a subscription.

Both engines already report their limits; they just disagree on shape.
Claude's SDK emits ``RateLimitEvent`` with a status, a utilization fraction
and a reset timestamp. Codex's app-server emits ``account/rateLimits/updated``
with a plan type and up to two rolling windows given as *used percent* plus a
window duration (Plus is a five-hour primary window with a weekly secondary).

Both normalise to the same thing: **how much headroom is left, and when does
it come back**. That is the number a router can act on; the per-turn dollar
figure the old UI showed is decoration under OAuth, because nobody is billed
per token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["ok", "warning", "exhausted", "unknown"]

# Below this fraction of a window remaining we call it a warning; at or below
# EXHAUSTED_AT we stop routing new work to that engine.
WARNING_AT = 0.20
EXHAUSTED_AT = 0.02


@dataclass
class QuotaWindow:
    """One rolling limit window."""

    used_fraction: float  # 0.0 – 1.0
    duration_minutes: int | None = None
    resets_at: float | None = None  # unix seconds
    label: str = ""

    @property
    def remaining_fraction(self) -> float:
        # Rounded: percent → fraction → remainder accumulates float error that
        # lands exactly on the threshold comparisons (0.98 used → 0.020000000000000018).
        return round(max(0.0, min(1.0, 1.0 - self.used_fraction)), 6)

    @property
    def resets_in_seconds(self) -> float | None:
        if self.resets_at is None:
            return None
        return max(0.0, self.resets_at - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "usedFraction": round(self.used_fraction, 4),
            "remainingFraction": round(self.remaining_fraction, 4),
            "durationMinutes": self.duration_minutes,
            "resetsAt": self.resets_at,
            "resetsInSeconds": self.resets_in_seconds,
        }


@dataclass
class QuotaSnapshot:
    """What one engine last told us about its limits."""

    engine: str
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: str | None = None
    updated_at: float = field(default_factory=time.time)
    reached: str | None = None  # the vendor's own "you hit this limit" marker
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def headroom(self) -> float:
        """Remaining fraction of the *tightest* window. 1.0 when unknown —
        an engine that has not reported is treated as available, not blocked."""
        if self.reached:
            return 0.0
        if not self.windows:
            return 1.0
        return min(w.remaining_fraction for w in self.windows)

    @property
    def status(self) -> Status:
        if not self.windows and not self.reached:
            return "unknown"
        headroom = self.headroom
        if headroom <= EXHAUSTED_AT:
            return "exhausted"
        if headroom <= WARNING_AT:
            return "warning"
        return "ok"

    @property
    def resets_in_seconds(self) -> float | None:
        """When the tightest window comes back."""
        candidates = [
            w.resets_in_seconds
            for w in self.windows
            if w.resets_in_seconds is not None and w.remaining_fraction <= WARNING_AT
        ]
        if not candidates:
            candidates = [
                w.resets_in_seconds for w in self.windows if w.resets_in_seconds is not None
            ]
        return min(candidates) if candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "plan": self.plan,
            "status": self.status,
            "headroom": round(self.headroom, 4),
            "resetsInSeconds": self.resets_in_seconds,
            "updatedAt": self.updated_at,
            "windows": [w.to_dict() for w in self.windows],
            "reached": self.reached,
        }


def _as_fraction(value: Any) -> float | None:
    """Accept a percent (0–100) or a fraction (0–1); both appear in the wild."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return min(1.0, number / 100.0 if number > 1.0 else number)


def _as_epoch(value: Any) -> float | None:
    """Vendors send seconds or milliseconds; both are plausible timestamps."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number / 1000.0 if number > 1e11 else number


def parse_rate_limit(engine: str, info: dict[str, Any]) -> QuotaSnapshot:
    """Normalise either engine's ``rate_limit`` event payload."""
    snapshot = QuotaSnapshot(engine=engine, raw=dict(info or {}))
    if not info:
        return snapshot

    # Codex shape: {plan, primary: {usedPercent, windowMinutes, resetsAt}, secondary, reached}
    if "primary" in info or "secondary" in info or "plan" in info:
        snapshot.plan = info.get("plan")
        snapshot.reached = info.get("reached") or None
        for label in ("primary", "secondary"):
            window = info.get(label)
            if not isinstance(window, dict):
                continue
            used = _as_fraction(window.get("usedPercent", window.get("used_percent")))
            if used is None:
                continue
            snapshot.windows.append(
                QuotaWindow(
                    used_fraction=used,
                    duration_minutes=window.get("windowMinutes")
                    or window.get("windowDurationMins"),
                    resets_at=_as_epoch(window.get("resetsAt")),
                    label=label,
                )
            )
        return snapshot

    # Claude shape: {status, utilization, resetsAt, type}
    status = str(info.get("status") or "").lower()
    used = _as_fraction(info.get("utilization"))
    if used is None:
        used = {"rejected": 1.0, "allowed_warning": 1.0 - WARNING_AT}.get(status)
    if used is not None:
        snapshot.windows.append(
            QuotaWindow(
                used_fraction=used,
                resets_at=_as_epoch(info.get("resetsAt") or info.get("resets_at")),
                label=str(info.get("type") or "primary"),
            )
        )
    if status == "rejected":
        snapshot.reached = str(info.get("type") or "rate_limit")
    return snapshot


class QuotaGovernor:
    """Tracks every engine's headroom and answers "who should run this?".

    Subscribe :meth:`observe` to a session's event stream; it consumes the
    ``rate_limit`` events both engines already emit and ignores everything
    else.
    """

    def __init__(
        self, *, warning_at: float = WARNING_AT, exhausted_at: float = EXHAUSTED_AT
    ) -> None:
        self.warning_at = warning_at
        self.exhausted_at = exhausted_at
        self._snapshots: dict[str, QuotaSnapshot] = {}

    # ── ingest ─────────────────────────────────────────────────────────

    def record(self, engine: str, info: dict[str, Any]) -> QuotaSnapshot:
        snapshot = parse_rate_limit(engine, info)
        self._snapshots[engine] = snapshot
        return snapshot

    def observe(self, engine: str) -> Any:
        """Return a listener for ``AgentSession.subscribe`` bound to ``engine``."""

        def listener(event: dict[str, Any]) -> None:
            if event.get("type") == "rate_limit":
                self.record(engine, event.get("info") or {})

        return listener

    # ── read ───────────────────────────────────────────────────────────

    def snapshot(self, engine: str) -> QuotaSnapshot | None:
        return self._snapshots.get(engine)

    def headroom(self, engine: str) -> float:
        snapshot = self._snapshots.get(engine)
        return snapshot.headroom if snapshot else 1.0

    def status(self, engine: str) -> Status:
        snapshot = self._snapshots.get(engine)
        return snapshot.status if snapshot else "unknown"

    def is_exhausted(self, engine: str) -> bool:
        return self.headroom(engine) <= self.exhausted_at

    def to_dict(self) -> dict[str, Any]:
        return {engine: snap.to_dict() for engine, snap in self._snapshots.items()}

    # ── route ──────────────────────────────────────────────────────────

    def choose(self, candidates: list[str], *, prefer: str | None = None) -> tuple[str | None, str]:
        """Pick an engine from ``candidates``.

        ``prefer`` wins while it has headroom above the warning line; past
        that the engine with the most headroom takes the work. Returns
        ``(engine, reason)``; ``engine`` is ``None`` when every candidate is
        exhausted, and the reason says when the best one comes back.
        """
        if not candidates:
            return None, "no candidate engines"
        usable = [e for e in candidates if not self.is_exhausted(e)]
        if not usable:
            # Rank by headroom, then by whichever window comes back soonest —
            # when both are at zero, "when does it return" is the actionable part.
            def recovery(engine: str) -> tuple[float, float]:
                snapshot = self._snapshots.get(engine)
                resets = snapshot.resets_in_seconds if snapshot else None
                return (-self.headroom(engine), resets if resets is not None else float("inf"))

            best = min(candidates, key=recovery)
            snapshot = self._snapshots.get(best)
            resets = snapshot.resets_in_seconds if snapshot else None
            when = f" (resets in {int(resets // 60)} min)" if resets else ""
            return None, f"every engine is at its limit; {best} is closest{when}"
        if prefer in usable and self.headroom(prefer) > self.warning_at:
            return prefer, f"{prefer} has {self.headroom(prefer):.0%} headroom"
        best = max(usable, key=self.headroom)
        if prefer and best != prefer:
            return best, (
                f"{prefer} is down to {self.headroom(prefer):.0%}; "
                f"routing to {best} at {self.headroom(best):.0%}"
            )
        return best, f"{best} has {self.headroom(best):.0%} headroom"

    def describe(self) -> str:
        """One human line per engine, for a status bar or a degraded reply."""
        if not self._snapshots:
            return "no quota reported yet"
        parts = []
        for engine, snap in sorted(self._snapshots.items()):
            resets = snap.resets_in_seconds
            tail = f", resets in {int(resets // 60)} min" if resets else ""
            parts.append(f"{engine}: {snap.headroom:.0%} left ({snap.status}{tail})")
        return "; ".join(parts)
