from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

# Matchup detector per [GPT 16]: "X vs Y" individual sports/player markets
# подвержены news-driven gaps. Detect через regex на question/slug.
_MATCHUP_RE = re.compile(r"\bvs\b|\bvs\.\b|\b vs\.? \b|\bv\.\s*[A-Z]", re.IGNORECASE)


def _is_matchup_market(question: str, slug: str) -> bool:
    text = f"{question} {slug}"
    return bool(_MATCHUP_RE.search(text))

from sqlalchemy import select

from app.ai.classifier import AIMarketClassifier, set_default as set_default_classifier
from app.ai.fair_price import AIFairPriceClient, set_default_client as set_default_fair_price_client
from app.ai.veto import AIVetoClient, set_default_client
from app.strategies.consensus_drift import CONSENSUS_DRIFT_EXIT_RULES, ConsensusDriftStrategy
from app.strategies.momentum_strategy import MomentumStrategy
from app.strategies.post_panic_rebound import POST_PANIC_REBOUND_EXIT_RULES, PostPanicReboundStrategy
from app.strategies.sports.external_fair import ExternalSportsFairPrice
from app.strategies.sports.sniper import SportsSniperStrategy
from app.strategies.asset_target.strategy import AssetTargetSniperStrategy
from app.integrations.odds_api import OddsApiClient
from app.integrations.crypto_price import CryptoPriceClient
from app.integrations.commodity_price import CommodityPriceClient, CompositePriceClient
from app.config import Settings
from app.db import db_session
from app.execution.position_manager import PositionManager
from app.logger import get_logger
from app.market_data.clob_client import ClobClient, OrderBookSnapshot
from app.market_data.gamma_client import GammaClient
from app.market_data.market_cache import MarketCache
from app.integrations.funnel_log import funnel_log
from app.market_data.normalizer import NormalizedMarket, normalize_market
from app.models import MarketSnapshot, OpportunityLog, Position
from app.pricing.filters import hours_to_resolution, passes_basic_filters
from app.pricing.signal_engine import SignalEngine


logger = get_logger(__name__)


class ScannerTask:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gamma_client = GammaClient(settings)
        self.clob_client = ClobClient(settings)
        self.cache = MarketCache()
        self.signal_engine = SignalEngine()
        self.position_manager = PositionManager()
        self.ai_veto = AIVetoClient(
            api_key=settings.groq_api_key if settings.ai_veto_enabled else "",
            model=settings.ai_veto_model,
            base_url=settings.ai_veto_base_url,
            cache_minutes=settings.ai_veto_cache_minutes,
            timeout_s=settings.ai_veto_timeout_s,
        )
        set_default_client(self.ai_veto)
        self.ai_fair_price = AIFairPriceClient(
            api_key=settings.groq_api_key if settings.ai_fair_price_enabled else "",
            model=settings.ai_fair_price_model,
            base_url=settings.ai_veto_base_url,
            cache_minutes=settings.ai_fair_price_cache_minutes,
            timeout_s=settings.ai_fair_price_timeout_s,
        )
        set_default_fair_price_client(self.ai_fair_price)
        self.momentum = MomentumStrategy()
        self.consensus_drift = ConsensusDriftStrategy()
        self.post_panic_rebound = PostPanicReboundStrategy()
        self.classifier = AIMarketClassifier(
            api_key=settings.groq_api_key if settings.consensus_drift_enabled else "",
            base_url=settings.ai_veto_base_url,
            cache_minutes=settings.classifier_cache_minutes,
        )
        set_default_classifier(self.classifier)
        self._peak_pnl: dict[str, float] = {}
        # Per [GPT 26] real-time MFE/MAE logging для event_strategy cycles
        self._trough_pnl: dict[str, float] = {}
        self._entry_meta: dict[str, dict] = {}  # market_id → {entry_ts, entry_price, strategy}
        self._ai_scores: dict[str, float] = {}

        # Sports Sniper (external-edge strategy per AI debate Day-0 spec).
        self.odds_client = OddsApiClient(
            api_key=settings.odds_api_key if settings.sniper_enabled else "",
            base_url=settings.odds_api_base_url,
            cache_seconds=settings.odds_api_cache_seconds,
            regions=settings.odds_api_regions,
        )
        self.external_fair = ExternalSportsFairPrice(self.odds_client)
        self.sniper = SportsSniperStrategy(
            live_enabled=settings.sniper_live_enabled,
            live_max_size_usd=settings.sniper_live_max_size_usd,
            live_max_open=settings.sniper_live_max_open,
            live_daily_stop_usd=settings.sniper_live_daily_stop_usd,
        )

        # Asset Target Sniper (price-target binaries: BTC/ETH/WTI/etc).
        # CompositePriceClient: Binance для crypto, Yahoo Finance для commodities.
        self.crypto_price_client = CryptoPriceClient()
        self.commodity_price_client = CommodityPriceClient()
        self.asset_price_client = CompositePriceClient(
            self.crypto_price_client, self.commodity_price_client,
        )
        self.asset_target_sniper = AssetTargetSniperStrategy(
            price_client=self.asset_price_client,
            live_enabled=settings.asset_target_live_enabled,
            live_max_size_usd=settings.asset_target_live_size_usd,
            live_max_open=settings.asset_target_live_max_open,
        )

        # Warmup: восстанавливаем history из БД чтобы первые сделки после
        # rebuild не шли на пустой кэш (см. инцидент 2026-05-01).
        try:
            fp_restored = self.signal_engine.fair_price_engine.bootstrap_from_db()
            cache_restored = self.cache.bootstrap_from_db()
            logger.info(
                "scanner.warmup_complete | fair_price_markets=%s cache_markets=%s",
                fp_restored, cache_restored,
            )
        except Exception as exc:
            logger.warning("scanner.warmup_failed | error=%s", exc)
        self._ai_fair_meta: dict[str, dict[str, Any]] = {}
        self._strategy_kind: dict[str, str] = {}
        self._peak_bid: dict[str, float] = {}

    async def run_once(self) -> dict[str, Any]:
        raw_markets = await self.gamma_client.fetch_markets(limit=150)
        # Per [GPT 21]: actively pull imminent matchups для Sports Sniper coverage.
        # Default scanner sorts by volume24hr — misses pre-match window.
        imminent = await self.gamma_client.fetch_imminent_matchups(max_hours_ahead=6.0, limit=50)
        if imminent:
            seen_ids = {str(m.get("id") or m.get("market_id") or "") for m in raw_markets}
            new_imminent = [m for m in imminent if str(m.get("id") or m.get("market_id") or "") not in seen_ids]
            if new_imminent:
                logger.info("scanner.imminent_added | count=%s", len(new_imminent))
                raw_markets = raw_markets + new_imminent
        order_books = await asyncio.gather(
            *[
                self.clob_client.get_order_book(
                    market_id=str(raw_market.get("id") or raw_market.get("market_id") or "unknown"),
                    token_id=raw_market.get("yes_token_id"),
                )
                for raw_market in raw_markets
            ]
        )
        books_by_market_id = {book.market_id: book for book in order_books}

        filtered_markets: list[NormalizedMarket] = []
        for raw_market in raw_markets:
            market_id = str(raw_market.get("id") or raw_market.get("market_id") or "unknown")
            book: OrderBookSnapshot | None = books_by_market_id.get(market_id)
            market: NormalizedMarket = normalize_market(raw_market, order_book=book)
            if not passes_basic_filters(market):
                logger.info("scanner.market_filtered | market_id=%s category=%s", market.market_id, market.category)
                continue
            filtered_markets.append(market)

        ai_estimates: dict[str, dict[str, Any]] = {}
        if self.settings.ai_fair_price_enabled and self.ai_fair_price.enabled() and filtered_markets:
            ai_estimates = await self.ai_fair_price.estimate_batch(filtered_markets)
            logger.info(
                "scanner.ai_fair_price | input=%s estimated=%s",
                len(filtered_markets), len(ai_estimates),
            )

        classifications: dict[str, dict[str, Any]] = {}
        if self.settings.consensus_drift_enabled and self.classifier.enabled() and filtered_markets:
            tte_candidates = []
            for m in filtered_markets:
                end_raw = m.raw.get("endDate") or m.raw.get("end_date")
                if not end_raw:
                    continue
                tte_candidates.append(m)
            if tte_candidates:
                classifications = await self.classifier.classify_batch(tte_candidates[:30])
                logger.info(
                    "scanner.classifier | input=%s classified=%s",
                    len(tte_candidates[:30]), len(classifications),
                )

        opportunities: list[dict[str, Any]] = []
        markets_seen = 0

        with db_session() as session:
            for market in filtered_markets:
                markets_seen += 1
                self.cache.upsert(market)
                self.position_manager.update_unrealized(market.market_id, market.mid_price)

                if self.settings.consensus_drift_only:
                    cls = classifications.get(market.market_id, {})
                    is_clean = (
                        cls.get("label") in {
                            "objective_scheduled_event",
                            "sports_or_match_result",
                            "official_count_or_measurable_outcome",
                            "calendar_deadline_outcome",
                        }
                        and cls.get("quality") == "clear"
                    )
                    history = self.cache.history(market.market_id)
                    logger.info(
                        "scanner.cd_eval | market_id=%s clean=%s label=%s quality=%s hist_len=%s ask=%.3f spread=%.4f vol=%.0f",
                        market.market_id, is_clean, cls.get("label", "?"), cls.get("quality", "?"),
                        len(history), market.best_ask, market.spread, market.volume,
                    )
                    cd_signal = self.consensus_drift.evaluate_with_context(
                        market,
                        history,
                        is_clean,
                    )
                    chosen_strategy_kind = "consensus_drift"
                    if cd_signal is None:
                        pp_signal = self.post_panic_rebound.evaluate_with_history(market, history)
                        if pp_signal is None:
                            continue
                        cd_signal = pp_signal
                        chosen_strategy_kind = "post_panic_rebound"
                        logger.info(
                            "scanner.post_panic_signal | market_id=%s reason=%s",
                            market.market_id, cd_signal.reason,
                        )
                    self._strategy_kind[market.market_id] = chosen_strategy_kind
                    self._peak_bid[market.market_id] = market.best_bid
                    signal_result = self.signal_engine.build_signal(
                        market,
                        ai_fair_price=cd_signal.metadata.get("expected_exit_bid", market.mid_price),
                        ai_confidence=0.7,
                    )
                    signal_result.signal = cd_signal
                    logger.info(
                        "scanner.consensus_drift_signal | market_id=%s reason=%s",
                        market.market_id, cd_signal.reason,
                    )
                    snapshot = MarketSnapshot(
                        market_id=market.market_id,
                        slug=market.slug,
                        question=market.question,
                        category=market.category,
                        outcome=market.outcome,
                        best_bid=market.best_bid,
                        best_ask=market.best_ask,
                        last_price=market.last_price,
                        fair_price=signal_result.fair_price,
                        payload={
                            **market.raw,
                            "spread": market.spread,
                            "volume": market.volume,
                            "liquidity": market.liquidity,
                            "updated_at": market.updated_at.isoformat(),
                        },
                    )
                    session.add(snapshot)
                    signal = cd_signal
                    ref_price = market.best_ask
                    target_notional = float(self.settings.max_order_notional)
                    order_size = round(target_notional / max(ref_price, 0.01), 4) if ref_price > 0 else 0.0
                    opportunities.append({
                        "market_id": market.market_id,
                        "slug": market.slug,
                        "question": market.question,
                        "category": market.category,
                        "strategy": signal.strategy_name,
                        "side": signal.side.value,
                        "price": ref_price,
                        "size": order_size,
                        "confidence": signal.confidence,
                        "reason": signal.reason,
                        "outcome": market.outcome,
                        "fair_price": signal.fair_price,
                        "edge": signal.edge,
                        "bid": market.best_bid,
                        "ask": market.best_ask,
                        "spread": market.spread,
                        "volume": market.volume,
                        "liquidity": market.liquidity,
                        "hours_to_resolution": round(hours_to_resolution(market), 2),
                        "is_matchup": _is_matchup_market(market.question, market.slug),
                        "updated_at": market.updated_at.isoformat(),
                    })
                    continue

                ai_est = ai_estimates.get(market.market_id)
                if self.settings.ai_fair_price_enabled:
                    if ai_est is None:
                        continue
                    if ai_est["confidence"] < self.settings.ai_fair_price_min_confidence:
                        logger.info(
                            "scanner.ai_low_conf | market_id=%s ai_fair=%.3f conf=%.2f reason=%s",
                            market.market_id, ai_est["fair_price"], ai_est["confidence"], ai_est.get("reason", ""),
                        )
                        continue
                    if abs(ai_est["fair_price"] - market.mid_price) < self.settings.ai_fair_price_min_edge:
                        continue
                    self._ai_fair_meta[market.market_id] = ai_est
                    signal_result = self.signal_engine.build_signal(
                        market,
                        ai_fair_price=ai_est["fair_price"],
                        ai_confidence=ai_est["confidence"],
                    )
                else:
                    signal_result = self.signal_engine.build_signal(market)

                snapshot = MarketSnapshot(
                    market_id=market.market_id,
                    slug=market.slug,
                    question=market.question,
                    category=market.category,
                    outcome=market.outcome,
                    best_bid=market.best_bid,
                    best_ask=market.best_ask,
                    last_price=market.last_price,
                    fair_price=signal_result.fair_price,
                    payload={
                        **market.raw,
                        "spread": market.spread,
                        "volume": market.volume,
                        "liquidity": market.liquidity,
                        "updated_at": market.updated_at.isoformat(),
                    },
                )
                session.add(snapshot)

                signal = signal_result.signal
                if signal is None:
                    momentum_signal = self.momentum.evaluate_with_history(
                        market, self.cache.history(market.market_id)
                    )
                    if momentum_signal is None:
                        continue
                    signal = momentum_signal
                    logger.info(
                        "scanner.momentum_signal | market_id=%s reason=%s",
                        market.market_id, signal.reason,
                    )

                ref_price = market.best_ask if signal.side.value == "BUY" else market.best_bid
                strategy_min = 4.0 if signal.strategy_name == "sports_strategy" else self.settings.min_order_notional
                # fade_any_canary forced $1 sizing per [GPT 23] ramp protocol +
                # [Claude 43] mechanical fix — was getting $15 default → 100% rejected.
                if signal.strategy_name == "fade_any":
                    target_notional = 1.0
                elif signal.confidence >= self.settings.high_conf_threshold:
                    target_notional = self.settings.high_conf_notional
                else:
                    span = self.settings.max_order_notional - strategy_min
                    target_notional = strategy_min + span * signal.confidence
                target_notional = min(target_notional, self.settings.max_order_notional)
                # don't apply strategy_min to fade_any (it overrides at $1)
                if signal.strategy_name != "fade_any":
                    target_notional = max(target_notional, strategy_min)
                order_size = round(target_notional / max(ref_price, 0.01), 4) if ref_price > 0 else 0.0

                # Bucket per strategy:
                #   fade_any → "fade_any_canary" ($1 cap, validator PASSED)
                #   financial → "financial_internal" (quarantined per [GPT 26])
                #   event/sports → "experiment"
                if signal.strategy_name == "fade_any":
                    bucket = "fade_any_canary"
                elif signal.strategy_name == "financial_strategy":
                    bucket = "financial_internal"
                else:
                    bucket = "experiment"
                opportunities.append(
                    {
                        "market_id": market.market_id,
                        "slug": market.slug,
                        "question": market.question,
                        "category": market.category,
                        "strategy": signal.strategy_name,
                        "side": signal.side.value,
                        "price": ref_price,
                        "size": order_size,
                        "confidence": signal.confidence,
                        "reason": signal.reason,
                        "outcome": market.outcome,
                        "fair_price": signal.fair_price,
                        "edge": signal.edge,
                        "bid": market.best_bid,
                        "ask": market.best_ask,
                        "spread": market.spread,
                        "volume": market.volume,
                        "liquidity": market.liquidity,
                        "hours_to_resolution": round(hours_to_resolution(market), 2),
                        "is_matchup": _is_matchup_market(market.question, market.slug),
                        "updated_at": market.updated_at.isoformat(),
                        "bucket": bucket,
                    }
                )
                ai_reason = ai_est.get("reason", "") if ai_est else ""
                if ai_reason:
                    opportunities[-1]["ai_reason"] = ai_reason
                    opportunities[-1]["ai_confidence"] = ai_est.get("confidence", 0.0)
                logger.info(
                    "scanner.signal | market_id=%s strategy=%s side=%s edge=%.4f fair=%.4f bid=%.4f ask=%.4f ai=%s",
                    market.market_id,
                    signal.strategy_name,
                    signal.side.value,
                    signal.edge,
                    signal.fair_price,
                    market.best_bid,
                    market.best_ask,
                    ai_reason[:60],
                )
                funnel_log(
                    stage="signal_generated",
                    market_id=market.market_id,
                    category=market.category,
                    strategy=signal.strategy_name,
                    side=signal.side.value,
                    edge=round(signal.edge, 4),
                    fair=round(signal.fair_price, 4),
                    bid=round(market.best_bid, 4),
                    ask=round(market.best_ask, 4),
                    spread=round(market.spread, 4),
                    volume=round(market.volume, 2),
                    liquidity=round(market.liquidity, 2),
                )

        if opportunities and self.ai_veto.enabled():
            decisions = await self.ai_veto.evaluate(opportunities)
            kept: list[dict[str, Any]] = []
            for opp in opportunities:
                dec = decisions.get(opp["market_id"], {"decision": "GO", "score": 60.0})
                score = float(dec.get("score", 60.0))
                if dec.get("decision") == "SKIP" or score < 65.0:
                    logger.info(
                        "scanner.ai_veto_skip | market_id=%s score=%.0f reason=%s",
                        opp["market_id"], score, dec.get("reason", ""),
                    )
                    continue
                opp["ai_reason"] = dec.get("reason", "")
                opp["ai_score"] = score
                opp["ai_confidence"] = dec.get("confidence", 0.0)
                self._ai_scores[opp["market_id"]] = score
                size_factor = min(score / 80.0, 1.0)
                opp["size"] = round(opp["size"] * size_factor, 4)
                kept.append(opp)
                logger.info(
                    "scanner.ai_veto_go | market_id=%s score=%.0f size_factor=%.2f reason=%s",
                    opp["market_id"], score, size_factor, dec.get("reason", ""),
                )
            logger.info("scanner.ai_veto | input=%s kept=%s", len(opportunities), len(kept))
            opportunities = kept

        # Sports Sniper layer (per AI debate Day-0): scans only sports markets
        # with mapping match → external odds → opportunity log. Live signals
        # only emitted if all strict gates pass (sniper_live_enabled + exact
        # mapping + window + spread + internal not vetoing).
        sniper_signals: list[dict[str, Any]] = []
        if self.settings.sniper_enabled and self.odds_client.enabled():
            sniper_signals = await self._run_sniper(filtered_markets)
            opportunities.extend(sniper_signals)

        # Asset Target Sniper layer (per AI debate Round 2): scans event/financial
        # markets вида "Will BTC hit $X by DATE" → barrier probability vs Polymarket.
        asset_target_signals: list[dict[str, Any]] = []
        if self.settings.asset_target_enabled:
            asset_target_signals = await self._run_asset_target_sniper(filtered_markets)
            opportunities.extend(asset_target_signals)

        exit_signals = self._check_exit_signals(books_by_market_id)
        opportunities.extend(exit_signals)

        cache_stats = self.cache.stats()
        logger.info(
            "scanner.completed | markets_seen=%s opportunities=%s exits=%s sniper=%s cache_size=%s",
            markets_seen,
            len(opportunities),
            len(exit_signals),
            len(sniper_signals),
            cache_stats.size,
        )
        return {
            "markets_seen": markets_seen,
            "opportunities": opportunities,
            "cache_size": cache_stats.size,
        }

    def _check_exit_signals(self, books: dict[str, OrderBookSnapshot]) -> list[dict[str, Any]]:
        tp = self.settings.take_profit_pct
        sl = self.settings.stop_loss_pct
        stale_hours = self.settings.stale_exit_hours
        orphan_hours = 12.0 if self.settings.consensus_drift_only else 0.5
        min_sl_age_minutes = self.settings.min_sl_age_minutes
        exits: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        with db_session() as session:
            open_positions = session.execute(
                select(Position).where(Position.quantity > 0)
            ).scalars().all()
            for pos in open_positions:
                cached = self.cache.get(pos.market_id)
                if pos.avg_price <= 0:
                    continue

                kind = self._strategy_kind.get(pos.market_id)
                if cached is not None and kind == "consensus_drift":
                    cd_exit = self._check_consensus_drift_exit(pos, cached, now)
                    if cd_exit is not None:
                        exits.append(cd_exit)
                    continue
                if cached is not None and kind == "post_panic_rebound":
                    pp_exit = self._check_post_panic_exit(pos, cached, now)
                    if pp_exit is not None:
                        exits.append(pp_exit)
                    continue

                if cached is None:
                    # Per [Claude] 2026-05-08 incident: sm_fade and SM mirror positions
                    # use condition_id (0x...) as market_id — scanner doesn't see them.
                    # They have their own resolution lifecycle (weather markets resolve
                    # daily). Skip orphan-exit for these buckets.
                    if pos.bucket and pos.bucket.startswith(("sm_fade_", "sm_mirror_")):
                        continue
                    age_hours = (now - pos.created_at.replace(tzinfo=UTC)).total_seconds() / 3600 if pos.created_at else 999
                    if age_hours >= orphan_hours:
                        exits.append({
                            "market_id": pos.market_id,
                            "slug": "orphan",
                            "category": "unknown",
                            "strategy": "exit_manager",
                            "side": "SELL",
                            "price": pos.avg_price,
                            "size": pos.quantity,
                            "confidence": 1.0,
                            "reason": f"orphan-exit: market not in scan after {age_hours:.1f}h",
                            "outcome": pos.outcome,
                            "fair_price": pos.avg_price,
                            "edge": 0.0,
                            "bid": pos.avg_price,
                            "ask": pos.avg_price,
                            "spread": 0.0,
                            "volume": 0.0,
                            "updated_at": now.isoformat(),
                        })
                        logger.info("scanner.exit_signal | market_id=%s reason=orphan age=%.1fh", pos.market_id, age_hours)
                    continue

                mark = cached.mid_price
                pnl_pct = (mark - pos.avg_price) / pos.avg_price
                age_minutes = (now - pos.created_at.replace(tzinfo=UTC)).total_seconds() / 60 if pos.created_at else 999
                hours_to_end = self._hours_to_resolution(cached)
                peak = max(self._peak_pnl.get(pos.market_id, 0.0), pnl_pct)
                self._peak_pnl[pos.market_id] = peak
                trough = min(self._trough_pnl.get(pos.market_id, 0.0), pnl_pct)
                self._trough_pnl[pos.market_id] = trough
                ai_score = self._ai_scores.get(pos.market_id, 0.0)
                high_conf = ai_score >= 80.0

                # Profit-protect edge flip per [GPT 16] #2: если позиция в плюсе,
                # и текущий fair упал ниже mid на >= 0.04 (opposite edge), exit немедленно.
                # Это ловит profit reversals до того как hard-stop триггерится.
                fair_now = None
                try:
                    fair_est = self.signal_engine.fair_price_engine.calculate(cached)
                    fair_now = fair_est.fair_price
                except Exception:
                    fair_now = None
                if fair_now is not None and pnl_pct >= 0.02 and (fair_now - mark) <= -0.04:
                    reason = f"profit_protect_edge_flip pnl=+{pnl_pct:.1%} fair={fair_now:.3f} mid={mark:.3f}"
                elif pnl_pct >= tp:
                    reason = f"take-profit {pnl_pct:.1%} >= {tp:.0%}"
                elif peak >= 0.15 and pnl_pct <= peak - 0.05 and pnl_pct >= 0.05:
                    reason = f"trailing-tp peak={peak:.1%} now={pnl_pct:.1%}"
                elif pnl_pct >= 0.05 and 0.0 < hours_to_end <= 6.0:
                    reason = f"pre-resolution exit {pnl_pct:.1%} (end in {hours_to_end:.1f}h)"
                elif pnl_pct <= -sl and age_minutes >= min_sl_age_minutes:
                    price_loss = pos.avg_price - mark
                    spread_buffer = cached.spread * 0.3 if cached.spread > 0 else 0.0
                    if price_loss < 0.05 and price_loss <= spread_buffer:
                        continue
                    reason = f"stop-loss {pnl_pct:.1%} <= -{sl:.0%} (age {age_minutes:.1f}m, loss={price_loss:.4f})"
                elif pnl_pct <= -0.25:
                    if age_minutes < self.settings.hard_stop_min_age_minutes:
                        logger.info(
                            "scanner.hard_stop_skip | market_id=%s pnl=%.1f%% age=%.1fm < %.1fm reason=age_filter",
                            pos.market_id, pnl_pct * 100, age_minutes,
                            self.settings.hard_stop_min_age_minutes,
                        )
                        continue
                    if cached.spread > self.settings.hard_stop_max_spread:
                        logger.info(
                            "scanner.hard_stop_skip | market_id=%s pnl=%.1f%% spread=%.3f > %.3f reason=spread_torn",
                            pos.market_id, pnl_pct * 100, cached.spread,
                            self.settings.hard_stop_max_spread,
                        )
                        continue
                    reason = f"hard-stop pnl={pnl_pct:.1%} <= -25% (age {age_minutes:.1f}m)"
                elif pos.created_at and pnl_pct < 0:
                    age_hours = (now - pos.created_at.replace(tzinfo=UTC)).total_seconds() / 3600
                    if age_hours >= stale_hours:
                        reason = f"stale-exit {pnl_pct:.1%} after {age_hours:.1f}h"
                    else:
                        continue
                else:
                    continue
                sell_price = cached.best_bid if cached.best_bid > 0 else mark
                exits.append({
                    "market_id": pos.market_id,
                    "slug": cached.slug,
                    "category": cached.category,
                    "strategy": "exit_manager",
                    "side": "SELL",
                    "price": sell_price,
                    "size": pos.quantity,
                    "confidence": 1.0,
                    "reason": reason,
                    "outcome": pos.outcome,
                    "fair_price": mark,
                    "edge": round(mark - pos.avg_price, 6),
                    "bid": cached.best_bid,
                    "ask": cached.best_ask,
                    "spread": cached.spread,
                    "volume": cached.volume,
                    "updated_at": cached.updated_at.isoformat(),
                })
                # MFE/MAE log per [GPT 26] real-time observability
                _mfe = self._peak_pnl.get(pos.market_id, 0.0)
                _mae = self._trough_pnl.get(pos.market_id, 0.0)
                _gave_back = ((_mfe - pnl_pct) / _mfe * 100) if _mfe > 0 else 0
                funnel_log(
                    stage="cycle_mfe_mae",
                    market_id=pos.market_id,
                    entry_price=pos.avg_price,
                    exit_price=sell_price,
                    exit_pnl_pct=round(pnl_pct * 100, 2),
                    mfe_pct=round(_mfe * 100, 2),
                    mae_pct=round(_mae * 100, 2),
                    gave_back_pct=round(_gave_back, 1),
                    age_minutes=round(age_minutes, 1),
                    exit_reason=reason,
                )
                logger.info(
                    "scanner.exit_signal | market_id=%s pnl_pct=%.2f%% reason=%s",
                    pos.market_id, pnl_pct * 100, reason,
                )
        return exits

    def _check_consensus_drift_exit(
        self,
        pos: Position,
        cached: NormalizedMarket,
        now: datetime,
    ) -> dict[str, Any] | None:
        rules = CONSENSUS_DRIFT_EXIT_RULES
        bid = cached.best_bid
        if bid <= 0:
            return None
        entry = pos.avg_price
        pnl_abs = bid - entry
        pnl_pct = pnl_abs / entry if entry > 0 else 0.0
        held_min = (now - pos.created_at.replace(tzinfo=UTC)).total_seconds() / 60.0 if pos.created_at else 999
        tte_min = self._hours_to_resolution(cached) * 60.0
        peak_bid = max(self._peak_bid.get(pos.market_id, bid), bid)
        self._peak_bid[pos.market_id] = peak_bid

        bid_ratio = cached.total_bid_size / max(cached.total_ask_size, 1.0)

        reason = None
        if pnl_abs >= rules["tp_abs_cents"] or pnl_pct >= rules["tp_pct"]:
            reason = f"tp_consensus_drift abs={pnl_abs:.3f} pct={pnl_pct:.2%}"
        elif (peak_bid - entry) >= rules["trailing_min_gain"] and bid <= peak_bid - rules["trailing_drop"]:
            reason = f"trailing_lock peak_bid={peak_bid:.3f} bid={bid:.3f}"
        elif bid <= entry - rules["sl_abs_cents"]:
            reason = f"sl_price_break loss={entry - bid:.3f}"
        elif bid_ratio < rules["support_lost_bid_ratio"] and cached.spread >= rules["support_lost_spread"]:
            reason = f"book_support_gone bid_ratio={bid_ratio:.2f} spread={cached.spread:.3f}"
        elif held_min >= rules["max_hold_min"]:
            reason = f"max_hold {held_min:.0f}m"
        elif 0 < tte_min <= rules["tte_min_floor"]:
            reason = f"do_not_hold_resolution tte={tte_min:.0f}m"
        if reason is None:
            return None
        sell_price = bid
        logger.info(
            "scanner.consensus_drift_exit | market_id=%s pnl_abs=%.3f pnl_pct=%.2f%% reason=%s",
            pos.market_id, pnl_abs, pnl_pct * 100, reason,
        )
        return {
            "market_id": pos.market_id,
            "slug": cached.slug,
            "category": cached.category,
            "strategy": "consensus_drift_exit",
            "side": "SELL",
            "price": sell_price,
            "size": pos.quantity,
            "confidence": 1.0,
            "reason": reason,
            "outcome": pos.outcome,
            "fair_price": bid,
            "edge": round(bid - entry, 6),
            "bid": cached.best_bid,
            "ask": cached.best_ask,
            "spread": cached.spread,
            "volume": cached.volume,
            "updated_at": cached.updated_at.isoformat(),
        }

    def _check_post_panic_exit(
        self,
        pos: Position,
        cached: NormalizedMarket,
        now: datetime,
    ) -> dict[str, Any] | None:
        rules = POST_PANIC_REBOUND_EXIT_RULES
        bid = cached.best_bid
        if bid <= 0:
            return None
        entry = pos.avg_price
        pnl_abs = bid - entry
        pnl_pct = pnl_abs / entry if entry > 0 else 0.0
        held_min = (now - pos.created_at.replace(tzinfo=UTC)).total_seconds() / 60.0 if pos.created_at else 999
        peak_bid = max(self._peak_bid.get(pos.market_id, bid), bid)
        self._peak_bid[pos.market_id] = peak_bid
        history = self.cache.history(pos.market_id)
        recent_low = min(history[-8:]) if len(history) >= 8 else entry

        reason = None
        if pnl_abs >= rules["tp_abs_cents"] or pnl_pct >= rules["tp_pct"]:
            reason = f"panic_rebound_tp abs={pnl_abs:.3f} pct={pnl_pct:.2%}"
        elif (peak_bid - entry) >= rules["trailing_min_gain"] and bid <= peak_bid - rules["trailing_drop"]:
            reason = f"panic_rebound_trailing peak_bid={peak_bid:.3f}"
        elif bid <= entry - rules["sl_abs_cents"]:
            reason = f"panic_rebound_sl loss={entry - bid:.3f}"
        elif bid <= recent_low - rules["new_low_buffer"]:
            reason = f"new_low_after_entry low={recent_low:.3f}"
        elif held_min >= rules["max_hold_min"]:
            reason = f"panic_rebound_timeout {held_min:.0f}m"
        if reason is None:
            return None
        logger.info(
            "scanner.post_panic_exit | market_id=%s pnl_abs=%.3f reason=%s",
            pos.market_id, pnl_abs, reason,
        )
        return {
            "market_id": pos.market_id,
            "slug": cached.slug,
            "category": cached.category,
            "strategy": "post_panic_exit",
            "side": "SELL",
            "price": bid,
            "size": pos.quantity,
            "confidence": 1.0,
            "reason": reason,
            "outcome": pos.outcome,
            "fair_price": bid,
            "edge": round(bid - entry, 6),
            "bid": cached.best_bid,
            "ask": cached.best_ask,
            "spread": cached.spread,
            "volume": cached.volume,
            "updated_at": cached.updated_at.isoformat(),
        }

    async def _run_sniper(
        self, markets: list[NormalizedMarket],
    ) -> list[dict[str, Any]]:
        """Sniper layer: external-anchored sports trading.

        Steps per market:
          1. Map title → SportsMatchScope (only sports/event categories)
          2. If scope.tradable → fetch external odds via OddsApiClient
          3. Compute external_fair, raw_edge, haircut, tradable_edge
          4. Get internal FairPriceEstimate (used as VETO only)
          5. SportsSniperStrategy.evaluate → SniperDecision
          6. Log opportunity unconditionally
          7. If decision.signal not None → add to opportunities list

        Returns list of opportunity dicts (only ENTERED_LIVE signals).
        """
        # Polymarket классифицирует UFC matches как 'financial', NBA series winners
        # как 'sports', NBA single games (when exist) как 'event'. Захватываем все три.
        sport_categories = {"sports", "event", "financial"}
        candidates = [m for m in markets if m.category in sport_categories]
        if not candidates:
            return []

        live_signals: list[dict[str, Any]] = []
        log_rows: list[OpportunityLog] = []

        # Limit per scan to control API quota: max 10 sports markets queried
        for market in candidates[:10]:
            try:
                ext_result = await self.external_fair.get_external_fair(market)
            except Exception as exc:
                logger.warning(
                    "sniper.external_fair_error | market_id=%s error=%s",
                    market.market_id, exc,
                )
                continue

            scope = ext_result.scope
            internal = self.signal_engine.fair_price_engine.calculate(market)
            decision = self.sniper.evaluate(market, ext_result, internal)

            log_rows.append(
                self._build_opportunity_log(market, ext_result, internal, decision)
            )

            if decision.signal:
                opp = {
                    "market_id": market.market_id,
                    "slug": market.slug,
                    "question": market.question,
                    "category": market.category,
                    "strategy": decision.signal.strategy_name,
                    "side": decision.signal.side.value,
                    "price": market.best_ask,
                    "size": decision.signal.metadata.get("size_qty", 0.0),
                    "confidence": decision.signal.confidence,
                    "reason": decision.signal.reason,
                    "outcome": market.outcome,
                    "fair_price": decision.signal.fair_price,
                    "edge": decision.signal.edge,
                    "bid": market.best_bid,
                    "ask": market.best_ask,
                    "spread": market.spread,
                    "volume": market.volume,
                    "updated_at": market.updated_at.isoformat(),
                    "bucket": "core_sniper_live",
                    "cluster_key": scope.cluster_key,
                }
                live_signals.append(opp)
                logger.info(
                    "sniper.live_signal | market_id=%s tradable_edge=%.4f window=%s",
                    market.market_id, ext_result.tradable_edge or 0.0, scope.window_bucket.value,
                )

        if log_rows:
            try:
                with db_session() as session:
                    for row in log_rows:
                        session.add(row)
            except Exception as exc:
                logger.warning("sniper.log_write_failed | error=%s", exc)

        quota = self.odds_client.quota()
        logger.info(
            "sniper.scan_complete | candidates=%s logs_written=%s live_signals=%s odds_remaining=%s",
            len(candidates), len(log_rows), len(live_signals), quota.get("remaining"),
        )
        return live_signals

    async def _run_asset_target_sniper(
        self, markets: list[NormalizedMarket],
    ) -> list[dict[str, Any]]:
        """Asset target sniper: parse market title → external price → barrier prob → edge.

        Looks at event/financial markets с price-target patterns (BTC/ETH/WTI/etc).
        Logs opportunity always; emits live signal only if:
          * parse exact + spot+vol available
          * tradable_edge >= asset-class threshold
          * spread OK
          * edge persists 2 consecutive scans
        """
        target_categories = {"event", "financial", "crypto"}
        candidates = [m for m in markets if m.category in target_categories]
        if not candidates:
            return []

        live_signals: list[dict[str, Any]] = []
        log_rows: list[OpportunityLog] = []

        # Limit per scan: 15 candidates чтобы не злоупотреблять Binance
        for market in candidates[:15]:
            try:
                decision = await self.asset_target_sniper.evaluate(market)
            except Exception as exc:
                logger.warning(
                    "asset_target.evaluate_error | market_id=%s error=%s",
                    market.market_id, exc,
                )
                continue

            log_rows.append(
                self._build_asset_target_log(market, decision)
            )

            if decision.signal:
                opp = {
                    "market_id": market.market_id,
                    "slug": market.slug,
                    "question": market.question,
                    "category": market.category,
                    "strategy": decision.signal.strategy_name,
                    "side": decision.signal.side.value,
                    "price": market.best_ask,
                    "size": decision.signal.metadata.get("size_qty", 0.0),
                    "confidence": decision.signal.confidence,
                    "reason": decision.signal.reason,
                    "outcome": market.outcome,
                    "fair_price": decision.signal.fair_price,
                    "edge": decision.signal.edge,
                    "bid": market.best_bid,
                    "ask": market.best_ask,
                    "spread": market.spread,
                    "volume": market.volume,
                    "updated_at": market.updated_at.isoformat(),
                    "bucket": "asset_target_live",
                    "cluster_key": decision.spec.cluster_key if decision.spec else None,
                }
                live_signals.append(opp)
                logger.info(
                    "asset_target.live_signal | market_id=%s asset=%s edge=%.4f",
                    market.market_id,
                    decision.spec.asset_symbol if decision.spec else "?",
                    decision.tradable_edge or 0.0,
                )

        if log_rows:
            try:
                with db_session() as session:
                    for row in log_rows:
                        session.add(row)
            except Exception as exc:
                logger.warning("asset_target.log_write_failed | error=%s", exc)

        logger.info(
            "asset_target.scan_complete | candidates=%s logs=%s live_signals=%s",
            len(candidates[:15]), len(log_rows), len(live_signals),
        )
        return live_signals

    def _build_asset_target_log(
        self, market: NormalizedMarket, decision: Any,
    ) -> OpportunityLog:
        """Build OpportunityLog row для asset_target decision."""
        spec = decision.spec
        raw = market.raw or {}
        return OpportunityLog(
            market_id=market.market_id,
            slug=market.slug,
            cluster_key=spec.cluster_key if spec else None,
            league=spec.asset_symbol if spec else None,  # пишем asset как league
            team_a_id=spec.asset_class if spec else None,
            team_b_id=str(spec.threshold_usd) if spec and spec.threshold_usd else None,
            market_type="asset_target",
            time_scope=spec.direction.value if spec else "unknown",
            mapping_confidence=spec.parse_confidence if spec else "none",
            window_bucket=spec.deadline_iso if spec and spec.deadline_iso else "unknown",
            poly_ask=market.best_ask,
            poly_bid=market.best_bid,
            poly_ask_size=float(raw.get("total_ask_size", 0.0) or 0.0),
            poly_bid_size=float(raw.get("total_bid_size", 0.0) or 0.0),
            poly_spread=market.spread,
            poly_volume=market.volume,
            poly_liquidity=market.liquidity,
            external_fair=decision.model_prob,
            external_books={"asset": spec.asset_symbol if spec else None,
                            "spot": decision.spot_price,
                            "vol": decision.annualized_vol},
            book_dispersion=None,
            raw_edge=decision.raw_edge,
            haircut=decision.haircut,
            tradable_edge=decision.tradable_edge,
            internal_fair=None,
            internal_confidence=None,
            internal_momentum=None,
            decision=decision.decision,
            reject_reason=decision.reject_reason,
            intended_size_usd=decision.intended_size_usd,
            actual_size_usd=decision.actual_size_usd,
            bucket=decision.bucket,
            odds_fetched_at=None,
            bookmaker_last_update=None,
            poly_book_fetched_at=datetime.now(UTC),
            minutes_to_event_start=None,
            sim_entry_price=decision.sim_entry_price,
            sim_exit_price=decision.sim_exit_price,
        )

    def _build_opportunity_log(
        self,
        market: NormalizedMarket,
        ext: Any,
        internal: Any,
        decision: Any,
    ) -> OpportunityLog:
        """Convert components to OpportunityLog row."""
        scope = ext.scope
        raw = market.raw or {}
        bid_size = float(raw.get("total_bid_size", 0.0) or 0.0)
        ask_size = float(raw.get("total_ask_size", 0.0) or 0.0)

        return OpportunityLog(
            market_id=market.market_id,
            slug=market.slug,
            cluster_key=scope.cluster_key,
            league=scope.league,
            team_a_id=scope.team_a_id,
            team_b_id=scope.team_b_id,
            market_type=scope.market_type.value,
            time_scope=scope.time_scope.value,
            mapping_confidence=scope.mapping_confidence.value,
            window_bucket=scope.window_bucket.value,
            poly_ask=market.best_ask,
            poly_bid=market.best_bid,
            poly_ask_size=ask_size,
            poly_bid_size=bid_size,
            poly_spread=market.spread,
            poly_volume=market.volume,
            poly_liquidity=market.liquidity,
            external_fair=ext.external_fair,
            external_books={"books": ext.books_used, "sharp": ext.sharp_count, "main": ext.main_count},
            book_dispersion=ext.book_dispersion,
            raw_edge=ext.raw_edge,
            haircut=ext.haircut,
            tradable_edge=ext.tradable_edge,
            internal_fair=internal.fair_price if internal else None,
            internal_confidence=internal.confidence if internal else None,
            internal_momentum=None,
            decision=decision.decision,
            reject_reason=decision.reject_reason,
            intended_size_usd=decision.intended_size_usd,
            actual_size_usd=decision.actual_size_usd,
            bucket=decision.bucket,
            odds_fetched_at=ext.odds_fetched_at,
            bookmaker_last_update=ext.bookmaker_last_update,
            poly_book_fetched_at=datetime.now(UTC),
            minutes_to_event_start=scope.minutes_to_start,
            sim_entry_price=decision.sim_entry_price,
            sim_exit_price=decision.sim_exit_price,
        )

    @staticmethod
    def _hours_to_resolution(market: NormalizedMarket) -> float:
        end_raw = market.raw.get("endDate") or market.raw.get("end_date") or market.raw.get("endDateIso")
        if not end_raw:
            return -1.0
        try:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return -1.0
        return (end_dt - datetime.now(UTC)).total_seconds() / 3600.0
