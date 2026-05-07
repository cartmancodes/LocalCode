from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..schemas import CatalogModel


router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[CatalogModel])
async def list_models() -> list[CatalogModel]:
    s = get_settings()
    return [CatalogModel(**e.to_dict()) for e in s.catalog()]
