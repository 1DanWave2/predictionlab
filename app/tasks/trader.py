from __future__ import annotations

from typing import Any

from app.config import Settings
from app.execution.execution_router import ExecutionRouter
from app.logger import get_logger


logger = get_logger(__name__)


class TraderTask:
    def __init__(self, settings: Settings, router: ExecutionRouter) -> None:
        self.settings = settings
        self.router = router

    async def run_once(self, opportunities: list[dict[str, Any]]) -> dict[str, Any]:
        orders_created = 0
        opened = 0
        closed = 0
        execution_results: list[dict[str, Any]] = []

        for opportunity in opportunities:
            result = await self.router.submit_order(
                {
                    "market_id": opportunity["market_id"],
                    "side": str(opportunity["side"]).upper(),
                    "price": float(opportunity["price"]),
                    "size": float(opportunity["size"]),
                    "mode": self.settings.app_mode,
                    "strategy": opportunity["strategy"],
                    "note": (
                        f"reason={opportunity['reason']} "
                        f"fair_price={opportunity['fair_price']:.4f} edge={opportunity['edge']:.4f}"
                    ),
                    "outcome": opportunity["outcome"],
                    "bucket": opportunity.get("bucket", "experiment"),
                    "liquidity": float(opportunity.get("liquidity", 0.0)),
                    "hours_to_resolution": float(opportunity.get("hours_to_resolution", 9999.0)),
                    "spread": float(opportunity.get("spread", 0.0)),
                    "is_matchup": bool(opportunity.get("is_matchup", False)),
                }
            )
            execution_results.append(result)

            if result["status"] in {"filled", "shadow_skipped"}:
                orders_created += 1
                if str(opportunity["side"]).upper() == "BUY":
                    opened += 1
                elif str(opportunity["side"]).upper() == "SELL":
                    closed += 1

        logger.info(
            "trader.completed | mode=%s opportunities=%s orders=%s opened=%s closed=%s",
            self.settings.app_mode,
            len(opportunities),
            orders_created,
            opened,
            closed,
        )
        return {
            "orders_created": orders_created,
            "opened": opened,
            "closed": closed,
            "results": execution_results,
        }
