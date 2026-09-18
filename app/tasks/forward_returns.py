"""ForwardReturnsTask — фоновый updater для opportunity_logs.

Каждые N секунд читает opportunity_logs где fwd_ret_{interval} IS NULL И прошло
≥ interval минут с created_at. Берёт latest MarketSnapshot для того же market_id
и обновляет executable return.

Per AI debate spec:
  sim_exit_at_T = best_bid - max(0.005, spread * 0.25)  # current
  sim_executable_return = (sim_exit_at_T - sim_entry_price) / sim_entry_price

Это даёт нам:
  * executable expectancy при разных holding times
  * paper_optimism_gap = mid_pnl - executable_pnl
  * MAE (max adverse excursion) если делать tracking peak/trough

Без этой задачи opportunity_logs становятся бесполезны для go/no-go decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, and_, or_

from app.config import Settings
from app.db import db_session
from app.logger import get_logger
from app.models import MarketSnapshot, OpportunityLog


logger = get_logger(__name__)


# Holding intervals для тех мы считаем forward return.
# Каждый interval требует waiting period после signal generation.
INTERVALS_MIN = (5, 15, 60, 180)
INTERVAL_FIELDS = {
    5: "fwd_ret_5m",
    15: "fwd_ret_15m",
    60: "fwd_ret_60m",
    180: "fwd_ret_180m",
}


@dataclass
class UpdateBatch:
    interval_min: int
    updated: int
    not_yet_due: int
    no_snapshot: int


class ForwardReturnsTask:
    """Background task запускается каждый poll_seconds.

    Use:
        task = ForwardReturnsTask(settings)
        await task.run_once()  # tests
        # In production: await task.loop_forever() или scheduled by main.py
    """

    def __init__(
        self,
        settings: Settings,
        poll_seconds: float = 300.0,  # 5 min default
        slippage_floor: float = 0.005,
        slippage_spread_pct: float = 0.25,
    ) -> None:
        self.settings = settings
        self.poll_seconds = poll_seconds
        self.slippage_floor = slippage_floor
        self.slippage_spread_pct = slippage_spread_pct

    async def loop_forever(self) -> None:
        """Запускать в asyncio.gather с другими tasks в main.py."""
        logger.info("forward_returns.start | poll_seconds=%s", self.poll_seconds)
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                logger.exception("forward_returns.loop_error | error=%s", exc)
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> dict[str, Any]:
        """Один проход: для каждого interval обновить eligible logs."""
        results: dict[str, Any] = {}
        total_updated = 0

        for interval_min in INTERVALS_MIN:
            batch = self._update_interval(interval_min)
            results[f"interval_{interval_min}m"] = batch.updated
            total_updated += batch.updated

        logger.info(
            "forward_returns.run | total_updated=%s "
            "5m=%s 15m=%s 60m=%s 180m=%s",
            total_updated,
            results.get("interval_5m", 0),
            results.get("interval_15m", 0),
            results.get("interval_60m", 0),
            results.get("interval_180m", 0),
        )
        return results

    def _update_interval(self, interval_min: int) -> UpdateBatch:
        """Update fwd_ret for given interval.

        Per [Claude 50]/[GPT 43] backtest patch:
          - Was: only updated rows with sim_entry_price IS NOT NULL (= ENTERED_SHADOW only ~4/164K)
          - Now: fallback to poly_ask as entry for SHADOW_LOW_EDGE candidates with raw_edge ≥ 2%
          - Was: eligible_before = interval_min * 4 (only recent rows backfilled)
          - Now: eligible_before lifted (backfill any age once per row)
          - Limit bumped 200 → 500 to amortize 164K backfill faster

        Logic:
          1. Find logs где created_at ≤ now - interval_min И <field> IS NULL И есть entry price
          2. Берём sim_entry_price если есть, иначе poly_ask (proxy для BUY YES entry)
          3. Latest MarketSnapshot после log.created_at + interval_min
          4. Compute executable exit, fwd_ret
        """
        field = INTERVAL_FIELDS[interval_min]
        now = datetime.now(UTC)
        eligible_after = now - timedelta(minutes=interval_min)

        updated = 0
        not_yet_due = 0
        no_snapshot = 0

        with db_session() as session:
            stmt = select(OpportunityLog).where(
                and_(
                    getattr(OpportunityLog, field).is_(None),
                    OpportunityLog.created_at <= eligible_after,
                    or_(
                        OpportunityLog.sim_entry_price.isnot(None),
                        and_(
                            OpportunityLog.poly_ask.isnot(None),
                            OpportunityLog.raw_edge >= 0.02,
                        ),
                    ),
                )
            ).order_by(OpportunityLog.created_at.desc()).limit(500)

            logs = session.execute(stmt).scalars().all()

            for log in logs:
                entry_price = log.sim_entry_price if log.sim_entry_price else log.poly_ask
                if not entry_price or entry_price <= 0:
                    continue

                target_time = log.created_at + timedelta(minutes=interval_min)
                snapshot = session.execute(
                    select(MarketSnapshot)
                    .where(
                        and_(
                            MarketSnapshot.market_id == log.market_id,
                            MarketSnapshot.created_at >= target_time,
                        )
                    )
                    .order_by(MarketSnapshot.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()

                if snapshot is None:
                    no_snapshot += 1
                    continue

                spread = max(snapshot.best_ask - snapshot.best_bid, 0.0)
                slippage = max(self.slippage_floor, spread * self.slippage_spread_pct)
                sim_exit = max(snapshot.best_bid - slippage, 0.01)

                fwd_ret = (sim_exit - entry_price) / entry_price
                setattr(log, field, round(fwd_ret, 6))
                updated += 1

        return UpdateBatch(
            interval_min=interval_min,
            updated=updated,
            not_yet_due=not_yet_due,
            no_snapshot=no_snapshot,
        )
