from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


def _enabled() -> bool:
    return os.getenv("POST_PANIC_REBOUND_ENABLED", "false").lower() == "true"


def _hours_to_resolution(market: NormalizedMarket) -> float:
    end_raw = market.raw.get("endDate") or market.raw.get("end_date") or market.raw.get("endDateIso")
    if not end_raw:
        return -1.0
    try:
        end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return -1.0
    return (end_dt - datetime.now(UTC)).total_seconds() / 3600.0


class PostPanicReboundStrategy(BaseStrategy):
    """Strategy 2: Post-Panic Rebound.

    Edge: in thin CLOB sharp drops often happen due to liquidity removal or one
    aggressive seller, not new info. After a drop, if a bid wall appears and price
    stops making new lows, technical rebound is likely without forecasting outcome.
    """

    name = "post_panic_rebound"
    supported_categories = ("sports", "event", "crypto", "financial")

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        return None

    def evaluate_with_history(self, market: NormalizedMarket, history: list[float]) -> StrategySignal | None:
        if not _enabled():
            return None
        if len(history) < 12:
            return None
        if market.best_ask <= 0 or market.best_bid <= 0:
            return None

        tte_h = _hours_to_resolution(market)
        if tte_h < 3.0 or tte_h > 720.0:
            return None

        ask = market.best_ask
        bid = market.best_bid
        if not (0.30 <= ask <= 0.78):
            return None
        if market.spread > 0.025:
            return None
        if market.volume < 1500 or market.liquidity < 500:
            return None
        if market.total_ask_size <= 0:
            return None

        win_size = min(len(history), 24)
        recent_high = max(history[-win_size:-3]) if win_size > 3 else max(history)
        recent_low = min(history[-min(8, win_size):])
        if recent_high <= 0:
            return None
        drop_abs = recent_high - recent_low
        drop_pct = drop_abs / recent_high
        bounce_from_low = history[-1] - recent_low
        last_3_up = history[-1] >= history[-2] >= history[-3]

        if drop_abs < 0.040:
            return None
        if drop_pct < 0.06:
            return None
        if bounce_from_low < 0.005:
            return None
        if not last_3_up:
            return None
        if min(history[-3:]) <= recent_low + 0.001:
            return None

        bid_ask_ratio = market.total_bid_size / max(market.total_ask_size, 1.0)
        if bid_ask_ratio < 1.4:
            return None
        bid_usd = market.total_bid_size * bid
        if bid_usd < 80:
            return None
        if market.best_bid_size * bid < 15:
            return None

        bounce_target_bid = recent_low + 0.35 * drop_abs
        net_edge = bounce_target_bid - ask
        if net_edge < 0.020:
            return None

        reason = (
            f"post_panic_rebound drop={drop_pct:.1%} bounce={bounce_from_low:.3f} "
            f"edge={net_edge:.3f} last_low={recent_low:.3f}"
        )
        signal = self._build_signal(
            market=market,
            fair_price=bounce_target_bid,
            side=SignalSide.BUY,
            reference_price=ask,
            reason=reason,
            threshold=0.030,
        )
        signal.metadata.update(
            {
                "strategy_kind": "post_panic_rebound",
                "tte_h": tte_h,
                "recent_low": recent_low,
                "recent_high": recent_high,
                "drop_pct": drop_pct,
                "net_edge": net_edge,
            }
        )
        return signal


POST_PANIC_REBOUND_EXIT_RULES: dict[str, Any] = {
    "tp_abs_cents": 0.035,
    "tp_pct": 0.075,
    "trailing_min_gain": 0.028,
    "trailing_drop": 0.012,
    "sl_abs_cents": 0.025,
    "max_hold_min": 25,
    "new_low_buffer": 0.005,
}
