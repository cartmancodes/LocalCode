"""The quota governor: remaining subscription headroom, as a number.

Two subscriptions are two independently exhaustible budgets on different
clocks. ``usage.py`` answers "what did this turn cost?" — a figure that bills
nobody, because neither vendor charges per token on a subscription. The number
that actually steers work is *how much of each plan's window is left*, and that
is what this module keeps.

**What a window is.** A provider has one or more rolling windows (a table, not
a branch: :data:`DEFAULT_WINDOWS`). Claude's plan resets on a 5-hour cycle;
Codex has a 5-hour rolling window *and* a weekly cap. Each :class:`WindowState`
knows how long it runs, when the current one opened, how much has gone into it,
and — only if a vendor said so — what the ceiling is.

**Two sources, in strict precedence.** A vendor-reported payload (Claude's
``RateLimitEvent``, whatever Codex's ``turn/completed`` carries) is
authoritative: it replaces ``used`` / ``limit`` / ``resets_at`` and sets
``source="provider"``. Absent that, tokens accumulate locally with an unknown
limit, so ``headroom()`` reads ``1.0`` with ``confidence="unknown"`` — the UI
must not draw a full bar as if it had been measured.

The CLI emits a rate-limit event only when the status *transitions*, so absence
of an event is "no change", never "reset": a provider-sourced window stands
until its ``resets_at`` passes, at which point it rolls and falls back to local
accumulation. That is also why the state is persisted — a restart between two
transitions would otherwise forget the only measurement anyone has.

**Units stay honest.** A provider-sourced window is a fraction (``used`` is the
reported utilization against a limit of 1.0); a local one counts tokens against
no limit at all. ``unit`` says which, so nothing downstream averages the two.

**Who may call this.** The main server process only. ``quota.json`` is a
read-modify-write file, and the fleet's sub-providers run in worker PROCESSES
(``orchestrator/fleet/subproc.py``) where two writers would lose increments and
race the rename. Providers therefore EMIT — token usage on ``assistant.done``,
a ``quota.limit`` Event for a vendor-reported limit — and the two main-process
sites that see those events record them: ``session_runner/turn.py`` for a
direct turn and ``orchestrator/dispatch.py`` beside ``budget.spend`` for a
fleet sub-step. Exactly one record per turn; a double count shows inflated use,
which is precisely the dishonesty this module exists to remove.

Nothing here may raise into a turn. A governor that estimates badly is a bad
number; a governor that takes the turn down is a bad harness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import get_settings

logger = logging.getLogger(__name__)

# Below this fraction of a window left, ``choose`` refuses and the orchestrator
# is told to queue the work instead. A module constant, not a setting: it is
# the definition of "practically empty", not an operator preference. Exactly at
# the threshold does NOT queue — only below it.
QUEUE_THRESHOLD = 0.05

FIVE_HOURS_S = 5 * 60 * 60.0
ONE_WEEK_S = 7 * 24 * 60 * 60.0

# Per-provider default windows, as a TABLE. Adding a provider is a row here;
# no caller anywhere branches on a provider name.
DEFAULT_WINDOWS: dict[str, tuple[tuple[str, float], ...]] = {
    # One 5-hour window by default. Claude's CLI reports up to four concurrent
    # windows (five_hour, seven_day, seven_day_opus, seven_day_sonnet, plus
    # overage) and each reported ``rate_limit_type`` adds or updates its own
    # WindowState on top of this one — see ``record``.
    "claude": (("five_hour", FIVE_HOURS_S),),
    # A 5-hour rolling window AND a weekly cap, per the plan spec.
    "codex": (("five_hour", FIVE_HOURS_S), ("weekly", ONE_WEEK_S)),
}
# A provider with no row (``opencode``, or one added later) still gets a window
# rather than silently having no ledger at all.
_FALLBACK_WINDOWS: tuple[tuple[str, float], ...] = (("five_hour", FIVE_HOURS_S),)

# How long a window a vendor's own window NAME implies, for the windows that
# only exist because a vendor reported them (Claude's model-specific seven-day
# caps). Only used for the length shown in the snapshot and for the local
# accumulation this window falls back to once the vendor's reset passes — while
# it is provider-sourced it rolls on ``resets_at``, not on this. A name that is
# not here is treated as a 5-hour window, which is the tighter guess.
REPORTED_WINDOW_LENGTHS: dict[str, float] = {
    "five_hour": FIVE_HOURS_S,
    "seven_day": ONE_WEEK_S,
    "seven_day_opus": ONE_WEEK_S,
    "seven_day_sonnet": ONE_WEEK_S,
    "weekly": ONE_WEEK_S,
    "overage": FIVE_HOURS_S,
}

SOURCE_LOCAL = "local"
SOURCE_PROVIDER = "provider"

UNIT_TOKENS = "tokens"
UNIT_FRACTION = "fraction"

CONFIDENCE_UNKNOWN = "unknown"
CONFIDENCE_REPORTED = "reported"

# The token counts a ``usage`` dict carries. The same four keys ``collect.py``
# pulls out of a sub-provider's ``assistant.done`` — named here rather than
# summing every int in the dict, because that dict also carries ``cost_usd``.
TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def default_quota_path() -> Path:
    """``~/.localcode/quota.json``, resolved when a :class:`Governor` is built
    — never at import time, for the same reason as
    ``usage.default_usage_log_path``: a test redirects ``HOME``, and a path
    frozen at import would still point at the developer's real one."""
    return Path.home() / ".localcode" / "quota.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write to ``.tmp``, fsync, rename — mirroring
    ``storage/sessions.py:_atomic_write_text``, which is the house idiom for
    this. Mirrored rather than imported so this module stays a leaf (importing
    ``storage.sessions`` would drag its import-time ``Path.home()`` constants
    in behind it); if that idiom changes, both copies change.

    A crash mid-write leaves either the old file or the new one, never a torn
    read-modify-write of the quota ledger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX


def _as_float(value: Any) -> float | None:
    """``float(value)`` when that means something finite, else ``None``.

    ``None`` covers every way a vendor payload can be malformed — a string, a
    list, ``NaN``, ``inf`` — in one place, so ``record`` has a single test for
    "is this usable?" instead of a shape check per field.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def tokens_from_usage(usage: Any) -> int:
    """Total tokens in a provider's ``usage`` dict — 0 for anything unusable.

    An estimate, deliberately: nobody publishes the weighting a subscription
    window actually applies, so all four counts are summed and the result is
    only ever used for a window whose limit is unknown (where it orders
    magnitudes) or alongside a vendor figure that overrides it outright.
    """
    if not isinstance(usage, Mapping):
        return 0
    total = 0
    for key in TOKEN_KEYS:
        value = _as_float(usage.get(key))
        if value is not None:
            total += int(value)
    return total


def governed_providers() -> tuple[str, ...]:
    """The providers the governor keeps windows for by default — the candidate
    set ``provider: "auto"`` resolves over, and what ``GET /api/system/quota``
    reports on before anything has been recorded."""
    return tuple(DEFAULT_WINDOWS)


@dataclass
class WindowState:
    """One rolling window of one provider's plan."""

    provider: str
    # Which window this is: a key from DEFAULT_WINDOWS, or a vendor's own
    # ``rate_limit_type`` (``seven_day_opus`` and friends).
    key: str
    window_s: float  # rolling window length
    started_at: float  # when the current window opened
    used: float  # consumed units in this window, in ``unit``
    limit: float | None  # None = unknown
    source: str  # "provider" | "local"
    resets_at: float | None
    unit: str = UNIT_TOKENS

    @property
    def confidence(self) -> str:
        return CONFIDENCE_UNKNOWN if not self.limit else CONFIDENCE_REPORTED

    def headroom(self) -> float:
        """Fraction of this window still available, clamped to ``0.0..1.0``.

        ``1.0`` when the limit is unknown — not because the window is empty,
        but because nothing measured it. ``confidence`` is what says so.
        """
        if not self.limit or self.limit <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - (self.used / self.limit)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "key": self.key,
            "window_s": self.window_s,
            "started_at": self.started_at,
            "used": self.used,
            "limit": self.limit,
            "source": self.source,
            "resets_at": self.resets_at,
            "unit": self.unit,
            "headroom": self.headroom(),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, provider: str, data: Any) -> WindowState | None:
        """Rebuild one window from ``quota.json``, or ``None`` if the entry is
        unusable. Skipping one bad entry keeps the rest of a hand-edited or
        version-skewed file readable, where raising would throw all of it away.
        """
        if not isinstance(data, Mapping):
            return None
        key = data.get("key")
        window_s = _as_float(data.get("window_s"))
        started_at = _as_float(data.get("started_at"))
        used = _as_float(data.get("used"))
        if not isinstance(key, str) or not key or window_s is None:
            return None
        if started_at is None or used is None:
            return None
        source = data.get("source")
        unit = data.get("unit")
        return cls(
            provider=provider,
            key=key,
            window_s=window_s,
            started_at=started_at,
            used=used,
            limit=_as_float(data.get("limit")),
            source=SOURCE_PROVIDER if source == SOURCE_PROVIDER else SOURCE_LOCAL,
            resets_at=_as_float(data.get("resets_at")),
            unit=UNIT_FRACTION if unit == UNIT_FRACTION else UNIT_TOKENS,
        )


@dataclass
class QuotaSnapshot:
    windows: dict[str, list[WindowState]]  # provider -> windows (5h, weekly)
    generated_at: float


class Governor:
    """The ledger. One per process — see :func:`get_governor`."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # ``None`` is the only safe default-time value: the real Path.home()
        # lookup happens here, at construction. See ``default_quota_path``.
        self.path = path if path is not None else default_quota_path()
        self._clock = clock
        self._windows: dict[str, dict[str, WindowState]] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("quota state at %s is unreadable (%s); starting fresh", self.path, exc)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("quota state at %s is corrupt (%s); starting fresh", self.path, exc)
            return
        windows = data.get("windows") if isinstance(data, Mapping) else None
        if not isinstance(windows, Mapping):
            logger.warning("quota state at %s has an unexpected shape; starting fresh", self.path)
            return
        for provider, entries in windows.items():
            if not isinstance(provider, str) or not isinstance(entries, list):
                continue
            for entry in entries:
                window = WindowState.from_dict(provider, entry)
                if window is not None:
                    self._windows.setdefault(provider, {})[window.key] = window

    def _save(self) -> None:
        """Persist, and never let a failure reach the turn this hangs off."""
        payload = {
            "version": 1,
            "generated_at": self._clock(),
            "windows": {
                provider: [w.to_dict() for w in windows.values()]
                for provider, windows in self._windows.items()
            },
        }
        try:
            _atomic_write_text(self.path, json.dumps(payload, sort_keys=True))
        except OSError as exc:
            logger.warning("could not persist quota state to %s: %s", self.path, exc)

    # ── recording ───────────────────────────────────────────────────────────

    def _defaults_for(self, provider: str) -> tuple[tuple[str, float], ...]:
        return DEFAULT_WINDOWS.get(provider, _FALLBACK_WINDOWS)

    def _windows_for(self, provider: str) -> dict[str, WindowState]:
        """This provider's windows, materialising its defaults on first sight
        and rolling any that have expired."""
        now = self._clock()
        existing = self._windows.setdefault(provider, {})
        for key, window_s in self._defaults_for(provider):
            if key not in existing:
                existing[key] = WindowState(
                    provider=provider,
                    key=key,
                    window_s=window_s,
                    started_at=now,
                    used=0.0,
                    limit=None,
                    source=SOURCE_LOCAL,
                    resets_at=None,
                    unit=UNIT_TOKENS,
                )
        for window in existing.values():
            self._roll(window, now)
        return existing

    def _roll(self, window: WindowState, now: float) -> None:
        """Open a fresh window when this one is over.

        A provider-sourced window rolls on the vendor's own ``resets_at`` and
        reverts to local accumulation: the measurement it held describes a
        window that no longer exists, and the next transition event is what
        replaces it. A local window rolls on its own length.
        """
        if window.source == SOURCE_PROVIDER:
            if window.resets_at is not None and now >= window.resets_at:
                window.started_at = now
                window.used = 0.0
                window.limit = None
                window.source = SOURCE_LOCAL
                window.resets_at = None
                window.unit = UNIT_TOKENS
            return
        if now - window.started_at >= window.window_s:
            window.started_at = now
            window.used = 0.0

    def record(
        self,
        provider: str,
        *,
        tokens: int = 0,
        reported: Any = None,
    ) -> None:
        """Record one turn (or sub-step) against ``provider``'s windows.

        ``reported`` — a vendor payload, from a ``quota.limit`` event — is
        authoritative and replaces the named window outright. Anything else,
        including a malformed payload, accumulates ``tokens`` locally: a shape
        change in a vendor's schema must cost precision, never the turn.

        A provider-sourced window ignores local tokens until it rolls, because
        its units are fractions and the vendor's figure already counts them.
        """
        try:
            windows = self._windows_for(provider)
            applied = self._apply_reported(provider, windows, reported)
            if not applied and tokens:
                for window in windows.values():
                    if window.source == SOURCE_LOCAL:
                        window.used += tokens
            self._save()
        except Exception:  # pragma: no cover — belt and braces; see the module docstring
            logger.exception("quota record failed for %s", provider)

    def _apply_reported(
        self,
        provider: str,
        windows: dict[str, WindowState],
        reported: Any,
    ) -> bool:
        """Apply a vendor payload; ``False`` if there was nothing usable in it.

        Read with the same defensiveness as ``usage.parse_claude_usage``: the
        one field that has to be there is a finite utilization, because without
        it there is no measurement — a payload carrying only a status says the
        window changed but not to what.
        """
        if not isinstance(reported, Mapping):
            return False
        utilization = _as_float(reported.get("utilization"))
        if utilization is None:
            return False
        key = reported.get("rate_limit_type")
        if not isinstance(key, str) or not key:
            # Named nothing — attribute it to the provider's primary window
            # rather than dropping the only measurement anyone has.
            key = self._defaults_for(provider)[0][0]
        now = self._clock()
        window_s = dict(self._defaults_for(provider)).get(
            key, REPORTED_WINDOW_LENGTHS.get(key, FIVE_HOURS_S)
        )
        resets_at = _as_float(reported.get("resets_at"))
        existing = windows.get(key)
        windows[key] = WindowState(
            provider=provider,
            key=key,
            window_s=window_s,
            started_at=existing.started_at if existing is not None else now,
            # utilization IS the fraction consumed, so the limit is 1.0 and the
            # unit says so — nothing downstream may average it with a token
            # count.
            used=utilization,
            limit=1.0,
            source=SOURCE_PROVIDER,
            resets_at=resets_at,
            unit=UNIT_FRACTION,
        )
        return True

    # ── reading ─────────────────────────────────────────────────────────────

    def headroom(self, provider: str) -> float:
        """Fraction of ``provider``'s plan still available: the MINIMUM across
        its windows, because the tightest one is what actually stops work."""
        windows = self._windows.get(provider)
        if not windows:
            return 1.0
        now = self._clock()
        for window in windows.values():
            self._roll(window, now)
        return min(w.headroom() for w in windows.values())

    def confidence(self, provider: str) -> str:
        windows = self._windows.get(provider) or {}
        if any(w.confidence == CONFIDENCE_REPORTED for w in windows.values()):
            return CONFIDENCE_REPORTED
        return CONFIDENCE_UNKNOWN

    def resets_at(self, provider: str) -> float | None:
        """The earliest reset among this provider's windows, when any is known."""
        windows = (self._windows.get(provider) or {}).values()
        stamps = [w.resets_at for w in windows if w.resets_at is not None]
        return min(stamps) if stamps else None

    def choose(self, candidates: Sequence[str]) -> str | None:
        """The candidate with the most headroom, or ``None`` when every one of
        them is below :data:`QUEUE_THRESHOLD`.

        Ties resolve to the first candidate — ``max`` keeps the first maximal
        element — so the caller's order is the tie-break and the choice is
        reproducible.
        """
        if not candidates:
            return None
        best = max(candidates, key=self.headroom)
        return None if self.headroom(best) < QUEUE_THRESHOLD else best

    def should_queue(self, candidates: Sequence[str]) -> bool:
        """Advice, not a queue: "every one of these is practically empty"."""
        return bool(candidates) and self.choose(candidates) is None

    def refusal(self, candidates: Sequence[str]) -> str:
        """The honest refusal. Names each candidate, what is left of it, and
        when it comes back — as a time a human can read — and says the work can
        be queued. No retry and no fallback to a provider below the bar: a
        silent downgrade is what turns "your plans are nearly spent" into an
        unexplained mid-plan failure.
        """
        lines = []
        for name in candidates:
            when = self.resets_at(name)
            resets = f"resets {_human_time(when)}" if when else "reset time unknown"
            lines.append(
                f"  - {name}: {self.headroom(name):.0%} left ({self.confidence(name)}), {resets}"
            )
        listed = "\n".join(lines) or "  - (no candidates configured)"
        return (
            "REFUSING to dispatch: every available subscription is at or near "
            "its cap.\n"
            f"{listed}\n"
            "STOP the workflow now and tell the user both subscriptions are "
            "nearly spent, naming the numbers above and when the earliest one "
            "resets. The work can be QUEUED and re-run after that reset — say "
            "so. Do NOT retry, and do NOT route around this."
        )

    def snapshot(self) -> QuotaSnapshot:
        now = self._clock()
        for windows in self._windows.values():
            for window in windows.values():
                self._roll(window, now)
        return QuotaSnapshot(
            windows={
                provider: list(windows.values())
                for provider, windows in self._windows.items()
            },
            generated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        """The snapshot as the API (and the meter) sees it.

        Every governed provider appears, whether or not anything has been
        recorded against it yet: a provider missing from the meter reads as "no
        such subscription", where the truth is "nothing has measured it". One
        with no windows reports ``headroom: 1.0`` with ``confidence:
        "unknown"``, which is exactly what the unmeasured bar is for.
        """
        snapshot = self.snapshot()
        providers = {
            provider: {
                "headroom": self.headroom(provider),
                "confidence": self.confidence(provider),
                "resets_at": self.resets_at(provider),
                "windows": [w.to_dict() for w in snapshot.windows.get(provider, [])],
            }
            for provider in dict.fromkeys([*governed_providers(), *snapshot.windows])
        }
        return {
            "generated_at": snapshot.generated_at,
            "queue_threshold": QUEUE_THRESHOLD,
            "providers": providers,
        }


def _human_time(stamp: float | None) -> str:
    """A local wall-clock time, because "1700003600" answers nobody's question
    about when they can work again."""
    if not stamp:
        return "unknown"
    try:
        return datetime.fromtimestamp(stamp).strftime("%H:%M on %a %d %b")
    except (OSError, OverflowError, ValueError):  # pragma: no cover — absurd stamps
        return "unknown"


@lru_cache(maxsize=1)
def get_governor() -> Governor:
    """The one governor this process records into and reports from.

    Cached like ``get_settings()`` — and, like it, resolved from settings when
    first called rather than at import. Tests must call
    ``get_governor.cache_clear()`` (the suite has an autouse fixture) or an
    instance built under one test's HOME serves the next one.
    """
    override = get_settings().quota_path
    path = Path(override).expanduser().resolve() if override else None
    return Governor(path)


async def record_turn(
    provider: str, *, tokens: int = 0, reported: Any = None
) -> None:
    """Record from the event loop without blocking it on disk.

    The write is offloaded exactly as ``usage.UsageLog.append`` is
    (``claude.py``'s ``asyncio.to_thread``), and every failure is swallowed
    with a log line: this hangs off a turn's terminal event, and a quota number
    is never worth a turn.
    """
    try:
        await asyncio.to_thread(
            get_governor().record, provider, tokens=tokens, reported=reported
        )
    except Exception:
        logger.warning("quota record for %s failed", provider, exc_info=True)


def candidates_from(values: Iterable[Any], allowed: Sequence[str]) -> list[str]:
    """The subset of ``values`` that names a provider in ``allowed``, in the
    order given. Used to read an agent's declared candidate list without
    letting a typo in a config become a dispatch to a provider that does not
    exist."""
    out: list[str] = []
    for value in values or ():
        name = str(value).strip()
        if name in allowed and name not in out:
            out.append(name)
    return out
