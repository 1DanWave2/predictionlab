"""Fade-Any-Pump strategy — validated edge from 51 historical episodes.

Per validator on 80K+ PM Fills:
  Test set: 42 episodes
  Median forward 30m return: +3.61%
  Hit rate: 57%
  Best simple strategy (beats Impact Fade filter -8.97%, beats momentum -3.61%)

Logic:
  Detect price drop ≥ 5pp in last 5 minutes (delta_mid_5m)
  Wait next scan (60s) for entry exhaustion
  Fade direction: BUY rebound expected (mean reversion)
  Half-reversion target

Filters (per [GPT 23] hard rules):
  - hours_to_resolution > 6
  - spread <= 6pp
  - liquidity >= 3000
  - mid in [0.08, 0.92]
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


PUMP_THRESHOLD_MIN = 0.06     # 6pp empirical sweet spot (validator +29.82% median)
PUMP_THRESHOLD_MAX = 0.10     # 10pp+ = real news, fade fails
MIN_HOURS_TO_RESOLUTION = 6.0
MAX_SPREAD = 0.06
MIN_LIQUIDITY = 3000.0


class FadeAnyStrategy(BaseStrategy):
    """Detects 5pp+ pumps DOWN, BUYs expecting half-reversion."""
    name = "fade_any"
    supported_categories = ()
    entry_threshold = 0.0
    exit_threshold = 0.0
    min_volume = 0.0
    max_spread = MAX_SPREAD

    def __init__(self) -> None:
        # Per-market mid history (last 6 min)
        self._price_history: dict[str, list[tuple[datetime, float]]] = {}

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        now = datetime.now(UTC)
        mid = (market.best_bid + market.best_ask) / 2 if market.best_bid > 0 and market.best_ask > 0 else market.last_price
        if mid <= 0:
            return None

        if market.spread > MAX_SPREAD:
            return None
        if market.liquidity < MIN_LIQUIDITY:
            return None
        if mid < 0.08 or mid > 0.92:
            return None

        end_raw = market.raw.get("endDate") or market.raw.get("endDateIso")
        if end_raw:
            try:
                end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UTC)
                if (end_dt - now).total_seconds() / 3600.0 < MIN_HOURS_TO_RESOLUTION:
                    return None
            except (ValueError, TypeError):
                pass

        hist = self._price_history.setdefault(market.market_id, [])
        hist.append((now, mid))
        cutoff = now - timedelta(minutes=6)
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

        if len(hist) < 2:
            return None
        oldest_ts, oldest_mid = hist[0]
        if (now - oldest_ts).total_seconds() < 240:
            return None

        delta_5m = mid - oldest_mid
        # Sweet spot 6-10pp DOWN per validator (+29.82% median, 70% WR на 57 episodes)
        if delta_5m > -PUMP_THRESHOLD_MIN or delta_5m < -PUMP_THRESHOLD_MAX:
            return None

        rebound_target = oldest_mid + 0.5 * delta_5m  # half-reversion (delta negative → above mid)
        edge_estimate = rebound_target - market.best_ask
        if edge_estimate < 0.02:
            return None

        return self._build_signal(
            market=market,
            fair_price=rebound_target,
            side=SignalSide.BUY,
            reference_price=market.best_ask,
            reason=f"fade_any_pump_down delta_5m={delta_5m:+.3f} target={rebound_target:.3f}",
            threshold=0.0,
        )
