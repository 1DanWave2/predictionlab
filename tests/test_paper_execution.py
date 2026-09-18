from __future__ import annotations

import asyncio

from app.config import get_settings
from app.execution.execution_router import ExecutionRouter
from app.execution.position_manager import PositionManager


def test_paper_execution_opens_and_closes_position(settings_factory) -> None:
    settings = settings_factory(app_mode="paper_auto")
    router = ExecutionRouter(settings)
    position_manager = PositionManager()

    buy_result = asyncio.run(
        router.submit_order(
            {
                "market_id": "sports-1",
                "outcome": "YES",
                "side": "BUY",
                "price": 0.45,
                "size": 5.0,
                "mode": "paper_auto",
                "strategy": "sports_strategy",
                "note": "open trade",
            }
        )
    )

    assert buy_result["status"] == "filled"
    assert buy_result["position"]["quantity"] == 5.0
    assert buy_result["position"]["avg_price"] == 0.45

    sell_result = asyncio.run(
        router.submit_order(
            {
                "market_id": "sports-1",
                "outcome": "YES",
                "side": "SELL",
                "price": 0.62,
                "size": 5.0,
                "mode": "paper_auto",
                "strategy": "sports_strategy",
                "note": "close trade",
            }
        )
    )

    final_position = position_manager.get_position("sports-1")

    assert sell_result["status"] == "filled"
    assert sell_result["closed_quantity"] == 5.0
    assert sell_result["realized_pnl_delta"] > 0
    assert final_position is not None
    assert final_position.quantity == 0.0
    assert final_position.realized_pnl > 0


def test_shadow_mode_skips_real_paper_execution(settings_factory) -> None:
    settings = settings_factory(app_mode="shadow")
    router = ExecutionRouter(settings)
    position_manager = PositionManager()

    result = asyncio.run(
        router.submit_order(
            {
                "market_id": "shadow-1",
                "outcome": "YES",
                "side": "BUY",
                "price": 0.48,
                "size": 5.0,
                "mode": "shadow",
                "strategy": "event_strategy",
                "note": "shadow trade",
            }
        )
    )

    assert result["status"] == "shadow_skipped"
    assert position_manager.get_position("shadow-1") is None


def test_enable_live_trading_is_false_by_default(settings_factory) -> None:
    settings = settings_factory(app_mode="paper_auto")
    assert settings.enable_live_trading is False
    assert get_settings().enable_live_trading is False
