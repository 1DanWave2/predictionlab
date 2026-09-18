from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket
from app.strategies.base import BaseStrategy, SignalSide, StrategySignal


logger = get_logger(__name__)


def _enabled() -> bool:
    return os.getenv("CONSENSUS_DRIFT_ENABLED", "false").lower() == "true"


def _lite_mode() -> bool:
    return os.getenv("CONSENSUS_DRIFT_LITE", "false").lower() == "true"


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


class ConsensusDriftStrategy(BaseStrategy):
    """Pre-Resolution Consensus Drift.

    Edge: market often "drifts" to consensus before resolution when result is
    nearly certain to participants but some lazy liquidity still sits at old
    prices. We don't predict outcomes — we buy YES only where book + price +
    time-to-end together indicate the market is repricing upward.

    Entry filters (all must hold):
      - Llama classifier label ∈ {clean types}, quality == "clear"
      - 1.5 <= hours_to_resolution <= 8.0
      - 0.68 <= ask <= 0.88
      - spread <= 0.012
      - volume >= 15000, liquidity >= 3000
      - bid_ask_ratio (total) >= 2.5
      - bid_usd_total >= $250
      - best_bid_size * bid >= $40
      - 0.010 <= ret_15m <= 0.060   (moderate uptrend)
      - ret_5m >= -0.005            (not currently dropping)
      - max drawdown over 15m <= 0.025
      - net_edge (expected_exit_bid - ask) >= 0.030
    """

    name = "consensus_drift"
    supported_categories = ("sports", "event", "crypto", "financial")

    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        return None

    def evaluate_with_context(
        self,
        market: NormalizedMarket,
        history: list[float],
        is_clean: bool,
    ) -> StrategySignal | None:
        if not _enabled():
            return None
        debug = os.getenv("STRATEGY_DEBUG", "false").lower() == "true"
        mid = market.market_id

        def _reject(why: str) -> None:
            if debug:
                logger.info("cd_reject | market_id=%s why=%s", mid, why)

        if not is_clean:
            _reject("not_clean_classification")
            return None

        bid = market.best_bid
        ask = market.best_ask
        spread = market.spread
        if bid <= 0 or ask <= 0:
            _reject("no_bid_ask")
            return None
        min_history = 4 if _lite_mode() else 16
        if len(history) < min_history:
            _reject(f"short_history len={len(history)}")
            return None

        lite = _lite_mode()
        tte_h = _hours_to_resolution(market)
        tte_min = 0.5 if lite else 1.5
        tte_max = 168.0 if lite else 8.0
        if tte_h < tte_min or tte_h > tte_max:
            _reject(f"tte={tte_h:.1f}h out [{tte_min},{tte_max}]")
            return None

        ask_min = 0.20 if lite else 0.68
        ask_max = 0.92 if lite else 0.88
        if not (ask_min <= ask <= ask_max):
            _reject(f"ask={ask:.3f} out [{ask_min},{ask_max}]")
            return None
        spread_max = 0.025 if lite else 0.012
        if spread > spread_max:
            _reject(f"spread={spread:.4f} > {spread_max}")
            return None
        vol_min = 3000 if lite else 15000
        liq_min = 800 if lite else 3000
        if market.volume < vol_min:
            _reject(f"volume={market.volume:.0f} < {vol_min}")
            return None
        if market.liquidity < liq_min:
            _reject(f"liquidity={market.liquidity:.0f} < {liq_min}")
            return None
        if market.total_ask_size <= 0:
            _reject("no_ask_size")
            return None

        bid_ask_ratio = market.total_bid_size / max(market.total_ask_size, 1.0)
        ratio_min = 1.0 if lite else 2.5
        if bid_ask_ratio < ratio_min:
            _reject(f"bid_ratio={bid_ask_ratio:.2f} < {ratio_min}")
            return None

        bid_usd_total = market.total_bid_size * bid
        bid_usd_min = 80 if lite else 250
        if bid_usd_total < bid_usd_min:
            _reject(f"bid_usd={bid_usd_total:.0f} < {bid_usd_min}")
            return None
        best_bid_usd_min = 15 if lite else 40
        if market.best_bid_size * bid < best_bid_usd_min:
            _reject(f"best_bid_usd={market.best_bid_size*bid:.0f} < {best_bid_usd_min}")
            return None

        mids = history[-min_history:]
        ret_5m = mids[-1] - mids[max(0, len(mids) - 6)]
        ret_15m = mids[-1] - mids[0]
        max_dd_15m = max(mids) - mids[-1]
        ret_15_min = 0.0 if lite else 0.010
        ret_15_max = 0.20 if lite else 0.060
        if not (ret_15_min <= ret_15m <= ret_15_max):
            _reject(f"ret_15m={ret_15m:.4f} out [{ret_15_min},{ret_15_max}]")
            return None
        if ret_5m < -0.015:
            _reject(f"ret_5m={ret_5m:.4f} < -0.015")
            return None
        max_dd_limit = 0.05 if lite else 0.025
        if max_dd_15m > max_dd_limit:
            _reject(f"max_dd={max_dd_15m:.4f} > {max_dd_limit}")
            return None

        expected_exit_bid = min(0.97, ask + 0.045)
        net_edge = expected_exit_bid - ask
        net_edge_min = 0.020 if lite else 0.030
        if net_edge < net_edge_min:
            _reject(f"net_edge={net_edge:.4f} < {net_edge_min}")
            return None

        reason = (
            f"consensus_drift tte={tte_h:.1f}h ask={ask:.3f} spread={spread:.3f} "
            f"ret15m={ret_15m:.3f} bid_ratio={bid_ask_ratio:.1f} net_edge={net_edge:.3f}"
        )
        signal = self._build_signal(
            market=market,
            fair_price=expected_exit_bid,
            side=SignalSide.BUY,
            reference_price=ask,
            reason=reason,
            threshold=0.030,
        )
        signal.metadata.update(
            {
                "strategy_kind": "consensus_drift",
                "tte_h": tte_h,
                "bid_ask_ratio": round(bid_ask_ratio, 3),
                "ret_15m": round(ret_15m, 4),
                "ret_5m": round(ret_5m, 4),
                "max_dd_15m": round(max_dd_15m, 4),
                "expected_exit_bid": expected_exit_bid,
                "net_edge": round(net_edge, 4),
            }
        )
        return signal


CONSENSUS_DRIFT_EXIT_RULES: dict[str, Any] = {
    "tp_abs_cents": 0.045,
    "tp_pct": 0.060,
    "trailing_min_gain": 0.035,
    "trailing_drop": 0.015,
    "sl_abs_cents": 0.030,
    "max_hold_min": 90,
    "tte_min_floor": 45,
    "support_lost_bid_ratio": 1.20,
    "support_lost_spread": 0.025,
}
