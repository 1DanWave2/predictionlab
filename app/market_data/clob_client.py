from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.logger import get_logger


logger = get_logger(__name__)


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0)


class OrderBookSnapshot(BaseModel):
    market_id: str
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    best_bid: float = Field(default=0.0, ge=0.0, le=1.0)
    best_ask: float = Field(default=0.0, ge=0.0, le=1.0)
    total_bid_size: float = Field(default=0.0, ge=0.0)
    total_ask_size: float = Field(default=0.0, ge=0.0)
    updated_at: datetime

    @property
    def spread(self) -> float:
        if self.best_bid <= 0 or self.best_ask <= 0:
            return 0.0
        return round(self.best_ask - self.best_bid, 6)


class ClobClient:
    """CLOB abstraction for top-of-book snapshots.

    TODO:
    Replace endpoint parsing with exact Polymarket CLOB schema once exchange
    integration is enabled. The MVP relies on this boundary and mock data.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._market_cycles: dict[str, int] = {}

    async def get_order_book(self, market_id: str, token_id: str | None = None) -> OrderBookSnapshot:
        if self.settings.use_mock_data:
            book = self._mock_order_book(market_id)
            logger.info(
                "clob.get_order_book | source=mock market_id=%s best_bid=%.4f best_ask=%.4f",
                market_id,
                book.best_bid,
                book.best_ask,
            )
            return book

        if not token_id:
            logger.warning("clob.get_order_book | market_id=%s no token_id, skip", market_id)
            return self._empty_book(market_id)

        url = f"{self.settings.clob_base_url.rstrip('/')}/book"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params={"token_id": token_id})
                response.raise_for_status()
                payload = response.json()
            book = self._parse_order_book(market_id, payload)
            logger.info(
                "clob.get_order_book | source=remote market_id=%s best_bid=%.4f best_ask=%.4f",
                market_id,
                book.best_bid,
                book.best_ask,
            )
            return book
        except Exception as exc:
            logger.warning(
                "clob.get_order_book_failed | market_id=%s error=%s",
                market_id,
                exc,
            )
            return self._empty_book(market_id)

    def _parse_order_book(self, market_id: str, payload: Any) -> OrderBookSnapshot:
        raw_bids = payload.get("bids", []) if isinstance(payload, dict) else []
        raw_asks = payload.get("asks", []) if isinstance(payload, dict) else []
        bids = [self._parse_level(level) for level in raw_bids]
        asks = [self._parse_level(level) for level in raw_asks]
        bids = [level for level in bids if level is not None]
        asks = [level for level in asks if level is not None]

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 0.0
        total_bid_size = round(sum(level.size for level in bids), 6)
        total_ask_size = round(sum(level.size for level in asks), 6)
        return OrderBookSnapshot(
            market_id=market_id,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            total_bid_size=total_bid_size,
            total_ask_size=total_ask_size,
            updated_at=datetime.now(UTC),
        )

    @staticmethod
    def _parse_level(level: Any) -> OrderBookLevel | None:
        if isinstance(level, dict):
            price = level.get("price")
            size = level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            return None

        try:
            return OrderBookLevel(price=float(price), size=float(size))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _empty_book(market_id: str) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            market_id=market_id,
            bids=[],
            asks=[],
            best_bid=0.0,
            best_ask=0.0,
            total_bid_size=0.0,
            total_ask_size=0.0,
            updated_at=datetime.now(UTC),
        )

    def _mock_order_book(self, market_id: str) -> OrderBookSnapshot:
        cycle = self._market_cycles.get(market_id, 0) + 1
        self._market_cycles[market_id] = cycle

        templates: dict[str, list[tuple[float, float, float, float]]] = {
            "sports-1": [(0.45, 140.0, 0.48, 160.0), (0.56, 180.0, 0.60, 150.0)],
            "crypto-1": [(0.40, 220.0, 0.43, 200.0), (0.53, 250.0, 0.56, 240.0)],
            "event-1": [(0.58, 130.0, 0.61, 125.0), (0.49, 190.0, 0.52, 180.0)],
        }
        best_bid, bid_size, best_ask, ask_size = templates.get(
            market_id,
            [(0.40, 100.0, 0.44, 120.0)],
        )[(cycle - 1) % 2]
        bids = [OrderBookLevel(price=best_bid, size=bid_size)]
        asks = [OrderBookLevel(price=best_ask, size=ask_size)]
        return OrderBookSnapshot(
            market_id=market_id,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            total_bid_size=bid_size,
            total_ask_size=ask_size,
            updated_at=datetime.now(UTC),
        )
