from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket


logger = get_logger(__name__)

# Hard warmup gate: ниже этого количества ticks не выдавать сигнал
_WARMUP_MIN_TICKS = 5


class FairPriceEstimate(BaseModel):
    fair_price: float = Field(ge=0.0, le=1.0)
    mark_price: float = Field(ge=0.0, le=1.0)
    spread: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)


class FairPriceEngine:
    _MAX_HISTORY = 30
    _HISTORY_WINDOW_MIN = 30

    def __init__(self) -> None:
        self._price_history: dict[str, list[tuple[datetime, float, float]]] = {}

    def bootstrap_from_db(self) -> int:
        """После rebuild контейнера читаем последние 30 минут MarketSnapshot
        и заполняем in-memory _price_history. Возвращает количество markets
        для которых восстановили историю.

        Это критично: без bootstrap первые сделки после rebuild идут на
        пустой history → momentum=0 → momentum_penalty=1.0 → нет защиты
        от падающих ножей. См. инцидент 2026-05-01 когда после reset мы
        потеряли -9.5% на 2 sports trades за 8 минут.
        """
        from sqlalchemy import select
        from app.db import db_session
        from app.models import MarketSnapshot

        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=self._HISTORY_WINDOW_MIN)

        try:
            with db_session() as session:
                rows = session.execute(
                    select(
                        MarketSnapshot.market_id,
                        MarketSnapshot.best_bid,
                        MarketSnapshot.best_ask,
                        MarketSnapshot.payload,
                        MarketSnapshot.created_at,
                    )
                    .where(MarketSnapshot.created_at >= cutoff)
                    .order_by(MarketSnapshot.market_id, MarketSnapshot.created_at)
                ).all()
        except Exception as exc:
            logger.warning("fair_price.bootstrap_failed | error=%s", exc)
            return 0

        for market_id, best_bid, best_ask, payload, created_at in rows:
            mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
            if mid <= 0:
                continue
            volume = float(payload.get("volume", 0.0) or 0.0) if payload else 0.0
            ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
            self._price_history.setdefault(market_id, []).append((ts, mid, volume))

        # Trim каждый market до MAX_HISTORY
        for market_id in self._price_history:
            self._price_history[market_id] = self._price_history[market_id][-self._MAX_HISTORY:]

        restored = len(self._price_history)
        total_ticks = sum(len(h) for h in self._price_history.values())
        logger.info(
            "fair_price.bootstrap | markets=%s total_ticks=%s window_min=%s",
            restored, total_ticks, self._HISTORY_WINDOW_MIN,
        )
        return restored

    def calculate(self, market: NormalizedMarket) -> FairPriceEstimate:
        mid = market.mid_price
        now = datetime.now(UTC)

        history = self._price_history.setdefault(market.market_id, [])
        history.append((now, mid, market.volume))
        cutoff = now - timedelta(minutes=self._HISTORY_WINDOW_MIN)
        history = [(t, p, v) for t, p, v in history if t > cutoff][-self._MAX_HISTORY:]
        self._price_history[market.market_id] = history

        vwap = self._compute_vwap(history) if len(history) >= 3 else mid

        ob_signal = self._order_book_signal(market)

        deviation = mid - vwap
        momentum = self._momentum_strength(history)
        mean_reversion_weight = 0.30 if abs(momentum) < 0.4 else 0.05
        mean_reversion = -deviation * mean_reversion_weight

        time_decay = self._time_decay_signal(market, mid)
        if momentum < -0.4:
            time_decay = 0.0

        raw_fair = vwap + ob_signal + mean_reversion + time_decay
        fair_price = max(0.01, min(0.99, raw_fair))

        # Hard warmup gate: если history короче _WARMUP_MIN_TICKS,
        # сбрасываем confidence в 0 чтобы signal не прошёл MIN_CONFIDENCE.
        # Это safety net на случай если bootstrap не сработал
        # (новый market без истории, или скан не успел накопить).
        if len(history) < _WARMUP_MIN_TICKS:
            confidence = 0.0
        else:
            confidence = self._confidence(
                history_len=len(history),
                spread=market.spread,
                volume=market.volume,
                ob_signal=ob_signal,
                mean_reversion=mean_reversion,
                time_decay=time_decay,
                momentum=momentum,
            )

        return FairPriceEstimate(
            fair_price=round(fair_price, 6),
            mark_price=round(mid, 6),
            spread=round(market.spread, 6),
            confidence=round(confidence, 4),
        )

    @staticmethod
    def _compute_vwap(history: list[tuple[datetime, float, float]]) -> float:
        weights = [v + 1.0 for _, _, v in history]
        total_weight = sum(weights)
        if total_weight <= 0:
            return sum(p for _, p, _ in history) / len(history)
        return sum(p * w for (_, p, _), w in zip(history, weights)) / total_weight

    @staticmethod
    def _order_book_signal(market: NormalizedMarket) -> float:
        raw = market.raw
        bid_size = float(raw.get("total_bid_size", 0) or 0)
        ask_size = float(raw.get("total_ask_size", 0) or 0)
        total = bid_size + ask_size
        if total < 1.0:
            return 0.0
        imbalance = (bid_size - ask_size) / total
        return imbalance * max(market.spread, 0.005) * 1.5

    @staticmethod
    def _momentum_strength(history: list[tuple[datetime, float, float]]) -> float:
        if len(history) < 5:
            return 0.0
        prices = [p for _, p, _ in history[-5:]]
        deltas = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
        if not deltas:
            return 0.0
        signed = sum(1 if d > 0 else -1 if d < 0 else 0 for d in deltas)
        return signed / len(deltas)

    @staticmethod
    def _time_decay_signal(market: NormalizedMarket, current_price: float) -> float:
        end_raw = (
            market.raw.get("endDate")
            or market.raw.get("end_date")
            or market.raw.get("endDateIso")
        )
        if not end_raw:
            return 0.0
        try:
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return 0.0

        hours_left = (end - datetime.now(UTC)).total_seconds() / 3600
        if hours_left <= 0 or hours_left > 24 * 14:
            return 0.0

        decay_rate = min(1.0, max(0.05, 12.0 / hours_left))
        target = 1.0 if current_price > 0.5 else 0.0
        return (target - current_price) * decay_rate * 0.04

    @staticmethod
    def _confidence(
        history_len: int,
        spread: float,
        volume: float,
        ob_signal: float,
        mean_reversion: float,
        time_decay: float,
        momentum: float,
    ) -> float:
        history_score = min(history_len / 10.0, 1.0)
        spread_score = max(0.0, 1.0 - spread * 8.0)
        volume_score = min(volume / 1000.0, 1.0)

        signals = [ob_signal, mean_reversion, time_decay]
        non_zero = [s for s in signals if abs(s) > 1e-6]
        if non_zero:
            same_dir = sum(1 if s > 0 else -1 for s in non_zero)
            agreement_score = abs(same_dir) / len(non_zero)
        else:
            agreement_score = 0.0

        momentum_penalty = 1.0 - abs(momentum) * 0.85

        score = (
            history_score * 0.25
            + spread_score * 0.30
            + volume_score * 0.20
            + agreement_score * 0.25
        ) * momentum_penalty

        return max(0.0, min(1.0, score))
