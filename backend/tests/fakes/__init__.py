"""Test doubles and measurement helpers shared across the backend suite.

Kept separate from the test modules so a helper more than one task needs —
the event-loop stall detector, for one — has a single implementation rather
than a copy per test file that drifts.
"""
from __future__ import annotations
