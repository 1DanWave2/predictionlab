from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket


logger = get_logger(__name__)


class MarketCacheStats(BaseModel):
    size: int
    last_refresh: datetime | None = None


class MarketCache:
    def __init__(self, history_size: int = 30) -> None:
        self._items: dict[str, NormalizedMarket] = {}
        self._history: dict[str, deque[float]] = {}
        self._history_size = history_size
        self._last_refresh: datetime | None = None

    def bootstrap_from_db(self, window_minutes: int = 30) -> int:
        """После rebuild читаем последние window_minutes из MarketSnapshot
        и заполняем _history. Возвращает количество markets с восстановленной
        историей.

        Это вторая половина warmup-fix. Без неё стратегии типа consensus_drift
        и post_panic_rebound получат пустой history и не смогут оценить тренд.
        """
        from sqlalchemy import select
        from app.db import db_session
        from app.models import MarketSnapshot

        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)

        try:
            with db_session() as session:
                rows = session.execute(
                    select(
                        MarketSnapshot.market_id,
                        MarketSnapshot.best_bid,
                        MarketSnapshot.best_ask,
                    )
                    .where(MarketSnapshot.created_at >= cutoff)
                    .order_by(MarketSnapshot.market_id, MarketSnapshot.created_at)
                ).all()
        except Exception as exc:
            logger.warning("market_cache.bootstrap_failed | error=%s", exc)
            return 0

        for market_id, best_bid, best_ask in rows:
            mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
            if mid <= 0:
                continue
            hist = self._history.setdefault(market_id, deque(maxlen=self._history_size))
            hist.append(mid)

        restored = len(self._history)
        total_ticks = sum(len(h) for h in self._history.values())
        logger.info(
            "market_cache.bootstrap | markets=%s total_ticks=%s window_min=%s",
            restored, total_ticks, window_minutes,
        )
        return restored

    def upsert(self, market: NormalizedMarket) -> None:
        self._items[market.market_id] = market
        hist = self._history.setdefault(market.market_id, deque(maxlen=self._history_size))
        hist.append(market.mid_price)
        self._last_refresh = datetime.now(UTC)

    def get(self, market_id: str) -> NormalizedMarket | None:
        return self._items.get(market_id)

    def history(self, market_id: str) -> list[float]:
        h = self._history.get(market_id)
        return list(h) if h else []

    def all(self) -> list[NormalizedMarket]:
        return list(self._items.values())

    def stats(self) -> MarketCacheStats:
        return MarketCacheStats(size=len(self._items), last_refresh=self._last_refresh)

    def snapshot(self) -> dict[str, dict]:
        return {market_id: market.model_dump(mode="json") for market_id, market in self._items.items()}
