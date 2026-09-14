"""The eval foundation: a headless, governor-paced SWE-bench Verified runner.

The agent runs on the host through the vendor binaries exactly as in normal
use — the auth invariant is untouched — and only scoring runs in Docker via
SWE-bench's own harness. See docs/superpowers/specs/2026-09-14-eval-foundation-design.md.
"""
