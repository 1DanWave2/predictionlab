from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.market_data.normalizer import NormalizedMarket


def passes_basic_filters(market: NormalizedMarket) -> bool:
    if not market.question.strip():
        return False
    if market.best_bid < 0 or market.best_ask < 0:
        return False
    if market.best_bid > 1 or market.best_ask > 1:
        return False
    if market.best_ask and market.best_bid and market.best_ask < market.best_bid:
        return False
    if market.spread > 0.20:
        return False
    if market.volume <= 0:
        return False
    if market.last_price > 0 and (market.last_price < 0.30 or market.last_price > 0.80):
        return False
    mid = market.mid_price
    if mid < 0.30 or mid > 0.80:
        return False
    if market.liquidity < 1000:
        return False
    min_hours = 2.0 if market.category == "sports" else 12.0
    if hours_to_resolution(market) < min_hours:
        return False
    return True


def hours_to_resolution(market: NormalizedMarket) -> float:
    end_raw = market.raw.get("endDate") or market.raw.get("end_date") or market.raw.get("endDateIso")
    if not end_raw:
        return 9_999.0
    if isinstance(end_raw, datetime):
        end_dt = end_raw if end_raw.tzinfo else end_raw.replace(tzinfo=UTC)
    else:
        try:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return 9_999.0
    delta: timedelta = end_dt - datetime.now(UTC)
    return delta.total_seconds() / 3600.0
