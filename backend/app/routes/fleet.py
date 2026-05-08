from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..orchestrator.fleet import (
    DEFAULT_FLEET_CONFIG,
    VALID_PROVIDERS,
    VALID_ROLES,
    config_to_dict,
    load_fleet_config,
)


router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@router.get("/config")
async def get_fleet_config() -> dict[str, Any]:
    """Return the active fleet config — what was loaded, from where, plus the
    role/provider vocabulary so a UI can render an editor without hard-coding
    the lists.
    """
    cfg = load_fleet_config()
    return {
        "config": config_to_dict(cfg),
        "is_default": cfg.config_source is None,
        "valid_providers": list(VALID_PROVIDERS),
        "valid_roles": list(VALID_ROLES),
        "defaults": config_to_dict(DEFAULT_FLEET_CONFIG),
    }
