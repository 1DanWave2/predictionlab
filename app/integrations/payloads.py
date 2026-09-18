from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    SIGNAL_EVENT = "signal_event"
    PAPER_TRADE_OPEN_EVENT = "paper_trade_open_event"
    PAPER_TRADE_CLOSE_EVENT = "paper_trade_close_event"
    RISK_ALERT_EVENT = "risk_alert_event"
    DAILY_REPORT_EVENT = "daily_report_event"


class BaseEventPayload(BaseModel):
    event_type: EventType
    source: str = "polymarket_bot"
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    bot_mode: str = "paper_auto"


class SignalPayload(BaseEventPayload):
    event_type: EventType = EventType.SIGNAL_EVENT
    market_id: str
    slug: str
    category: str
    strategy: str
    side: str
    price: float
    fair_price: float
    edge: float
    confidence: float
    bid: float
    ask: float
    spread: float
    volume: float
    reason: str


class PaperTradeOpenPayload(BaseEventPayload):
    event_type: EventType = EventType.PAPER_TRADE_OPEN_EVENT
    order_id: int
    market_id: str
    side: str
    strategy: str
    price: float
    size: float
    quantity: float
    avg_price: float
    realized_pnl: float
    unrealized_pnl: float
    note: str = ""


class PaperTradeClosePayload(BaseEventPayload):
    event_type: EventType = EventType.PAPER_TRADE_CLOSE_EVENT
    order_id: int
    market_id: str
    side: str
    strategy: str
    price: float
    size: float
    closed_quantity: float
    realized_pnl_delta: float
    realized_pnl_total: float
    unrealized_pnl: float
    note: str = ""


class RiskAlertPayload(BaseEventPayload):
    event_type: EventType = EventType.RISK_ALERT_EVENT
    severity: str = "warning"
    reason: str
    market_id: str | None = None
    side: str | None = None
    strategy: str | None = None
    order: dict[str, Any] = Field(default_factory=dict)


class DailyReportPayload(BaseEventPayload):
    event_type: EventType = EventType.DAILY_REPORT_EVENT
    report_date: str
    paused: bool
    total_trades: int
    open_positions: int
    closed_positions: int
    total_realized_pnl: float
    total_unrealized_pnl: float
    gross_exposure: float
    winning_positions: int
    losing_positions: int

