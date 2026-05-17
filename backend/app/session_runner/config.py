"""Tuning constants for the session runner.

No dependencies — safe to import from anywhere in the package.
"""
from __future__ import annotations

# Bounded ring buffer for replay-on-reconnect. ~256 events covers a typical
# 4-step fleet pipeline (roughly 30-80 events) with margin for chatty turns.
# Larger means more memory per idle session; smaller means a slow reconnect
# can't fully resume and has to refetch via /messages.
REPLAY_BUFFER_SIZE = 256

# Per-subscriber queue cap. A turn that emits faster than the WS can drain
# would block on `put_nowait` — we drop instead so the producer can't be held
# hostage by one slow viewer. The dropped subscriber re-syncs on its next
# reconnect via the replay buffer.
SUBSCRIBER_QUEUE_MAX = 512

# Back-compat aliases — the original module exposed these underscore names.
_REPLAY_BUFFER_SIZE = REPLAY_BUFFER_SIZE
_SUBSCRIBER_QUEUE_MAX = SUBSCRIBER_QUEUE_MAX
