from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import func, select

from app.db import db_session
from app.models import Position


class PositionSnapshot(BaseModel):
    id: int | None = None
    market_id: str
    outcome: str = "YES"
    quantity: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


class PositionUpdate(BaseModel):
    position: PositionSnapshot
    realized_pnl_delta: float = 0.0
    closed_quantity: float = 0.0


class PositionManager:
    def get_position(self, market_id: str) -> PositionSnapshot | None:
        with db_session() as session:
            stmt = select(Position).where(Position.market_id == market_id)
            position = session.execute(stmt).scalar_one_or_none()
            if position is None:
                return None
            return self._to_snapshot(position)

    def apply_fill(
        self,
        market_id: str,
        outcome: str,
        side: str,
        size: float,
        price: float,
        mark_price: float | None = None,
        bucket: str = "experiment",
        cluster_key: str | None = None,
    ) -> PositionUpdate:
        side = side.upper()
        with db_session() as session:
            stmt = select(Position).where(Position.market_id == market_id)
            position = session.execute(stmt).scalar_one_or_none()
            if position is None:
                position = Position(
                    market_id=market_id,
                    outcome=outcome,
                    quantity=0.0,
                    avg_price=0.0,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    bucket=bucket,
                    cluster_key=cluster_key,
                )
                session.add(position)
                session.flush()
            else:
                # Update bucket/cluster on fresh BUY into closed position.
                if side == "BUY" and position.quantity == 0:
                    position.bucket = bucket
                    position.cluster_key = cluster_key

            realized_pnl_delta = 0.0
            closed_quantity = 0.0

            if side == "BUY":
                total_cost = (position.quantity * position.avg_price) + (size * price)
                position.quantity = round(position.quantity + size, 6)
                position.avg_price = round(total_cost / position.quantity, 6) if position.quantity > 0 else 0.0
            elif side == "SELL":
                closed_quantity = round(min(size, position.quantity), 6)
                if closed_quantity > 0:
                    realized_pnl_delta = round((price - position.avg_price) * closed_quantity, 6)
                    position.quantity = round(position.quantity - closed_quantity, 6)
                    position.realized_pnl = round(position.realized_pnl + realized_pnl_delta, 6)
                    if position.quantity <= 0:
                        position.quantity = 0.0
                        position.avg_price = 0.0
                else:
                    closed_quantity = 0.0

            current_mark = mark_price if mark_price is not None else price
            position.unrealized_pnl = self._calculate_unrealized(
                quantity=position.quantity,
                avg_price=position.avg_price,
                mark_price=current_mark,
            )
            session.flush()
            session.refresh(position)
            return PositionUpdate(
                position=self._to_snapshot(position),
                realized_pnl_delta=realized_pnl_delta,
                closed_quantity=closed_quantity,
            )

    def update_unrealized(self, market_id: str, mark_price: float) -> PositionSnapshot | None:
        with db_session() as session:
            stmt = select(Position).where(Position.market_id == market_id)
            position = session.execute(stmt).scalar_one_or_none()
            if position is None:
                return None

            position.unrealized_pnl = self._calculate_unrealized(
                quantity=position.quantity,
                avg_price=position.avg_price,
                mark_price=mark_price,
            )
            session.flush()
            session.refresh(position)
            return self._to_snapshot(position)

    def count_open_positions(self) -> int:
        with db_session() as session:
            stmt = select(func.count()).select_from(Position).where(Position.quantity > 0)
            return int(session.execute(stmt).scalar_one())

    def total_realized_pnl(self) -> float:
        with db_session() as session:
            stmt = select(func.coalesce(func.sum(Position.realized_pnl), 0.0))
            return float(session.execute(stmt).scalar_one())

    @staticmethod
    def _calculate_unrealized(quantity: float, avg_price: float, mark_price: float) -> float:
        if quantity <= 0:
            return 0.0
        return round((mark_price - avg_price) * quantity, 6)

    @staticmethod
    def _to_snapshot(position: Position) -> PositionSnapshot:
        return PositionSnapshot(
            id=position.id,
            market_id=position.market_id,
            outcome=position.outcome,
            quantity=position.quantity,
            avg_price=position.avg_price,
            realized_pnl=position.realized_pnl,
            unrealized_pnl=position.unrealized_pnl,
        )
