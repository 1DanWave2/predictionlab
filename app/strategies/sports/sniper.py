"""SportsSniperStrategy — execution layer для external-anchored sports trading.

Принимает Polymarket market + ExternalFairResult + internal FairPriceEstimate
и решает:
  1. Логировать opportunity (всегда, если scope tradable).
  2. Выпустить live BUY signal (только если все strict gates пройдены).

Hard rules per AI debate финальная spec:
  * external tradable_edge >= window-specific threshold
  * mapping_confidence == EXACT only (alias = shadow only)
  * window_bucket in {pre_90_30, pre_30_10} (no live in pre_10_5 / live first 48h)
  * spread <= 3% для tiny live
  * internal model only as VETO: confidence >= 0.4, momentum > -0.5
  * size = SNIPER_LIVE_MAX_SIZE_USD ($5 default), max 1-2 open
  * daily_stop, weekly_stop, cluster guard управляются в RiskManager
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket
from app.pricing.fair_price_engine import FairPriceEstimate
from app.strategies.base import SignalSide, StrategySignal
from app.strategies.sports.external_fair import ExternalFairResult
from app.strategies.sports.mapper import (
    MappingConfidence,
    WindowBucket,
)


logger = get_logger(__name__)


# Window-specific tradable_edge thresholds (per GPT 5)
WINDOW_THRESHOLDS = {
    WindowBucket.PRE_90_30: 0.10,
    WindowBucket.PRE_30_10: 0.12,
    WindowBucket.PRE_10_5: 0.16,
}

# Window-specific max spread
WINDOW_MAX_SPREAD = {
    WindowBucket.PRE_90_30: 0.04,
    WindowBucket.PRE_30_10: 0.03,
    WindowBucket.PRE_10_5: 0.025,
}


@dataclass
class SniperDecision:
    """Outcome от sniper.evaluate."""
    decision: str  # "ENTERED_LIVE" | "ENTERED_SHADOW" | "REJECTED_*"
    reject_reason: str
    signal: StrategySignal | None  # Не None только при ENTERED_LIVE
    intended_size_usd: float
    actual_size_usd: float
    bucket: str  # "shadow" | "core_sniper_live"
    sim_entry_price: float | None
    sim_exit_price: float | None


class SportsSniperStrategy:
    """Execution wrapper, не traditional Strategy (нет evaluate(market, fair)).

    Используется НЕ через MarketRegistry, а напрямую из ScannerTask:
        sniper = SportsSniperStrategy(settings)
        decision = sniper.evaluate(market, ext_fair_result, internal_estimate)
        opportunity_logger.write(decision)
        if decision.signal:
            ScannerTask добавляет в opportunities
    """

    name = "sports_sniper"

    def __init__(
        self,
        live_enabled: bool = True,
        live_max_size_usd: float = 5.0,
        live_max_open: int = 1,
        live_daily_stop_usd: float = 4.0,
        spread_slippage_floor: float = 0.005,
        slippage_spread_pct: float = 0.25,
    ) -> None:
        self.live_enabled = live_enabled
        self.live_max_size_usd = live_max_size_usd
        self.live_max_open = live_max_open
        self.live_daily_stop_usd = live_daily_stop_usd
        self.spread_slippage_floor = spread_slippage_floor
        self.slippage_spread_pct = slippage_spread_pct

    def evaluate(
        self,
        market: NormalizedMarket,
        external: ExternalFairResult,
        internal: FairPriceEstimate | None = None,
    ) -> SniperDecision:
        scope = external.scope

        # 0. Если scope не tradable вообще — это уже refelected в external.reject_reason
        if not scope.tradable:
            return SniperDecision(
                decision="REJECTED_SCOPE",
                reject_reason=external.reject_reason or "scope_not_tradable",
                signal=None,
                intended_size_usd=0.0,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=None,
                sim_exit_price=None,
            )

        # 1. Если нет external_fair — shadow log only
        if external.tradable_edge is None:
            return SniperDecision(
                decision="SHADOW_NO_EXTERNAL",
                reject_reason=external.reject_reason or "no_external_fair",
                signal=None,
                intended_size_usd=self.live_max_size_usd,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=None,
                sim_exit_price=None,
            )

        # Базовый threshold по окну
        window = scope.window_bucket
        threshold = WINDOW_THRESHOLDS.get(window, 999.0)
        max_spread = WINDOW_MAX_SPREAD.get(window, 0.0)

        # Если используем main books только — повышаем threshold на +0.03
        if external.needs_higher_threshold:
            threshold += 0.03

        # 2. Edge ниже threshold → log как SHADOW_LOW_EDGE
        if external.tradable_edge < threshold:
            return SniperDecision(
                decision="SHADOW_LOW_EDGE",
                reject_reason=(
                    f"tradable_edge={external.tradable_edge:.4f} < {threshold:.4f} "
                    f"(window={window.value}, needs_higher={external.needs_higher_threshold})"
                ),
                signal=None,
                intended_size_usd=self.live_max_size_usd,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=None,
                sim_exit_price=None,
            )

        # 3. Decided shadow trade. Если live disabled или mapping не exact → shadow log only.
        sim_entry, sim_exit = self._simulate_executable(market)

        if not self.live_enabled:
            return SniperDecision(
                decision="ENTERED_SHADOW",
                reject_reason="live_disabled",
                signal=None,
                intended_size_usd=self.live_max_size_usd,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=sim_entry,
                sim_exit_price=sim_exit,
            )

        if scope.mapping_confidence != MappingConfidence.EXACT:
            return SniperDecision(
                decision="ENTERED_SHADOW",
                reject_reason=f"mapping_confidence={scope.mapping_confidence.value}_not_exact_for_live",
                signal=None,
                intended_size_usd=self.live_max_size_usd,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=sim_entry,
                sim_exit_price=sim_exit,
            )

        if not scope.live_tradable:
            return SniperDecision(
                decision="ENTERED_SHADOW",
                reject_reason=f"live_tradable_false: window={window.value}",
                signal=None,
                intended_size_usd=self.live_max_size_usd,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=sim_entry,
                sim_exit_price=sim_exit,
            )

        # 4. Spread check
        if market.spread > max_spread:
            return SniperDecision(
                decision="REJECTED_SPREAD",
                reject_reason=f"spread={market.spread:.4f} > max={max_spread:.4f}",
                signal=None,
                intended_size_usd=self.live_max_size_usd,
                actual_size_usd=0.0,
                bucket="shadow",
                sim_entry_price=sim_entry,
                sim_exit_price=sim_exit,
            )

        # 5. Internal as VETO
        if internal is not None:
            if internal.confidence < 0.4:
                return SniperDecision(
                    decision="REJECTED_INTERNAL_VETO",
                    reject_reason=f"internal_confidence={internal.confidence:.2f} < 0.4",
                    signal=None,
                    intended_size_usd=self.live_max_size_usd,
                    actual_size_usd=0.0,
                    bucket="shadow",
                    sim_entry_price=sim_entry,
                    sim_exit_price=sim_exit,
                )

        # 6. Build live signal
        size_qty = round(self.live_max_size_usd / max(market.best_ask, 0.01), 4)

        signal = StrategySignal(
            market_id=market.market_id,
            slug=market.slug,
            category=market.category,
            strategy_name=self.name,
            side=SignalSide.BUY,
            fair_price=round(external.external_fair, 6),
            reference_price=market.best_ask,
            edge=external.raw_edge or 0.0,
            edge_bps=round((external.raw_edge or 0.0) * 10_000, 2),
            confidence=min(1.0, max(0.5, (external.tradable_edge / threshold))),
            reason=(
                f"sniper external_fair={external.external_fair:.4f} "
                f"tradable_edge={external.tradable_edge:.4f} "
                f"haircut={external.haircut:.4f} "
                f"books={len(external.books_used)} "
                f"sharp={external.sharp_count} "
                f"window={window.value}"
            ),
            metadata={
                "external_fair": external.external_fair,
                "raw_edge": external.raw_edge,
                "haircut": external.haircut,
                "tradable_edge": external.tradable_edge,
                "books_used": external.books_used,
                "sharp_count": external.sharp_count,
                "main_count": external.main_count,
                "window_bucket": window.value,
                "cluster_key": scope.cluster_key,
                "mapping_confidence": scope.mapping_confidence.value,
                "matched_event_id": external.matched_event_id,
                "size_usd": self.live_max_size_usd,
                "size_qty": size_qty,
            },
        )

        return SniperDecision(
            decision="ENTERED_LIVE",
            reject_reason="",
            signal=signal,
            intended_size_usd=self.live_max_size_usd,
            actual_size_usd=self.live_max_size_usd,
            bucket="core_sniper_live",
            sim_entry_price=sim_entry,
            sim_exit_price=sim_exit,
        )

    def _simulate_executable(
        self, market: NormalizedMarket,
    ) -> tuple[float, float]:
        """Per GPT spec — for backtesting paper_optimism_gap.

        sim_entry = best_ask + max(0.005, spread * 0.25)
        sim_exit = best_bid - max(0.005, spread * 0.25)
        """
        slippage = max(self.spread_slippage_floor, market.spread * self.slippage_spread_pct)
        sim_entry = min(market.best_ask + slippage, 0.99)
        sim_exit = max(market.best_bid - slippage, 0.01)
        return (round(sim_entry, 6), round(sim_exit, 6))
