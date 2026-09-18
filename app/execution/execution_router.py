from __future__ import annotations

from typing import Any

from app.config import Settings
from app.execution.paper_execution import PaperExecutionEngine, PaperOrderRequest
from app.execution.position_manager import PositionManager
from app.execution.risk_manager import RiskManager
from app.integrations.funnel_log import funnel_log
from app.logger import get_logger


logger = get_logger(__name__)


class ExecutionRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paper_execution = PaperExecutionEngine()
        self.position_manager = PositionManager()
        self.risk_manager = RiskManager(settings)

    async def submit_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        order = PaperOrderRequest.model_validate(order_payload)

        if self.settings.is_shadow:
            logger.info(
                "execution.shadow_skip | market_id=%s side=%s price=%.4f size=%.4f strategy=%s",
                order.market_id,
                order.side,
                order.price,
                order.size,
                order.strategy,
            )
            return {"status": "shadow_skipped", "order": order.model_dump(mode="json")}

        if self.settings.is_live_auto or self.settings.enable_live_trading:
            logger.error("execution.live_blocked | market_id=%s", order.market_id)
            return {"status": "blocked", "reason": "live trading is disabled"}

        current_position = self.position_manager.get_position(order.market_id)
        risk = self.risk_manager.check_order(order, current_position)
        if not risk.allowed:
            logger.info(
                "execution.risk_rejected | market_id=%s side=%s reason=%s",
                order.market_id,
                order.side,
                risk.reason,
            )
            funnel_log(
                stage="risk_rejected",
                market_id=order.market_id,
                side=order.side,
                strategy=order.strategy,
                bucket=order.bucket,
                price=order.price,
                requested_size=order.size,
                requested_notional=round(order.price * order.size, 4),
                liquidity=order.liquidity,
                hours_to_resolution=order.hours_to_resolution,
                spread=order.spread,
                reason=risk.reason,
            )
            return {"status": "risk_rejected", "reason": risk.reason, "order": order.model_dump(mode="json")}

        effective_size = float(risk.clipped_size or order.size)
        if effective_size <= 0:
            return {"status": "skipped", "reason": "effective size is zero"}

        effective_order = order.model_copy(update={"size": effective_size})
        execution = await self.paper_execution.execute(effective_order)
        position_update = self.position_manager.apply_fill(
            market_id=effective_order.market_id,
            outcome=effective_order.outcome,
            side=effective_order.side,
            size=effective_order.size,
            price=effective_order.price,
            mark_price=effective_order.price,
            bucket=getattr(effective_order, "bucket", "experiment"),
            cluster_key=getattr(effective_order, "cluster_key", None),
        )
        self.risk_manager.register_fill(
            market_id=effective_order.market_id,
            side=effective_order.side,
            realized_pnl_delta=position_update.realized_pnl_delta,
            bucket=getattr(effective_order, "bucket", "experiment"),
        )

        logger.info(
            "execution.filled | market_id=%s side=%s size=%.4f price=%.4f qty=%.4f realized=%.4f unrealized=%.4f",
            effective_order.market_id,
            effective_order.side,
            effective_order.size,
            effective_order.price,
            position_update.position.quantity,
            position_update.position.realized_pnl,
            position_update.position.unrealized_pnl,
        )
        funnel_log(
            stage="order_filled",
            market_id=effective_order.market_id,
            side=effective_order.side,
            strategy=effective_order.strategy,
            bucket=effective_order.bucket,
            price=effective_order.price,
            effective_size=effective_order.size,
            effective_notional=round(effective_order.price * effective_order.size, 4),
            requested_size=order.size,
            clipped=(risk.clipped_size is not None and risk.clipped_size != order.size),
            risk_reason=risk.reason,
            realized_pnl_delta=position_update.realized_pnl_delta,
        )
        return {
            "status": execution.status,
            "order_id": execution.order_id,
            "market_id": execution.market_id,
            "side": execution.side,
            "size": execution.size,
            "price": execution.price,
            "position": position_update.position.model_dump(mode="json"),
            "realized_pnl_delta": position_update.realized_pnl_delta,
            "closed_quantity": position_update.closed_quantity,
        }
