from __future__ import annotations

import json as _json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.logger import get_logger


logger = get_logger(__name__)


class GammaClient:
    """Gamma REST abstraction.

    TODO:
    Real Polymarket Gamma response schemas evolve. Keep this client as the
    boundary and tighten the parser when the exact payload contract is fixed.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._mock_cycle = 0

    async def fetch_markets(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.settings.use_mock_data:
            markets = self._mock_markets(limit)
            logger.info(
                "gamma.fetch_markets | source=mock limit=%s returned=%s cycle=%s",
                limit,
                len(markets),
                self._mock_cycle,
            )
            return markets

        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "archived": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
        url = f"{self.settings.gamma_base_url.rstrip('/')}/markets"
        # Per morning incident 2026-05-08: timeout=10s caused fallback under
        # async loop pressure (news_alert spam). Bumped to 30s + repr(exc) so
        # silent timeouts are visible in logs.
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning("gamma.fetch_markets_failed | error=%r fallback=mock", exc)
            return self._mock_markets(limit)

        data = self._extract_list(payload)
        if not data:
            logger.warning("gamma.fetch_markets_empty | fallback=mock")
            return self._mock_markets(limit)

        enriched = [self._enrich_real_market(m) for m in data[:limit]]
        logger.info("gamma.fetch_markets | source=remote returned=%s", len(enriched))
        return enriched

    async def fetch_imminent_matchups(self, max_hours_ahead: float = 6.0, limit: int = 50) -> list[dict[str, Any]]:
        """Pull matchup markets with endDate в next max_hours_ahead.

        Per [GPT 21]: Sports Sniper needs scanner to actively pull imminent games.
        Default scanner sorts by volume24hr — misses pre-match window.
        Эта функция запрашивает up to 200 markets и filters в Python к imminent matchups.
        """
        if self.settings.use_mock_data:
            return []
        params = {
            "limit": 200,
            "active": "true",
            "closed": "false",
            "archived": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
        url = f"{self.settings.gamma_base_url.rstrip('/')}/markets"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning("gamma.fetch_imminent_failed | error=%r", exc)
            return []
        data = self._extract_list(payload)
        if not data:
            return []

        cutoff = datetime.now(UTC) + timedelta(hours=max_hours_ahead)
        floor_dt = datetime.now(UTC) + timedelta(minutes=5)
        out: list[dict[str, Any]] = []
        for m in data:
            title = m.get("question", "") or ""
            if not (" vs " in title.lower() or " vs." in title.lower() or " v. " in title.lower()):
                continue
            end_raw = m.get("endDate") or m.get("endDateIso")
            if not end_raw:
                continue
            try:
                end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UTC)
            except Exception:
                continue
            if end_dt < floor_dt or end_dt > cutoff:
                continue
            out.append(self._enrich_real_market(m))
            if len(out) >= limit:
                break
        logger.info("gamma.fetch_imminent_matchups | source=remote returned=%s window_h=%.1f", len(out), max_hours_ahead)
        return out

    @staticmethod
    def _enrich_real_market(market: dict[str, Any]) -> dict[str, Any]:
        outcome_prices_raw = market.get("outcomePrices")
        if isinstance(outcome_prices_raw, str):
            try:
                prices = _json.loads(outcome_prices_raw)
                if isinstance(prices, list) and len(prices) >= 1:
                    yes_price = float(prices[0])
                    market.setdefault("last_price", yes_price)
            except (ValueError, IndexError):
                pass

        token_ids_raw = market.get("clobTokenIds")
        if isinstance(token_ids_raw, str):
            try:
                token_ids = _json.loads(token_ids_raw)
                if isinstance(token_ids, list) and len(token_ids) >= 1:
                    market["yes_token_id"] = token_ids[0]
                    if len(token_ids) >= 2:
                        market["no_token_id"] = token_ids[1]
            except (ValueError, IndexError):
                pass

        return market

    @staticmethod
    def _extract_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "markets", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _mock_markets(self, limit: int) -> list[dict[str, Any]]:
        self._mock_cycle += 1
        now = datetime.now(UTC)
        cycle = self._mock_cycle

        base_markets: list[dict[str, Any]] = [
            {
                "id": "sports-1",
                "slug": "nba-finals-demo",
                "question": "Will Team Alpha win the NBA Finals?",
                "category": "sports",
                "outcome": "YES",
                "best_bid": 0.44,
                "best_ask": 0.47,
                "last_price": 0.66 if cycle % 2 else 0.37,
                "volume": 1680.0,
                "liquidity": 920.0,
                "tags": ["sports", "basketball"],
                "updated_at": (now - timedelta(seconds=3)).isoformat(),
            },
            {
                "id": "crypto-1",
                "slug": "btc-above-100k-demo",
                "question": "Will BTC trade above 100k this month?",
                "category": "crypto",
                "outcome": "YES",
                "best_bid": 0.39,
                "best_ask": 0.42,
                "last_price": 0.63 if cycle % 2 else 0.35,
                "volume": 3250.0,
                "liquidity": 1430.0,
                "tags": ["crypto", "bitcoin", "financial"],
                "updated_at": (now - timedelta(seconds=2)).isoformat(),
            },
            {
                "id": "event-1",
                "slug": "fed-rate-cut-demo",
                "question": "Will the Fed cut rates at the next meeting?",
                "category": "event",
                "outcome": "YES",
                "best_bid": 0.57,
                "best_ask": 0.60,
                "last_price": 0.76 if cycle % 2 else 0.38,
                "volume": 2140.0,
                "liquidity": 1040.0,
                "tags": ["macro", "event", "news"],
                "updated_at": (now - timedelta(seconds=1)).isoformat(),
            },
        ]
        return [deepcopy(item) for item in base_markets[:limit]]
