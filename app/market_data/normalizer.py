from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.market_data.clob_client import OrderBookSnapshot


class NormalizedMarket(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    market_id: str
    slug: str
    question: str
    category: str
    outcome: str = "YES"
    best_bid: float = Field(default=0.0, ge=0.0, le=1.0)
    best_ask: float = Field(default=0.0, ge=0.0, le=1.0)
    last_price: float = Field(default=0.0, ge=0.0, le=1.0)
    spread: float = Field(default=0.0, ge=0.0)
    volume: float = Field(default=0.0, ge=0.0)
    liquidity: float = Field(default=0.0, ge=0.0)
    total_bid_size: float = Field(default=0.0, ge=0.0)
    total_ask_size: float = Field(default=0.0, ge=0.0)
    best_bid_size: float = Field(default=0.0, ge=0.0)
    best_ask_size: float = Field(default=0.0, ge=0.0)
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def mid_price(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return round((self.best_bid + self.best_ask) / 2, 6)
        if self.last_price > 0:
            return round(self.last_price, 6)
        return 0.5


def normalize_market(
    raw_market: Mapping[str, Any],
    order_book: OrderBookSnapshot | None = None,
) -> NormalizedMarket:
    tags = _normalize_tags(raw_market.get("tags"))
    category = _infer_category(raw_market, tags)

    best_bid = _as_float(
        raw_market.get("best_bid")
        or raw_market.get("bestBid")
        or raw_market.get("bid")
        or raw_market.get("yes_bid")
        or raw_market.get("yesBid")
    )
    best_ask = _as_float(
        raw_market.get("best_ask")
        or raw_market.get("bestAsk")
        or raw_market.get("ask")
        or raw_market.get("yes_ask")
        or raw_market.get("yesAsk")
    )
    last_price = _as_float(
        raw_market.get("last_price")
        or raw_market.get("lastPrice")
        or raw_market.get("last_trade_price")
        or raw_market.get("price")
    )

    if order_book is not None:
        if order_book.best_bid > 0:
            best_bid = order_book.best_bid
        if order_book.best_ask > 0:
            best_ask = order_book.best_ask

    spread = round(max(best_ask - best_bid, 0.0), 6) if best_ask and best_bid else 0.0
    volume = _as_float(raw_market.get("volume") or raw_market.get("volume24hr") or raw_market.get("volumeNum"))
    if order_book is not None:
        volume = max(volume, order_book.total_bid_size + order_book.total_ask_size)

    liquidity = _as_float(raw_market.get("liquidity") or raw_market.get("liquidityNum"))
    updated_at = _parse_datetime(
        raw_market.get("updated_at") or raw_market.get("updatedAt") or raw_market.get("endDate")
    )
    if order_book is not None and order_book.updated_at > updated_at:
        updated_at = order_book.updated_at

    extra_raw = dict(raw_market)
    total_bid_size = 0.0
    total_ask_size = 0.0
    best_bid_size = 0.0
    best_ask_size = 0.0
    if order_book is not None:
        total_bid_size = float(order_book.total_bid_size)
        total_ask_size = float(order_book.total_ask_size)
        best_bid_size = float(order_book.bids[0].size) if order_book.bids else 0.0
        best_ask_size = float(order_book.asks[0].size) if order_book.asks else 0.0
        extra_raw["total_bid_size"] = total_bid_size
        extra_raw["total_ask_size"] = total_ask_size
        extra_raw["best_bid_size"] = best_bid_size
        extra_raw["best_ask_size"] = best_ask_size

    return NormalizedMarket(
        market_id=str(raw_market.get("id") or raw_market.get("market_id") or raw_market.get("conditionId") or "unknown"),
        slug=str(raw_market.get("slug") or raw_market.get("ticker") or "unknown-market"),
        question=str(raw_market.get("question") or raw_market.get("title") or "Unknown question"),
        category=category,
        outcome=str(raw_market.get("outcome") or raw_market.get("groupItemTitle") or "YES").upper(),
        best_bid=_clamp_price(best_bid),
        best_ask=_clamp_price(best_ask),
        last_price=_clamp_price(last_price),
        spread=spread,
        volume=round(volume, 6),
        liquidity=round(liquidity, 6),
        total_bid_size=round(total_bid_size, 6),
        total_ask_size=round(total_ask_size, 6),
        best_bid_size=round(best_bid_size, 6),
        best_ask_size=round(best_ask_size, 6),
        updated_at=updated_at,
        tags=tags,
        raw=extra_raw,
    )


def _normalize_tags(raw_tags: Any) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        return [raw_tags.lower()]
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for item in raw_tags:
            if isinstance(item, dict):
                value = item.get("name") or item.get("slug") or item.get("label")
            else:
                value = item
            if value is not None:
                tags.append(str(value).lower())
    return tags


def _infer_category(raw_market: Mapping[str, Any], tags: list[str]) -> str:
    explicit = str(raw_market.get("category") or raw_market.get("type") or "").strip().lower()
    if explicit in {"sports", "crypto", "financial", "event"}:
        return explicit

    question = str(raw_market.get("question") or raw_market.get("title") or "").lower()
    corpus = " ".join(tags + [explicit, question])

    if any(keyword in corpus for keyword in ("nba", "nfl", "soccer", "sports", "team", "finals")):
        return "sports"
    if any(keyword in corpus for keyword in ("crypto", "bitcoin", "btc", "eth", "sol", "financial", "rates", "stocks")):
        return "crypto" if "crypto" in corpus or "bitcoin" in corpus or "btc" in corpus else "financial"
    return "event"


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp_price(value: float) -> float:
    return round(min(max(value, 0.0), 1.0), 6)
