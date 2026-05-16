"""Workflow presets — name → role membership + entry role.

These are starting points the UI exposes as one-click buttons. When the user
picks a preset, the modal pre-fills the role cards using ``ROLE_LIBRARY`` for
any role that wasn't already present in their existing config; from there
they can tune per-role provider/model freely.
"""
from __future__ import annotations

from typing import Any

WORKFLOW_PRESETS: dict[str, dict[str, Any]] = {
    "full": {
        "label": "Full crew",
        "description": "Planner writes a detailed plan; coder implements; reviewer gates plan compliance; tester writes & runs tests as the final smoke check.",
        "roles": ["planner", "coder", "reviewer", "tester"],
        "entry_role": "coder",
    },
    "plan-code-review-test": {
        "label": "Plan + code + review + test",
        "description": "Same as Full crew. Explicit name for clarity on the canonical order.",
        "roles": ["planner", "coder", "reviewer", "tester"],
        "entry_role": "coder",
    },
    "plan-code-test": {
        "label": "Plan + code + test",
        "description": "Planner → coder → tester. Skips review gate; tester is the only check.",
        "roles": ["planner", "coder", "tester"],
        "entry_role": "coder",
    },
    "plan-and-code": {
        "label": "Plan + code",
        "description": "Planner breaks the task down, coder executes. Skips testing + review.",
        "roles": ["planner", "coder"],
        "entry_role": "coder",
    },
    "design-and-code": {
        "label": "Design + code",
        "description": "Planner → developer (extra design pass) → coder. No reviewer.",
        "roles": ["planner", "developer", "coder"],
        "entry_role": "coder",
    },
    "design-only": {
        "label": "Design only",
        "description": "Single developer turn — produces a technical design doc, no code.",
        "roles": ["developer"],
        "entry_role": "developer",
    },
    "code-and-review": {
        "label": "Code + review",
        "description": "Planner → coder → reviewer. No tester.",
        "roles": ["planner", "coder", "reviewer"],
        "entry_role": "coder",
    },
    "code-only": {
        "label": "Code only",
        "description": "Single coder turn — no planning, testing, or review. Fastest.",
        "roles": ["coder"],
        "entry_role": "coder",
    },
    "plan-only": {
        "label": "Plan only",
        "description": "Just the planner — produces the markdown plan, no execution. Useful for review.",
        "roles": ["planner"],
        "entry_role": "planner",
    },
    "review-only": {
        "label": "Review only",
        "description": "Single reviewer turn — paste content into the prompt for an LGTM/NACK pass.",
        "roles": ["reviewer"],
        "entry_role": "reviewer",
    },
}
