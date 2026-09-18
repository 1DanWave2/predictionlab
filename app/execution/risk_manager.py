from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select

from app.config import Settings
from app.db import db_session
from app.execution.paper_execution import PaperOrderRequest
from app.execution.position_manager import PositionManager, PositionSnapshot


class RiskDecision(BaseModel):
    allowed: bool
    reason: str
    clipped_size: float | None = None


_EXIT_STRATEGIES = frozenset({"exit_manager"})
_INTERNAL_ENTRY_STRATEGIES = frozenset({"event_strategy", "sports_strategy", "financial_strategy"})
_SETUP_COOLDOWN_SECONDS = 600  # 10 min market+side cooldown per [GPT 10] Q5
_KILL_SWITCH_DURATION_SECONDS = 12 * 3600  # 12h disable per [GPT 21]
_KILL_SWITCH_CONSECUTIVE_SL_THRESHOLD = 3  # 3 SL подряд → disable


def _compute_risk_score(order: PaperOrderRequest) -> int:
    """Risk score per [GPT 14] для high-gamma trap detection.

    score >= 5: REJECT
    score >= 3: cap → min(bucket_cap, $5)
    score < 3:  normal cap
    """
    score = 0
    if order.liquidity < 3000:
        score += 2
    elif order.liquidity < 5000:
        score += 1
    if order.hours_to_resolution < 3:
        score += 3
    elif order.hours_to_resolution < 6:
        score += 2
    elif order.hours_to_resolution < 12:
        score += 1
    if order.spread > 0.05:
        score += 1
    return score


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.position_manager = PositionManager()
        self.max_position_size = float(getattr(settings, "max_position_size", settings.default_order_size * 3))
        self.max_simultaneous_positions = int(getattr(settings, "max_open_positions", 10))
        self.daily_loss_limit = abs(float(getattr(settings, "daily_loss_limit", settings.max_order_notional * 2)))
        self.cooldown_seconds = int(getattr(settings, "market_cooldown_seconds", 60))
        self.stoploss_cooldown_seconds = int(getattr(settings, "stoploss_cooldown_seconds", 1800))
        self.max_trades_per_market = int(getattr(settings, "max_trades_per_market", 4))
        self._market_cooldowns: dict[str, datetime] = {}
        self._stoploss_cooldowns: dict[str, datetime] = {}
        self._market_trade_count: dict[str, int] = {}
        # Setup cooldown per [GPT 10] Q5: 10-min market+side cooldown after ANY
        # entry attempt (filled or rejected). Не применяется к exit_manager —
        # он должен иметь возможность закрыть позицию в любое время.
        self._setup_cooldowns: dict[tuple[str, str], datetime] = {}
        # Kill-switch per [GPT 21]: per-bucket consecutive SL count + disabled timestamps.
        # When 3 SL подряд → disable bucket entries for 12h.
        self._bucket_consecutive_sl: dict[str, int] = {}
        self._bucket_disabled_until: dict[str, datetime] = {}
        self._daily_realized_pnl = 0.0
        self._current_day = date.today()
        # Per-bucket realized PnL для bucket-aware stop logic.
        # Bucket "core_sniper_live": tiny canary, daily_stop = sniper_live_daily_stop_usd
        # Bucket "experiment": old strategy, daily_stop -$3 per AI debate spec
        self._bucket_realized: dict[str, float] = {}
        # Per-bucket closed-trade count для GPT 28 telemetry-only canary gate.
        self._bucket_closed_trades: dict[str, int] = {}
        # Profit lock: после +15% session равно — stop new entries.
        self._session_realized_pnl = 0.0
        self._profit_lock_active = False

    def check_order(
        self,
        order: PaperOrderRequest,
        current_position: PositionSnapshot | None,
    ) -> RiskDecision:
        decision = self._check_order_impl(order, current_position)
        # Set 10-min setup cooldown after ANY entry attempt (per [GPT 10] Q5).
        # Skip exit_manager — closing positions must always work.
        if order.strategy not in _EXIT_STRATEGIES:
            key = (order.market_id, order.side.upper())
            self._setup_cooldowns[key] = datetime.now(UTC) + timedelta(seconds=_SETUP_COOLDOWN_SECONDS)
        return decision

    def _check_order_impl(
        self,
        order: PaperOrderRequest,
        current_position: PositionSnapshot | None,
    ) -> RiskDecision:
        self._roll_day()

        if self.settings.is_live_auto or self.settings.enable_live_trading:
            return RiskDecision(allowed=False, reason="live trading is hard blocked in MVP")

        if order.price <= 0 or order.price >= 1:
            return RiskDecision(allowed=False, reason="invalid binary market price")
        if order.size <= 0:
            return RiskDecision(allowed=False, reason="order size must be positive")

        # Setup cooldown check — 10 min на (market_id, side) для entry strategies.
        # exit_manager пропускается, чтобы он всегда мог закрыть позицию.
        if order.strategy not in _EXIT_STRATEGIES:
            key = (order.market_id, order.side.upper())
            cd = self._setup_cooldowns.get(key)
            if cd is not None and datetime.now(UTC) < cd:
                remaining = int((cd - datetime.now(UTC)).total_seconds())
                return RiskDecision(
                    allowed=False,
                    reason=f"setup_cooldown_active ({remaining}s remaining)",
                )

        # Kill-switch check per [GPT 21]: bucket disabled for 12h после 3 SL подряд.
        if order.strategy not in _EXIT_STRATEGIES:
            disabled_until = self._bucket_disabled_until.get(order.bucket)
            if disabled_until is not None and datetime.now(UTC) < disabled_until:
                remaining_h = (disabled_until - datetime.now(UTC)).total_seconds() / 3600
                return RiskDecision(
                    allowed=False,
                    reason=f"kill_switch_active bucket={order.bucket} for {remaining_h:.1f}h",
                )

        side = order.side.upper()
        current_qty = current_position.quantity if current_position is not None else 0.0

        if side == "SELL":
            if current_qty <= 0:
                return RiskDecision(allowed=False, reason="no open position to close")
            clipped_size = min(order.size, current_qty)
            return RiskDecision(allowed=True, reason="position reduction allowed", clipped_size=clipped_size)

        if side != "BUY":
            return RiskDecision(allowed=False, reason=f"unsupported side={order.side}")

        if current_position is not None and current_position.quantity > 0 and current_position.unrealized_pnl < 0:
            return RiskDecision(allowed=False, reason="no DCA into losing position")

        # Profit lock: после session +15% — stop new entries
        # (per GPT 4 spec: "Цель достигнута — не отдавать обратно ради ещё одной")
        if self._profit_lock_active:
            return RiskDecision(allowed=False, reason="profit_lock_active: session reached +15%")

        # Cluster guard: запретить duplicate trades в same event cluster.
        # Защищает от утренней катастрофы (две correlated sports позиции одновременно).
        if order.cluster_key:
            if self._has_open_cluster(order.cluster_key, exclude_market_id=order.market_id):
                return RiskDecision(
                    allowed=False,
                    reason=f"cluster_already_open: {order.cluster_key}",
                )

        # Matchup gate per [GPT 16]: individual "X vs Y" markets подвержены
        # news-driven gaps (NBA/MLB/ATP/tennis видели -$3-4 losses).
        # endDate market'а != game time — match может сейчас идти даже если
        # endDate через неделю (e.g., ATP Tennis 2140921 hrs=167h но матч NOW).
        # Internal strategies НЕ должны trade matchup markets вообще —
        # это территория external-anchor strategies (Sports Sniper).
        if order.strategy in _INTERNAL_ENTRY_STRATEGIES and order.is_matchup:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"internal_matchup_blocked "
                    f"hrs={order.hours_to_resolution:.1f} "
                    f"strategy={order.strategy}"
                ),
            )

        # Risk-score gate per [GPT 14]: high-gamma trap detection.
        # >= 5: REJECT; >= 3: tighten cap до $5 для legacy/internal strategies.
        risk_score = 0
        if order.strategy in _INTERNAL_ENTRY_STRATEGIES:
            risk_score = _compute_risk_score(order)
            if risk_score >= 5:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"high_gamma_trap risk_score={risk_score} "
                        f"liq={order.liquidity:.0f} hrs={order.hours_to_resolution:.1f} "
                        f"spread={order.spread:.3f}"
                    ),
                )

        # Bucket-aware daily stop + size CLIP (per [GPT 10]):
        # - notional > 2*cap → REJECT (sizing logic явно сломана upstream)
        # - notional > cap (но ≤ 2*cap) → CLIP до cap, allowed
        # - notional ≤ cap → pass через без изменений
        bucket_realized = self._bucket_realized.get(order.bucket, 0.0)
        effective_size = order.size
        bucket_clip_reason: str | None = None
        if order.bucket == "experiment":
            if bucket_realized <= -3.0:
                return RiskDecision(
                    allowed=False,
                    reason=f"experiment_bucket_daily_stop: realized={bucket_realized:.2f}",
                )
            cap = 7.5
            if risk_score >= 3:
                cap = min(cap, 5.0)
            order_notional = order.price * order.size
            if order_notional > cap * 2 + 0.01:
                return RiskDecision(
                    allowed=False,
                    reason=f"experiment_bucket_size_too_large: notional={order_notional:.2f} > 2*${cap:.2f}",
                )
            if order_notional > cap + 0.01:
                effective_size = cap / order.price
                bucket_clip_reason = f"clipped_to_experiment_cap: requested_notional=${order_notional:.2f} effective_notional=${cap:.2f}"
        elif order.bucket == "core_sniper_live":
            sniper_stop = abs(float(getattr(self.settings, "sniper_live_daily_stop_usd", 4.0)))
            if bucket_realized <= -sniper_stop:
                return RiskDecision(
                    allowed=False,
                    reason=f"sniper_live_daily_stop: realized={bucket_realized:.2f} <= -${sniper_stop:.2f}",
                )
            cap = float(getattr(self.settings, "sniper_live_max_size_usd", 5.0))
            order_notional = order.price * order.size
            if order_notional > cap * 2 + 0.01:
                return RiskDecision(
                    allowed=False,
                    reason=f"sniper_live_size_too_large: notional={order_notional:.2f} > 2*${cap:.2f}",
                )
            if order_notional > cap + 0.01:
                effective_size = cap / order.price
                bucket_clip_reason = f"clipped_to_sniper_cap: requested_notional=${order_notional:.2f} effective_notional=${cap:.2f}"
        elif order.bucket == "fade_any_canary":
            # Per [GPT 23] live ramp + [GPT 28] telemetry-only gate:
            # max_loss_budget $3 total; auto-disable after 10 closed trades if net negative.
            if bucket_realized <= -3.0:
                return RiskDecision(
                    allowed=False,
                    reason=f"fade_any_canary_daily_stop: realized={bucket_realized:.2f}",
                )
            closed_n = self._bucket_closed_trades.get("fade_any_canary", 0)
            if closed_n >= 10 and bucket_realized < 0:
                return RiskDecision(
                    allowed=False,
                    reason=f"fade_any_canary_telemetry_disabled: closed={closed_n} realized={bucket_realized:.2f} (per [GPT 28])",
                )
            cap = 1.0  # $1 starting per [GPT 23] ramp protocol
            order_notional = order.price * order.size
            if order_notional > cap * 2 + 0.01:
                return RiskDecision(
                    allowed=False,
                    reason=f"fade_any_size_too_large: notional={order_notional:.2f} > 2*${cap:.2f}",
                )
            if order_notional > cap + 0.01:
                effective_size = cap / order.price
                bucket_clip_reason = f"clipped_to_fade_any_cap: requested_notional=${order_notional:.2f} effective_notional=${cap:.2f}"
        elif order.bucket == "financial_internal":
            fin_stop = abs(float(getattr(self.settings, "financial_strategy_daily_stop", 4.0)))
            if bucket_realized <= -fin_stop:
                return RiskDecision(
                    allowed=False,
                    reason=f"financial_canary_daily_stop: realized={bucket_realized:.2f} <= -${fin_stop:.2f}",
                )
            cap = float(getattr(self.settings, "financial_strategy_max_size_usd", 7.5))
            if risk_score >= 3:
                cap = min(cap, 5.0)
            order_notional = order.price * order.size
            if order_notional > cap * 2 + 0.01:
                return RiskDecision(
                    allowed=False,
                    reason=f"financial_canary_size_too_large: notional={order_notional:.2f} > 2*${cap:.2f}",
                )
            if order_notional > cap + 0.01:
                effective_size = cap / order.price
                bucket_clip_reason = f"clipped_to_financial_cap: requested_notional=${order_notional:.2f} effective_notional=${cap:.2f}"

        sl_cooldown = self._stoploss_cooldowns.get(order.market_id)
        if sl_cooldown is not None and datetime.now(UTC) < sl_cooldown:
            remaining = int((sl_cooldown - datetime.now(UTC)).total_seconds())
            return RiskDecision(allowed=False, reason=f"stop-loss cooldown active ({remaining}s remaining)")

        cooldown_until = self._market_cooldowns.get(order.market_id)
        if cooldown_until is not None and datetime.now(UTC) < cooldown_until:
            return RiskDecision(allowed=False, reason="market cooldown active")

        trade_count = self._market_trade_count.get(order.market_id, 0)
        if trade_count >= self.max_trades_per_market:
            return RiskDecision(allowed=False, reason=f"max trades per market reached ({trade_count}/{self.max_trades_per_market})")

        if self._daily_realized_pnl <= -self.daily_loss_limit:
            return RiskDecision(allowed=False, reason="daily loss stop active")

        if current_qty <= 0 and self.position_manager.count_open_positions() >= self.max_simultaneous_positions:
            return RiskDecision(allowed=False, reason="max simultaneous positions reached")

        if (current_qty + effective_size) > self.max_position_size:
            position_clipped = max(self.max_position_size - current_qty, 0.0)
            if position_clipped <= 0:
                return RiskDecision(allowed=False, reason="max position size reached")
            if (order.price * position_clipped) > self.settings.max_order_notional:
                return RiskDecision(allowed=False, reason="max order notional exceeded after clipping")
            return RiskDecision(allowed=True, reason="size clipped by max position size", clipped_size=position_clipped)

        if (order.price * effective_size) > self.settings.max_order_notional:
            return RiskDecision(allowed=False, reason="max order notional exceeded")

        order_cost = order.price * effective_size
        available = self._available_balance()
        if available is not None and order_cost > available:
            return RiskDecision(allowed=False, reason=f"insufficient balance ({available:.2f} available, need {order_cost:.2f})")

        if bucket_clip_reason is not None:
            return RiskDecision(allowed=True, reason=bucket_clip_reason, clipped_size=effective_size)
        return RiskDecision(allowed=True, reason="risk checks passed", clipped_size=effective_size)

    def register_fill(
        self,
        market_id: str,
        side: str,
        realized_pnl_delta: float = 0.0,
        bucket: str = "experiment",
    ) -> None:
        self._roll_day()
        self._market_trade_count[market_id] = self._market_trade_count.get(market_id, 0) + 1

        if side.upper() == "BUY":
            self._market_cooldowns[market_id] = datetime.now(UTC) + timedelta(seconds=self.cooldown_seconds)

        if side.upper() == "SELL" and realized_pnl_delta < 0:
            self._stoploss_cooldowns[market_id] = datetime.now(UTC) + timedelta(seconds=self.stoploss_cooldown_seconds)

        self._daily_realized_pnl = round(self._daily_realized_pnl + realized_pnl_delta, 6)
        self._bucket_realized[bucket] = round(
            self._bucket_realized.get(bucket, 0.0) + realized_pnl_delta, 6
        )

        # Track closed-trade count per bucket (used by GPT 28 fade_any gate).
        if side.upper() == "SELL":
            self._bucket_closed_trades[bucket] = self._bucket_closed_trades.get(bucket, 0) + 1

        # Kill-switch trigger per [GPT 21]: track consecutive SL per bucket.
        # SELL with negative PnL = SL/loss. Reset on win.
        if side.upper() == "SELL":
            if realized_pnl_delta < 0:
                self._bucket_consecutive_sl[bucket] = self._bucket_consecutive_sl.get(bucket, 0) + 1
                if self._bucket_consecutive_sl[bucket] >= _KILL_SWITCH_CONSECUTIVE_SL_THRESHOLD:
                    self._bucket_disabled_until[bucket] = datetime.now(UTC) + timedelta(seconds=_KILL_SWITCH_DURATION_SECONDS)
            elif realized_pnl_delta > 0:
                self._bucket_consecutive_sl[bucket] = 0

        # Update session pnl + profit lock check.
        # Profit lock срабатывает на >=+15% от initial paper balance.
        self._session_realized_pnl = round(self._session_realized_pnl + realized_pnl_delta, 6)
        initial = float(self.settings.initial_paper_balance or 100.0)
        if initial > 0 and self._session_realized_pnl >= initial * 0.15:
            self._profit_lock_active = True

    def _has_open_cluster(self, cluster_key: str, exclude_market_id: str = "") -> bool:
        """Check if any open position has this cluster_key."""
        with db_session() as session:
            from app.models import Position
            stmt = select(Position).where(
                Position.quantity > 0,
                Position.cluster_key == cluster_key,
            )
            if exclude_market_id:
                stmt = stmt.where(Position.market_id != exclude_market_id)
            row = session.execute(stmt).scalar_one_or_none()
            return row is not None

    def _available_balance(self) -> float | None:
        initial = self.settings.initial_paper_balance
        if initial <= 0:
            return None
        total_realized = self.position_manager.total_realized_pnl()
        with db_session() as session:
            from sqlalchemy import func as sa_func
            from app.models import Position
            in_positions = float(
                session.execute(
                    select(sa_func.coalesce(
                        sa_func.sum(Position.quantity * Position.avg_price), 0.0
                    ))
                ).scalar_one()
            )
        return round(initial + total_realized - in_positions, 2)

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self._daily_realized_pnl = 0.0
            self._market_cooldowns.clear()
            self._stoploss_cooldowns.clear()
            self._market_trade_count.clear()
            self._bucket_realized.clear()
            # Profit lock сбрасывается на новый день, session_pnl тоже.
            self._session_realized_pnl = 0.0
            self._profit_lock_active = False
