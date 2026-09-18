from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from sqlalchemy import func as sa_func

from app.config import TradingMode, get_settings
from app.db import db_session
from app.models import PaperOrder, Position


router = APIRouter()


class SetModeRequest(BaseModel):
    mode: str


class RuntimeState:
    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self.paused = False
        self.mode = TradingMode.PAPER_AUTO.value
        self.last_tick_at: datetime | None = None
        self.last_report_at: datetime | None = None
        self.last_scan_markets = 0
        self.last_signals = 0
        self.last_orders = 0
        self.last_error: str | None = None
        self.manual_actions: list[dict[str, Any]] = []

    def initialize(self, mode: str) -> None:
        if mode == TradingMode.LIVE_AUTO.value:
            self.mode = TradingMode.PAPER_AUTO.value
            return
        self.mode = mode

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def set_mode(self, mode: str) -> None:
        if mode == TradingMode.LIVE_AUTO.value:
            raise ValueError("live_auto is blocked in this MVP")
        if mode not in {TradingMode.SHADOW.value, TradingMode.PAPER_AUTO.value}:
            raise ValueError("mode must be shadow or paper_auto")
        self.mode = mode

    def record_tick(self, scan_result: dict[str, Any], trade_result: dict[str, Any]) -> None:
        self.last_tick_at = datetime.now(UTC)
        self.last_scan_markets = int(scan_result.get("markets_seen", 0))
        self.last_signals = len(scan_result.get("opportunities", []))
        self.last_orders = int(trade_result.get("orders_created", 0))
        self.last_error = None

    def record_report(self) -> None:
        self.last_report_at = datetime.now(UTC)

    def record_error(self, error: str) -> None:
        self.last_error = error

    def record_manual_action(self, action: str, payload: dict[str, Any]) -> None:
        self.manual_actions.append(
            {
                "action": action,
                "payload": payload,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )
        self.manual_actions = self.manual_actions[-20:]

    def snapshot(self) -> dict[str, Any]:
        settings = get_settings()
        initial = settings.initial_paper_balance

        with db_session() as session:
            total_realized = float(
                session.execute(
                    select(sa_func.coalesce(sa_func.sum(Position.realized_pnl), 0.0))
                ).scalar_one()
            )
            total_unrealized = float(
                session.execute(
                    select(sa_func.coalesce(sa_func.sum(Position.unrealized_pnl), 0.0))
                ).scalar_one()
            )
            total_cost = float(
                session.execute(
                    select(sa_func.coalesce(
                        sa_func.sum(Position.quantity * Position.avg_price), 0.0
                    ))
                ).scalar_one()
            )

        current_balance = round(initial + total_realized + total_unrealized - total_cost, 2)

        return {
            "started_at": self.started_at.isoformat(),
            "paused": self.paused,
            "mode": self.mode,
            "initial_balance": initial,
            "current_balance": current_balance,
            "in_positions": round(total_cost, 2),
            "realized_pnl": round(total_realized, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "total_pnl": round(total_realized + total_unrealized, 2),
            "roi_pct": round((total_realized + total_unrealized) / initial * 100, 1) if initial > 0 else 0.0,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_report_at": self.last_report_at.isoformat() if self.last_report_at else None,
            "last_scan_markets": self.last_scan_markets,
            "last_signals": self.last_signals,
            "last_orders": self.last_orders,
            "last_error": self.last_error,
            "manual_actions_count": len(self.manual_actions),
        }


runtime_state = RuntimeState()


def _serialize_position(position: Position) -> dict[str, Any]:
    return {
        "id": position.id,
        "market_id": position.market_id,
        "outcome": position.outcome,
        "quantity": position.quantity,
        "avg_price": position.avg_price,
        "realized_pnl": position.realized_pnl,
        "unrealized_pnl": position.unrealized_pnl,
        "created_at": position.created_at.isoformat() if position.created_at else None,
        "updated_at": position.updated_at.isoformat() if position.updated_at else None,
    }


def _serialize_trade(trade: PaperOrder) -> dict[str, Any]:
    return {
        "id": trade.id,
        "market_id": trade.market_id,
        "outcome": trade.outcome,
        "side": trade.side,
        "price": trade.price,
        "size": trade.size,
        "status": trade.status,
        "mode": trade.mode,
        "strategy": trade.strategy,
        "note": trade.note,
        "created_at": trade.created_at.isoformat() if trade.created_at else None,
        "updated_at": trade.updated_at.isoformat() if trade.updated_at else None,
    }


@router.get("/status")
async def status() -> dict[str, Any]:
    with db_session() as session:
        positions = session.execute(select(Position)).scalars().all()
        trades = session.execute(select(PaperOrder).order_by(PaperOrder.id.desc())).scalars().all()
    return {
        **runtime_state.snapshot(),
        "positions_count": len(positions),
        "trades_count": len(trades),
        "open_positions": sum(1 for item in positions if item.quantity > 0),
        "safe_mode": True,
    }


@router.get("/positions")
async def positions() -> list[dict[str, Any]]:
    with db_session() as session:
        rows = session.execute(select(Position).order_by(Position.id.asc())).scalars().all()
    return [_serialize_position(row) for row in rows]


@router.get("/trades")
async def trades() -> list[dict[str, Any]]:
    with db_session() as session:
        rows = session.execute(select(PaperOrder).order_by(PaperOrder.id.desc())).scalars().all()
    return [_serialize_trade(row) for row in rows]


@router.post("/pause")
async def pause() -> dict[str, Any]:
    runtime_state.pause()
    return {"status": "paused", "mode": runtime_state.mode}


@router.post("/resume")
async def resume() -> dict[str, Any]:
    runtime_state.resume()
    return {"status": "running", "mode": runtime_state.mode}


@router.post("/set-mode")
async def set_mode(payload: SetModeRequest) -> dict[str, Any]:
    try:
        runtime_state.set_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "mode": runtime_state.mode}


@router.post("/reset-paper-account")
async def reset_paper_account() -> dict[str, Any]:
    settings = get_settings()
    if settings.enable_live_trading:
        raise HTTPException(status_code=403, detail="live trading must remain disabled")

    with db_session() as session:
        session.execute(delete(PaperOrder))
        session.execute(delete(Position))

    runtime_state.last_orders = 0
    runtime_state.last_signals = 0
    runtime_state.last_scan_markets = 0
    return {"status": "reset", "mode": runtime_state.mode}
