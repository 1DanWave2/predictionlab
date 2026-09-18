from __future__ import annotations

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


class CryptoStrategy(BaseStrategy):
    name = "crypto_strategy"
    supported_categories = ("crypto", "financial")
    entry_threshold = 0.04
    exit_threshold = 0.04
    min_volume = 3000.0
    max_spread = 0.02

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        if not self._market_is_eligible(market):
            return None

        spread_cost = market.spread / 2.0

        net_buy_edge = (fair_price - market.best_ask) - spread_cost
        net_sell_edge = (fair_price - market.best_bid) + spread_cost

        if net_buy_edge >= self.entry_threshold and market.best_ask > 0:
            return self._build_signal(
                market=market,
                fair_price=fair_price,
                side=SignalSide.BUY,
                reference_price=market.best_ask,
                reason=f"crypto net_edge={net_buy_edge:.4f} after spread cost",
                threshold=self.entry_threshold,
            )
        if net_sell_edge <= -self.exit_threshold and market.best_bid > 0:
            return self._build_signal(
                market=market,
                fair_price=fair_price,
                side=SignalSide.SELL,
                reference_price=market.best_bid,
                reason=f"crypto net_edge={net_sell_edge:.4f} above fair after spread cost",
                threshold=self.exit_threshold,
            )
        return None
