"""Polymarket Data API client per [GPT 18] Smart Money MVP.

Endpoint: https://data-api.polymarket.com/trades

Filters supported (verified 3 May 2026):
- user=<wallet>          — single wallet's fills
- market=<conditionId>   — single market's fills
- takerOnly=true         — only taker side trades
- maxTimestamp=<unix>    — fills before this timestamp
- limit=<N>              — page size (max ~500)
- offset=<N>             — pagination

Order: DESC by timestamp (newest first).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel, Field


class PMFill(BaseModel):
    """Single fill from Polymarket Data API."""
    timestamp: int
    wallet: str = Field(alias="proxyWallet")
    side: str
    asset: str  # token_id
    condition_id: str = Field(alias="conditionId")
    size: float
    price: float
    title: str = ""
    slug: str = ""
    outcome: str = ""
    outcome_index: int = Field(alias="outcomeIndex", default=0)
    transaction_hash: str = Field(alias="transactionHash", default="")

    model_config = {"populate_by_name": True}

    @property
    def notional(self) -> float:
        return self.size * self.price


class PolymarketDataApiClient:
    BASE_URL = "https://data-api.polymarket.com"
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout or self.DEFAULT_TIMEOUT

    async def fetch_trades(
        self,
        *,
        market: str | None = None,
        user: str | None = None,
        taker_only: bool = False,
        max_timestamp: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[PMFill]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if market:
            params["market"] = market
        if user:
            params["user"] = user
        if taker_only:
            params["takerOnly"] = "true"
        if max_timestamp is not None:
            params["maxTimestamp"] = max_timestamp

        url = f"{self.base_url}/trades"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        return [PMFill.model_validate(item) for item in data]

    async def fetch_prices_history(
        self,
        token_id: str,
        *,
        interval: str | None = None,
    ) -> list[tuple[int, float]]:
        """Fetch raw price history for one token (outcome).

        Returns list of (timestamp_unix, price) tuples.
        Без interval — raw ~60s points (only short-term markets).
        Long-running markets need interval='1h' or '1d' (else 400).
        """
        url = "https://clob.polymarket.com/prices-history"
        params: dict[str, Any] = {"market": token_id}
        if interval:
            params["interval"] = interval
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, params=params)
            if r.status_code == 400 and interval is None:
                # fallback for long-running markets
                params["interval"] = "1h"
                r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        history = data.get("history", [])
        return [(int(item["t"]), float(item["p"])) for item in history]

    async def iterate_market_fills(
        self,
        condition_id: str,
        *,
        since_ts: int,
        page_size: int = 500,
        max_pages: int = 200,
    ) -> list[PMFill]:
        """Iterate fills for one market until since_ts is reached.

        Uses maxTimestamp pagination — each page fetches max_timestamp = oldest_in_prev_page.
        Stops когда oldest fetched ts < since_ts.
        """
        all_fills: list[PMFill] = []
        max_ts: int | None = None
        for _ in range(max_pages):
            page = await self.fetch_trades(
                market=condition_id,
                limit=page_size,
                max_timestamp=max_ts,
            )
            if not page:
                break
            new = [f for f in page if f.timestamp >= since_ts]
            all_fills.extend(new)
            if len(page) < page_size:
                break
            oldest = min(f.timestamp for f in page)
            if oldest < since_ts:
                break
            max_ts = oldest - 1
        return all_fills
