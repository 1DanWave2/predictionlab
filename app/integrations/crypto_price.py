"""Crypto price feed клиент. Binance public API (no auth).

Endpoints:
  GET /api/v3/ticker/price?symbol=BTCUSDT       → spot
  GET /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30  → candles

В нашем v1 это primary feed для BTC/ETH/SOL/etc. Для commodities
(WTI/Brent/gold) — fallback с Yahoo/Coingecko позже.

Кэш: 60 сек для spot (price двигается каждую секунду, но 60s достаточно
для нашего scan interval 30s).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx

from app.logger import get_logger


logger = get_logger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3"

# Polymarket symbol → Binance ticker
_BINANCE_TICKER = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "DOT": "DOTUSDT",
    "MATIC": "MATICUSDT",
    "LTC": "LTCUSDT",
}


@dataclass
class PriceSnapshot:
    symbol: str
    spot_price: float
    fetched_at: float  # monotonic


@dataclass
class HistoricalReturns:
    symbol: str
    interval: str          # "1d", "1h"
    log_returns: list[float]
    fetched_at: float


class CryptoPriceClient:
    """Binance public API клиент с in-memory cache."""

    def __init__(
        self,
        cache_seconds: float = 60.0,
        candle_cache_seconds: float = 600.0,  # 10 min для historical
        timeout_s: float = 5.0,
    ) -> None:
        self.cache_seconds = cache_seconds
        self.candle_cache_seconds = candle_cache_seconds
        self.timeout_s = timeout_s
        self._spot_cache: dict[str, PriceSnapshot] = {}
        self._candle_cache: dict[tuple[str, str, int], HistoricalReturns] = {}

    def supported(self, symbol: str) -> bool:
        return symbol in _BINANCE_TICKER

    async def get_spot(self, symbol: str) -> PriceSnapshot | None:
        if symbol not in _BINANCE_TICKER:
            return None
        now = time.monotonic()
        cached = self._spot_cache.get(symbol)
        if cached and now - cached.fetched_at < self.cache_seconds:
            return cached

        ticker = _BINANCE_TICKER[symbol]
        url = f"{BINANCE_BASE}/ticker/price"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(url, params={"symbol": ticker})
                resp.raise_for_status()
                price = float(resp.json()["price"])
        except Exception as exc:
            logger.warning("crypto_price.spot_failed | symbol=%s error=%s", symbol, exc)
            return cached  # stale OK

        snap = PriceSnapshot(symbol=symbol, spot_price=price, fetched_at=now)
        self._spot_cache[symbol] = snap
        return snap

    async def get_log_returns(
        self,
        symbol: str,
        interval: str = "1d",
        limit: int = 30,
    ) -> HistoricalReturns | None:
        """Returns log-returns of close prices for last `limit` periods."""
        if symbol not in _BINANCE_TICKER:
            return None
        cache_key = (symbol, interval, limit)
        now = time.monotonic()
        cached = self._candle_cache.get(cache_key)
        if cached and now - cached.fetched_at < self.candle_cache_seconds:
            return cached

        ticker = _BINANCE_TICKER[symbol]
        url = f"{BINANCE_BASE}/klines"
        params = {"symbol": ticker, "interval": interval, "limit": limit + 1}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                # Each kline: [openTime, open, high, low, close, volume, ...]
                closes = [float(k[4]) for k in data]
        except Exception as exc:
            logger.warning(
                "crypto_price.candles_failed | symbol=%s interval=%s error=%s",
                symbol, interval, exc,
            )
            return cached

        if len(closes) < 2:
            return cached
        log_returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                log_returns.append(math.log(closes[i] / closes[i - 1]))
        result = HistoricalReturns(
            symbol=symbol, interval=interval,
            log_returns=log_returns, fetched_at=now,
        )
        self._candle_cache[cache_key] = result
        return result
