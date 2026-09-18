from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.market_data.normalizer import NormalizedMarket


class SignalSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class StrategySignal(BaseModel):
    market_id: str
    slug: str
    category: str
    strategy_name: str
    side: SignalSide
    fair_price: float = Field(ge=0.0, le=1.0)
    reference_price: float = Field(ge=0.0, le=1.0)
    edge: float
    edge_bps: float
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    size_multiplier: float = Field(default=1.0, ge=0.1, le=3.0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseStrategy(ABC):
    name = "base"
    supported_categories: tuple[str, ...] = ()
    entry_threshold: float = 0.02
    exit_threshold: float = 0.02
    min_volume: float = 100.0
    max_spread: float = 0.12

    def supports(self, market: NormalizedMarket) -> bool:
        return market.category in self.supported_categories

    @abstractmethod
    def evaluate(self, market: NormalizedMarket, fair_price: float) -> StrategySignal | None:
        raise NotImplementedError

    def _build_signal(
        self,
        market: NormalizedMarket,
        fair_price: float,
        side: SignalSide,
        reference_price: float,
        reason: str,
        threshold: float,
    ) -> StrategySignal:
        edge = round(fair_price - reference_price, 6)
        edge_ratio = abs(edge) / max(threshold, 0.0001)
        confidence = min(1.0, max(edge_ratio, 0.05))
        size_mult = min(edge_ratio * 0.5, 1.5)
        return StrategySignal(
            market_id=market.market_id,
            slug=market.slug,
            category=market.category,
            strategy_name=self.name,
            side=side,
            fair_price=round(fair_price, 6),
            reference_price=round(reference_price, 6),
            edge=edge,
            edge_bps=round(edge * 10_000, 2),
            confidence=round(confidence, 4),
            reason=reason,
            size_multiplier=round(max(0.3, size_mult), 4),
            metadata={
                "best_bid": market.best_bid,
                "best_ask": market.best_ask,
                "spread": market.spread,
                "volume": market.volume,
            },
        )

    def _market_is_eligible(self, market: NormalizedMarket) -> bool:
        if market.volume < self.min_volume:
            return False
        if market.spread > self.max_spread:
            return False
        return True
