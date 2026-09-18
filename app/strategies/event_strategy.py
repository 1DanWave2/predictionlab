from __future__ import annotations

import os

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


def _reverse_mode() -> bool:
    return os.getenv("REVERSE_MODE", "false").lower() == "true"


class EventStrategy(BaseStrategy):
    name = "event_strategy"
    supported_categories = ("event",)
    # Tiered threshold per [GPT 26] partial calibration на 27 cycles:
    #   p_30_50 (n=17, +6.38% avg, 65% WR) → 6% threshold
    #   p_50_70 (n=10, -1.31% avg, 60% WR) → 8% (stricter)
    #   p_70+ (unproven, fewer samples)    → 10% (very strict)
    entry_threshold = 0.06   # base — used для p_30_50 zone
    exit_threshold = 0.06
    min_volume = 5000.0

    @classmethod
    def get_threshold_for_price(cls, price: float) -> float:
        """Price-tiered threshold (per [GPT 26] partial calibration overlay)."""
        if price >= 0.70:
            return 0.10
        if price >= 0.50:
            return 0.08
        return 0.06
    max_spread = 0.015

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        if not self._market_is_eligible(market):
            return None

        spread_cost = market.spread / 2.0
        net_buy_edge = (fair_price - market.best_ask) - spread_cost
        net_sell_edge = (fair_price - market.best_bid) + spread_cost
        # Price-tiered threshold per [GPT 26] partial calibration
        tiered_threshold = self.get_threshold_for_price(market.best_ask if market.best_ask > 0 else market.last_price)

        if _reverse_mode():
            if net_sell_edge <= -tiered_threshold and market.best_ask > 0:
                rev_edge = -net_sell_edge
                return self._build_signal(
                    market=market,
                    fair_price=fair_price,
                    side=SignalSide.BUY,
                    reference_price=market.best_ask,
                    reason=f"REV event rev_edge={rev_edge:.4f} (model says overvalued, fade BUY)",
                    threshold=tiered_threshold,
                )
            return None

        if net_buy_edge >= tiered_threshold and market.best_ask > 0:
            return self._build_signal(
                market=market,
                fair_price=fair_price,
                side=SignalSide.BUY,
                reference_price=market.best_ask,
                reason=f"event net_edge={net_buy_edge:.4f} after spread cost",
                threshold=tiered_threshold,
            )
        if net_sell_edge <= -tiered_threshold and market.best_bid > 0:
            return self._build_signal(
                market=market,
                fair_price=fair_price,
                side=SignalSide.SELL,
                reference_price=market.best_bid,
                reason=f"event net_edge={net_sell_edge:.4f} above fair after spread cost",
                threshold=tiered_threshold,
            )
        return None
