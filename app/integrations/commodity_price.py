"""Commodity price feed через Yahoo Finance public endpoint (no auth).

Endpoints:
  GET https://query1.finance.yahoo.com/v8/finance/chart/CL=F?range=30d&interval=1d
  → candles + meta.regularMarketPrice (current spot)

Tickers Yahoo:
  CL=F → WTI Crude Oil futures
  BZ=F → Brent Crude
  GC=F → Gold
  SI=F → Silver
  ^GSPC → S&P 500
  ^NDX → Nasdaq 100

Совместимый интерфейс с CryptoPriceClient (PriceSnapshot, HistoricalReturns).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx

from app.integrations.crypto_price import HistoricalReturns, PriceSnapshot
from app.logger import get_logger


logger = get_logger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"

# Polymarket symbol → Yahoo ticker
_YAHOO_TICKER = {
    "WTI": "CL=F",
    "BRENT": "BZ=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "NDX": "^NDX",
    "SPX": "^GSPC",
}


class CommodityPriceClient:
    """Yahoo Finance public chart endpoint client.

    Mirrors interface CryptoPriceClient: supported(), get_spot(), get_log_returns().
    Используется fallback'ом когда Binance не торгует символ.
    """

    def __init__(
        self,
        cache_seconds: float = 120.0,
        candle_cache_seconds: float = 1800.0,
        timeout_s: float = 8.0,
    ) -> None:
        self.cache_seconds = cache_seconds
        self.candle_cache_seconds = candle_cache_seconds
        self.timeout_s = timeout_s
        self._spot_cache: dict[str, PriceSnapshot] = {}
        self._candle_cache: dict[tuple[str, str, int], HistoricalReturns] = {}

    def supported(self, symbol: str) -> bool:
        return symbol in _YAHOO_TICKER

    async def _fetch_chart(self, symbol: str, range_str: str = "1mo", interval: str = "1d") -> dict | None:
        if symbol not in _YAHOO_TICKER:
            return None
        ticker = _YAHOO_TICKER[symbol]
        url = f"{YAHOO_CHART}/{ticker}"
        params = {"range": range_str, "interval": interval}
        # Yahoo иногда требует User-Agent чтобы не отдавать 404
        headers = {"User-Agent": "Mozilla/5.0 (compatible; PolymarketBot/1.0)"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            chart = data.get("chart", {})
            if chart.get("error"):
                logger.warning("yahoo.chart_error | symbol=%s error=%s", symbol, chart.get("error"))
                return None
            results = chart.get("result", [])
            if not results:
                return None
            return results[0]
        except Exception as exc:
            logger.warning("yahoo.fetch_failed | symbol=%s error=%s", symbol, exc)
            return None

    async def get_spot(self, symbol: str) -> PriceSnapshot | None:
        if symbol not in _YAHOO_TICKER:
            return None
        now = time.monotonic()
        cached = self._spot_cache.get(symbol)
        if cached and now - cached.fetched_at < self.cache_seconds:
            return cached

        chart = await self._fetch_chart(symbol, range_str="1d", interval="1m")
        if not chart:
            return cached  # stale OK
        meta = chart.get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if price is None:
            return cached
        try:
            price_f = float(price)
        except (ValueError, TypeError):
            return cached
        snap = PriceSnapshot(symbol=symbol, spot_price=price_f, fetched_at=now)
        self._spot_cache[symbol] = snap
        return snap

    async def get_log_returns(
        self,
        symbol: str,
        interval: str = "1d",
        limit: int = 30,
    ) -> HistoricalReturns | None:
        if symbol not in _YAHOO_TICKER:
            return None
        cache_key = (symbol, interval, limit)
        now = time.monotonic()
        cached = self._candle_cache.get(cache_key)
        if cached and now - cached.fetched_at < self.candle_cache_seconds:
            return cached

        # Range подбираем: 30 daily ≈ 1mo, 60 daily ≈ 3mo
        range_str = "3mo" if limit > 30 else "1mo"
        chart = await self._fetch_chart(symbol, range_str=range_str, interval=interval)
        if not chart:
            return cached
        indicators = chart.get("indicators", {})
        quote_arr = indicators.get("quote", [])
        if not quote_arr:
            return cached
        closes_raw = quote_arr[0].get("close", [])
        # Filter Nones (Yahoo иногда возвращает gap-days как null)
        closes = [c for c in closes_raw if c is not None]
        if len(closes) < 2:
            return cached

        closes = closes[-(limit + 1):]
        log_returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                log_returns.append(math.log(closes[i] / closes[i - 1]))

        result = HistoricalReturns(
            symbol=symbol,
            interval=interval,
            log_returns=log_returns,
            fetched_at=now,
        )
        self._candle_cache[cache_key] = result
        return result


class CompositePriceClient:
    """Маршрутизатор который пробует CryptoPriceClient first, затем CommodityPriceClient.

    Совместим с интерфейсом CryptoPriceClient (используется asset_target/strategy.py).
    """

    def __init__(self, crypto_client, commodity_client: CommodityPriceClient | None = None) -> None:
        self.crypto = crypto_client
        self.commodity = commodity_client or CommodityPriceClient()

    def supported(self, symbol: str) -> bool:
        return self.crypto.supported(symbol) or self.commodity.supported(symbol)

    async def get_spot(self, symbol: str) -> PriceSnapshot | None:
        if self.crypto.supported(symbol):
            return await self.crypto.get_spot(symbol)
        if self.commodity.supported(symbol):
            return await self.commodity.get_spot(symbol)
        return None

    async def get_log_returns(self, symbol: str, interval: str = "1d", limit: int = 30) -> HistoricalReturns | None:
        if self.crypto.supported(symbol):
            return await self.crypto.get_log_returns(symbol, interval, limit)
        if self.commodity.supported(symbol):
            return await self.commodity.get_log_returns(symbol, interval, limit)
        return None
