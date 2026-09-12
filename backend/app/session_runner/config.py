"""Tuning constants for the session runner.

No dependencies — safe to import from anywhere in the package.
"""
from __future__ import annotations

# Bounded ring buffer for replay-on-reconnect.
#
# INVARIANT: REPLAY_BUFFER_SIZE > SUBSCRIBER_QUEUE_MAX. The ring has to be able
# to cover any gap a subscriber queue can open, or `?since=` is a lie: the ring
# was 256 against a 512-deep queue, so by the time a viewer had dropped a single
# event the replay could no longer reach back far enough to hand it over, and
# the reconnect silently resumed past the hole. Asserted by
# `test_event_integrity.py` so a future retune cannot quietly invert it.
#
# Cost is memory per live session (a few hundred KB of already-allocated event
# dicts at 2048 entries), paid only while a session has a runner.
REPLAY_BUFFER_SIZE = 2048

# Per-subscriber queue cap. A turn that emits faster than the WS can drain
# would block on `put_nowait` — we drop instead so the producer can't be held
# hostage by one slow viewer. Every drop is reported to that subscriber as a
# `stream.gap` event, and the ring above can always cover it.
SUBSCRIBER_QUEUE_MAX = 512

# Back-compat aliases — the original module exposed these underscore names.
_REPLAY_BUFFER_SIZE = REPLAY_BUFFER_SIZE
_SUBSCRIBER_QUEUE_MAX = SUBSCRIBER_QUEUE_MAX
