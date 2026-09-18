from __future__ import annotations

import os

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, StrategySignal
from app.strategies.event_strategy import EventStrategy
from app.strategies.fade_any import FadeAnyStrategy
from app.strategies.financial_strategy import FinancialStrategy
from app.strategies.sports_strategy import SportsStrategy


def _financial_enabled() -> bool:
    """Per [GPT 9]: canary toggle для financial coverage. Default True
    (fix coverage bug 24+ часов 0 trades). Можно disable через ENV.
    """
    return os.getenv("FINANCIAL_STRATEGY_ENABLED", "true").lower() == "true"


def _fade_any_enabled() -> bool:
    """Per [GPT 44]: pause fade_any after 31 trades / 58% WR / -$1.49 net (fat-tail-left).
    Default true for backward-compat; set FADE_ANY_ENABLED=false to disable."""
    return os.getenv("FADE_ANY_ENABLED", "true").lower() == "true"


class MarketRegistry:
    def __init__(self) -> None:
        sports = SportsStrategy()
        event = EventStrategy()
        financial = FinancialStrategy()
        # Fade-Any per validator PASS (80 ep, +18.73% median, 66% WR).
        # Live canary $1 stake per [GPT 23] ramp. Independent strategy layer —
        # works на ALL categories (its own filter via 6-10pp pump detection).
        self.fade_any = FadeAnyStrategy()
        self._strategies: dict[str, BaseStrategy] = {
            "sports": sports,
            "event": event,
            "financial": financial,
        }
        self._fallback = event

    def get_strategy(self, market: NormalizedMarket) -> BaseStrategy | None:
        if "sports" in market.tags or market.category == "sports":
            return self._strategies["sports"]
        if market.category == "crypto":
            # Crypto markets handled by AssetTargetSnipper (math anchor).
            # Internal model не подходит — нет VWAP стабильного.
            return None
        if market.category == "financial":
            # Per [GPT 9]: canary using legacy internal logic but separate bucket.
            if not _financial_enabled():
                return None
            return self._strategies["financial"]
        return self._strategies.get(market.category, self._fallback)

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        # Try fade_any FIRST (independent edge, не depends on internal fair).
        # Если fade signal generated — return it; else fall through to category strategy.
        if _fade_any_enabled():
            fade_signal = self.fade_any.evaluate(market, fair_price)
            if fade_signal is not None:
                return fade_signal

        strategy = self.get_strategy(market)
        if strategy is None:
            return None
        return strategy.evaluate(market, fair_price)
