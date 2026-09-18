"""AssetTargetSniperStrategy — composite parser + price + vol + barrier prob → decision.

Per [GPT 6] spec:
  * one-touch barrier probability (NOT vanilla BS)
  * tradable_edge = max(edge_yes, edge_no) - uncertainty_haircut
  * thresholds: BTC/ETH 12%, WTI/equity 15%, altcoins 18%
  * cluster_key = asset:deadline:direction_family
  * edge persists 2 consecutive scans
  * exit if tradable_edge < 0.03
  * no DCA, no scalping
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.integrations.crypto_price import CryptoPriceClient
from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket
from app.strategies.asset_target.barrier_probability import (
    BarrierInputs,
    first_passage_probability,
)
from app.strategies.asset_target.parser import (
    AssetTargetSpec,
    TargetDirection,
    parse_asset_target,
)
from app.strategies.asset_target.volatility import VolEstimate, estimate_volatility
from app.strategies.base import SignalSide, StrategySignal


logger = get_logger(__name__)


# Thresholds for tradable_edge per asset class (live-trade)
LIVE_THRESHOLDS = {
    "BTC": 0.12,
    "ETH": 0.12,
    "SOL": 0.15,        # less liquid
    "DOGE": 0.18,
    "XRP": 0.18,
    "ADA": 0.18,
    "AVAX": 0.18,
    "LINK": 0.18,
    "DOT": 0.18,
    "MATIC": 0.18,
    "LTC": 0.18,
    "WTI": 0.15,
    "BRENT": 0.15,
    "GOLD": 0.15,
    "SILVER": 0.18,
    "NDX": 0.15,
    "SPX": 0.15,
}
DEFAULT_LIVE_THRESHOLD = 0.18


@dataclass
class AssetTargetDecision:
    decision: str  # "ENTERED_LIVE" | "ENTERED_SHADOW" | "REJECTED_*"
    reject_reason: str
    signal: StrategySignal | None
    intended_size_usd: float
    actual_size_usd: float
    bucket: str
    spec: AssetTargetSpec | None
    spot_price: float | None
    annualized_vol: float | None
    model_prob: float | None    # P(barrier hit) per model
    raw_edge: float | None
    haircut: float | None
    tradable_edge: float | None
    sim_entry_price: float | None
    sim_exit_price: float | None
    debug: dict[str, Any] = field(default_factory=dict)


class AssetTargetSniperStrategy:
    """Direct callable, не через MarketRegistry. Аналогично SportsSniper."""

    name = "asset_target_sniper"

    def __init__(
        self,
        price_client: CryptoPriceClient,
        live_enabled: bool = False,
        live_max_size_usd: float = 5.0,
        live_max_open: int = 2,
        spread_slippage_floor: float = 0.005,
        slippage_spread_pct: float = 0.25,
        max_spread_pct: float = 0.05,  # 5% shadow, 3% live (per GPT)
        max_spread_live: float = 0.03,
    ) -> None:
        self.price_client = price_client
        self.live_enabled = live_enabled
        self.live_max_size_usd = live_max_size_usd
        self.live_max_open = live_max_open
        self.spread_slippage_floor = spread_slippage_floor
        self.slippage_spread_pct = slippage_spread_pct
        self.max_spread_pct = max_spread_pct
        self.max_spread_live = max_spread_live
        # Persistence: edge должен быть >threshold 2 scans подряд для tiny-live
        self._consecutive_edge: dict[str, int] = {}

    async def evaluate(self, market: NormalizedMarket) -> AssetTargetDecision:
        """Главный entry-point. Всегда возвращает decision (даже REJECTED)."""
        title = market.raw.get("question") or market.raw.get("title") or market.slug or ""
        spec = parse_asset_target(title)

        result = AssetTargetDecision(
            decision="REJECTED_PARSE",
            reject_reason="",
            signal=None,
            intended_size_usd=self.live_max_size_usd,
            actual_size_usd=0.0,
            bucket="shadow",
            spec=spec,
            spot_price=None,
            annualized_vol=None,
            model_prob=None,
            raw_edge=None,
            haircut=None,
            tradable_edge=None,
            sim_entry_price=None,
            sim_exit_price=None,
        )

        if not spec.tradable:
            result.reject_reason = (
                f"parse_confidence={spec.parse_confidence} "
                f"asset={spec.asset_symbol} threshold={spec.threshold_usd} "
                f"deadline={spec.deadline_iso} direction={spec.direction.value}"
            )
            return result

        # Get spot
        if not self.price_client.supported(spec.asset_symbol):
            result.decision = "REJECTED_NO_FEED"
            result.reject_reason = f"no price feed for {spec.asset_symbol} (commodity/equity v1.5)"
            return result

        spot_snap = await self.price_client.get_spot(spec.asset_symbol)
        if spot_snap is None:
            result.decision = "REJECTED_PRICE_FETCH"
            result.reject_reason = f"spot fetch failed for {spec.asset_symbol}"
            return result
        result.spot_price = spot_snap.spot_price

        # Get vol
        vol = await estimate_volatility(spec.asset_symbol, self.price_client, spec.asset_class or "crypto")
        if vol is None:
            result.decision = "REJECTED_VOL_FETCH"
            result.reject_reason = f"vol estimate failed for {spec.asset_symbol}"
            return result
        result.annualized_vol = vol.annualized_vol

        # Time to deadline
        try:
            deadline_dt = datetime.fromisoformat(f"{spec.deadline_iso}T23:59:59+00:00")
        except (ValueError, TypeError):
            result.decision = "REJECTED_DEADLINE_PARSE"
            result.reject_reason = f"deadline_iso={spec.deadline_iso}"
            return result

        seconds_left = (deadline_dt - datetime.now(UTC)).total_seconds()
        if seconds_left <= 0:
            result.decision = "REJECTED_EXPIRED"
            result.reject_reason = f"deadline already passed: {spec.deadline_iso}"
            return result
        years_left = seconds_left / (365.25 * 24 * 3600)

        # Barrier probability
        barrier_inputs = BarrierInputs(
            spot=spot_snap.spot_price,
            barrier=spec.threshold_usd,
            years_to_deadline=years_left,
            annualized_vol=vol.annualized_vol,
            drift=0.0,  # conservative
        )
        barrier_result = first_passage_probability(barrier_inputs)
        model_prob_yes = barrier_result.probability
        result.model_prob = model_prob_yes

        # Edge calculation
        # Polymarket binary "Will X hit threshold by Y?" → YES = condition met.
        # Если spec.direction is upper → YES = hit upper (barrier hit prob = model_prob).
        # Если spec.direction is lower → YES = hit lower (тоже model_prob, т.к. barrier_result уже direction-aware).
        poly_yes_ask = market.best_ask  # cost to buy YES
        # NO ask: 1 - bid_yes (приближение если нет отдельного NO market)
        poly_no_ask = 1.0 - market.best_bid if market.best_bid > 0 else None

        edge_yes = model_prob_yes - poly_yes_ask if poly_yes_ask > 0 else -1.0
        edge_no = ((1.0 - model_prob_yes) - poly_no_ask) if poly_no_ask else -1.0

        haircut = (
            max(0.03, market.spread / 2.0)
            + vol.uncertainty
            + 0.02  # resolution_rule_uncertainty
        )

        if edge_yes >= edge_no:
            best_edge = edge_yes
            best_side = SignalSide.BUY  # buy YES
            entry_price = market.best_ask
        else:
            best_edge = edge_no
            best_side = SignalSide.BUY  # buy NO (shadow only — нет NO infrastructure)
            entry_price = poly_no_ask or 0.5

        tradable_edge = best_edge - haircut
        result.raw_edge = round(best_edge, 6)
        result.haircut = round(haircut, 6)
        result.tradable_edge = round(tradable_edge, 6)

        # Threshold check
        live_threshold = LIVE_THRESHOLDS.get(spec.asset_symbol, DEFAULT_LIVE_THRESHOLD)
        shadow_threshold = max(0.05, live_threshold - 0.03)  # shadow ниже на 3%

        if tradable_edge < shadow_threshold:
            result.decision = "SHADOW_LOW_EDGE"
            result.reject_reason = (
                f"tradable_edge={tradable_edge:.4f} < {shadow_threshold:.4f}"
            )
            self._consecutive_edge[spec.cluster_key or ""] = 0
            return result

        # Compute simulated executable prices
        slippage = max(self.spread_slippage_floor, market.spread * self.slippage_spread_pct)
        sim_entry = min(market.best_ask + slippage, 0.99)
        sim_exit = max(market.best_bid - slippage, 0.01)
        result.sim_entry_price = round(sim_entry, 6)
        result.sim_exit_price = round(sim_exit, 6)

        # Persistence: edge должен быть выше threshold 2 раза подряд
        cluster = spec.cluster_key or ""
        if best_side == SignalSide.BUY and entry_price > 0:
            self._consecutive_edge[cluster] = self._consecutive_edge.get(cluster, 0) + 1
        consecutive = self._consecutive_edge.get(cluster, 0)

        # Live decision
        meets_live = (
            self.live_enabled
            and tradable_edge >= live_threshold
            and consecutive >= 2
            and market.spread <= self.max_spread_live
            and best_side == SignalSide.BUY
            and entry_price > 0
            and entry_price < 1.0
            # NO buys пока shadow only — нет infrastructure
            and edge_yes >= edge_no
        )

        if not meets_live:
            result.decision = "ENTERED_SHADOW"
            result.reject_reason = (
                f"shadow: live_disabled={not self.live_enabled} "
                f"edge_below_live_thr={tradable_edge < live_threshold} "
                f"consec={consecutive}<2 "
                f"spread_too_wide={market.spread > self.max_spread_live} "
                f"side={best_side.value}"
            )
            result.bucket = "shadow"
            return result

        # Build live signal
        size_qty = round(self.live_max_size_usd / max(entry_price, 0.01), 4)
        signal = StrategySignal(
            market_id=market.market_id,
            slug=market.slug,
            category=market.category,
            strategy_name=self.name,
            side=best_side,
            fair_price=round(model_prob_yes if best_side == SignalSide.BUY and edge_yes >= edge_no else (1.0 - model_prob_yes), 6),
            reference_price=entry_price,
            edge=round(best_edge, 6),
            edge_bps=round(best_edge * 10_000, 2),
            confidence=min(1.0, max(0.5, tradable_edge / live_threshold)),
            reason=(
                f"asset_target {spec.asset_symbol} {spec.direction.value} "
                f"@ {spec.threshold_usd} by {spec.deadline_iso} "
                f"model_prob={model_prob_yes:.4f} "
                f"poly_ask={poly_yes_ask:.4f} "
                f"raw_edge={best_edge:.4f} "
                f"haircut={haircut:.4f} "
                f"tradable={tradable_edge:.4f} "
                f"vol={vol.annualized_vol:.2%}"
            ),
            metadata={
                "asset_symbol": spec.asset_symbol,
                "asset_class": spec.asset_class,
                "threshold_usd": spec.threshold_usd,
                "deadline_iso": spec.deadline_iso,
                "direction": spec.direction.value,
                "spot_price": spot_snap.spot_price,
                "annualized_vol": vol.annualized_vol,
                "vol_components": vol.components,
                "model_prob_yes": model_prob_yes,
                "raw_edge": best_edge,
                "haircut": haircut,
                "tradable_edge": tradable_edge,
                "cluster_key": spec.cluster_key,
                "size_usd": self.live_max_size_usd,
                "size_qty": size_qty,
                "consecutive_scans": consecutive,
            },
        )

        result.decision = "ENTERED_LIVE"
        result.signal = signal
        result.actual_size_usd = self.live_max_size_usd
        result.bucket = "asset_target_live"
        return result
