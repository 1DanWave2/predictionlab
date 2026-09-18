from __future__ import annotations

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.execution.paper_execution import PaperOrderRequest
from app.execution.position_manager import PositionManager
from app.execution.risk_manager import RiskManager


def test_live_trading_is_blocked_by_default(monkeypatch) -> None:
    monkeypatch.setenv("APP_MODE", "live_auto")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    get_settings.cache_clear()

    try:
        Settings()
        assert False, "Expected live_auto to be blocked"
    except ValidationError:
        pass


def test_risk_manager_blocks_market_on_cooldown(settings_factory) -> None:
    settings = settings_factory(app_mode="paper_auto")
    risk_manager = RiskManager(settings)

    order = PaperOrderRequest(
        market_id="sports-1",
        outcome="YES",
        side="BUY",
        price=0.45,
        size=5,
        mode="paper_auto",
        strategy="sports_strategy",
        note="cooldown test",
    )

    first = risk_manager.check_order(order, current_position=None)
    assert first.allowed is True

    risk_manager.register_fill(market_id="sports-1", side="BUY", realized_pnl_delta=0.0)
    second = risk_manager.check_order(order, current_position=None)

    # Setup cooldown (10 min, per [GPT 10] Q5) перекрывает старый market_cooldown.
    # Reason starts with "setup_cooldown_active" — обе guarantee block после fill.
    assert second.allowed is False
    assert second.reason.startswith("setup_cooldown_active")


def test_risk_manager_blocks_when_max_positions_reached(settings_factory) -> None:
    settings = settings_factory(app_mode="paper_auto")
    risk_manager = RiskManager(settings)
    position_manager = PositionManager()

    position_manager.apply_fill(
        market_id="mkt-1",
        outcome="YES",
        side="BUY",
        size=5,
        price=0.40,
        mark_price=0.40,
    )
    position_manager.apply_fill(
        market_id="mkt-2",
        outcome="YES",
        side="BUY",
        size=5,
        price=0.40,
        mark_price=0.40,
    )

    order = PaperOrderRequest(
        market_id="mkt-3",
        outcome="YES",
        side="BUY",
        price=0.42,
        size=5,
        mode="paper_auto",
        strategy="event_strategy",
        note="max positions test",
    )

    decision = risk_manager.check_order(order, current_position=None)

    assert decision.allowed is False
    assert decision.reason == "max simultaneous positions reached"
