from __future__ import annotations

from pydantic import BaseModel

from app.market_data.normalizer import NormalizedMarket
from app.pricing.fair_price_engine import FairPriceEngine, FairPriceEstimate
from app.strategies.base import StrategySignal
from app.strategies.market_registry import MarketRegistry


MIN_CONFIDENCE = 0.40
AI_MIN_CONFIDENCE = 0.60


class SignalResult(BaseModel):
    market_id: str
    fair_price: float
    mark_price: float
    spread: float
    edge: float
    fair_price_confidence: float
    signal: StrategySignal | None = None


class SignalEngine:
    def __init__(self) -> None:
        self.fair_price_engine = FairPriceEngine()
        self.registry = MarketRegistry()

    def build_signal(
        self,
        market: NormalizedMarket,
        ai_fair_price: float | None = None,
        ai_confidence: float | None = None,
    ) -> SignalResult:
        if ai_fair_price is not None:
            fair = ai_fair_price
            conf = ai_confidence if ai_confidence is not None else 0.5
            mark = market.mid_price
            spread = market.spread
            min_conf = AI_MIN_CONFIDENCE
        else:
            fpr: FairPriceEstimate = self.fair_price_engine.calculate(market)
            fair = fpr.fair_price
            conf = fpr.confidence
            mark = fpr.mark_price
            spread = fpr.spread
            min_conf = MIN_CONFIDENCE

        signal = None
        if conf >= min_conf:
            signal = self.registry.evaluate(market, fair)

        reference_price = market.best_ask if signal is None or signal.side == "BUY" else market.best_bid
        edge = round(fair - reference_price, 6)
        return SignalResult(
            market_id=market.market_id,
            fair_price=fair,
            mark_price=mark,
            spread=spread,
            edge=edge,
            fair_price_confidence=conf,
            signal=signal,
        )
