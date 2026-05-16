"""Fleet data types: role/workflow config and the per-step invocation record.

Pure dataclasses with no I/O. ``dispatch.py`` imports ``RoleConfig`` and
``Step`` from the package root, so both stay re-exported there.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import VALID_ROLES, WORKER_ROLES


@dataclass
class RoleConfig:
    provider: str  # "claude" or "opencode"
    model: str
    system_prompt: str


@dataclass
class FleetConfig:
    """A workflow, defined by which agents it contains.

    Invariants (enforced at construction by ``_merge_config``):
      - ``roles`` is non-empty
      - every key in ``roles`` is in ``VALID_ROLES``
      - ``entry_role`` is one of ``roles``' keys
    """

    name: str
    # Presence = membership. The workflow is exactly these agents.
    roles: dict[str, RoleConfig]
    # Which role runs first if there is no planner (or the planner fails).
    # Always one of the keys in ``roles``.
    entry_role: str
    max_steps: int = 6
    # Auto-retry on reviewer NACK. After a reviewer step that begins with
    # "NACK", the upstream worker step is re-run with the reviewer's feedback
    # appended to its prompt, then the reviewer runs again. Bounded so a
    # consistently-failing step can't loop forever. 0 disables retries.
    max_review_retries: int = 1
    # HITL: when true and the workflow has a planner, the pipeline pauses
    # after the planner emits its plan and waits for the user to approve.
    # Reject ends the turn with the user's feedback recorded as the assistant
    # message. Has no effect on planner-less workflows.
    require_plan_approval: bool = False
    # Where the active config came from — None means built-in defaults. Useful
    # for the UI to surface "Fleet config: ~/.localcode/fleet.yaml".
    config_source: str | None = None

    # Convenience accessors — keep callers from poking `cfg.roles[...]` directly.
    def has(self, role: str) -> bool:
        return role in self.roles

    def role_names(self) -> list[str]:
        """Roles in canonical execution order (planner → dev → coder → reviewer)."""
        return [r for r in VALID_ROLES if r in self.roles]

    def workers(self) -> list[str]:
        """Non-planner roles present, in canonical order."""
        return [r for r in WORKER_ROLES if r in self.roles]

    def get(self, role: str) -> RoleConfig | None:
        return self.roles.get(role)


@dataclass
class Step:
    """Per-role invocation record.

    The planner produces a Markdown document — we don't parse it into discrete
    steps. The plan is the artifact; the Coder reads it whole and iterates
    through its tasks with file/bash tools. ``Step`` just labels a single
    per-role invocation so we can tag its tool_use events and track order.
    """

    id: str
    role: str  # any role in VALID_ROLES — typed loose so planner-only single-shot fits too
    prompt: str
