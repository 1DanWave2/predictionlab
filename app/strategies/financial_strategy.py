"""FinancialStrategy — canary для category='financial' Polymarket markets.

Per AI debate [GPT 9] verdict: Polymarket классифицирует UFC, tennis, WTI,
crypto targets как category='financial'. Старая event_strategy looks at
category='event' only → financial markets полностью пропускаются.

Constraint от maintainer: НЕ трогать event_strategy.py (она дала +$11.72).

Решение: отдельный module с identical logic event_strategy v1, но scope =
financial. Bucket="financial_internal" — отдельные stats. Canary limits в
risk_manager: max_size=$7.50, max_open=1, daily_stop=-$4 первые 24ч.

Это НЕ "fix старой стратегии". Это **financial category coverage canary
using legacy internal logic**.
"""

from __future__ import annotations

import os

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


def _reverse_mode() -> bool:
    return os.getenv("REVERSE_MODE", "false").lower() == "true"


class FinancialStrategy(BaseStrategy):
    name = "financial_strategy"
    supported_categories = ("financial",)
    # Same numeric thresholds as event_strategy v1 — это canary для проверки
    # что наша internal model работает на financial-classified markets.
    entry_threshold = 0.04
    exit_threshold = 0.04
    min_volume = 2000.0
    max_spread = 0.015

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        if not self._market_is_eligible(market):
            return None

        spread_cost = market.spread / 2.0
        net_buy_edge = (fair_price - market.best_ask) - spread_cost
        net_sell_edge = (fair_price - market.best_bid) + spread_cost

        if _reverse_mode():
            if net_sell_edge <= -self.entry_threshold and market.best_ask > 0:
                rev_edge = -net_sell_edge
                return self._build_signal(
                    market=market,
                    fair_price=fair_price,
                    side=SignalSide.BUY,
                    reference_price=market.best_ask,
                    reason=f"REV financial rev_edge={rev_edge:.4f} (model says overvalued, fade BUY)",
                    threshold=self.entry_threshold,
                )
            return None

        if net_buy_edge >= self.entry_threshold and market.best_ask > 0:
            return self._build_signal(
                market=market,
                fair_price=fair_price,
                side=SignalSide.BUY,
                reference_price=market.best_ask,
                reason=f"financial net_edge={net_buy_edge:.4f} after spread cost",
                threshold=self.entry_threshold,
            )
        if net_sell_edge <= -self.exit_threshold and market.best_bid > 0:
            return self._build_signal(
                market=market,
                fair_price=fair_price,
                side=SignalSide.SELL,
                reference_price=market.best_bid,
                reason=f"financial net_edge={net_sell_edge:.4f} above fair after spread cost",
                threshold=self.exit_threshold,
            )
        return None
