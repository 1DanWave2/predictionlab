from __future__ import annotations

import os

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


def _enabled() -> bool:
    return os.getenv("MOMENTUM_ENABLED", "true").lower() == "true"


class MomentumStrategy(BaseStrategy):
    """Buys when mid price rose >= threshold over last `lookback` snapshots.

    Backtest result: ~+1.2% EV/trade at TP=30%/SL=3%, WR 27% but R:R 3.16.
    Independent from event/sports strategies — runs as parallel signal source.
    """

    name = "momentum_strategy"
    supported_categories = ("sports", "event", "crypto", "financial")
    entry_threshold = 0.02
    exit_threshold = 0.02
    min_volume = 1000.0
    max_spread = 0.02
    lookback_snaps = 5

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        return None

    def evaluate_with_history(
        self,
        market: NormalizedMarket,
        history: list[float],
    ) -> StrategySignal | None:
        if not _enabled():
            return None
        if not self._market_is_eligible(market):
            return None
        if market.mid_price < 0.25 or market.mid_price > 0.85:
            return None
        if len(history) < self.lookback_snaps + 1:
            return None
        past = history[-(self.lookback_snaps + 1)]
        if past <= 0:
            return None
        delta = (market.mid_price - past) / past
        if delta < self.entry_threshold or market.best_ask <= 0:
            return None
        return self._build_signal(
            market=market,
            fair_price=market.mid_price,
            side=SignalSide.BUY,
            reference_price=market.best_ask,
            reason=f"momentum delta={delta:.3%} over {self.lookback_snaps} snaps",
            threshold=self.entry_threshold,
        )
