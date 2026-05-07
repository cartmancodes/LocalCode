from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from ..config import get_settings
from ..litellm_client import LiteLLMClient
from ..schemas import BudgetOut


router = APIRouter(prefix="/api/budget", tags=["budget"])
_client = LiteLLMClient()


@router.get("", response_model=BudgetOut)
async def get_budget() -> BudgetOut:
    s = get_settings()
    today = date.today()
    spent = await _client.daily_spend(today)
    return BudgetOut(
        spend_usd=round(spent, 4),
        daily_budget_usd=s.daily_budget_usd,
        remaining_usd=round(max(s.daily_budget_usd - spent, 0.0), 4),
        window=today.isoformat(),
    )
