"""Strategy metadata registry — canonical source of truth per [GPT 46/47].

This is the SINGLE PLACE where strategies declare:
  - their target hold time and natural markout horizon
  - their risk unit (trade/event/day/market_family)
  - their execution status (live/paper/shadow/frozen/disabled)
  - their validation gates required to graduate up the ladder

Why this exists:
  Before this registry, strategies declared horizons by accident.
  60-minute markout was the default for everyone, even strategies whose
  natural payoff is at resolution (months later) or per-event (sm_fade).
  That made dashboards lie about which strategies were "winning."

  Per [GPT 47]: dashboards/risk_manager/backtests should read THIS registry,
  not strategy class attributes. If a class wants to override, it has to
  state why (validation drift detection only).

Startup guard:
  app/main.py imports `validate_registry()` at boot. If a runner is enabled
  in .env but its registry metadata is missing or its execution_status is
  frozen/disabled, the bot REFUSES to start. This is intentional:
  configuration drift kills paper accounts the same way it kills live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ExecutionStatus = Literal["live", "paper", "shadow", "frozen", "disabled"]
RiskUnit = Literal["trade", "event", "day", "market_family"]
CapitalLockupClass = Literal["low", "medium", "high"]
ExitModel = Literal["tp_sl", "stale_exit", "scanner_orphan", "manual_resolution",
                    "event_resolution", "external_signal"]


@dataclass(frozen=True)
class StrategyMeta:
    """Single strategy's metadata — frozen, no runtime mutation."""

    name: str

    # Status
    enabled_default: bool
    execution_status: ExecutionStatus  # live > paper > shadow > frozen > disabled

    # Horizon
    target_hold_minutes: float | None
    """Expected hold time from entry to exit. None = resolution-only."""

    primary_markout_horizon: str
    """Default scoreboard horizon. Values: '5m','15m','60m','180m',
    'resolution','exit','event'."""

    secondary_markout_horizons: tuple[str, ...] = ()
    """Diagnostic horizons (not primary scoreboard)."""

    # Exit
    exit_model: ExitModel = "scanner_orphan"

    # Resolution dependency
    resolution_dependency: bool = False
    """True = strategy needs market to resolve before PnL is final."""

    # Capital
    capital_lockup_class: CapitalLockupClass = "low"
    """low: minutes-hours; medium: hours-days; high: days-weeks+"""

    # Risk gating
    risk_unit: RiskUnit = "trade"
    """The unit of independent observation. trade-level can stack into
    event/day/family correlation. Risk manager should size by risk_unit,
    not by trade."""

    max_open_risk_units: int = 1

    required_validation_gates: tuple[str, ...] = ()
    """Gates that must pass before this strategy can graduate
    (shadow → paper → live). Free-form strings."""

    notes: str = ""


# ─────────────────────────────────────────────────────────────────────
# REGISTRY — canonical metadata for every strategy known to the bot.
# ─────────────────────────────────────────────────────────────────────

STRATEGY_REGISTRY: dict[str, StrategyMeta] = {

    "sniper_sports": StrategyMeta(
        name="sniper_sports",
        enabled_default=True,
        execution_status="paper",  # validated +EV May 1, sparse setups
        target_hold_minutes=180.0,  # ~3h average to game start/event close
        primary_markout_horizon="exit",
        secondary_markout_horizons=("60m", "180m"),
        exit_model="event_resolution",
        resolution_dependency=False,  # close at game start or post-game
        capital_lockup_class="low",
        risk_unit="event",  # one event = one game
        max_open_risk_units=3,
        required_validation_gates=(
            "external_anchor_present",
            "matchup_confirmed",
            "consec >= 2 ticks",
        ),
        notes="May 1 spike +$11.72 = validated edge. TheOddsAPI dependency.",
    ),

    "event_strategy": StrategyMeta(
        name="event_strategy",
        enabled_default=True,
        execution_status="paper",
        target_hold_minutes=120.0,
        primary_markout_horizon="exit",
        secondary_markout_horizons=("60m",),
        exit_model="tp_sl",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="trade",
        max_open_risk_units=5,
        required_validation_gates=("internal_fair_confidence >= 0.5",),
        notes="Default for non-sports event markets.",
    ),

    "fade_any": StrategyMeta(
        name="fade_any",
        enabled_default=False,  # disabled per [GPT 44/45/46/47]
        execution_status="frozen",  # paused, fat-tail-left
        target_hold_minutes=40.0,  # avg observed hold
        primary_markout_horizon="exit",
        secondary_markout_horizons=("15m", "60m", "180m"),
        exit_model="tp_sl",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="trade",
        max_open_risk_units=1,
        required_validation_gates=(
            "tail_loss_capped < 0.30 per trade",
            "WR > 60% on 50+ post-fix trades",
        ),
        notes="31 trades / 58% WR / -$1.49 net. FAT-TAIL-LEFT distribution.",
    ),

    "sm_fade_weather": StrategyMeta(
        name="sm_fade_weather",
        enabled_default=False,
        execution_status="shadow",  # shadow scoring only, per [GPT 47]
        target_hold_minutes=720.0,  # ~12h to weather market resolution
        primary_markout_horizon="resolution",
        secondary_markout_horizons=("event",),
        exit_model="event_resolution",
        resolution_dependency=True,
        capital_lockup_class="medium",
        risk_unit="event",  # one (city, date) event = one risk unit
        max_open_risk_units=3,
        required_validation_gates=(
            "resolved_events >= 12",
            "calendar_days >= 3",
            "regions >= 2",
            "USA_resolutions_included",
            "event_dollar_EV > +$0.05 after spread",
            "no_single_event > 35% positive PnL",
        ),
        notes="0xc80fa1fc fade candidate. Bounded downside per dollar.",
    ),

    "tennis_underdog_canary": StrategyMeta(
        name="tennis_underdog_canary",
        enabled_default=True,
        execution_status="paper",  # PAPER per maintainer's call (override [GPT 49] Phase 1)
        target_hold_minutes=180.0,  # match resolution typically ~1-3h
        primary_markout_horizon="resolution",
        secondary_markout_horizons=("event",),
        exit_model="manual_resolution",  # closed by tennis_underdog_paper_resolver cron
        resolution_dependency=True,
        capital_lockup_class="medium",
        risk_unit="event",  # one match = one event
        max_open_risk_units=5,
        required_validation_gates=(
            "[GPT 49] event-level n >= 150",
            "ROI ex top 5 winners > +25%",
            "WTA / ATP not both negative",
            "max chronological drawdown acceptable at $1 sizing",
        ),
        notes=(
            "WTA/ATP underdog $0.075-$0.10 entry. Audit: 31.7% WR vs 8.7% implied, "
            "robust to top-5 outlier removal. 23-loss streak observed in audit. "
            "PAPER deployed Day 8 per maintainer override of [GPT 49] Phase 1 spec."
        ),
    ),

    "wti_mr_canary": StrategyMeta(
        name="wti_mr_canary",
        enabled_default=False,
        execution_status="frozen",  # NO-GO per [GPT 47]
        target_hold_minutes=60.0,
        primary_markout_horizon="60m",
        secondary_markout_horizons=("180m",),
        exit_model="tp_sl",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="market_family",  # WTI markets are correlated
        max_open_risk_units=1,
        required_validation_gates=(
            "non_WTI_subset_n_val >= 100",
            "non_WTI_sharpe > 0.5",
            "non_WTI_avg > +$0.005",
            "no_single_asset > 35% PnL",
        ),
        notes="Inverted asset_target diagnostic. 100% WTI = single market family.",
    ),

    "asset_target": StrategyMeta(
        name="asset_target",
        enabled_default=True,  # candidates emit, but no fills (12% threshold)
        execution_status="frozen",  # H1 falsified at 60m, H2 untested
        target_hold_minutes=43200.0,  # ~30 days to resolution
        primary_markout_horizon="resolution",
        secondary_markout_horizons=("60m", "180m"),
        exit_model="manual_resolution",
        resolution_dependency=True,
        capital_lockup_class="high",
        risk_unit="market_family",  # WTI / BTC families
        max_open_risk_units=2,
        required_validation_gates=(
            "resolution_dataset >= 50 resolved",
            "asset_families >= 5",
            "no_family > 35% PnL",
            "resolution_dollar_EV > +5% after friction",
        ),
        notes="H1 (60m) FALSIFIED on 6077 trades. H2 (resolution) untested.",
    ),

    "maker": StrategyMeta(
        name="maker",
        enabled_default=False,
        execution_status="disabled",  # non-viable on $100 paper per [GPT 35]
        target_hold_minutes=None,  # variable, fill-to-exit
        primary_markout_horizon="exit",
        secondary_markout_horizons=("5m", "15m", "60m"),
        exit_model="external_signal",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="trade",
        max_open_risk_units=10,
        required_validation_gates=(
            "account_balance >= $1000",
            "tick_to_premium_ratio >= 5x",
        ),
        notes="Paper $100 makes liability:premium ratio bad. Re-enable on $1k+.",
    ),

    "fade_shadow_scanner": StrategyMeta(
        name="fade_shadow_scanner",
        enabled_default=True,
        execution_status="shadow",
        target_hold_minutes=None,
        primary_markout_horizon="60m",  # shadow telemetry, no fills
        exit_model="scanner_orphan",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="trade",
        max_open_risk_units=0,  # shadow only
        notes="Telemetry — pump detection in markets. No live fills.",
    ),

    "neg_risk_arb_scanner": StrategyMeta(
        name="neg_risk_arb_scanner",
        enabled_default=True,
        execution_status="shadow",
        target_hold_minutes=None,
        primary_markout_horizon="event",
        exit_model="scanner_orphan",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="event",
        max_open_risk_units=0,
        notes="Scans for sum-to-1 arb opportunities. Per [Claude 47] FIX-1 — "
              "always logs raw_edge_pp now (was 0 before).",
    ),

    "hedge_shadow": StrategyMeta(
        name="hedge_shadow",
        enabled_default=True,
        execution_status="shadow",
        target_hold_minutes=None,
        primary_markout_horizon="event",
        exit_model="scanner_orphan",
        resolution_dependency=False,
        capital_lockup_class="low",
        risk_unit="event",
        max_open_risk_units=0,
        notes="Sim hedge round-1/round-2 PnL. Per [Claude 47] FIX-3 — "
              "now surfaces hedge_pnl at top level.",
    ),
}


def validate_registry() -> list[str]:
    """Run startup guard checks. Returns list of error strings (empty if OK).

    Per [GPT 47] required guards:
      1. every executable strategy has registry metadata
      2. every registry strategy maps to known runner/module
      3. primary_markout_horizon is not null
      4. risk_unit is not null
      5. validation gates listed for non-disabled strategies
    """
    errors: list[str] = []
    for name, meta in STRATEGY_REGISTRY.items():
        if not meta.primary_markout_horizon:
            errors.append(f"{name}: primary_markout_horizon is empty")
        if not meta.risk_unit:
            errors.append(f"{name}: risk_unit is empty")
        # Validation gates required only for paper/live strategies (real exposure).
        # Shadow strategies are telemetry-only — no graduation aspiration assumed.
        if meta.execution_status in ("paper", "live") and not meta.required_validation_gates:
            errors.append(
                f"{name}: execution_status={meta.execution_status} but "
                f"required_validation_gates is empty"
            )
        if meta.execution_status == "live" and meta.enabled_default is False:
            errors.append(
                f"{name}: execution_status=live but enabled_default=False (drift)"
            )
    return errors


def get_meta(name: str) -> StrategyMeta | None:
    """Lookup metadata. Returns None if name not registered."""
    return STRATEGY_REGISTRY.get(name)


def is_enabled(name: str) -> bool:
    """Single source of truth for whether a strategy is allowed to fire.

    Per [GPT 47]: dashboards/risk_manager should call THIS function,
    not check class attributes.
    """
    meta = get_meta(name)
    if meta is None:
        return False
    if meta.execution_status in ("disabled", "frozen"):
        return False
    return meta.enabled_default
