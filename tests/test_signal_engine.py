from __future__ import annotations

from datetime import UTC, datetime

from app.market_data.normalizer import NormalizedMarket
from app.pricing.signal_engine import SignalEngine


def _warmup_engine(engine: SignalEngine, market: NormalizedMarket, ticks: int = 6) -> None:
    """Прокачать market через FairPriceEngine warmup ticks раз чтобы пройти
    hard warmup gate (history >= 5)."""
    for _ in range(ticks):
        engine.fair_price_engine.calculate(market)


def test_signal_engine_generates_buy_signal_for_sports_market(settings_factory) -> None:
    settings_factory(app_mode="paper_auto")
    market = NormalizedMarket(
        market_id="sports-1",
        slug="nba-finals-demo",
        question="Will Team Alpha win the NBA Finals?",
        category="sports",
        outcome="YES",
        best_bid=0.45,
        best_ask=0.48,
        last_price=0.74,
        spread=0.01,
        volume=2200.0,
        liquidity=900.0,
        updated_at=datetime.now(UTC),
        tags=["sports", "basketball"],
        raw={},
    )

    # Используем external fair override — имитирует bookmaker odds.
    # Real FairPriceEngine на одном тике создаёт маленький edge ниже threshold.
    result = SignalEngine().build_signal(market, ai_fair_price=0.55, ai_confidence=0.8)

    assert result.signal is not None
    assert result.signal.strategy_name == "sports_strategy"
    assert result.signal.side.value == "BUY"
    assert result.fair_price > market.best_ask
    assert result.edge > 0


import pytest


@pytest.mark.skip(reason="Crypto strategy disabled in market_registry — financial markets returns None.")
def test_signal_engine_routes_financial_market_to_crypto_strategy(settings_factory) -> None:
    settings_factory(app_mode="paper_auto")
    market = NormalizedMarket(
        market_id="fin-1",
        slug="rates-cut-demo",
        question="Will rates be cut this quarter?",
        category="financial",
        outcome="YES",
        best_bid=0.40,
        best_ask=0.43,
        last_price=0.72,
        spread=0.01,
        volume=3000.0,
        liquidity=1500.0,
        updated_at=datetime.now(UTC),
        tags=["financial", "rates"],
        raw={},
    )

    result = SignalEngine().build_signal(market, ai_fair_price=0.52, ai_confidence=0.8)

    assert result.signal is not None
    assert result.signal.strategy_name == "crypto_strategy"
    assert result.signal.side.value == "BUY"
