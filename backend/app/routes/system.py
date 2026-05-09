from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from ..config import get_settings


router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/cwd")
async def get_system_cwd() -> dict[str, Any]:
    """Return the orchestrator process's current working directory plus the
    configured allowlist of valid roots. The UI uses this as the sensible
    default when the user hasn't explicitly chosen a project root."""
    s = get_settings()
    return {
        "cwd": str(Path.cwd().resolve()),
        "home": str(Path.home()),
        "allowed_roots": [str(r) for r in s.cwd_allowlist()],
        # When `allowed_roots` is empty the backend is permissive — any cwd
        # the user passes is accepted (single-user dev mode).
        "permissive": len(s.cwd_allowlist()) == 0,
    }
