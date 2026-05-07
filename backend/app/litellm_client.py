"""Tiny wrapper around the LiteLLM proxy admin API for budget readout."""
from __future__ import annotations

from datetime import date

import httpx

from .config import get_settings


class LiteLLMClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.litellm_api_base.rstrip("/")
        self._master_key = s.litellm_master_key
        self._client = httpx.AsyncClient(timeout=10.0)

    async def daily_spend(self, day: date | None = None) -> float:
        """Return total spend (USD) for `day` (defaults to today, UTC)."""
        d = (day or date.today()).isoformat()
        try:
            resp = await self._client.get(
                f"{self._base}/spend/logs",
                params={"start_date": d, "end_date": d, "summarize": "true"},
                headers={"Authorization": f"Bearer {self._master_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError:
            return 0.0

        # /spend/logs?summarize=true returns either a list of {spend, ...} rows
        # or a dict with 'total_spend'. Handle both shapes defensively.
        if isinstance(payload, dict):
            total = payload.get("total_spend")
            if total is not None:
                return float(total)
            payload = payload.get("data") or payload.get("logs") or []
        if isinstance(payload, list):
            return float(sum(row.get("spend", 0.0) for row in payload))
        return 0.0

    async def aclose(self) -> None:
        await self._client.aclose()
