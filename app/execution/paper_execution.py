from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.db import db_session
from app.models import PaperOrder


class PaperOrderRequest(BaseModel):
    market_id: str
    outcome: str = "YES"
    side: str
    price: float = Field(gt=0.0, le=1.0)
    size: float = Field(gt=0.0)
    mode: str
    strategy: str
    note: str = ""
    # bucket: "experiment" (default — старая стратегия) | "core_sniper_live" (sniper)
    bucket: str = "experiment"
    # cluster_key: для correlation guard в risk_manager. Если null — guard не активен.
    cluster_key: str | None = None
    # Market context для risk_score (per [GPT 14]).
    liquidity: float = 0.0
    hours_to_resolution: float = 9999.0
    spread: float = 0.0
    # Matchup market flag (per [GPT 16]): "X vs Y" individual sports/player matchup,
    # подвержены news-driven gaps. Internal strategies должны их пропускать.
    is_matchup: bool = False


class PaperExecutionResult(BaseModel):
    order_id: int
    market_id: str
    side: str
    price: float
    size: float
    status: str
    executed_at: datetime


class PaperExecutionEngine:
    async def execute(self, order: PaperOrderRequest) -> PaperExecutionResult:
        with db_session() as session:
            db_order = PaperOrder(
                market_id=order.market_id,
                outcome=order.outcome,
                side=order.side,
                price=order.price,
                size=order.size,
                status="filled",
                mode=order.mode,
                strategy=order.strategy,
                note=order.note,
            )
            session.add(db_order)
            session.flush()
            session.refresh(db_order)

        return PaperExecutionResult(
            order_id=db_order.id,
            market_id=order.market_id,
            side=order.side,
            price=order.price,
            size=order.size,
            status="filled",
            executed_at=datetime.now(UTC),
        )
