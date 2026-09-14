"""Built-in role library and the default full-crew config.

Default model picks reflect the economic intent of the fleet:
  - planner  → biggest Claude (deep decomposition is worth the cost)
  - developer→ mid-tier Claude (used only when the plan needs more design)
  - coder    → mid-tier Claude by default; `codex` when its binary is present
  - tester   → cheap Claude haiku (writes + runs tests; doesn't reason much)
  - reviewer → mid-tier Claude (LGTM/NACK gate needs real judgement)
"""
from __future__ import annotations

from .models import FleetConfig, RoleConfig
from .prompts import (
    CODER_SYSTEM,
    DEVELOPER_SYSTEM,
    PLANNER_SYSTEM,
    REVIEWER_SYSTEM,
    TESTER_SYSTEM,
)

# The UI uses this to pre-fill a role card when the user adds a
# previously-absent role to their workflow.
ROLE_LIBRARY: dict[str, RoleConfig] = {
    "planner": RoleConfig(
        provider="claude",
        model="claude-opus-4-7",
        system_prompt=PLANNER_SYSTEM,
    ),
    "developer": RoleConfig(
        provider="claude",
        model="claude-sonnet-4-6",
        system_prompt=DEVELOPER_SYSTEM,
    ),
    # The coder defaults to `claude` because that binary is the one every
    # install already has — a default that cannot run is worse than one that
    # can. `codex` is the better fit for this role when its binary is present
    # (a cheaper model doing the mechanical work, with real approvals and real
    # additional_dirs); it is one line, and the fleet editor offers it per role:
    #
    #   "coder": RoleConfig(
    #       provider="codex",
    #       model="gpt-5.3-codex",
    #       system_prompt=CODER_SYSTEM,
    #   ),
    #
    # See docs/codex.md for what `codex login` needs first.
    "coder": RoleConfig(
        provider="claude",
        model="claude-sonnet-4-6",
        system_prompt=CODER_SYSTEM,
    ),
    "tester": RoleConfig(
        provider="claude",
        model="claude-haiku-4-5",
        system_prompt=TESTER_SYSTEM,
    ),
    "reviewer": RoleConfig(
        provider="claude",
        model="claude-sonnet-4-6",
        system_prompt=REVIEWER_SYSTEM,
    ),
}


# Default workflow: planner produces the plan, coder implements, reviewer
# gates plan compliance, tester writes & runs tests as the final smoke check.
# Developer is omitted from the default — most tasks don't need a separate
# design step now that the plan IS the design.
DEFAULT_FLEET_CONFIG = FleetConfig(
    name="default",
    roles={r: ROLE_LIBRARY[r] for r in ("planner", "coder", "reviewer", "tester")},
    entry_role="coder",
)
