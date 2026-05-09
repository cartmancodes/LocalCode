from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..orchestrator.fleet import (
    DEFAULT_FLEET_CONFIG,
    VALID_PROVIDERS,
    VALID_ROLES,
    WORKFLOW_PRESETS,
    config_to_dict,
    load_fleet_config,
    role_library_dict,
)


router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@router.get("/config")
async def get_fleet_config() -> dict[str, Any]:
    """Return the active fleet config + the metadata the UI needs to render
    its editor (role library for "add role" defaults, presets, vocabularies).
    """
    cfg = load_fleet_config()
    return {
        "config": config_to_dict(cfg),
        "is_default": cfg.config_source is None,
        "valid_providers": list(VALID_PROVIDERS),
        "valid_roles": list(VALID_ROLES),
        "role_library": role_library_dict(),
        "presets": WORKFLOW_PRESETS,
        "defaults": config_to_dict(DEFAULT_FLEET_CONFIG),
    }
